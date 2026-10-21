"""主动发言调度器的离线测试（对应设计文档 3.10 的 8 个测试点）。

全部测试不联网、不调用模型、不碰生产数据库：时钟、候选、生成、投递、诊断全部注入。
"""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random
import sqlite3

import pytest
from aiohttp.test_utils import TestClient, TestServer

from myatri_bridge.proactive import (
    SELF_INITIATED_METADATA,
    Candidate,
    JsonlDiagnostics,
    ProactiveScheduler,
    ProactiveSettings,
    ProactiveState,
    is_listening_evidence,
    is_self_initiated,
)
from myatri_bridge.server import create_app
from myatri_bridge.short_memory import ShortMemory


TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 21, 14, 0, tzinfo=TZ)
SCOPE = "0123456789abcdef0123456789abcdef"


def base_state(**overrides):
    state = {
        "scope": SCOPE,
        "last_speech_at": NOW - timedelta(hours=1),
        "proactive_today": 0,
        "group_last_message_at": NOW - timedelta(minutes=5),
        "budget_used_ratio": 0.2,
        "explicit_reply_pending": False,
    }
    state.update(overrides)
    return state


def make_candidate(candidate_id="m1", kind="failed_explicit", *, age_seconds=5, payload=None):
    return Candidate(
        candidate_id=candidate_id,
        kind=kind,
        created_at=NOW - timedelta(seconds=age_seconds),
        reason="之前被呼叫但没答上",
        context_hash="ctx-hash-1",
        excerpt="在吗",
        payload=payload or {},
    )


def make_draft(parts=None, metadata=None, record_id="pro_1"):
    return {
        "parts": parts if parts is not None else ["嗯，我在。"],
        "metadata": dict(SELF_INITIATED_METADATA if metadata is None else metadata),
        "record_id": record_id,
        "context_hash": "ctx-hash-1",
    }


class Rig:
    """把调度器的六个注入点全部换成可观测的假实现。"""

    def __init__(
        self,
        tmp_path,
        *,
        config=None,
        enabled=True,
        shadow=False,
        state=None,
        diagnostic=None,
    ):
        self.tmp_path = Path(tmp_path)
        self.now = NOW
        self.state = base_state() if state is None else state
        self.candidates_items = []
        self.generated = []
        self.delivered = []
        self.records = []
        self.draft_result = make_draft()
        self.deliver_result = {"id": "pro_1", "status": "sent"}
        self.deliver_error = None
        self.generate_hook = None
        self.state_calls = 0
        settings = {
            "enabled": enabled,
            "shadow_mode": shadow,
            "kill_switch_path": str(self.tmp_path / "KILL"),
            **(config or {}),
        }
        self.scheduler = ProactiveScheduler(
            settings,
            scope=SCOPE,
            state=self._state,
            candidates=self._candidates,
            generate=self._generate,
            deliver=self._deliver,
            clock=lambda: self.now,
            diagnostic=diagnostic or self.records.append,
            base_dir=self.tmp_path,
            rng=random.Random(11),
        )

    def _state(self, scope):
        self.state_calls += 1
        return self.state

    def _candidates(self, scope):
        return self.candidates_items

    def _generate(self, *, scope, candidate, now, instruction):
        self.generated.append(
            {"candidate": candidate, "instruction": instruction, "scope": scope, "now": now}
        )
        if self.generate_hook is not None:
            self.generate_hook()
        return self.draft_result

    def _deliver(self, *, scope, candidate, draft, now):
        self.delivered.append(
            {"candidate": candidate, "draft": draft, "scope": scope, "now": now}
        )
        if self.deliver_error is not None:
            raise self.deliver_error
        return self.deliver_result


# --------------------------------------------------------------------- 测试点 1


async def test_default_config_is_disabled_and_shadow(tmp_path):
    settings = ProactiveSettings.from_config({}, base_dir=tmp_path)
    assert settings.enabled is False
    assert settings.shadow_mode is True
    assert settings.daily_limit == 10
    assert settings.candidate_ttl_seconds == 45
    assert settings.kill_switch_path.name == "KILL_PROACTIVE"


async def test_disabled_scheduler_does_not_start(tmp_path):
    rig = Rig(tmp_path, enabled=False)
    rig.candidates_items = [make_candidate()]
    await asyncio.wait_for(rig.scheduler.run(), timeout=1)
    assert rig.generated == []
    assert rig.delivered == []
    assert rig.state_calls == 0
    assert rig.records[-1]["event"] == "proactive_disabled"

    result = await rig.scheduler.tick()
    assert result["skipped"] == "disabled"
    assert result["spoke"] is False
    assert rig.generated == []


# --------------------------------------------------------------------- 测试点 2


async def test_kill_switch_stops_immediately(tmp_path):
    rig = Rig(tmp_path)
    rig.candidates_items = [make_candidate()]
    (tmp_path / "KILL").write_text("1", encoding="utf-8")
    result = await rig.scheduler.tick()
    assert result["skipped"] == "kill_switch"
    assert rig.state_calls == 0
    assert rig.generated == [] and rig.delivered == []


async def test_kill_switch_appearing_during_generation_prevents_send(tmp_path):
    rig = Rig(tmp_path)
    rig.candidates_items = [make_candidate()]
    rig.generate_hook = lambda: (tmp_path / "KILL").write_text("1", encoding="utf-8")
    result = await rig.scheduler.tick()
    assert result["skipped"] == "kill_switch"
    assert rig.delivered == []


# --------------------------------------------------------------------- 测试点 3


def _quiet_hours(rig):
    rig.now = datetime(2026, 10, 21, 7, 0, tzinfo=TZ)


def _speech_cooldown(rig):
    rig.state["last_speech_at"] = NOW - timedelta(minutes=5)


def _daily_limit(rig):
    rig.state["proactive_today"] = 10


def _group_silent(rig):
    rig.state["group_last_message_at"] = NOW - timedelta(hours=3)


def _group_silent_unknown(rig):
    rig.state["group_last_message_at"] = None


def _budget_margin(rig):
    rig.state["budget_used_ratio"] = 0.85


def _budget_unknown(rig):
    rig.state["budget_used_ratio"] = None


def _no_candidate(rig):
    rig.candidates_items = []


def _expired_candidate(rig):
    rig.candidates_items = [make_candidate(age_seconds=46)]


@pytest.mark.parametrize(
    "setup,expected",
    [
        (_quiet_hours, "quiet_hours"),
        (_speech_cooldown, "speech_cooldown"),
        (_daily_limit, "daily_limit"),
        (_group_silent, "group_silent"),
        (_group_silent_unknown, "group_silent"),
        (_budget_margin, "budget_margin"),
        (_budget_unknown, "budget_margin"),
        (_no_candidate, "no_candidate"),
        (_expired_candidate, "candidate_expired"),
    ],
    ids=[
        "quiet_hours",
        "speech_cooldown",
        "daily_limit",
        "group_silent",
        "group_silent_unknown",
        "budget_margin",
        "budget_margin_unknown",
        "no_candidate",
        "candidate_ttl",
    ],
)
async def test_each_gate_short_circuits_without_generation(tmp_path, setup, expected):
    rig = Rig(tmp_path)
    rig.candidates_items = [make_candidate()]
    setup(rig)
    result = await rig.scheduler.tick()
    assert result["skipped"] == expected
    assert result["spoke"] is False
    assert rig.generated == []
    assert rig.delivered == []


async def test_happy_path_passes_all_gates_and_delivers(tmp_path):
    rig = Rig(tmp_path)
    rig.candidates_items = [make_candidate()]
    result = await rig.scheduler.tick()
    assert result["spoke"] is True
    assert result["skipped"] is None
    assert result["candidate"] == "m1"
    assert result["priority"] == 1
    assert result["delivery_id"] == "pro_1"
    assert result["delivery_status"] == "sent"
    assert len(rig.delivered) == 1
    # 生成指令必须带上"主动开口"的边界规则。
    instruction = rig.generated[0]["instruction"]
    assert "刚看到" in instruction
    assert "不要每次都提问" in instruction
    # 投递的草稿必须保留 self_initiated 标记。
    metadata = rig.delivered[0]["draft"].metadata
    assert metadata["self_initiated"] is True
    assert metadata["author_kind"] == "self_initiated"
    assert metadata["reality_scope"] == "internal"


async def test_candidate_priority_follows_the_four_levels(tmp_path):
    rig = Rig(tmp_path)
    rig.candidates_items = [
        make_candidate("m4", "unanswered_question"),
        make_candidate("m3", "temporary_entry"),
        make_candidate("m2", "deferred_topic"),
        make_candidate("m1", "failed_explicit"),
    ]
    for _ in range(4):
        await rig.scheduler.tick()
    assert [item["candidate"].candidate_id for item in rig.generated] == [
        "m1",
        "m2",
        "m3",
        "m4",
    ]
    assert [item["candidate"].candidate_id for item in rig.delivered] == [
        "m1",
        "m2",
        "m3",
        "m4",
    ]


# --------------------------------------------------------------------- 测试点 4


def test_self_initiated_metadata_is_not_human_evidence(tmp_path):
    assert is_self_initiated(SELF_INITIATED_METADATA) is True
    assert is_listening_evidence(SELF_INITIATED_METADATA) is False
    record = {
        "role": "user",
        **SELF_INITIATED_METADATA,
        "message_id": "pro_1",
        "sender_id": "bot",
        "sender_name": "小温暖",
        "sent_at": NOW.isoformat(),
        "text": "嗯，我在。",
        "bot_id": "bot",
    }
    assert is_listening_evidence(record) is False
    memory = ShortMemory(tmp_path / "short-memory.sqlite3")
    try:
        # 父代理接线要求：写短记忆前用 is_listening_evidence 过滤；
        # 即使漏了这道过滤，ShortMemory.add 的既有守卫也要挡住。
        added = memory.add(SCOPE, record) if is_listening_evidence(record) else False
        assert added is False
        assert memory.add(SCOPE, record) is False
        assert memory.stats(SCOPE)["total_messages"] == 0
        assert memory.recent(SCOPE) == []
    finally:
        memory.close()


async def test_draft_without_self_initiated_marker_is_rejected(tmp_path):
    rig = Rig(tmp_path)
    rig.candidates_items = [make_candidate()]
    rig.draft_result = make_draft(metadata={"author_kind": "human"})
    result = await rig.scheduler.tick()
    assert result["skipped"] == "draft_rejected"
    assert result["detail"] == "draft_missing_self_initiated"
    assert rig.delivered == []


@pytest.mark.parametrize(
    "metadata,detail",
    [
        ({**SELF_INITIATED_METADATA, "self_initiated": False}, "draft_missing_self_initiated"),
        ({**SELF_INITIATED_METADATA, "author_kind": "human"}, "draft_missing_author_kind"),
        ({**SELF_INITIATED_METADATA, "reality_scope": "external"}, "draft_missing_reality_scope"),
        ({**SELF_INITIATED_METADATA, "role": "assistant"}, "draft_role_invalid"),
    ],
)
async def test_incomplete_self_initiated_markers_are_rejected(tmp_path, metadata, detail):
    rig = Rig(tmp_path)
    rig.candidates_items = [make_candidate()]
    rig.draft_result = make_draft(metadata=metadata)
    result = await rig.scheduler.tick()
    assert result["skipped"] == "draft_rejected"
    assert result["detail"] == detail
    assert rig.delivered == []


async def test_self_initiated_candidate_is_source_pollution(tmp_path):
    rig = Rig(tmp_path)
    rig.candidates_items = [
        make_candidate(payload={"role": "user", **SELF_INITIATED_METADATA})
    ]
    result = await rig.scheduler.tick()
    assert result["skipped"] == "candidate_source_polluted"
    assert rig.generated == [] and rig.delivered == []


@pytest.mark.parametrize(
    "parts,detail",
    [([], "draft_empty"), (["  "], "draft_empty"), (["一", "二", "三", "四"], "draft_too_many_parts")],
    ids=["empty", "blank", "too_many_parts"],
)
async def test_bad_draft_shapes_are_rejected(tmp_path, parts, detail):
    rig = Rig(tmp_path)
    rig.candidates_items = [make_candidate()]
    rig.draft_result = make_draft(parts=parts)
    result = await rig.scheduler.tick()
    assert result["skipped"] == "draft_rejected"
    assert result["detail"] == detail
    assert rig.delivered == []


async def test_invalid_state_values_are_treated_as_unavailable(tmp_path):
    rig = Rig(
        tmp_path,
        state={
            "last_speech_at": "not-a-timestamp",
            "group_last_message_at": NOW,
            "budget_used_ratio": 0.1,
        },
    )
    rig.candidates_items = [make_candidate()]
    result = await rig.scheduler.tick()
    assert result["skipped"] == "state_unavailable"
    assert rig.generated == []


# --------------------------------------------------------------------- 测试点 5


async def test_unknown_delivery_is_not_retried(tmp_path):
    rig = Rig(tmp_path)
    rig.candidates_items = [make_candidate()]
    rig.deliver_result = {"id": "pro_1", "status": "unknown"}
    first = await rig.scheduler.tick()
    assert first["spoke"] is False
    assert first["skipped"] == "delivery_unknown"
    assert first["delivery_status"] == "unknown"
    second = await rig.scheduler.tick()
    assert second["skipped"] == "no_candidate"
    assert len(rig.delivered) == 1


# --------------------------------------------------------------------- 测试点 6


async def test_superseded_when_explicit_call_arrives_during_generation(tmp_path):
    rig = Rig(tmp_path)
    rig.candidates_items = [make_candidate()]

    def hook():
        rig.state["explicit_reply_pending"] = True

    rig.generate_hook = hook
    result = await rig.scheduler.tick()
    assert result["skipped"] == "superseded"
    assert rig.delivered == []
    again = await rig.scheduler.tick()
    assert again["skipped"] == "no_candidate"
    assert len(rig.generated) == 1


async def test_superseded_when_generation_exceeds_ttl(tmp_path):
    rig = Rig(tmp_path)
    rig.candidates_items = [make_candidate(age_seconds=30)]
    rig.generate_hook = lambda: setattr(rig, "now", NOW + timedelta(seconds=20))
    result = await rig.scheduler.tick()
    assert result["skipped"] == "superseded"
    assert rig.delivered == []


# --------------------------------------------------------------------- 测试点 7


async def test_daily_limit_stops_generation_after_cap(tmp_path):
    rig = Rig(tmp_path)
    rig.candidates_items = [make_candidate()]
    rig.state["proactive_today"] = 10
    result = await rig.scheduler.tick()
    assert result["skipped"] == "daily_limit"
    assert rig.generated == []


# --------------------------------------------------------------------- 测试点 8


async def test_pre_send_failure_records_unknown_without_retry(tmp_path):
    rig = Rig(tmp_path)
    rig.candidates_items = [make_candidate()]
    rig.deliver_error = RuntimeError("HOOK_VERSION_MISMATCH")
    first = await rig.scheduler.tick()
    assert first["spoke"] is False
    assert first["skipped"] == "delivery_error"
    assert first["delivery_status"] == "unknown"
    assert first["delivery_id"] is None
    second = await rig.scheduler.tick()
    assert second["skipped"] == "no_candidate"
    assert len(rig.delivered) == 1


# ------------------------------------------------------------------ 影子模式


async def test_shadow_mode_writes_draft_only(tmp_path):
    records = []
    writer = JsonlDiagnostics(tmp_path)

    def diagnostic(record):
        records.append(record)
        writer(record)

    rig = Rig(tmp_path, shadow=True, diagnostic=diagnostic)
    rig.candidates_items = [make_candidate()]
    result = await rig.scheduler.tick()
    assert result["spoke"] is False
    assert result["skipped"] == "shadow_mode"
    assert result["shadow"] is True
    assert rig.delivered == []

    drafts = writer.draft_path.read_text(encoding="utf-8").splitlines()
    assert len(drafts) == 1
    record = json.loads(drafts[0])
    assert record["event"] == "proactive_draft"
    assert record["parts"] == ["嗯，我在。"]
    assert record["metadata"]["self_initiated"] is True
    assert record["candidate"] == "m1"

    ticks = writer.log_path.read_text(encoding="utf-8").splitlines()
    assert json.loads(ticks[-1])["event"] == "proactive_tick"


# ------------------------------------------------------------------ 间隔采样


def test_interval_sampling_is_bounded_and_configurable(tmp_path):
    rig = Rig(tmp_path)
    samples = [rig.scheduler.sample_interval_seconds() for _ in range(200)]
    assert all(20 * 60 <= value <= 90 * 60 for value in samples)
    assert len(set(samples)) > 50
    assert min(samples) < 45 * 60 < max(samples)

    fixed = Rig(
        tmp_path,
        config={"min_interval_minutes": 30, "max_interval_minutes": 30},
    )
    assert fixed.scheduler.sample_interval_seconds() == 1800


@pytest.mark.parametrize(
    "config",
    [
        {"active_start": "23:00", "active_end": "09:00"},
        {"active_start": "9点", "active_end": "23:30"},
        {"min_interval_minutes": 90, "max_interval_minutes": 20},
        {"enabled": "yes"},
        {"daily_limit": -1},
        {"candidate_ttl_seconds": 0},
        {"budget_limit_ratio": 1.5},
        {"kill_switch_path": "  "},
        {"unknown_key": 1},
    ],
)
def test_invalid_config_is_rejected(tmp_path, config):
    with pytest.raises(ValueError):
        ProactiveSettings.from_config(config, base_dir=tmp_path)


# ------------------------------------------------------- 只读 pending 端点


class FakeBridge:
    """create_app 只需要 path/config 与生命周期钩子。"""

    def __init__(self, path):
        self.path = Path(path)
        self.config = {"mode": "offline"}
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    async def close(self):
        self.closed = True


def deliveries_db(tmp_path):
    path = tmp_path / "deliveries.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE deliveries(id TEXT PRIMARY KEY, scope TEXT, fingerprint TEXT, "
            "status TEXT, reply TEXT, reply_ref TEXT, updated REAL, envelope TEXT, "
            "trigger_kind TEXT, received_at REAL, reply_parts TEXT, "
            "sent_parts INTEGER NOT NULL DEFAULT 0)"
        )
        db.execute(
            "INSERT INTO deliveries VALUES('pro_1',?,'fp1','prepared',NULL,NULL,0,NULL,"
            "'proactive',0,?,0)",
            (SCOPE, json.dumps(["嗯，我在。", "刚才说到哪儿了"], ensure_ascii=False)),
        )
        db.execute(
            "INSERT INTO deliveries VALUES('passive_1',?,'fp2','sent','好',NULL,0,NULL,"
            "'explicit',0,NULL,1)",
            (SCOPE,),
        )
        db.execute(
            "INSERT INTO deliveries VALUES('other_1',?,'fp3','prepared','别群',NULL,0,NULL,"
            "'proactive',0,NULL,0)",
            ("f" * 32,),
        )
    return path


async def test_pending_endpoint_requires_bearer_and_returns_prepared_only(tmp_path):
    token = "t" * 40
    headers = {"Authorization": "Bearer " + token}
    bridge = FakeBridge(deliveries_db(tmp_path))
    async with TestClient(TestServer(create_app(bridge, token))) as client:
        unauthorized = await client.get(
            "/v1/deliveries/pending", params={"scope": SCOPE}
        )
        assert unauthorized.status == 401

        response = await client.get(
            "/v1/deliveries/pending", params={"scope": SCOPE}, headers=headers
        )
        assert response.status == 200
        body = await response.json()
        assert body["scope"] == SCOPE
        assert body["deliveries"] == [
            {
                "id": "pro_1",
                "reply_parts": ["嗯，我在。", "刚才说到哪儿了"],
                "sent_parts": 0,
                "trigger_kind": "proactive",
            }
        ]

        invalid = await client.get(
            "/v1/deliveries/pending", params={"scope": "not-a-scope"}, headers=headers
        )
        assert invalid.status == 400

        unexpected = await client.get(
            "/v1/deliveries/pending",
            params={"scope": SCOPE, "limit": "5"},
            headers=headers,
        )
        assert unexpected.status == 400
    assert bridge.started is True and bridge.closed is True


async def test_pending_endpoint_is_empty_for_unknown_scope(tmp_path):
    token = "u" * 40
    bridge = FakeBridge(deliveries_db(tmp_path))
    async with TestClient(TestServer(create_app(bridge, token))) as client:
        response = await client.get(
            "/v1/deliveries/pending",
            params={"scope": "0" * 32},
            headers={"Authorization": "Bearer " + token},
        )
        assert response.status == 200
        assert await response.json() == {"scope": "0" * 32, "deliveries": []}


async def test_state_dataclass_is_accepted(tmp_path):
    rig = Rig(
        tmp_path,
        state=ProactiveState(
            scope=SCOPE,
            group_last_message_at=NOW - timedelta(minutes=1),
            budget_used_ratio=0.1,
        ),
    )
    rig.candidates_items = [make_candidate()]
    result = await rig.scheduler.tick()
    assert result["spoke"] is True

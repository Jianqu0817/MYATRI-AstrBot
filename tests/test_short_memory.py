import asyncio
import json
import sqlite3

import pytest

from myatri.providers import FixtureProvider, Gateway
from myatri_bridge.short_memory import PROMPT_VERSION, ShortMemory


def message(number, *, sender="member1", text="我喜欢红茶", **extra):
    return {"message_id": f"m{number}", "sender_id": sender, "sender_name": "甲",
            "sent_at": "2026-10-21T12:00:00+08:00", "text": text, **extra}


def add_messages(memory, start, end, *, scope="room", **kwargs):
    for number in range(start, end + 1):
        memory.add(scope, message(number, **kwargs))


def gateway(items=None):
    provider = FixtureProvider([{"content": json.dumps({"items": items or []}, ensure_ascii=False)}])
    return Gateway(provider, daily_token_limit=1000000), provider


def item(**changes):
    return {"text": "甲自述喜欢红茶", "speaker_id": "member1", "subject_id": "member1",
            "source_message_ids": ["m1"], "attribution": "self_report",
            "source_quotes": [{"message_id": "m1", "quote": "我喜欢红茶"}], **changes}


def test_partial_batch_durable_and_replay_does_not_count(tmp_path):
    path = tmp_path / "listening.sqlite3"
    memory = ShortMemory(path)
    add_messages(memory, 1, 19)
    assert memory.stats("room")["total_messages"] == 19
    memory = ShortMemory(path)
    assert len(memory.recent("room")) == 19
    assert not memory.add("room", message(1))
    assert memory.stats("room")["total_messages"] == 19
    with pytest.raises(ValueError, match="reused"):
        memory.add("room", message(1, text="改变了的原话"))
    assert not memory.add("room", message(20, role="assistant"))
    assert not memory.add("room", message(20, bot_id="member1"))
    assert memory.stats("room")["total_messages"] == 19
    memory.add("room", message(20))
    assert len(memory.recent("room")) == 20
    assert memory.pending_scopes() == []


def test_fixed_windows_and_raw_rolling_limit(tmp_path):
    memory = ShortMemory(tmp_path / "memory.db")
    add_messages(memory, 1, 320)
    assert len(memory.recent("room")) == 200
    assert memory.recent("room")[0]["message_id"] == "m121"
    assert memory.stats("room")["pending_summaries"] == 3
    with sqlite3.connect(memory.path) as db:
        window = json.loads(db.execute(
            "SELECT payload FROM listening_pending WHERE end_seq=100").fetchone()[0])
    assert [window[0]["message_id"], window[-1]["message_id"]] == ["m1", "m100"]
    memory.flush("room")
    assert memory.stats("room")["total_messages"] == 320
    assert memory.stats("room")["pending_summaries"] == 3
    add_messages(memory, 321, 399)
    assert memory.stats("room")["pending_summaries"] == 3
    memory.add("room", message(400))
    assert memory.stats("room")["pending_summaries"] == 4


async def test_summary_uses_shared_gateway_and_retains_source(tmp_path):
    memory = ShortMemory(tmp_path / "memory.db")
    add_messages(memory, 1, 99)
    shared, provider = gateway([item()])
    assert (await memory.summarize_pending("room", shared))["status"] == "idle"
    assert not provider.requests
    memory.add("room", message(100))
    result = await memory.summarize_pending("room", shared)
    assert result["status"] == "summarized"
    assert shared.used_tokens > 0
    request = provider.requests[0]
    assert request.role == "background"
    assert request.purpose == "group_short_memory"
    payload = json.loads(request.messages[1]["content"])
    assert payload["complete"] is True
    assert len(payload["messages"]) == 100
    assert payload["messages"][0]["sender_id"] == "member1"
    summary = memory.summaries("room")[0]
    assert summary["window_start"] == 1 and summary["window_end"] == 100
    assert summary["items"][0]["source_message_ids"] == ["m1"]
    assert summary["items"][0]["source_quotes"][0]["quote"] == "我喜欢红茶"
    assert len(summary["source_message_ids"]) == 100
    assert memory.stats("room")["pending_summaries"] == 0
    assert (await memory.summarize_pending("room", shared))["status"] == "idle"
    assert len(provider.requests) == 1


@pytest.mark.parametrize("changes", [
    {"speaker_id": "member2", "subject_id": "member2"},
    {"subject_id": "member2"},
    {"source_message_ids": ["absent"], "source_quotes": [{"message_id": "absent", "quote": "不存在"}]},
    {"source_quotes": [{"message_id": "m1", "quote": "我喜欢咖啡"}]},
    {"source_quotes": [{"message_id": "m2", "quote": "我喜欢红茶"}]},
])
async def test_wrong_member_and_fabricated_citations_rejected(tmp_path, changes):
    memory = ShortMemory(tmp_path / "memory.db")
    add_messages(memory, 1, 99)
    memory.add("room", message(100, sender="member2"))
    shared, provider = gateway([item(**changes)])
    assert (await memory.summarize_pending("room", shared))["status"] == "failed"
    assert memory.summaries("room") == []
    assert memory.stats("room")["pending_summaries"] == 1
    assert (await memory.summarize_pending("room", shared))["status"] == "deferred"
    assert len(provider.requests) == 1


async def test_reported_fact_keeps_actual_speaker(tmp_path):
    memory = ShortMemory(tmp_path / "memory.db")
    add_messages(memory, 1, 99, text="乙喜欢红茶")
    memory.add("room", message(100, sender="member2"))
    shared, _ = gateway([item(text="甲转述乙喜欢红茶，尚未获乙确认", subject_id="member2",
                             attribution="reported", source_quotes=[{"message_id": "m1", "quote": "乙喜欢红茶"}])])
    assert (await memory.summarize_pending("room", shared))["status"] == "summarized"
    saved = memory.summaries("room")[0]["items"][0]
    assert saved["speaker_id"] == "member1"
    assert saved["subject_id"] == "member2"
    assert saved["attribution"] == "reported"


async def test_failure_backoff_survives_restart_and_preserves_window(tmp_path):
    path = tmp_path / "memory.db"
    memory = ShortMemory(path)
    add_messages(memory, 1, 100)
    shared, provider = gateway([item(subject_id="unknown")])
    await memory.summarize_pending("room", shared)
    memory = ShortMemory(path)
    add_messages(memory, 101, 300)
    assert memory.pending_scopes() == ["room"]
    assert memory.pending_scopes(ready_only=False) == ["room"]
    later_gateway, _ = gateway()
    later = await memory.summarize_pending("room", later_gateway)
    assert later["status"] == "summarized" and later["window_end"] == 200
    with sqlite3.connect(path) as db:
        db.execute("UPDATE listening_pending SET next_attempt=0 WHERE end_seq=100")
    shared, provider = gateway([item()])
    result = await memory.summarize_pending("room", shared)
    assert result["window_end"] == 100
    assert json.loads(provider.requests[0].messages[1]["content"])["messages"][0]["message_id"] == "m1"


async def test_scoped_summary_retention_ten_and_no_cross_group_data(tmp_path):
    memory = ShortMemory(tmp_path / "memory.db", batch_size=1, summary_every=1)
    for number in range(1, 13):
        memory.add("room", message(number))
        shared, _ = gateway()
        assert (await memory.summarize_pending("room", shared))["status"] == "summarized"
    memory.add("other", message(1, text="另一个群的内容"))
    assert len(memory.summaries("room")) == 10
    assert memory.summaries("room")[0]["window_end"] == 3
    assert memory.summaries("other") == []
    assert len(memory.recent("other")) == 1
    assert all(m["text"] != "另一个群的内容" for m in memory.recent("room"))


async def test_input_too_large_does_not_bill_or_discard_pending(tmp_path):
    memory = ShortMemory(tmp_path / "memory.db", max_input_chars=2000)
    add_messages(memory, 1, 100, text="长消息" * 1000)
    shared, provider = gateway()
    result = await memory.summarize_pending("room", shared)
    assert result["error"] == "input_too_large"
    assert not provider.requests
    assert not memory.summaries("room")
    assert memory.stats("room")["pending_summaries"] == 1
    with sqlite3.connect(memory.path) as db:
        pending = json.loads(db.execute("SELECT payload FROM listening_pending").fetchone()[0])
    assert len(pending) == 100
    assert len(pending[0]["text"]) == 3000


async def test_old_oversized_window_does_not_block_new_ready_window(tmp_path):
    memory = ShortMemory(tmp_path / "memory.db", max_input_chars=20000)
    add_messages(memory, 1, 100, text="长消息" * 1000)
    shared, provider = gateway()
    first = await memory.summarize_pending("room", shared)
    assert first["error"] == "input_too_large"
    assert not provider.requests
    add_messages(memory, 101, 200, text="接着聊")
    assert memory.pending_scopes() == ["room"]
    later = await memory.summarize_pending("room", shared)
    assert later["status"] == "summarized" and later["window_end"] == 200
    assert len(provider.requests) == 1
    source = json.loads(provider.requests[0].messages[1]["content"])["messages"]
    assert source[0]["message_id"] == "m101" and source[-1]["message_id"] == "m200"
    assert memory.stats("room")["pending_summaries"] == 1
    assert memory.pending_scopes() == []
    deferred = await memory.summarize_pending("room", shared)
    assert deferred["status"] == "deferred"
    assert deferred["retry_at"] == first["retry_at"]
    with sqlite3.connect(memory.path) as db:
        pending = json.loads(db.execute("SELECT payload FROM listening_pending").fetchone()[0])
    assert len(pending) == 100 and len(pending[0]["text"]) == 3000


async def test_corrupt_window_retained_with_backoff_while_later_window_runs(tmp_path):
    memory = ShortMemory(tmp_path / "memory.db")
    add_messages(memory, 1, 200)
    with sqlite3.connect(memory.path) as db:
        db.execute("UPDATE listening_pending SET payload='broken JSON' WHERE end_seq=100")
    shared, provider = gateway()
    first = await memory.summarize_pending("room", shared)
    assert first["status"] == "failed" and first["error"] == "JSONDecodeError"
    later = await memory.summarize_pending("room", shared)
    assert later["status"] == "summarized" and later["window_end"] == 200
    assert len(provider.requests) == 1


async def test_parallel_workers_claim_window_once(tmp_path):
    first = ShortMemory(tmp_path / "memory.db")
    second = ShortMemory(tmp_path / "memory.db")
    add_messages(first, 1, 100)
    shared, provider = gateway()
    provider.delay = 0.02
    results = await asyncio.gather(first.summarize_pending("room", shared), second.summarize_pending("room", shared))
    assert {result["status"] for result in results} == {"summarized", "deferred"}
    assert len(provider.requests) == 1


async def test_callback_failure_reuses_validated_result_without_second_api_call(tmp_path):
    path = tmp_path / "memory.db"
    memory = ShortMemory(path)
    add_messages(memory, 1, 100)
    shared, provider = gateway([item()])
    callbacks = []

    def archive(summary, source_messages):
        callbacks.append((summary, source_messages))
        # The result is durable, but the pending window is not yet deleted.
        with sqlite3.connect(path) as db:
            pending = db.execute("SELECT result,payload FROM listening_pending WHERE end_seq=100").fetchone()
        assert pending is not None and json.loads(pending[0]) == summary
        assert len(json.loads(pending[1])) == 100
        if len(callbacks) == 1:
            raise OSError("archive temporarily unavailable")

    result = await memory.summarize_pending("room", shared, on_summary=archive)
    assert result["status"] == "failed"
    assert result["error"] == "OSError"
    assert memory.summaries("room") == []
    assert len(provider.requests) == 1
    memory = ShortMemory(path)  # Simulate a restart and eviction from the raw cache.
    add_messages(memory, 101, 320)
    assert memory.recent("room")[0]["message_id"] == "m121"
    with sqlite3.connect(path) as db:
        db.execute("UPDATE listening_pending SET next_attempt=0 WHERE end_seq=100")
    result = await memory.summarize_pending("room", shared, on_summary=archive)
    assert result["status"] == "summarized"
    assert len(provider.requests) == 1
    assert len(callbacks) == 2
    assert callbacks[0] == callbacks[1]
    summary, original_window = callbacks[1]
    assert len(original_window) == 100
    assert original_window[0]["message_id"] == "m1"
    assert original_window[0]["text"] == "我喜欢红茶"
    assert summary["generator"]["provider"] == "fixture"
    assert summary["generator"]["model"] == "offline-fixture-v1"
    assert summary["generator"]["prompt_version"] == PROMPT_VERSION
    assert summary["generator"]["run_id"].startswith("listening_")
    assert memory.summaries("room")[0] == summary
    assert memory.stats("room")["pending_summaries"] == 2


async def test_cached_result_revalidated_against_original_evidence(tmp_path):
    path = tmp_path / "memory.db"
    memory = ShortMemory(path)
    add_messages(memory, 1, 100)
    shared, provider = gateway([item()])

    def unavailable(summary, sources):
        raise OSError("not ready")

    await memory.summarize_pending("room", shared, on_summary=unavailable)
    with sqlite3.connect(path) as db:
        summary = json.loads(db.execute("SELECT result FROM listening_pending").fetchone()[0])
        summary["items"][0]["subject_id"] = "somebody_else"
        db.execute("UPDATE listening_pending SET result=?,next_attempt=0", (json.dumps(summary),))
    callbacks = []
    result = await memory.summarize_pending("room", shared, on_summary=lambda *args: callbacks.append(args))
    assert result["status"] == "failed"
    assert not callbacks
    assert len(provider.requests) == 1
    assert memory.stats("room")["pending_summaries"] == 1


def test_pending_result_column_migrates_existing_database(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE listening_pending(scope TEXT NOT NULL,end_seq INTEGER NOT NULL,"
                   "payload TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,"
                   "next_attempt REAL NOT NULL DEFAULT 0,last_error TEXT,PRIMARY KEY(scope,end_seq))")
    ShortMemory(path)
    with sqlite3.connect(path) as db:
        assert "result" in {row[1] for row in db.execute("PRAGMA table_info(listening_pending)")}

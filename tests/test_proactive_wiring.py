"""Parent-side wiring for proactive speech: lifecycle, state, delivery hooks."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

from myatri.app import load_config
from myatri.providers import FixtureProvider
from myatri_bridge.bridge import Bridge, Incoming
from myatri_bridge.proactive import Candidate

PROJECT = Path(__file__).resolve().parents[1]
SCOPE = "0" * 32


def _message(**update):
    base = dict(
        platform_id="fixture", bot_id="bot", group_id="room1", sender_id="member1",
        sender_name="甲", message_id="m1", sent_at="2026-10-21T01:42:05+08:00",
        text="今天想安静待一会儿。", mentioned=True,
    )
    base.update(update)
    return Incoming(**base)


def _bridge(tmp_path, proactive=None):
    settings = {
        "mode": "offline",
        "allowed_groups": ["fixture|room1"],
        "cooldown_seconds": 0,
        "random_reply_probability": 0,
        "recent_context_messages": 20,
        "dialogue_turns": 15,
    }
    if proactive is not None:
        settings["proactive"] = proactive
    return Bridge(
        tmp_path / "groups",
        settings,
        load_config(PROJECT / "config/engine.toml"),
        PROJECT / "config/persona/persona.md",
        provider_factory=lambda scope: FixtureProvider([{"content": "嗯，我在。"}]),
    )


async def test_no_scheduler_without_config(tmp_path):
    bridge = _bridge(tmp_path)
    try:
        await bridge.receive(_message())
        assert bridge.proactive_schedulers == {}
        assert bridge._ensure_proactive(_message().scope) is None
    finally:
        await bridge.close()


async def test_disabled_scheduler_is_created_but_never_speaks(tmp_path):
    bridge = _bridge(tmp_path, proactive={"enabled": False, "shadow_mode": True})
    try:
        message = _message()
        await bridge.receive(message)
        assert message.scope in bridge.proactive_schedulers
        scheduler, task = bridge.proactive_schedulers[message.scope]
        result = await scheduler.tick()
        assert result["spoke"] is False
        assert result["skipped"] in {"disabled", "quiet_hours", "group_silent"}
        await task
    finally:
        await bridge.close()
    assert bridge.proactive_schedulers == {}


async def test_state_reads_durable_ledger(tmp_path):
    bridge = _bridge(tmp_path, proactive={"enabled": False})
    try:
        message = _message()
        await bridge.receive(message)
        state = bridge._proactive_state(message.scope)
        assert state["proactive_today"] == 0
        assert state["explicit_reply_pending"] is True  # delivered, still prepared
        assert state["group_last_message_at"] is not None
        assert 0.0 <= state["budget_used_ratio"] <= 1.0
    finally:
        await bridge.close()


async def test_candidates_only_come_from_unfinished_or_addressed_sources(tmp_path):
    bridge = _bridge(tmp_path, proactive={"enabled": False})
    try:
        message = _message(mentioned=False, text="有人知道周末爬山吗？")
        assert (await bridge.receive(message))["status"] == "ignored"
        candidates = bridge._proactive_candidates(message.scope)
        assert candidates and candidates[0].kind == "unanswered_question"
        assert not any(c.payload for c in candidates)
    finally:
        await bridge.close()


async def test_deliver_then_claim_expires_stale_proactive(tmp_path):
    bridge = _bridge(tmp_path, proactive={"enabled": False, "candidate_ttl_seconds": 45})
    try:
        message = _message()
        await bridge.receive(message)
        old = datetime.now(timezone.utc) - timedelta(minutes=5)
        candidate = Candidate(candidate_id="c1", kind="unanswered_question", created_at=old)
        draft = SimpleNamespace(parts=["我先去倒杯水，等我一下。"], context_hash="h1")
        result = await bridge._proactive_deliver(
            scope=message.scope, candidate=candidate, draft=draft, now=old)
        assert result["status"] == "prepared"
        assert result["id"].startswith("pro_")

        with closing(sqlite3.connect(bridge.path)) as db:
            kind, parts = db.execute(
                "SELECT trigger_kind,reply_parts FROM deliveries WHERE id=?", (result["id"],)).fetchone()
        assert kind == "proactive" and json.loads(parts) == draft.parts

        # A five-minute-old idea must not be spoken after the moment passed.
        assert await bridge.claim(result["id"]) == {"claimed": False}
        with closing(sqlite3.connect(bridge.path)) as db:
            assert db.execute("SELECT status FROM deliveries WHERE id=?", (result["id"],)).fetchone()[0] == "superseded"
    finally:
        await bridge.close()


async def test_proactive_reply_enters_assistant_history_without_reply_to(tmp_path):
    bridge = _bridge(tmp_path, proactive={"enabled": False})
    try:
        message = _message()
        await bridge.receive(message)
        candidate = Candidate(candidate_id="c1", kind="unanswered_question",
                              created_at=datetime.now(timezone.utc))
        draft = SimpleNamespace(parts=["我刚才想了一下。"], context_hash="h1")
        result = await bridge._proactive_deliver(
            scope=message.scope, candidate=candidate, draft=draft,
            now=datetime.now(timezone.utc))

        assert await bridge.claim(result["id"]) == {"claimed": True}
        await bridge.acknowledge(result["id"], "sent", 0)

        assistant = bridge.short_memory.recent_assistant(message.scope)
        own = [row for row in assistant if row["message_id"] == "reply_" + result["id"]]
        assert own and own[0]["role"] == "assistant"
        assert "reply_to_id" not in own[0]
        assert own[0]["text"] == draft.parts[0]
        # Still not human evidence.
        assert bridge.short_memory.stats(message.scope)["total_messages"] == 1
    finally:
        await bridge.close()

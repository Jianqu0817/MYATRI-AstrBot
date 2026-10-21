"""Proactive generation must never write a human-looking turn."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from myatri.app import load_config
from myatri.providers import FixtureProvider
from myatri_bridge.bridge import Bridge, Incoming
from myatri_bridge.proactive import Candidate

PROJECT = Path(__file__).resolve().parents[1]
REPLY = "我刚看到你们在聊爬山，那个路线我好像也走过。"


def _message(**update):
    base = dict(
        platform_id="fixture", bot_id="bot", group_id="room1", sender_id="member1",
        sender_name="甲", message_id="m1", sent_at="2026-10-21T01:42:05+08:00",
        text="有人知道周末爬山吗？", mentioned=True,
    )
    base.update(update)
    return Incoming(**base)


def _bridge(tmp_path):
    settings = {
        "mode": "offline",
        "allowed_groups": ["fixture|room1"],
        "cooldown_seconds": 0,
        "random_reply_probability": 0,
        "recent_context_messages": 20,
        "dialogue_turns": 15,
        "proactive": {"enabled": False, "shadow_mode": True},
    }
    return Bridge(
        tmp_path / "groups", settings,
        load_config(PROJECT / "config/engine.toml"),
        PROJECT / "config/persona/persona.md",
        provider_factory=lambda scope: FixtureProvider([{"content": REPLY}]),
    )


async def test_generate_writes_no_human_record(tmp_path):
    bridge = _bridge(tmp_path)
    try:
        message = _message()
        assert (await bridge.receive(message))["status"] == "prepared"
        app = bridge.apps[message.scope]
        before_records = len(app.store.list_records("raw_message"))
        before_human = bridge.short_memory.stats(message.scope)["total_messages"]

        candidate = Candidate(candidate_id="c1", kind="unanswered_question",
                              created_at=datetime.now(timezone.utc),
                              reason="群里有未回答的问句", excerpt="有人知道周末爬山吗？",
                              context_hash="h1")
        draft = await bridge._proactive_generate(
            scope=message.scope, candidate=candidate,
            now=datetime.now(timezone.utc), instruction="现在是主动开口的时机。")

        assert draft is not None, "fixture provider should produce a draft"
        assert draft["parts"] and all(isinstance(p, str) and p.strip() for p in draft["parts"])
        assert draft["metadata"]["self_initiated"] is True
        assert draft["metadata"]["author_kind"] == "self_initiated"
        assert "role" not in draft["metadata"]
        assert draft["record_id"].startswith("pro_")

        # The decisive assertion: nothing was archived as a user turn.
        assert len(app.store.list_records("raw_message")) == before_records
        assert bridge.short_memory.stats(message.scope)["total_messages"] == before_human
        assert [m["sender_id"] for m in bridge.short_memory.recent(message.scope)] == ["member1"]
    finally:
        await bridge.close()


async def test_generated_draft_flows_through_delivery_and_stays_assistant(tmp_path):
    bridge = _bridge(tmp_path)
    try:
        message = _message()
        await bridge.receive(message)
        candidate = Candidate(candidate_id="c1", kind="unanswered_question",
                              created_at=datetime.now(timezone.utc), context_hash="h1")
        draft = await bridge._proactive_generate(
            scope=message.scope, candidate=candidate,
            now=datetime.now(timezone.utc), instruction="现在是主动开口的时机。")
        assert draft is not None

        from types import SimpleNamespace
        delivered = await bridge._proactive_deliver(
            scope=message.scope, candidate=candidate,
            draft=SimpleNamespace(parts=draft["parts"], context_hash=draft["context_hash"]),
            now=datetime.now(timezone.utc))
        assert delivered["status"] == "prepared"
        assert await bridge.claim(delivered["id"]) == {"claimed": True}
        await bridge.acknowledge(delivered["id"], "sent", 0)

        assistant = bridge.short_memory.recent_assistant(message.scope)
        own = [m for m in assistant if m["message_id"] == "reply_" + delivered["id"]]
        assert own and own[0]["role"] == "assistant"
        assert "reply_to_id" not in own[0]
        # Still exactly one human message: the bot never became a member.
        assert bridge.short_memory.stats(message.scope)["total_messages"] == 1
        with closing(sqlite3.connect(bridge.path)) as db:
            kinds = dict(db.execute("SELECT trigger_kind,COUNT(*) FROM deliveries GROUP BY trigger_kind"))
        assert kinds.get("proactive") == 1
    finally:
        await bridge.close()

import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3
import time
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer
import pytest

from myatri_bridge.bridge import Bridge
from myatri_bridge.identity import IdentityRegistry
from myatri_bridge.proactive import Candidate
from myatri_bridge.server import create_app


def test_identity_backfill_uses_one_transaction_and_preserves_alias_sources(tmp_path, monkeypatch):
    ids = IdentityRegistry(tmp_path / "ids.sqlite3")
    base = {"sender_id": "a", "sender_name": "甲", "message_id": "m1",
        "sent_at": "2026-10-21T10:00:00+08:00", "text": "普通消息", "bot_id": "bot"}
    records = [{"ref": "raw@1", "body_markdown": json.dumps({**base,
        "recent_messages": [{**base, "sender_id": "b", "sender_name": "乙", "message_id": "m2"}]})}] * 500
    connect = ids._connect
    connections, trace = [], []

    def observe_connect():
        db = connect()
        db.set_trace_callback(trace.append)
        connections.append(1)
        return db

    monkeypatch.setattr(ids, "_connect", observe_connect)
    stats = ids.backfill("room", records)
    assert len(connections) == 1
    assert sum(s == "COMMIT" for s in trace) == 1
    assert stats["observations"] == 1000 and stats["members"] == 2 and stats["aliases"] == 2
    assert ids.resolve_legacy("room", "乙", ["raw@1"])["status"] == "candidate"
    with closing(connect()) as db:
        rows = db.execute("SELECT * FROM aliases ORDER BY member_id").fetchall()
    ids.backfill("room", records)
    with closing(connect()) as db:
        assert [tuple(r) for r in rows] == [tuple(r) for r in db.execute("SELECT * FROM aliases ORDER BY member_id")]


async def test_http_binds_before_due_summary_load_and_retains_pending(bridge, message, monkeypatch):
    from test_short_memory import add_messages
    add_messages(bridge.short_memory, 1, 100, scope=message.scope)
    bridge.routes[message.scope] = message.model_dump()
    called = asyncio.Event()

    def load(_message):
        called.set()
        raise RuntimeError("synthetic load boundary; no API")

    monkeypatch.setattr(bridge, "_application", load)
    started = time.monotonic()
    async with TestClient(TestServer(create_app(bridge, "x" * 40))) as client:
        response = await client.get("/health", headers={"Authorization": "Bearer " + "x" * 40})
        assert response.status == 200 and (await response.json())["ok"]
        assert time.monotonic() - started < 1
        assert not called.is_set()
        await asyncio.wait_for(called.wait(), timeout=3)
        assert bridge.short_memory.stats(message.scope)["pending_summaries"] == 1
        assert not bridge.test_providers


@pytest.mark.parametrize("status,progress", [("prepared", 0), ("sending", 1), ("sent", 2), ("unknown", 1), ("superseded", 0)])
async def test_requeue_preserves_existing_delivery_and_progress_after_restart(bridge, message, status, progress):
    now = datetime.now(timezone.utc)
    candidate = Candidate(candidate_id="source1", kind="unanswered_question", created_at=now)
    draft = SimpleNamespace(parts=["原始第一段", "原始第二段"], context_hash="original")
    result = await bridge._proactive_deliver(scope=message.scope, candidate=candidate, draft=draft, now=now)
    with closing(sqlite3.connect(bridge.path)) as db, db:
        db.execute("UPDATE deliveries SET status=?,sent_parts=? WHERE id=?", (status, progress, result["id"]))
        before = db.execute("SELECT * FROM deliveries WHERE id=?", (result["id"],)).fetchone()
    await bridge.close()
    fresh = Bridge(bridge.root, bridge.config, bridge.engine_config, bridge.persona_file,
        provider_factory=bridge.provider_factory)
    try:
        # Restart intentionally converts in-flight sending to unknown, retaining progress.
        expected_status = "unknown" if status == "sending" else status
        result2 = await fresh._proactive_deliver(scope=message.scope, candidate=candidate,
            draft=SimpleNamespace(parts=["重启后新文本"], context_hash="changed"), now=now)
        assert result2["id"] == result["id"] and result2["status"] == expected_status
        assert result2["sent_parts"] == progress and result2["reply_parts"] == draft.parts
        with closing(sqlite3.connect(fresh.path)) as db:
            after = db.execute("SELECT * FROM deliveries WHERE id=?", (result["id"],)).fetchone()
            assert before[:3] == after[:3] and before[4:] == after[4:]
        if expected_status != "prepared":
            assert await fresh.claim(result["id"]) == {"claimed": False}
    finally:
        await fresh.close()


async def test_legacy_unknown_without_source_mapping_is_not_resurrected(bridge, message):
    now = datetime.now(timezone.utc)
    candidate = Candidate(candidate_id="old", kind="unanswered_question", created_at=now)
    draft = SimpleNamespace(parts=["旧消息"], context_hash="old")
    first = await bridge._proactive_deliver(scope=message.scope, candidate=candidate, draft=draft, now=now)
    with closing(sqlite3.connect(bridge.path)) as db, db:
        db.execute("DELETE FROM proactive_sources")
        db.execute("UPDATE deliveries SET status='unknown' WHERE id=?", (first["id"],))
    replay = await bridge._proactive_deliver(scope=message.scope, candidate=candidate, draft=draft, now=now)
    assert replay["status"] == "unknown"
    assert await bridge.claim(first["id"]) == {"claimed": False}


async def test_consumed_source_not_selected_or_generated_again(bridge, message):
    now = datetime.now(timezone.utc)
    question = message.model_copy(update={"mentioned": False, "text": "周末去哪里？"})
    await bridge.receive(question)
    candidate = bridge._proactive_candidates(message.scope)[0]
    await bridge._proactive_deliver(scope=message.scope, candidate=candidate,
        draft=SimpleNamespace(parts=["先休息一会"], context_hash="one"), now=now)
    assert bridge._proactive_candidates(message.scope) == []
    # An already-loaded foreground app must not be charged for the consumed source.
    bridge._application(message, start_workers=False)
    provider = bridge.test_providers[0]
    assert await bridge._proactive_generate(scope=message.scope, candidate=candidate, now=now, instruction="主动轮") is None
    assert not provider.requests

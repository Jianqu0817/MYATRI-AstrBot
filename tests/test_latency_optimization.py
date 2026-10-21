"""Verified per-pass caching, snapshot boundaries and numerical telemetry."""
from copy import copy
import json

import pytest

from myatri.storage import ArchiveStore
from myatri.storage.archive import ArchiveError, IntegrityError
from myatri_bridge.context_read_view import ContextReadView
from test_rich_group_context import seeded


def test_view_retains_revisions_isolation_and_new_pass_freshness(tmp_path):
    store = ArchiveStore(tmp_path)
    first = store.append_message("user", "旧原话", message_id="old")
    snapshot = store.snapshot()
    view = ContextReadView(store)
    store.append_message("user", "后来的原话", message_id="new")
    assert view.snapshot() == snapshot
    assert [r.ref for r in view.list_records()] == [first.ref]
    assert view.get_record("old", 1, snapshot).body_markdown == "旧原话"
    changed = view.get_record("old")
    changed.payload["role"] = "assistant"
    assert view.get_record("old").payload["role"] == "user"
    assert len(ContextReadView(store).list_records()) == 2
    assert len(ContextReadView(store).list_records(snapshot=snapshot)) == 1
    with pytest.raises(ArchiveError):
        view.list_records(snapshot="not_in_this_archive")
    with pytest.raises(AttributeError):
        view.commit([])


def test_new_view_does_not_hide_tampered_source(tmp_path):
    store = ArchiveStore(tmp_path)
    record = store.append_message("user", "保留这句话", message_id="one")
    ContextReadView(store)
    manifests, _ = store._load()
    path = store.domain_path / manifests[-1]["records"][0]["path"]
    path.write_text("damaged", encoding="utf-8")
    with pytest.raises(IntegrityError):
        ContextReadView(store)
    assert record.record_id == "one"


async def test_cached_assembly_preserves_messages_selection_and_recall_owner(bridge, message, monkeypatch):
    app, timeline = seeded(bridge, message, count=12)
    context = app.conversation.context
    data = {**message.model_dump(), "message_id": "current", "recent_messages": timeline,
            "text": "还记得纸飞机吗"}
    query = json.dumps(data, ensure_ascii=False)
    snapshot = app.store.snapshot()
    states, activation = context.selector.select_active_entries(query, snapshot=snapshot, budget=600, persist=False)
    before = copy(context)
    baseline, baseline_info = before._assemble_view(query, snapshot, states, activation, [])
    calls, load = [], app.store._load

    def counted(*args, **kwargs):
        calls.append(1)
        return load(*args, **kwargs)

    monkeypatch.setattr(app.store, "_load", counted)
    messages, info = context._assemble(query, snapshot, states, activation, [])
    assert messages == baseline
    assert info["history_refs"] == baseline_info["history_refs"]
    assert info["context_stats"] == baseline_info["context_stats"]
    assert len(calls) == 1
    assert context.recall.current_sender == message.sender_id
    assert context.store is app.store and context.recall.store is app.store
    assert info["preparation_timings"]["total_seconds"] >= 0


async def test_latency_saved_without_messages_and_default_low_reaches_provider(bridge, message):
    await bridge.receive(message)
    app = bridge.apps[message.scope]
    assert bridge.test_providers[0].requests[0].reasoning_effort == "low"
    timings = app.queue.get_protocol(message.key)["latency"]
    assert all(timings[k] >= 0 for k in ("lock_wait_seconds", "input_seconds", "context_seconds",
                                        "model_seconds", "guard_seconds", "total_seconds"))
    assert message.text not in json.dumps(timings, ensure_ascii=False)

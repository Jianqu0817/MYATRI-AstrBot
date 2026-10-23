"""Owner runtime controls refresh actual next requests without deleting archives."""
from contextlib import closing
import importlib
import sqlite3

from aiohttp.test_utils import TestClient, TestServer
import pytest

from myatri.persona.state import PersonaState
from myatri_bridge.bridge import GROUP_SCOPE_ENTRY, Conflict
from myatri_bridge.server import create_app


async def test_reload_reads_disk_card_and_new_prompt_reaches_next_request(bridge, message, tmp_path, monkeypatch):
    await bridge.receive(message)
    app = bridge.apps[message.scope]
    old = app.persona.read_state()
    original = app.store.get_record(message.key)
    card = tmp_path / "custom-role.md"
    entries = {"P00_current_identity": "小温暖，现在使用刚修改的新角色卡。", "P01": "语气随意，不文绉绉。"}
    card.write_text(PersonaState.render(entries), encoding="utf-8")
    bridge.persona_file = card
    before_bytes = card.read_bytes()
    real_reload = importlib.reload

    def changed_prompt(module):
        loaded = real_reload(module)
        if module.__name__ == "myatri_bridge.group_prompt":
            loaded.TURN_FOCUS_RULES += "\nPROMPT_RELOADED_FOR_TEST"
        return loaded

    monkeypatch.setattr(importlib, "reload", changed_prompt)
    try:
        result = await bridge.reload_runtime(message.scope)
        assert result["ok"] and result["persona_changed"] and result["prompts_reloaded"]
        assert len(bridge.test_providers[0].requests) == 1
        current = app.persona.read_state()
        assert current.revision == old.revision + 1
        assert current.payload["entries"]["P00_current_identity"] == entries["P00_current_identity"]
        assert current.payload["entries"]["P15_group_scope"] == GROUP_SCOPE_ENTRY
        assert app.store.get_record(message.key).body_markdown == original.body_markdown
        assert app.store.get_record("persona", revision=old.revision).body_markdown == old.body_markdown
        await bridge.receive(message.model_copy(update={"message_id": "after-reload", "text": "现在叫什么？"}))
        request = bridge.test_providers[0].requests[-1]
        assert any(entries["P00_current_identity"] in row["content"] for row in request.messages)
        assert "PROMPT_RELOADED_FOR_TEST" in request.messages[-2]["content"]
        assert card.read_bytes() == before_bytes
    finally:
        monkeypatch.undo()
        bridge._reload_prompt_modules(bridge.apps.values())


async def test_invalid_card_and_busy_delivery_leave_runtime_unchanged(bridge, message, tmp_path):
    await bridge.receive(message)
    app = bridge.apps[message.scope]
    persona = app.persona.read_state().ref
    card = tmp_path / "bad.md"
    card.write_text("no stable entry headings", encoding="utf-8")
    bridge.persona_file = card
    with pytest.raises(ValueError):
        await bridge.reload_runtime(message.scope)
    assert app.persona.read_state().ref == persona
    await bridge.claim(message.key)
    with pytest.raises(Conflict):
        await bridge.clear_runtime_context(message.scope)
    assert app.conversation.context.context_boundary() is None
    with pytest.raises(KeyError):
        await bridge.clear_runtime_context("0" * 32)
    with pytest.raises(ValueError):
        await bridge.clear_runtime_context("../bad-path")


async def test_clear_context_is_persistent_and_preserves_source_records(bridge, message):
    await bridge.receive(message)
    app = bridge.apps[message.scope]
    before = [(row.ref, row.body_markdown) for row in app.store.list_records()]
    app.conversation.context.budget.anchor = {"prompt_tokens": 123, "parts": []}
    app.queue.save_protocol("old-active", {"status": "active", "messages": [{"role": "user", "content": "old"}]})
    result = await bridge.clear_runtime_context(message.scope)
    assert result["ok"] and result["context_reset"]["snapshot"]
    assert app.conversation.context.budget.anchor is None
    assert app.queue.get_protocol("old-active")["status"] == "context_reset"
    assert [(row.ref, row.body_markdown) for row in app.store.list_records()] == before
    boundary = app.queue.get_meta("group_context_boundary")
    assert message.message_id in boundary["excluded_message_ids"]
    assert boundary["listening_seq"] == 1
    with closing(sqlite3.connect(bridge.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM recent_messages WHERE scope=?", (message.scope,)).fetchone()[0] == 0
        assert db.execute("SELECT status FROM deliveries WHERE id=?", (message.key,)).fetchone()[0] == "superseded"


async def test_control_for_unloaded_group_does_not_start_workers_or_call_model(bridge, message):
    await bridge.receive(message.model_copy(update={"mentioned": False}))
    assert not bridge.apps
    result = await bridge.clear_runtime_context(message.scope)
    assert result["ok"] and message.scope in bridge.apps
    assert not bridge.tasks and not bridge.index_tasks
    assert not bridge.test_providers[0].requests
    await bridge.receive(message.model_copy(update={"message_id": "fresh"}))
    assert message.scope in bridge.tasks
    assert len(bridge.test_providers[0].requests) == 1


async def test_admin_api_auth_scope_validation_and_actions(bridge, message):
    await bridge.receive(message.model_copy(update={"mentioned": False}))
    async with TestClient(TestServer(create_app(bridge, "t" * 32))) as client:
        response = await client.post("/v1/admin/reload", json={"scope": message.scope})
        assert response.status == 401
        headers = {"Authorization": "Bearer " + "t" * 32}
        for payload in ({}, {"scope": "../bad"}, {"scope": message.scope, "path": "other"}):
            response = await client.post("/v1/admin/clear-context", json=payload, headers=headers)
            assert response.status == 400
        response = await client.post("/v1/admin/reload", json={"scope": message.scope}, headers=headers)
        assert response.status == 200 and (await response.json())["prompts_reloaded"]
        response = await client.post("/v1/admin/clear-context", json={"scope": message.scope}, headers=headers)
        body = await response.json()
        assert response.status == 200 and body["action"] == "clear-context"

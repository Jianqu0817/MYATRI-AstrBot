"""Clearing a live group window removes replay, not durable source memory."""
import json
import asyncio

import pytest

from myatri_bridge.bridge import Conflict


async def test_control_rejects_queued_generation_before_it_acquires_group_lock(bridge, message):
    await bridge.receive(message.model_copy(update={"mentioned": False}))
    pending = asyncio.create_task(asyncio.Event().wait())
    bridge.generations[message.key] = pending
    try:
        with pytest.raises(Conflict):
            await bridge.clear_runtime_context(message.scope)
        assert not bridge.apps
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        bridge.generations.pop(message.key, None)


async def test_new_group_request_keeps_fresh_input_without_old_turns_or_old_short_summary(bridge, message, monkeypatch):
    first = await bridge.receive(message)
    await bridge.claim(first["id"])
    await bridge.acknowledge(first["id"], "sent")
    app = bridge.apps[message.scope]
    old = app.store.get_record(message.key)
    await bridge.clear_runtime_context(message.scope)

    def summary(start, text, source):
        return {"window_start": start, "window_end": start, "created_at": "2026-10-21T10:00:00Z",
            "kind": "group_short_summary", "items": [{"text": text, "speaker_id": message.sender_id,
            "subject_id": message.sender_id, "attribution": "self_report", "source_message_ids": [source]}]}

    monkeypatch.setattr(bridge.short_memory, "summaries", lambda scope: [
        summary(1, "这是清空前窗口的旧摘要", message.message_id),
        summary(2, "这是新窗口允许使用的摘要", "new-window")])
    result = await bridge.receive(message.model_copy(update={"message_id": "new-window", "text": "从现在重新聊吧"}))
    assert result["status"] == "prepared"
    request = bridge.test_providers[0].requests[-1]
    assert len([row for row in request.messages if row["role"] == "user"]) == 1
    assert not any(row["role"] == "assistant" for row in request.messages)
    envelope = json.loads(request.messages[-1]["content"].split("\n", 1)[1])
    assert envelope["text"] == "从现在重新聊吧" and envelope["recent_messages"] == []
    policy = next(row["content"] for row in request.messages if "<group_channel_data>" in row["content"])
    data = json.loads(policy.split("<group_channel_data>\n", 1)[1].split("\n</group_channel_data>", 1)[0])
    assert [item["text"] for s in data["short_summaries"] for item in s["items"]] == ["这是新窗口允许使用的摘要"]
    assert app.store.get_record(old.record_id).body_markdown == old.body_markdown
    assert old.ref not in app.conversation.context.explain_context()["history_refs"]

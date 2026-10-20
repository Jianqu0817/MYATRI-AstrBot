"""Offline transport -> listening -> attributed archive -> reply-context checks."""
import asyncio
import json
from pathlib import Path

from myatri.app import load_config
from myatri.providers import FixtureProvider
from myatri_bridge.bridge import Bridge


PROJECT = Path(__file__).resolve().parents[1]


def reopen(bridge):
    return Bridge(bridge.root, bridge.config, bridge.engine_config,
                  PROJECT / "config/persona/persona.md", provider_factory=bridge.provider_factory)


def policy_data(request):
    content = next(message["content"] for message in request.messages
                   if message["role"] == "system" and "<group_channel_data>" in message["content"])
    return json.loads(content.split("<group_channel_data>\n", 1)[1].split("\n</group_channel_data>", 1)[0])


async def test_nontrigger_listening_durable_without_constructing_engine(bridge, message):
    for number in range(1, 40):
        incoming = message.model_copy(update={"message_id": f"ambient{number}",
                                             "text": f"旁听消息{number}", "mentioned": False})
        assert (await bridge.receive(incoming))["reason"] == "not_a_trigger"
    await asyncio.sleep(0)  # Let the actual summary worker inspect the still-empty outbox.
    assert bridge.apps == {}
    assert bridge.test_providers == []
    assert bridge.short_memory.stats(message.scope) == {
        "total_messages": 39, "pending_summaries": 0, "next_attempt": None}
    assert len(bridge.short_memory.recent(message.scope)) == 39
    assert bridge.short_memory.recent(message.scope)[-1]["message_id"] == "ambient39"


async def test_restarting_bridge_does_not_recount_listening_replay(bridge, message):
    incoming = message.model_copy(update={"mentioned": False, "text": "无需回答的普通发言"})
    await bridge.receive(incoming)
    await bridge.close()
    restored = reopen(bridge)
    try:
        assert (await restored.receive(incoming))["reason"] == "already_seen"
        assert restored.short_memory.stats(message.scope)["total_messages"] == 1
        assert restored.short_memory.recent(message.scope) == bridge.short_memory.recent(message.scope)
        assert restored.apps == {}
        assert restored.routes[message.scope]["message_id"] == incoming.message_id
    finally:
        await restored.close()


async def test_hundred_nontriggers_summarize_archive_and_enter_next_context(tmp_path, message):
    item = {
        "text": "甲自述喜欢红茶", "speaker_id": "member1", "subject_id": "member1",
        "source_message_ids": ["ambient1"], "attribution": "self_report",
        "source_quotes": [{"message_id": "ambient1", "quote": "我喜欢红茶"}],
    }
    provider = FixtureProvider([{"content": json.dumps({"items": [item]}, ensure_ascii=False)},
        {"content": json.dumps({"checks": [{"index": 0, "verdict": "supported", "reason": "原话明确自述", "source_message_ids": ["ambient1"]}]}, ensure_ascii=False)}])
    instance = Bridge(tmp_path / "groups", {
        "mode": "offline", "allowed_groups": [message.route], "random_reply_probability": 0,
        "cooldown_seconds": 0, "owner_id": "owner", "owner_name": "历史原对话人",
    }, load_config(PROJECT / "config/engine.toml"), PROJECT / "config/persona/persona.md",
        provider_factory=lambda scope: provider)
    try:
        for number in range(1, 101):
            incoming = message.model_copy(update={
                "message_id": f"ambient{number}", "mentioned": False,
                "text": "我喜欢红茶" if number == 1 else f"今天群里继续聊日常{number}",
            })
            assert (await instance.receive(incoming))["reason"] == "not_a_trigger"
        # No foreground request is necessary to create/complete the summary task.
        async with asyncio.timeout(10):
            while not instance.short_memory.summaries(message.scope):
                await asyncio.sleep(0.01)
        assert instance.short_memory.stats(message.scope)["pending_summaries"] == 0
        assert len(provider.requests) == 2
        assert provider.requests[0].purpose == "group_short_memory"
        app = instance.apps[message.scope]
        assert app.gateway.budget_ledger is instance.budget
        runs = [record for record in app.store.list_records("cognitive_run")
                if record.payload.get("role") == "group_short_summary"]
        assert len(runs) == 1
        assert runs[0].generator["provider"] == "fixture"
        archived_raw = [record for record in app.store.list_records("raw_message")
                        if record.record_id.startswith("listen_")]
        assert len(archived_raw) == 1  # Retain the actual personal-claim evidence, not 100 fake dialogue turns.
        assert archived_raw[0].body_markdown == "我喜欢红茶"
        assert archived_raw[0].payload["channel_source"]["sender_id"] == "member1"
        events = app.store.list_records("event")
        assert len(events) == 1
        assert events[0].payload["participants"] == ["member1"]
        assert events[0].payload["source_spans"][0]["quote"] == "我喜欢红茶"
        assert events[0].payload["source_spans"][0]["message_ref"] == archived_raw[0].ref
        assert instance.short_memory.recent(message.scope)[0]["message_id"] == "ambient1"

        trigger = message.model_copy(update={"message_id": "question", "text": "还记得我喜欢喝什么吗"})
        assert (await instance.receive(trigger))["status"] == "prepared"
        request = next(request for request in reversed(provider.requests) if request.role == "foreground")
        data = policy_data(request)
        assert data["current_message"]["sender_id"] == "member1"
        assert len(data["short_summaries"]) == 1
        assert data["short_summaries"][0]["items"][0]["subject_id"] == "member1"
        assert data["short_summaries"][0]["items"][0]["text"] == "甲自述喜欢红茶"
        assert "甲自述喜欢红茶" in "\n".join(m["content"] for m in request.messages)
    finally:
        await instance.close()


async def test_confirmed_dialogue_survives_twenty_message_timeline_eviction(bridge, message):
    first_reply = None
    for number in range(1, 12):
        incoming = message.model_copy(update={"message_id": f"chat{number}",
                                             "text": f"第{number}次认真接着聊，旧话题标记{number}"})
        result = await bridge.receive(incoming)
        assert result["status"] == "prepared"
        assert (await bridge.claim(result["id"]))["claimed"]
        await bridge.acknowledge(result["id"], "sent")
        if number == 1:
            first_reply = result["reply"]
    # More than 20 subsequent nontrigger messages evict all confirmed pairs from
    # the rolling timeline. Their complete turns must still come from the archive.
    for number in range(25):
        await bridge.receive(message.model_copy(update={"message_id": f"ambient{number}",
                                                       "mentioned": False, "text": f"背景闲聊{number}"}))
    await bridge.receive(message.model_copy(update={"message_id": "continue", "text": "接着最早的话题聊"}))
    request = bridge.test_providers[0].requests[-1]
    assistants = [part for part in request.messages if part["role"] == "assistant"]
    users = [part for part in request.messages if part["role"] == "user"]
    assert len(assistants) == 11
    assert len(users) == 12
    assert assistants[0]["content"] == first_reply
    first = json.loads(users[0]["content"])
    assert first["message_id"] == "chat1"
    assert first["text"] == "第1次认真接着聊，旧话题标记1"
    recent = json.loads(users[-1]["content"].split("\n", 1)[1])["recent_messages"]
    assert len(recent) <= 20
    assert not any(part["message_id"].startswith("chat") for part in recent)
    assert bridge.short_memory.stats(message.scope)["total_messages"] == 37

import json

import pytest


@pytest.mark.parametrize("name", ["小温暖", "小溫暖"])
async def test_keyword_bypasses_random_draw_and_cooldown(bridge, message, name):
    bridge.config["cooldown_seconds"] = 3600
    await bridge.receive(message)
    result = await bridge.receive(message.model_copy(update={
        "message_id": "keyword", "mentioned": False, "text": name + "出来聊聊"}))
    assert result["status"] == "prepared"
    assert len(bridge.test_providers[0].requests) == 2


async def test_random_reply_has_twenty_recent_group_messages_and_keeps_memory(bridge, message):
    for i in range(23):
        item = message.model_copy(update={"message_id": f"ordinary{i}", "mentioned": False,
                                         "text": f"不触发的群消息{i}", "sender_id": f"member{i}"})
        assert (await bridge.receive(item))["status"] == "ignored"
    assert not bridge.apps
    bridge.random_probability = 1
    trigger = message.model_copy(update={"message_id": "random", "mentioned": False, "text": "现在随机回一句"})
    assert (await bridge.receive(trigger))["status"] == "prepared"
    request = bridge.test_providers[0].requests[-1]
    turns = [m for m in request.messages if m["role"] in {"user", "assistant"}]
    assert len(turns) == 1
    envelope = json.loads(turns[0]["content"].split("\n", 1)[1])
    recent = envelope["recent_messages"]
    assert len(recent) == 19  # Current message is supplied once, in the top-level envelope.
    assert [m["message_id"] for m in recent] == [f"ordinary{i}" for i in range(4, 23)]
    assert envelope["message_id"] == "random"
    assert recent[0]["sender_id"] == "member4" and envelope["sent_at"] == trigger.sent_at
    assert any("当前人格" in m["content"] for m in request.messages if m["role"] == "system")
    assert (await bridge.receive(trigger))["status"] == "prepared"
    assert len(bridge.test_providers[0].requests) == 1
    old = message.model_copy(update={"message_id": "ordinary0", "mentioned": False,
                                    "text": "不触发的群消息0", "sender_id": "member0"})
    assert (await bridge.receive(old))["status"] == "ignored"
    assert len(bridge.test_providers[0].requests) == 1


async def test_only_confirmed_own_replies_join_context_and_same_nickname_stays_distinct(bridge, message):
    result = await bridge.receive(message)
    await bridge.claim(result["id"])
    await bridge.acknowledge(result["id"], "sent")
    next_message = message.model_copy(update={"message_id": "different_member", "sender_id": "member2",
                                             "text": "刚才你说什么", "sender_name": message.sender_name})
    result2 = await bridge.receive(next_message)
    request = bridge.test_providers[0].requests[-1]
    text = next(m["content"] for m in reversed(request.messages) if m["role"] == "user")
    recent = json.loads(text.split("\n", 1)[1])["recent_messages"]
    assert recent == [] # Earlier messages already appear as a complete dialogue pair.
    user_turns = [m for m in request.messages if m["role"] == "user"]
    earlier = json.loads(user_turns[0]["content"])
    current = json.loads(user_turns[-1]["content"].split("\n", 1)[1])
    assert earlier["sender_id"] == "member1" and current["sender_id"] == "member2"
    assert earlier["sender_name"] == current["sender_name"]
    assert next(m["content"] for m in request.messages if m["role"] == "assistant") == result["reply"]
    await bridge.claim(result2["id"])
    await bridge.acknowledge(result2["id"], "unknown")
    await bridge.receive(message.model_copy(update={"message_id": "third"}))
    turns = bridge.test_providers[0].requests[-1].messages
    assert sum(m["role"] == "assistant" for m in turns) == 1

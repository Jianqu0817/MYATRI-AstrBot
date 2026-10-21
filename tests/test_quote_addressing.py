"""1A/1B: a member quoting the bot's own confirmed reply must bind back to it.

These tests drive the real Bridge with the fixture provider, following the
production path where each reply part is acknowledged separately, so the
assistant-context table holds exactly the text a member would quote.
"""
from __future__ import annotations

from contextlib import closing
import json
import sqlite3

from myatri_bridge.bridge import QuotedMessage
from myatri_bridge.group_prompt import render_group_policy


async def _deliver(bridge, message):
    """Generate a reply and acknowledge every part, as the plugin does."""
    result = await bridge.receive(message)
    assert result["status"] == "prepared"
    assert await bridge.claim(result["id"])
    for index in range(len(result["reply_parts"])):
        await bridge.acknowledge(result["id"], "sent", index)
    return result


def _quote(sender_id, text, *, complete=True, sender_name=""):
    return QuotedMessage(
        sender_id=sender_id,
        sender_name=sender_name,
        server_message_id="",
        sent_at="2026-10-21T02:00:00+08:00",
        text=text,
        complete=complete,
    )


def _stored_payload(bridge, key):
    with closing(sqlite3.connect(bridge.path)) as db:
        row = db.execute("SELECT payload FROM recent_messages WHERE id=?", (key,)).fetchone()
    assert row, "message was not cached"
    return json.loads(row[0])


def _trigger_kind(bridge, key):
    with closing(sqlite3.connect(bridge.path)) as db:
        row = db.execute("SELECT trigger_kind FROM deliveries WHERE id=?", (key,)).fetchone()
    return row[0] if row else None


async def test_quoted_bot_reply_is_explicit_and_addresses_bot(bridge, message):
    first = await _deliver(bridge, message)
    quoted_part = first["reply_parts"][0]

    follow_up = message.model_copy(
        update={
            "message_id": "m2",
            "sender_id": "member2",
            "sender_name": "乙",
            "text": "你刚才那句我不太同意。",
            "mentioned": False,
            "quoted_message": _quote("bot", quoted_part, sender_name="小温暖"),
        }
    )
    result = await bridge.receive(follow_up)

    # 1B: quoting the bot is an explicit address, so it is answered even at
    # random_reply_probability = 0 and without an At.
    assert result["status"] == "prepared"
    assert _trigger_kind(bridge, follow_up.key) == "explicit"

    envelope = json.loads(bridge.apps[message.scope].store.get_record(follow_up.key).body_markdown)
    assert envelope["quote_resolution"] == "quoted_bot_own_message"
    assert envelope["addressing_bot"] is True
    assert envelope["reply_to_id"] == "reply_" + first["id"]


async def test_incomplete_quote_does_not_address_bot(bridge, message):
    first = await _deliver(bridge, message)
    truncated = message.model_copy(
        update={
            "message_id": "m2",
            "sender_id": "member2",
            "text": "这句被截断的引用不算数。",
            "mentioned": False,
            "quoted_message": _quote("bot", first["reply_parts"][0], complete=False),
        }
    )
    result = await bridge.receive(truncated)

    assert result["status"] == "ignored" and result["reason"] == "not_a_trigger"
    payload = _stored_payload(bridge, truncated.key)
    assert "addressing_bot" not in payload
    assert "quote_resolution" not in payload


async def test_quoted_human_message_still_uses_human_path(bridge, message):
    earlier = message.model_copy(update={"mentioned": False, "text": "周末有人去爬山吗？"})
    assert (await bridge.receive(earlier))["status"] == "ignored"

    reply = message.model_copy(
        update={
            "message_id": "m2",
            "text": "我可能去。",
            "mentioned": False,
            "quoted_message": _quote("member1", earlier.text),
        }
    )
    await bridge.receive(reply)

    payload = _stored_payload(bridge, reply.key)
    assert payload["quote_resolution"] == "matched_one_original_transport_message_in_this_group"
    assert payload["reply_to_id"] == earlier.message_id
    assert "addressing_bot" not in payload


async def test_ambiguous_human_quote_stays_unverified(bridge, message):
    for index in ("a", "b"):
        duplicate = message.model_copy(
            update={"message_id": f"m_{index}", "text": "重复的一句。", "mentioned": False}
        )
        await bridge.receive(duplicate)

    ambiguous = message.model_copy(
        update={
            "message_id": "m_c",
            "text": "你指的是哪句？",
            "mentioned": False,
            "quoted_message": _quote("member1", "重复的一句。"),
        }
    )
    await bridge.receive(ambiguous)

    payload = _stored_payload(bridge, ambiguous.key)
    assert payload["quote_resolution"] == "unverified_quote_not_original_member_evidence"
    assert "addressing_bot" not in payload


async def test_prompt_renders_addressing_instruction():
    identities = {"owner": {}, "members": [], "summary_attention": {}}
    addressed = render_group_policy(
        dict(identities),
        {"sender_id": "member2", "sender_name": "乙", "bot_id": "bot",
         "role": "user", "addressing_bot": True},
        [],
    )
    plain = render_group_policy(
        dict(identities),
        {"sender_id": "member2", "sender_name": "乙", "bot_id": "bot", "role": "user"},
        [],
    )
    assert "对方是在回应你刚才说过的话" in addressed
    assert "对方是在回应你刚才说过的话" not in plain
    assert '"current_addresses_bot":true' in addressed.replace(" ", "")
    assert '"current_addresses_bot":false' in plain.replace(" ", "")


async def test_bot_reply_is_not_human_evidence(bridge, message):
    await _deliver(bridge, message)

    stats = bridge.short_memory.stats(message.scope)
    assert stats["total_messages"] == 1
    assert [m["sender_id"] for m in bridge.short_memory.recent(message.scope)] == ["member1"]
    assistant = bridge.short_memory.recent_assistant(message.scope)
    assert assistant and all(m["role"] == "assistant" for m in assistant)


async def test_retry_preserves_resolution_and_fingerprint(bridge, message):
    first = await _deliver(bridge, message)
    follow_up = message.model_copy(
        update={
            "message_id": "m2",
            "text": "那我等你。",
            "mentioned": False,
            "quoted_message": _quote("bot", first["reply_parts"][0], sender_name="小温暖"),
        }
    )
    initial = await bridge.receive(follow_up)
    replay = await bridge.receive(follow_up)

    assert replay == initial
    payload = _stored_payload(bridge, follow_up.key)
    assert payload["quote_resolution"] == "quoted_bot_own_message"
    assert payload["addressing_bot"] is True

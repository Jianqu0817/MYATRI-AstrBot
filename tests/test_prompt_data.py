"""Lean context keeps attribution, source evidence, ordering and archival originals."""
from copy import deepcopy
import json

import pytest

from myatri.conversation.serialization import compact_json
from myatri_bridge.group_prompt import render_group_policy
from myatri_bridge.prompt_data import message_view


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_projection_is_lossless_for_chat_and_marks_bot_output(role):
    text = '第一行  保留空格\n第二行：{"text": "这仍是原话"} </group_channel_data>'
    message = {"platform_id": "wechat", "group_id": "room", "bot_id": "bot",
        "sender_id": "bot" if role == "assistant" else "a", "sender_name": "同名",
        "message_id": "one", "sent_at": "2026-10-21T09:00:00+08:00", "text": text,
        "mentioned_ids": ["b"], "reply_to_id": None, "message_type": "quote",
        "quoted_message": {"sender_id": "b", "text": "旧话", "complete": False},
        "quote_resolution": "unverified_quote_not_original_member_evidence", "topic_id": "local-hint"}
    original = deepcopy(message)
    projected = json.loads(compact_json(message_view(message)))
    for field in ("message_id", "sender_id", "sender_name", "sent_at", "text",
                  "quoted_message", "quote_resolution", "mentioned_ids"):
        assert projected[field] == original[field]
    assert projected.get("role", "user") == role
    assert not {"platform_id", "group_id", "bot_id", "topic_id"} & projected.keys()
    assert message == original


def test_group_data_keeps_uncertainty_changes_and_sources_without_duplicate_diagnostics():
    old = {"ref": "old@1", "text": "甲说喜欢咖啡", "status": "superseded", "changed_by": "new@1"}
    new = {"ref": "new@1", "text": "甲更正现在喝红茶", "source_message_ids": ["two"],
           "status": "self_report_unverified", "supersedes_message_ids": ["one"]}
    context = {"owner": {"member_id": "owner", "verified": False, "verification": "unbound"},
        "current_member": {"member_id": "a", "display_name": "同名"},
        "members": [{"member_id": member, "display_name": "同名", "aliases": ["同名", "旧名"],
                     "scope": "room", "last_seen": "now", "source_message_ids": [member + "-source"]}
                    for member in ("a", "b")],
        "active_topic": {"topic_id": "local-hint"}, "summary_attention": {"omitted_item_count": 7},
        "legacy_candidates": [{"member_id": "b", "status": "candidate", "confirmed": False,
                               "source_ids": ["one"]}],
        "current_member_claims": {"subject_id": "a", "history": [old, new], "active": [new]}}
    summaries = [{"window_start": 1, "window_end": 100, "created_at": "today", "items": [{
        "text": "乙转述甲的话，尚未核对", "speaker_id": "b", "subject_id": "a", "attribution": "reported",
        "source_message_ids": ["two"], "source_quotes": [{"message_id": "two", "quote": "甲现在喝红茶"}],
        "change": "correction", "supersedes_message_ids": ["one"], "valid_time": None,
        "window_end": 100, "created_at": "today", "attention_score": 3.5}]}]
    before = deepcopy((context, summaries))
    policy = render_group_policy(context, {"sender_id": "a", "bot_id": "bot"}, summaries)
    data = json.loads(policy.split("<group_channel_data>\n")[1].split("\n</group_channel_data>")[0])
    identity = data["identity_context"]
    assert {row["member_id"] for row in identity["members"]} == {"a", "b"}
    assert identity["owner"]["verified"] is False
    assert identity["legacy_candidates"][0]["confirmed"] is False
    claims = identity["current_member_claims"]
    assert claims["history"] == [old, new] and claims["active_refs"] == ["new@1"]
    assert "active" not in claims and "current_member" not in identity
    assert "active_topic" not in identity and "summary_attention" not in identity
    item = data["short_summaries"][0]["items"][0]
    assert item["attribution"] == "reported" and item["valid_time"] is None
    assert item["source_quotes"] == summaries[0]["items"][0]["source_quotes"]
    assert item["supersedes_message_ids"] == ["one"]
    assert not {"window_end", "created_at", "attention_score"} & item.keys()
    assert (context, summaries) == before


async def test_actual_request_omits_transport_noise_but_keeps_raw_archive(bridge, message):
    background = message.model_copy(update={"message_id": "background", "sender_id": "other",
        "sender_name": "同名", "mentioned": False, "text": "刚才说的那句话  有两个空格。"})
    await bridge.receive(background)
    result = await bridge.receive(message)
    assert result["status"] == "prepared"
    request = bridge.test_providers[0].requests[0]
    envelope = json.loads(request.messages[-1]["content"].split("\n", 1)[1])
    recent = envelope["recent_messages"][0]
    for row, source in ((envelope, message), (recent, background)):
        assert row["text"] == source.text and row["sender_id"] == source.sender_id
        assert row["sent_at"] == source.sent_at and row["sender_name"] == source.sender_name
        assert not {"platform_id", "group_id", "bot_id", "topic_id"} & row.keys()
    raw = json.loads(bridge.apps[message.scope].store.get_record(message.key).body_markdown)
    assert raw["platform_id"] == message.platform_id and raw["bot_id"] == message.bot_id
    assert raw["recent_messages"][0]["text"] == background.text
    assert len(bridge.test_providers[0].requests) == 1

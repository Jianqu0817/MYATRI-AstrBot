"""Interaction detail survives projections without becoming a personal fact."""
from copy import deepcopy
import json

import pytest

from myatri_bridge.short_memory import ShortMemory
from myatri_bridge.topics import select_summaries


def interaction():
    return {"text": "甲把加班叫作给月亮打工，仍想下次接着聊这个玩笑。",
        "speaker_id": "a", "subject_id": "", "attribution": "conversation",
        "source_message_ids": ["m1"], "interaction_kind": "joke",
        "source_quotes": [{"message_id": "m1", "quote": "给月亮打工"}]}


def test_interaction_keeps_original_quote_and_attribution_in_context():
    item = interaction()
    messages = [{"message_id": "m1", "sender_id": "a", "text": "我今晚又在给月亮打工。"}]
    ShortMemory._validate_items({"items": [item]}, messages)
    result = select_summaries([{"window_end": 100, "created_at": "2026-10-21", "items": [item]}], "月亮打工", "a")
    projected = result["items"][0]
    assert projected["source_quotes"] == item["source_quotes"]
    assert projected["interaction_kind"] == "joke"
    assert projected["attribution"] == "conversation"
    assert projected["subject_id"] == ""
    assert result["omitted_item_count"] == 0


@pytest.mark.parametrize("kind", ["joke", "open_thread", "emotional_cue"])
def test_temporary_interaction_cannot_be_promoted_to_personal_claim(kind):
    item = {**interaction(), "interaction_kind": kind, "attribution": "self_report", "subject_id": "a"}
    with pytest.raises(ValueError, match="transient interaction"):
        ShortMemory._validate_items({"items": [item]}, [{"message_id": "m1", "sender_id": "a", "text": "给月亮打工"}])


def test_rich_projection_stays_bounded_and_does_not_merge_same_named_people():
    rows = []
    for number in range(30):
        item = deepcopy(interaction())
        item.update(text=f"共同玩笑{number}：" + "详细语境" * 20, speaker_id="a" if number % 2 else "b")
        rows.append(item)
    result = select_summaries([{"window_end": 100, "created_at": "2026-10-21", "items": rows}], "共同玩笑", "a", max_chars=2000)
    assert result["omitted_item_count"] > 0
    assert sum(len(json.dumps(row, ensure_ascii=False)) for row in result["items"]) <= 2000
    assert {row["speaker_id"] for row in result["items"]} == {"a", "b"}

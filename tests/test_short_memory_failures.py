"""Failure metadata and bounded recovery use synthetic fixed windows only."""
import json
import sqlite3
from contextlib import closing

import pytest

from myatri.providers import FixtureProvider, Gateway
from myatri_bridge.memory_review import review_summary
from myatri_bridge.short_memory import ShortMemory
from test_short_memory import add_messages, item


def ready(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE listening_pending SET next_attempt=0")


async def test_length_recovery_preserves_complete_window_and_is_bounded(tmp_path):
    path = tmp_path / "memory.db"
    memory = ShortMemory(path)
    add_messages(memory, 1, 100)
    provider = FixtureProvider([
        {"content": '{"items":[', "finish_reason": "length"},
        {"content": '{"items":[', "finish_reason": "length"},
        {"content": json.dumps({"items": [item()]}, ensure_ascii=False)},
    ])
    gateway = Gateway(provider, daily_token_limit=1000000)
    first = await memory.summarize_pending("room", gateway)
    assert first["diagnostic"] == {"stage": "generate", "code": "output_length",
                                   "exception_type": "ProviderError", "max_tokens": 8000}
    assert (await memory.summarize_pending("room", gateway))["status"] == "deferred"
    assert len(provider.requests) == 1
    memory = ShortMemory(path)  # Persistent failure classification survives restart.
    ready(path)
    second = await memory.summarize_pending("room", gateway)
    assert second["diagnostic"]["max_tokens"] == 12000
    ready(path)
    assert (await memory.summarize_pending("room", gateway))["status"] == "summarized"
    assert [r.max_tokens for r in provider.requests] == [8000, 12000, 12000]
    assert all(not r.thinking for r in provider.requests)
    windows = [json.loads(r.messages[-1]["content"]) for r in provider.requests]
    assert all(w == windows[0] for w in windows)
    assert len(windows[0]["messages"]) == 100 and not windows[0]["omitted_message_ids"]
    assert memory.summaries("room")[0]["items"] == [item()]


@pytest.mark.parametrize(("response", "code"), [
    ('{"items":[', "invalid_json"),
    ('{"items":"wrong type"}', "invalid_schema"),
])
async def test_structured_failure_is_classified_without_saving_content(tmp_path, response, code):
    memory = ShortMemory(tmp_path / "memory.db")
    add_messages(memory, 1, 100, text="synthetic private marker")
    provider = FixtureProvider([{"content": response}])
    result = await memory.summarize_pending("room", Gateway(provider))
    assert result["diagnostic"]["stage"] == "generate"
    assert result["diagnostic"]["code"] == code
    assert memory.summaries("room") == []
    diagnostics = memory.pending_diagnostics("room")
    assert diagnostics[0]["details"] == result["diagnostic"]
    assert diagnostics[0]["draft_cached"] is False
    assert "synthetic private marker" not in json.dumps(diagnostics)
    assert response not in json.dumps(diagnostics)


async def test_review_length_failure_reuses_draft_after_restart(tmp_path):
    path = tmp_path / "memory.db"
    memory = ShortMemory(path)
    add_messages(memory, 1, 100)
    provider = FixtureProvider([
        {"content": json.dumps({"items": [item()]}, ensure_ascii=False)},
        {"content": '{"checks":[', "finish_reason": "length"},
        {"content": json.dumps({"checks": [{"index": 0, "verdict": "supported", "reason": "原话支持",
                                             "source_message_ids": ["m1"]}]}, ensure_ascii=False)},
    ])
    gateway = Gateway(provider, daily_token_limit=1000000)
    first = await memory.summarize_pending("room", gateway, reviewer=review_summary)
    assert first["diagnostic"]["stage"] == "review"
    assert first["diagnostic"]["code"] == "output_length"
    assert memory.pending_diagnostics("room")[0]["draft_cached"] is True
    memory = ShortMemory(path)
    ready(path)
    assert (await memory.summarize_pending("room", gateway, reviewer=review_summary))["status"] == "summarized"
    assert [r.purpose for r in provider.requests] == ["group_short_memory", "group_memory_support_review",
                                                    "group_memory_support_review"]
    assert memory.summaries("room")[0]["items"] == [item()]


async def test_many_member_facts_survive_without_programmatic_item_truncation(tmp_path):
    memory = ShortMemory(tmp_path / "memory.db")
    values = []
    for number in range(1, 101):
        sender = f"member{number}" if number <= 30 else "member1"
        add_messages(memory, number, number, sender=sender)
        if number <= 30:
            values.append(item(speaker_id=sender, subject_id=sender, source_message_ids=[f"m{number}"],
                               source_quotes=[{"message_id": f"m{number}", "quote": "我喜欢红茶"}]))
    provider = FixtureProvider([{"content": json.dumps({"items": values}, ensure_ascii=False)}])
    assert (await memory.summarize_pending("room", Gateway(provider, daily_token_limit=1000000)))["item_count"] == 30
    assert memory.summaries("room")[0]["items"] == values


async def test_thirty_item_review_retains_all_source_checks_with_bounded_output(tmp_path):
    memory = ShortMemory(tmp_path / "memory.db")
    values = []
    for number in range(1, 101):
        sender = f"member{number}" if number <= 30 else "member1"
        add_messages(memory, number, number, sender=sender)
        if number <= 30:
            values.append(item(speaker_id=sender, subject_id=sender, source_message_ids=[f"m{number}"],
                               source_quotes=[{"message_id": f"m{number}", "quote": "我喜欢红茶"}]))
    checks = [{"index": i, "verdict": "supported", "reason": "本人原话明确支持",
               "source_message_ids": value["source_message_ids"]} for i, value in enumerate(values)]
    provider = FixtureProvider([
        {"content": json.dumps({"items": values}, ensure_ascii=False)},
        {"content": json.dumps({"checks": checks}, ensure_ascii=False)},
    ])
    result = await memory.summarize_pending("room", Gateway(provider, daily_token_limit=1000000), reviewer=review_summary)
    assert result["status"] == "summarized" and result["item_count"] == 30
    review_request = provider.requests[1]
    assert review_request.max_tokens == 6000 and review_request.thinking is False
    review_input = json.loads(review_request.messages[-1]["content"])
    assert len(review_input["claims"]) == len(review_input["messages"]) == 30
    assert memory.summaries("room")[0]["review"]["checks"] == checks

"""Durable bounded repair, candidate isolation and legacy backlog migration."""
import asyncio
from contextlib import closing
import json
import sqlite3

from myatri.providers import FixtureProvider, Gateway
from myatri_bridge.short_memory import MAX_REPAIR_FAILURES, ShortMemory
from test_short_memory import add_messages, item


def ready(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE listening_pending SET next_attempt=0")


def old_backlog(path, *, attempts=6):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("UPDATE listening_pending SET attempts=?,repair_state=NULL,next_attempt=9999999999,"
                   "error_details=?", (attempts, json.dumps({"code": "output_length"})))


def response(values):
    return {"content": json.dumps({"items": values}, ensure_ascii=False)}


def second_item():
    return item(source_message_ids=["m51"], source_quotes=[{"message_id": "m51", "quote": "我喜欢红茶"}])


async def test_output_allowance_never_falls_after_mixed_failure_and_restart(tmp_path):
    path = tmp_path / "memory.db"
    memory = ShortMemory(path)
    add_messages(memory, 1, 100)
    provider = FixtureProvider([{"content": '{"items":[', "finish_reason": "length"},
                                {"content": '{"items":"invalid"}'}, response([item()])])
    gateway = Gateway(provider, daily_token_limit=1000000)
    assert (await memory.summarize_pending("room", gateway))["status"] == "failed"
    for expected in ("failed", "summarized"):
        memory = ShortMemory(path)
        ready(path)
        assert (await memory.summarize_pending("room", gateway))["status"] == expected
    assert [r.max_tokens for r in provider.requests] == [8000, 12000, 12000]


async def test_legacy_backlog_released_once_and_split_progress_survives_restart(tmp_path):
    path = tmp_path / "memory.db"
    memory = ShortMemory(path)
    add_messages(memory, 1, 100)
    old_backlog(path)
    memory = ShortMemory(path)
    assert memory.pending_scopes() == ["room"]
    provider = FixtureProvider([response([item()]), response([second_item()])])
    gateway = Gateway(provider, daily_token_limit=1000000)
    progress = await memory.summarize_pending("room", gateway)
    assert progress == {"status": "progress", "window_end": 100, "completed_parts": 1, "total_parts": 2}
    assert not memory.summaries("room")
    memory = ShortMemory(path)
    done = await memory.summarize_pending("room", gateway)
    assert done["status"] == "summarized" and done["item_count"] == 2
    windows = [json.loads(r.messages[-1]["content"]) for r in provider.requests]
    assert [len(w["messages"]) for w in windows] == [50, 50]
    assert [w["messages"][0]["message_id"] for w in windows] == ["m1", "m51"]
    assert windows[1]["prior_claims"] == [item()]
    saved = memory.summaries("room")[0]
    assert saved["complete"] is True and len(saved["source_message_ids"]) == 100
    assert len(provider.requests) == 2


async def test_completed_split_checkpoint_resumes_without_regeneration(tmp_path, monkeypatch):
    path = tmp_path / "memory.db"
    memory = ShortMemory(path)
    add_messages(memory, 1, 100)
    old_backlog(path)
    memory = ShortMemory(path)
    provider = FixtureProvider([response([item()]), response([second_item()])])
    gateway = Gateway(provider, daily_token_limit=1000000)
    assert (await memory.summarize_pending("room", gateway))["status"] == "progress"
    validate = memory._validate_items

    def interruption(value, messages):
        if len(value["items"]) == 2:
            raise OSError("synthetic crash before final draft persistence")
        return validate(value, messages)

    monkeypatch.setattr(memory, "_validate_items", interruption)
    assert (await memory.summarize_pending("room", gateway))["status"] == "failed"
    memory = ShortMemory(path)
    memory.retry_pending("room")
    assert (await memory.summarize_pending("room", gateway))["status"] == "summarized"
    assert len(provider.requests) == 2


async def test_invalid_candidate_is_quarantined_not_rebound_or_silently_lost(tmp_path):
    path = tmp_path / "memory.db"
    memory = ShortMemory(path)
    add_messages(memory, 1, 100)
    bad = item(subject_id="untrusted-account")
    provider = FixtureProvider([response([item(), bad])])
    result = await memory.summarize_pending("room", Gateway(provider))
    assert result["status"] == "summarized" and result["item_count"] == 1
    saved = memory.summaries("room")[0]
    assert saved["items"] == [item()]
    assert saved["candidate_coverage"]["status"] == "partial_candidates_quarantined"
    assert memory.rejection_diagnostics("room") == [
        {"scope": "room", "window_end": 100, "code": "unknown_member_id", "count": 1}]
    with closing(sqlite3.connect(path)) as db, db:
        candidate, sources = db.execute("SELECT payload,source_payload FROM listening_rejections").fetchone()
    assert json.loads(candidate) == bad
    assert len(json.loads(sources)) == 100
    assert "untrusted-account" not in json.dumps(memory.rejection_diagnostics())


async def test_all_rejected_is_bounded_and_pause_survives_restart(tmp_path):
    path = tmp_path / "memory.db"
    memory = ShortMemory(path)
    add_messages(memory, 1, 100)
    provider = FixtureProvider([response([item(subject_id="invented")]) for _ in range(MAX_REPAIR_FAILURES)])
    gateway = Gateway(provider, daily_token_limit=1000000)
    for _ in range(MAX_REPAIR_FAILURES):
        ready(path)
        result = await memory.summarize_pending("room", gateway)
        assert result["status"] == "failed"
    assert result["needs_attention"] is True
    memory = ShortMemory(path)
    ready(path)  # Merely passing time does not reset an exhausted repair budget.
    assert memory.pending_scopes() == []
    assert memory.pending_scopes(ready_only=False) == ["room"]
    assert memory.stats("room")["pending_summaries"] == 1
    assert memory.pending_diagnostics()[0]["needs_attention"] is True
    assert not memory.summaries("room")
    assert (await memory.summarize_pending("room", gateway))["status"] == "deferred"
    assert len(provider.requests) == MAX_REPAIR_FAILURES
    assert memory.retry_pending("room")["windows"] == 1
    assert memory.pending_scopes() == ["room"]


async def test_budget_deferral_does_not_consume_repair_failures(tmp_path):
    memory = ShortMemory(tmp_path / "memory.db")
    add_messages(memory, 1, 100)
    provider = FixtureProvider([response([item()])])
    gateway = Gateway(provider, daily_token_limit=1)
    for _ in range(5):
        ready(memory.path)
        result = await memory.summarize_pending("room", gateway)
        assert result["diagnostic"]["code"] == "budget_daily_limit"
        assert result["needs_attention"] is False
    assert memory.pending_diagnostics()[0]["repair"]["failed_attempts"] == 0
    assert not provider.requests


async def test_last_split_segment_keeps_lease_during_support_review(tmp_path):
    path = tmp_path / "memory.db"
    first = ShortMemory(path)
    add_messages(first, 1, 100)
    old_backlog(path)
    first = ShortMemory(path)
    second = ShortMemory(path)
    provider = FixtureProvider([response([item()]), response([second_item()])])
    gateway = Gateway(provider, daily_token_limit=1000000)
    assert (await first.summarize_pending("room", gateway))["status"] == "progress"
    entered, release = asyncio.Event(), asyncio.Event()

    async def review(summary, messages, gateway):
        entered.set()
        await release.wait()
        return {**summary, "review": {"status": "synthetic"}}

    task = asyncio.create_task(first.summarize_pending("room", gateway, reviewer=review))
    await entered.wait()
    assert (await second.summarize_pending("room", gateway))["status"] == "deferred"
    release.set()
    assert (await task)["status"] == "summarized"
    assert len(provider.requests) == 2


def distinct_items(start, count):
    return [item(text=f"第{number}条消息中成员自述喜欢红茶", source_message_ids=[f"m{number}"],
                 source_quotes=[{"message_id": f"m{number}", "quote": "我喜欢红茶"}])
            for number in range(start, start + count)]


async def test_segment_with_sixteen_valid_items_uses_shared_window_budget(tmp_path):
    path = tmp_path / "memory.db"
    memory = ShortMemory(path)
    add_messages(memory, 1, 100)
    old_backlog(path)
    memory = ShortMemory(path)
    first, second = distinct_items(1, 16), distinct_items(51, 1)
    provider = FixtureProvider([response(first), response(second)])
    gateway = Gateway(provider, daily_token_limit=1000000)
    assert (await memory.summarize_pending("room", gateway))["status"] == "progress"
    assert (await memory.summarize_pending("room", gateway))["status"] == "summarized"
    assert memory.summaries("room")[0]["items"] == first + second
    assert not memory.rejection_diagnostics("room")
    assert provider.requests[0].output_schema["properties"]["items"]["maxItems"] == 100


async def test_excess_valid_candidates_are_durably_quarantined_after_both_segments(tmp_path):
    path = tmp_path / "memory.db"
    memory = ShortMemory(path)
    add_messages(memory, 1, 100)
    old_backlog(path)
    memory = ShortMemory(path)
    first, second = distinct_items(1, 20), distinct_items(51, 20)
    provider = FixtureProvider([response(first), response(second)])
    gateway = Gateway(provider, daily_token_limit=1000000)
    assert (await memory.summarize_pending("room", gateway))["status"] == "progress"
    assert memory.rejection_diagnostics() == []  # No premature 15-item cutoff.
    memory = ShortMemory(path)
    result = await memory.summarize_pending("room", gateway)
    assert result["status"] == "summarized" and result["item_count"] == 30
    saved = memory.summaries("room")[0]
    assert saved["items"] == first[:15] + second[:15]
    assert saved["candidate_coverage"]["quarantined_items"] == 10
    assert memory.rejection_diagnostics("room") == [
        {"scope": "room", "window_end": 100, "code": "item_budget", "count": 10}]
    with closing(sqlite3.connect(path)) as db:
        recovered = [json.loads(row[0]) for row in db.execute("SELECT payload FROM listening_rejections")]
    assert {i["source_message_ids"][0] for i in recovered} == {
        i["source_message_ids"][0] for i in first[15:] + second[15:]}
    assert len(provider.requests) == 2

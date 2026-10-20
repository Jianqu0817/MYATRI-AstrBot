import json
import sqlite3

from myatri.contracts import Record
from myatri.providers import Gateway, FixtureProvider
from myatri_bridge.claims import claim_projection
from myatri_bridge.memory_review import review_summary
from myatri_bridge.short_memory import ShortMemory
from myatri_bridge.reply_guard import ReplyGuard
from test_short_memory import add_messages, item


def review(verdict="supported", refs=None):
    return {"content": json.dumps({"checks": [{"index": 0, "verdict": verdict, "reason": "逐字原话核对",
                                                "source_message_ids": refs or ["m1"]}]}, ensure_ascii=False)}


async def test_semantic_rejection_does_not_become_longterm_fact(tmp_path):
    memory = ShortMemory(tmp_path / "memory.db")
    add_messages(memory, 1, 100, text="我不喜欢红茶")
    value = item(text="甲喜欢红茶", source_quotes=[{"message_id": "m1", "quote": "我不喜欢红茶"}])
    provider = FixtureProvider([{"content": json.dumps({"items": [value]}, ensure_ascii=False)}, review("contradicted")])
    archived = []
    result = await memory.summarize_pending("room", Gateway(provider),
        on_summary=lambda summary, sources: archived.append(summary), reviewer=review_summary)
    assert result["status"] == "summarized"
    assert archived[0]["items"] == []
    assert archived[0]["review"]["rejected_items"][0]["item"]["text"] == "甲喜欢红茶"
    assert len(provider.requests) == 2


async def test_review_retry_reuses_extraction_and_shared_budget(tmp_path):
    memory = ShortMemory(tmp_path / "memory.db")
    add_messages(memory, 1, 100)
    provider = FixtureProvider([{"content": json.dumps({"items": [item()]}, ensure_ascii=False)},
                                {"content": '{"checks":[]}'}, review()])
    gateway = Gateway(provider)
    first = await memory.summarize_pending("room", gateway, reviewer=review_summary)
    assert first["status"] == "failed" and memory.stats("room")["pending_summaries"] == 1
    with sqlite3.connect(memory.path) as db:
        assert json.loads(db.execute("SELECT result FROM listening_pending").fetchone()[0])["items"]
        db.execute("UPDATE listening_pending SET next_attempt=0")
    second = await memory.summarize_pending("room", gateway, reviewer=review_summary)
    assert second["status"] == "summarized"
    assert [request.purpose for request in provider.requests] == ["group_short_memory", "group_memory_support_review", "group_memory_support_review"]
    assert gateway.used_tokens > 0


def claim(identifier, subject, facet, text, refs, *, change="assertion", replaces=None, observed="2026-10-21T01:00:00+00:00"):
    return Record(record_id=identifier, record_kind="interpretation", source_refs=["raw@1"], body_markdown=text,
        payload={"group_attribution": {"subject_id": subject, "attribution": "self_report", "source_message_ids": refs},
                 "claim_state": {"facet": facet, "change": change, "supersedes_message_ids": replaces or [], "observed_at": [observed]}})


def test_projection_changes_only_same_subject_and_facet():
    old = claim("old", "a", "饮品", "以前喝红茶", ["m1"])
    corrected = claim("new", "a", "饮品", "现在不喝了", ["m2"], change="correction", replaces=["m1"], observed="2026-10-21T02:00:00+00:00")
    unrelated = claim("other", "a", "运动", "喜欢游泳", ["m3"], change="correction", replaces=["m1"])
    impostor = claim("fake", "b", "饮品", "别人的转述", ["m4"], change="retraction", replaces=["m2"])
    result = claim_projection([corrected, unrelated, old, impostor], "a")
    assert next(row for row in result["history"] if row["ref"] == old.ref)["status"] == "superseded"
    assert {row["ref"] for row in result["active"]} == {corrected.ref, unrelated.ref}
    assert all(row["ref"] != impostor.ref for row in result["history"])


def test_projection_does_not_choose_between_unlinked_conflicts():
    a = claim("a", "member", "饮品", "喝红茶", ["m1"])
    b = claim("b", "member", "饮品", "喝咖啡", ["m2"])
    result = claim_projection([a, b], "member")
    assert len(result["active"]) == 2
    assert all(row["unresolved_peer_refs"] for row in result["active"])


async def test_reply_guard_is_selective_and_sanitizes_ids(tmp_path):
    from myatri.storage import ArchiveStore
    store = ArchiveStore(tmp_path)
    raw = store.append_message("user", "普通聊天", "raw")
    provider = FixtureProvider([{"content": json.dumps({"reply": "这段我先核对原话，免得记混了。", "reason": "记忆没有充分来源"}, ensure_ascii=False)}])
    guard = ReplyGuard(Gateway(provider), store, lambda: set())
    assert await guard("今天阳光很好。", raw, []) == "今天阳光很好。"
    assert not provider.requests
    assert "wxid_" not in await guard("你以前说过你是wxid_test。", raw, [])
    assert len(provider.requests) == 1
    assert guard.last_check["status"] == "reviewed"


async def test_summary_update_rejects_fabricated_old_source(tmp_path):
    memory = ShortMemory(tmp_path / "memory.db")
    add_messages(memory, 1, 100)
    value = item(facet="饮品", change="correction", supersedes_message_ids=["invented"])
    provider = FixtureProvider([{"content": json.dumps({"items": [value]}, ensure_ascii=False)}])
    result = await memory.summarize_pending("room", Gateway(provider))
    assert result["status"] == "failed"
    assert not memory.summaries("room")


async def test_context_releases_database_handle_after_close(bridge, message):
    await bridge.receive(message)
    await bridge.close()
    moved = bridge.path.with_suffix(".moved")
    bridge.path.rename(moved)
    moved.rename(bridge.path)


async def test_diagnostics_authenticated_and_contains_counts(bridge, message):
    from aiohttp.test_utils import TestClient, TestServer
    from myatri_bridge.server import create_app
    await bridge.receive(message)
    async with TestClient(TestServer(create_app(bridge, "test-secret-" + "x"*40))) as client:
        assert (await client.get("/v1/diagnostics")).status == 401
        response = await client.get("/v1/diagnostics", headers={"Authorization": "Bearer test-secret-"+"x"*40})
        report = await response.json()
        assert report["deliveries"]["prepared"] == 1
        assert report["groups"][0]["listening"]["total_messages"] == 1
        assert "api_key" not in json.dumps(report)

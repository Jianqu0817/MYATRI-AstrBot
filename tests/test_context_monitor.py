import json
import sqlite3

from aiohttp.test_utils import TestClient, TestServer

from myatri_bridge.monitor import create_monitor_app, read_library, snapshot
from myatri_bridge.monitor import _context_view


def library(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    with sqlite3.connect(root / "runtime.sqlite3") as db:
        db.executescript("CREATE TABLE usage(id INTEGER PRIMARY KEY,payload TEXT);"
                         "CREATE TABLE protocols(payload TEXT);")
    return root


def add_usage(root, prompt, **extra):
    payload = {"purpose": "conversation", "role": "foreground", "usage": {
        "prompt_tokens": prompt, "completion_tokens": 27, "prompt_cache_hit_tokens": 6000},
        "model": "fixture", "elapsed_seconds": 1.5, "status": "received", **extra}
    with sqlite3.connect(root / "runtime.sqlite3") as db:
        db.execute("INSERT INTO usage(payload) VALUES(?)", (json.dumps(payload),))
        db.execute("INSERT INTO protocols VALUES(?)", (json.dumps({"status": "completed",
            "replied_at": "2026-10-21T01:00:00+08:00", "steps": 1, "messages": [{"content": "SECRET CHAT"}]}),))


def test_actual_context_is_latest_request_not_daily_sum_or_uncached_only(tmp_path):
    root = library(tmp_path)
    add_usage(root, 10000)
    add_usage(root, 18647, thinking_enabled=False, finish_reason="stop", api_key="NEVER EXPOSE",
              reasoning_content="PRIVATE REASONING", messages=[{"content": "SECRET CHAT"}])
    result = read_library(root, "测试", {"context": {"limit": 1000000, "compact_at": 650000}}, key="test")
    assert result["latest_conversation"]["prompt_tokens"] == 18647
    assert result["latest_conversation"]["cached_input_tokens"] == 6000
    assert result["context_usage_percent"] == 1.86
    assert result["compaction_usage_percent"] == 2.87
    assert result["latest_conversation"]["thinking_enabled"] is False
    assert result["latest_conversation"]["reasoning_tokens"] is None
    encoded = json.dumps(result)
    assert not any(secret in encoded for secret in ("NEVER EXPOSE", "SECRET CHAT", "PRIVATE REASONING"))


def test_old_logs_keep_unknown_thinking_and_reasoning_instead_of_invented_zero(tmp_path):
    root = library(tmp_path)
    add_usage(root, 0)
    result = read_library(root, "测试", {}, key="test")
    assert result["latest_conversation"]["thinking_enabled"] is None
    assert result["latest_conversation"]["prompt_tokens"] == 0
    assert result["latest_conversation"]["recorded_at"] is None
    assert result["configured_context_limit"] is None


def test_monitor_read_does_not_write_or_create_runtime_database(tmp_path):
    root = library(tmp_path)
    add_usage(root, 42)
    db_path = root / "runtime.sqlite3"
    before = db_path.read_bytes()
    read_library(root, "测试", {}, key="test")
    assert before == db_path.read_bytes()
    missing = tmp_path / "missing"
    assert read_library(missing, "测试", {}, key="missing")["status"] == "no_data"
    assert not missing.exists()


async def test_read_only_http_and_cross_origin_protection(tmp_path):
    async with TestClient(TestServer(create_monitor_app(tmp_path))) as client:
        page = await client.get("/")
        assert page.status == 200 and "上下文监视" in await page.text()
        result = await client.get("/api/metrics")
        assert result.status == 200
        assert (await result.json())["libraries"] == []
        assert result.headers["Cache-Control"] == "no-store"
        assert (await client.post("/api/metrics")).status == 405
        assert (await client.get("/api/metrics", headers={"Origin": "https://example.com"})).status == 403
        assert (await client.get("/api/metrics", headers={"Host": "evil.example"})).status == 403


def test_snapshot_has_no_side_effects_in_empty_project(tmp_path):
    assert snapshot(tmp_path)["libraries"] == []
    assert list(tmp_path.iterdir()) == []


def test_reasoning_count_uses_provider_field_only(tmp_path):
    root = library(tmp_path)
    add_usage(root, 500, thinking_enabled=True, usage={"prompt_tokens": 500,
        "completion_tokens": 120, "completion_tokens_details": {"reasoning_tokens": 90}})
    result = read_library(root, "测试", {}, key="test")
    assert result["latest_conversation"]["reasoning_tokens"] == 90


def test_configured_thinking_and_actual_effort_keep_unknown_old_rows(tmp_path):
    root = library(tmp_path)
    add_usage(root, 500, thinking_enabled=True, reasoning_effort="high")
    result = read_library(root, "测试", {"chat": {"thinking": True}}, key="test")
    assert result["default_thinking_enabled"] is True
    assert result["latest_conversation"]["reasoning_effort"] == "high"
    add_usage(root, 500, reasoning_effort=["malformed"])
    result = read_library(root, "测试", {}, key="test")
    assert result["latest_conversation"]["reasoning_effort"] is None


def test_malformed_old_usage_is_skipped_and_busy_data_stays_unknown(tmp_path):
    root = library(tmp_path)
    with sqlite3.connect(root / "runtime.sqlite3") as db:
        db.execute("INSERT INTO usage(payload) VALUES(?)", ("bad json",))
    assert read_library(root, "测试", {}, key="test")["latest_conversation"] is None
    (root / "runtime.sqlite3").write_bytes(b"not a database")
    assert read_library(root, "测试", {}, key="test")["status"] == "temporarily_unavailable"


def test_composition_allowlist_rejects_text_ids_boolean_and_nonfinite_values(tmp_path):
    root = library(tmp_path)
    add_usage(root, 20000)
    stats = {"version": 1, "dialogue_turns": 80, "recent_group_messages": 100,
        "history_messages": True, "summary_items": float("inf"), "api_key": "SECRET KEY",
        "components": {"dialogue": {"messages": 160, "chars": 30000, "estimated_tokens": 90000,
                                      "content": "SECRET CHAT", "sender_id": "wxid_secret"},
                       "SECRET CATEGORY": {"messages": 1}},
        "omission_reasons": {"dialogue_window": 20, "token_budget": "SECRET TOKEN", "sender_id": "wxid_secret"}}
    with sqlite3.connect(root / "runtime.sqlite3") as db:
        db.execute("INSERT INTO protocols VALUES(?)", (json.dumps({"status": "completed",
            "replied_at": "2026-10-21T12:00:00+08:00", "context_stats": stats}),))
    view = read_library(root, "测试", {}, key="test")["context_composition"]
    assert view["dialogue_turns"] == 80 and view["recent_group_messages"] == 100
    assert view["history_messages"] is None and view["summary_items"] is None
    assert view["components"] == {"dialogue": {"messages": 160, "chars": 30000, "estimated_tokens": 90000}}
    assert view["omission_reasons"] == {"dialogue_window": 20, "token_budget": None}
    encoded = json.dumps(view, allow_nan=False)
    assert not any(secret in encoded for secret in ("SECRET", "wxid_secret", "sender_id", "api_key"))
    assert _context_view({"version": True}) is None


def test_malformed_nested_usage_and_nondict_protocol_do_not_crash_monitor(tmp_path):
    root = library(tmp_path)
    add_usage(root, 100, usage={"prompt_tokens": 100, "completion_tokens_details": [], "prompt_tokens_details": "bad"})
    with sqlite3.connect(root / "runtime.sqlite3") as db:
        db.execute("INSERT INTO protocols VALUES(?)", ("[]",))
    result = read_library(root, "测试", {}, key="test")
    assert result["status"] == "ok"
    assert result["latest_conversation"]["prompt_tokens"] == 100

from datetime import datetime, timezone
import json
import sqlite3

import pytest

from myatri_bridge.monitor import create_monitor_app, snapshot
from myatri_bridge.monitor_stats import daily_usage, job_state, memory_state, merge_daily
from aiohttp.test_utils import TestClient, TestServer

NOW = datetime(2026, 10, 22, 2, tzinfo=timezone.utc)


def usage_db(tmp_path, rows):
    path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE usage(id INTEGER PRIMARY KEY,payload TEXT)")
        db.executemany("INSERT INTO usage(payload) VALUES(?)", [(json.dumps(row),) for row in rows])
    return path


def usage(stamp="2026-10-22T01:00:00+00:00", **extra):
    return {"recorded_at": stamp, "status": "received", "purpose": "conversation",
        "usage": {"prompt_tokens": 100, "completion_tokens": 20,
            "prompt_cache_hit_tokens": 80, "completion_tokens_details": {"reasoning_tokens": 15}},
        "messages": [{"content": "SECRET CHAT"}], "trace_id": "SECRET ID", **extra}


def test_daily_beijing_boundary_offsets_all_attempts_and_no_double_count(tmp_path):
    path = usage_db(tmp_path, [usage("2026-10-21T15:59:59+00:00"),
        usage("2026-10-21T16:00:00+00:00"), usage("2026-10-23T00:00:00+08:00"),
        usage("2026-10-22T10:00:00+08:00", purpose="emotion_batch"),
        usage(status="failed_unknown", usage={}, charged_tokens=10000),
        usage(usage={"prompt_tokens": True, "completion_tokens": 20}),
        usage("2026-10-22T01:00:00"), usage(None)])
    result = daily_usage(path, NOW)
    assert result["date"] == "2026-10-22" and result["requests"] == 4
    assert result["total_tokens"] == 240  # Cache and reasoning are already included.
    assert result["prompt_tokens"] == 200 and result["completion_tokens"] == 40
    assert result["reasoning_tokens"] == 30 and result["cached_input_tokens"] == 160
    assert result["unknown_usage_requests"] == 2 and result["failed_requests"] == 1
    assert result["undated_rows"] == 2
    assert result["hourly"][0]["tokens"] == 120 and result["hourly"][10]["tokens"] == 120
    assert set(result["by_purpose"]) == {"conversation", "emotion_batch"}
    assert "SECRET" not in json.dumps(result)


def test_daily_is_not_truncated_at_recent_2000_rows_and_does_not_write(tmp_path):
    path = usage_db(tmp_path, [usage() for _ in range(2100)])
    before = path.read_bytes()
    result = daily_usage(path, NOW)
    assert result["total_tokens"] == 252000 and result["requests"] == 2100
    assert path.read_bytes() == before


def test_unknown_tokens_are_not_fabricated_and_partial_library_is_visible(tmp_path):
    path = usage_db(tmp_path, [usage(usage={"total_tokens": 1234})])
    unknown = daily_usage(path, NOW)
    assert unknown["total_tokens"] is None and unknown["unknown_usage_requests"] == 1
    missing = daily_usage(tmp_path / "missing.sqlite3", NOW)
    merged = merge_daily([unknown, missing], NOW)
    assert merged["status"] == "partial" and merged["libraries_available"] == 1
    assert merged["total_tokens"] is None and merged["unknown_usage_requests"] == 1
    assert not (tmp_path / "missing.sqlite3").exists()


def projection(tmp_path, records):
    domain = tmp_path / "domains" / "fixture"
    domain.mkdir(parents=True)
    commits = domain / "commits"
    commits.mkdir()
    (commits / "00000000000000000001-first.json").write_text("{}")
    path = domain / "projection.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE metadata(key TEXT,value TEXT);"
            "INSERT INTO metadata VALUES('archive_watermark','first');"
            "CREATE TABLE records(ref TEXT,record_id TEXT,revision INTEGER,kind TEXT,created_at TEXT,body TEXT,json TEXT);")
        for rid, rev, kind, payload in records:
            db.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?)", (f"{rid}@{rev}", rid, rev, kind,
                "2026-10-22T01:00:00+00:00", "SECRET BODY", json.dumps({"payload": payload})))
    return domain, path


def appraisal():
    return {"target": {"valence": -.8, "arousal": .8, "dominance": .1},
        "confidence": 1.0, "label": "SECRET LABEL", "meaning": "SECRET MEANING",
        "event_ref": "event@1", "persona_ref": "persona@1", "observed_at": NOW.isoformat(),
        "source_spans": [{"message_ref": "message@1", "start": 0, "end": 6, "quote": "SECRET"}],
        "evidence_refs": ["event@1"]}


def test_memory_latest_revisions_and_emotion_reuse_signed_pad_decay_and_retraction(tmp_path):
    domain, path = projection(tmp_path, [("a", 1, "emotion_appraisal", appraisal()),
        ("temp", 1, "temporary_state", {"kind_hint": "emotional_echo", "status": "open"}),
        ("temp", 2, "temporary_state", {"kind_hint": "emotional_echo", "status": "discarded"}),
        ("message", 1, "raw_message", {"text": "SECRET"})])
    before = path.read_bytes()
    first = memory_state(tmp_path, {}, NOW)
    assert first["status"] == "ok" and first["record_count"] == 3
    assert first["counts"]["temporary_state"] == 1 and first["projection_current"] is True
    assert first["index_present"] is False
    e = first["emotion"]
    assert e["status"] == "ok" and e["affect"]["valence"] == pytest.approx(-.52)
    assert e["mood"]["valence"] == pytest.approx(-.08) and e["open_concerns"] == 0
    later = memory_state(tmp_path, {}, datetime(2026, 10, 22, 2, 30, tzinfo=timezone.utc))
    assert later["emotion"]["affect"]["valence"] == pytest.approx(-.26)
    assert path.read_bytes() == before and "SECRET" not in json.dumps(first)
    (domain / "commits/00000000000000000002-next.json").write_text("{}")
    (domain / "indexes").mkdir()
    (domain / "indexes/current.json").write_text(json.dumps({"generation": "old", "watermark": "first"}))
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?)", ("r@1", "r", 1, "emotion_retraction",
            NOW.isoformat(), "SECRET", json.dumps({"payload": {"appraisal_ref": "a@1", "reason": "SECRET"}})))
    after = memory_state(tmp_path, {}, NOW)
    assert after["projection_current"] is False and after["index_current"] is False
    assert after["emotion"]["status"] == "no_appraisals"
    assert after["emotion"]["retracted_count"] == 1 and after["emotion"]["affect"]["valence"] == 0


def test_invalid_emotion_does_not_hide_valid_memory_counts(tmp_path):
    projection(tmp_path, [("bad", 1, "emotion_appraisal", {"target": "bad"})])
    result = memory_state(tmp_path, {}, NOW)
    assert result["status"] == "ok" and result["counts"]["emotion_appraisal"] == 1
    assert result["emotion"]["status"] == "temporarily_unavailable"


def test_missing_and_corrupt_subsystems_are_not_reported_healthy(tmp_path):
    assert memory_state(tmp_path, {}, NOW)["status"] == "no_data"
    assert job_state(tmp_path / "missing")["status"] == "no_data"
    path = tmp_path / "runtime.sqlite3"
    path.write_bytes(b"broken")
    assert daily_usage(path, NOW)["status"] == "temporarily_unavailable"
    assert job_state(path)["status"] == "temporarily_unavailable"


def test_jobs_whitelist_and_astrbot_scope_do_not_merge_adjacent_cli(tmp_path):
    bridge = tmp_path / "MYATRI-AstrBot"
    bridge.mkdir()
    core = tmp_path / "MYATRI/var/xiaowennuan"
    core.mkdir(parents=True)
    usage_db(core, [usage()])
    assert snapshot(bridge, now=NOW)["libraries"] == []
    path = bridge / "runtime.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE jobs(role TEXT,status TEXT,payload TEXT)")
        db.executemany("INSERT INTO jobs VALUES(?,?,?)", [("maintenance", s, "SECRET")
            for s in ("pending", "running", "succeeded", "retry_wait", "failed", "SECRET STATUS")])
    view = job_state(path)
    assert view["counts"]["succeeded"] == 1 and view["counts"]["retry_wait"] == 1
    assert "SECRET" not in json.dumps(view)


def test_expired_maintenance_lease_is_distinct_from_running_status(tmp_path):
    path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE jobs(role TEXT,status TEXT,lease_until REAL)")
        db.executemany("INSERT INTO jobs VALUES('maintenance','running',?)",
            [(NOW.timestamp()-1,), (NOW.timestamp()+60,), (None,)])
    result = job_state(path, NOW)
    assert result["counts"]["running"] == 3 and result["expired_leases"] == 2


async def test_api_exports_new_metrics_and_keeps_read_only_cross_site_guard(tmp_path):
    group = tmp_path / "var/groups/private-route"
    group.mkdir(parents=True)
    usage_db(group, [usage(datetime.now(timezone.utc).isoformat())])
    projection(group, [])
    async with TestClient(TestServer(create_monitor_app(tmp_path))) as client:
        response = await client.get("/api/metrics")
        result = await response.json()
        assert result["daily_usage"]["total_tokens"] == 120
        assert result["libraries"][0]["memory"]["emotion"]["status"] == "no_appraisals"
        assert "private-route" not in json.dumps(result) and "SECRET" not in json.dumps(result)
        assert (await client.get("/api/metrics", headers={"Sec-Fetch-Site": "cross-site"})).status == 403
        assert (await client.post("/api/metrics")).status == 405

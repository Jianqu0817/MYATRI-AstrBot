import json
import sqlite3

import pytest
from aiohttp.test_utils import TestClient, TestServer

from myatri_bridge.health import health_snapshot
from myatri_bridge.server import create_app


def test_health_alerts_are_read_only_and_do_not_expose_diagnostic_payloads(tmp_path):
    with sqlite3.connect(tmp_path / "short-memory.sqlite3") as db:
        db.executescript("CREATE TABLE listening_summaries(id); CREATE TABLE listening_pending(attempts,needs_attention);"
            "CREATE TABLE listening_rejections(payload);")
        db.executemany("INSERT INTO listening_pending VALUES(?,?)", [(3, 1), (1, 0)])
        db.execute("INSERT INTO listening_rejections VALUES('SECRET CHAT')")
    with sqlite3.connect(tmp_path / "deliveries.sqlite3") as db:
        db.executescript("CREATE TABLE deliveries(id,status,received_at,updated); CREATE TABLE seen_messages(id);"
            "CREATE TABLE delivery_events(reason_code,status,created);")
        db.executemany("INSERT INTO deliveries VALUES(?,?,?,?)",
            [(i, "unknown" if i < 6 else "sent", 1000, 1000) for i in range(20)])
        db.execute("INSERT INTO delivery_events VALUES('SECRET CHAT','unknown',1000)")
    group = tmp_path / "group"
    group.mkdir()
    with sqlite3.connect(group / "runtime.sqlite3") as db:
        db.executescript("CREATE TABLE jobs(role,status); CREATE TABLE schedule(name,value);")
        db.execute("INSERT INTO jobs VALUES('maintenance','failed')")
        db.execute("INSERT INTO schedule VALUES(?,?)", ("maintenance_deferred_batches", json.dumps({
            "PRIVATE ID": {"status": "deferred", "input_refs": ["SECRET CHAT"]}})))
        db.execute("INSERT INTO schedule VALUES(?,?)", ("a1_quarantine_counts", json.dumps({"PRIVATE ID": 2})))
    before = {path: path.read_bytes() for path in tmp_path.rglob("*.sqlite3")}
    data = health_snapshot(tmp_path, now=1001)
    assert {item["code"] for item in data["alerts"]} == {
        "summary_no_success", "summary_repeated_failure", "maintenance_failed",
        "maintenance_deferred", "delivery_unknown_rate", "summary_needs_attention",
        "summary_candidates_quarantined", "a1_candidates_quarantined"}
    assert data["maintenance"]["unconfirmed_candidates"] == 2
    assert data["delivery"]["unknown_percent"] == 30
    assert data["delivery"]["unknown_reasons"] == {"unclassified": 1}
    assert "SECRET" not in json.dumps(data) and "PRIVATE" not in json.dumps(data)
    assert all(path.read_bytes() == content for path, content in before.items())


def test_no_alert_for_small_unknown_sample_or_expired_deliveries(tmp_path):
    with sqlite3.connect(tmp_path / "deliveries.sqlite3") as db:
        db.executescript("CREATE TABLE deliveries(id,status,received_at,updated); CREATE TABLE seen_messages(id);")
        db.execute("INSERT INTO deliveries VALUES(1,'unknown',1000,1000)")
    assert not health_snapshot(tmp_path, now=1001)["alerts"]
    assert health_snapshot(tmp_path, now=100000)["delivery"]["terminal_count"] == 0


async def test_acks_record_safe_timing_once_and_legacy_terminal_replay_repairs_seen(bridge, message):
    result = await bridge.receive(message)
    await bridge.claim(result["id"])
    await bridge.acknowledge(result["id"], "unknown", reason_code="receipt_timeout", elapsed_seconds=18.3)
    await bridge.acknowledge(result["id"], "unknown", reason_code="receipt_timeout", elapsed_seconds=18.3)
    with sqlite3.connect(bridge.path) as db:
        assert db.execute("SELECT status,reason_code,elapsed_seconds FROM delivery_events").fetchall() == [
            ("unknown", "receipt_timeout", 18.3)]
        db.execute("DELETE FROM seen_messages")
    requests = len(bridge.test_providers[0].requests)
    assert (await bridge.receive(message))["status"] == "unknown"
    assert len(bridge.test_providers[0].requests) == requests
    with sqlite3.connect(bridge.path) as db:
        assert db.execute("SELECT COUNT(*) FROM seen_messages").fetchone()[0] == 1


async def test_ack_http_rejects_free_text_and_nonfinite_timing(bridge, message):
    result = await bridge.receive(message)
    await bridge.claim(result["id"])
    async with TestClient(TestServer(create_app(bridge, "x" * 40))) as client:
        for extra in ({"reason_code": "PRIVATE exception"}, {"elapsed_seconds": -1},
                      {"elapsed_seconds": float("inf")}):
            response = await client.post(f"/v1/deliveries/{result['id']}/ack",
                headers={"Authorization": "Bearer " + "x" * 40}, json={"status": "unknown", **extra})
            assert response.status == 400
    with sqlite3.connect(bridge.path) as db:
        assert db.execute("SELECT COUNT(*) FROM delivery_events").fetchone()[0] == 0


@pytest.mark.parametrize("reason,elapsed", [("raw error", None), (None, float("nan")), (None, True)])
async def test_direct_ack_validates_diagnostic_metadata(bridge, reason, elapsed):
    with pytest.raises(ValueError):
        await bridge.acknowledge("missing", "unknown", reason_code=reason, elapsed_seconds=elapsed)


async def test_retry_endpoint_releases_pause_without_running_model_and_rejects_busy(bridge, message, monkeypatch):
    import asyncio
    from test_short_memory import add_messages
    await bridge.receive(message)
    add_messages(bridge.short_memory, 2, 100, scope=message.scope)
    with sqlite3.connect(bridge.short_memory.path) as db:
        db.execute("UPDATE listening_pending SET needs_attention=1,next_attempt=9999999999")
    monkeypatch.setattr(bridge, "start", lambda: None)
    count = len(bridge.test_providers[0].requests)
    async with TestClient(TestServer(create_app(bridge, "x" * 40))) as client:
        headers = {"Authorization": "Bearer " + "x" * 40}
        response = await client.post("/v1/admin/retry-summaries", json={"scope": message.scope})
        assert response.status == 401
        lock = bridge.short_memory._locks.setdefault(message.scope, asyncio.Lock())
        async with lock:
            response = await client.post("/v1/admin/retry-summaries", headers=headers, json={"scope": message.scope})
            assert response.status == 409
        response = await client.post("/v1/admin/retry-summaries", headers=headers, json={"scope": message.scope})
        assert response.status == 200 and (await response.json())["windows"] == 1
        assert len(bridge.test_providers[0].requests) == count
        pending = bridge.short_memory.pending_diagnostics(message.scope)[0]
        assert not pending["needs_attention"] and pending["next_attempt"] == 0
        assert pending["repair"]["split"]

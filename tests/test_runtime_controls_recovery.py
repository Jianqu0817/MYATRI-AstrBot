import asyncio
from contextlib import closing
import json
import sqlite3
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer
import pytest

from myatri.contracts import JobSpec
from myatri.runtime.jobs import JobQueue
from myatri_bridge.monitor import create_monitor_app
from myatri_bridge.recovery import AutoRecovery
from myatri_bridge.runtime_controls import RuntimeSettings, SettingsConflict
from myatri_bridge.server import create_app


def test_settings_are_atomic_validated_and_persist_across_restart(tmp_path):
    state = RuntimeSettings(tmp_path / "controls.sqlite3", .1)
    assert state.read()["random_reply_probability"] == .1
    state.update(0, .333, False)
    assert RuntimeSettings(state.path, .9).read() == {"revision": 1, "random_reply_probability": .333,
        "recovery_enabled": False}
    with pytest.raises(SettingsConflict):
        state.update(0, .9, True)
    for value in (-.1, 1.1, float("nan"), True, "0.1"):
        with pytest.raises(ValueError):
            state.update(1, value, True)
    assert state.read()["revision"] == 1


async def test_runtime_settings_auth_validation_conflict_and_immediate_effect(bridge, message, monkeypatch):
    monkeypatch.setattr(bridge, "start", lambda: None)
    token = "x" * 40
    async with TestClient(TestServer(create_app(bridge, token))) as client:
        url = "/v1/admin/runtime-settings"
        assert (await client.get(url)).status == 401
        headers = {"Authorization": "Bearer " + token}
        assert (await (await client.get(url, headers=headers)).json())["random_reply_probability"] == 0
        payload = {"revision": 0, "random_reply_probability": 1.0, "recovery_enabled": False}
        response = await client.post(url, headers=headers, json=payload)
        assert response.status == 200 and bridge.random_probability == 1
        assert bridge.recovery_enabled is False
        assert (await client.post(url, headers=headers, json=payload)).status == 409
        for value in (-1, float("inf"), True, "0.5"):
            assert (await client.post(url, headers=headers, json={**payload, "revision": 1,
                "random_reply_probability": value})).status == 400
        # An ordinary new message uses the new probability without a service restart.
        result = await bridge.receive(message.model_copy(update={"mentioned": False, "text": "随机输入"}))
        assert result["status"] == "prepared"


async def test_monitor_control_cookie_origin_and_no_token_exposure(tmp_path, monkeypatch):
    calls = []
    async def proxy(project, payload=None):
        calls.append(payload)
        return {"ok": True, "revision": 1, "random_reply_probability": .5, "recovery_enabled": True}, 200
    monkeypatch.setattr("myatri_bridge.monitor.runtime_settings_proxy", proxy)
    async with TestClient(TestServer(create_monitor_app(tmp_path))) as client:
        assert (await client.get('/api/runtime-settings')).status == 403
        await client.get('/')
        origin = str(client.make_url('/')).rstrip('/')
        payload = {"revision": 0, "random_reply_probability": .5, "recovery_enabled": True}
        assert (await client.post('/api/runtime-settings', json=payload)).status == 403
        headers = {"Origin": origin, "X-Monitor-Control": "1"}
        assert (await client.post('/api/runtime-settings', headers=headers, json={**payload, "extra": True})).status == 400
        assert (await client.post('/api/runtime-settings', headers=headers, json=payload)).status == 200
        assert calls == [payload]
        assert (await client.post('/api/metrics', headers=headers, json=payload)).status == 405


def recovery_app(tmp_path):
    queue = JobQueue(tmp_path / "runtime.sqlite3")
    return SimpleNamespace(queue=queue, conversation=SimpleNamespace(_lock=asyncio.Lock()),
        maintenance=SimpleNamespace(lock=asyncio.Lock()))


def failed_job(app, key, error="ValueError: association outside bounded evidence candidates"):
    identifier = app.queue.enqueue(JobSpec(role="maintenance", input_refs=[key+"@1"]), now=0)
    with app.queue.db() as db:
        db.execute("UPDATE jobs SET status='failed',error=?,attempts=4,available=1 WHERE id=?", (error, identifier))
    return identifier


def test_recovery_once_per_job_survives_restart_and_keeps_inputs_results_attempts(tmp_path):
    app = recovery_app(tmp_path)
    identifier = failed_job(app, "source")
    with app.queue.db() as db:
        db.execute("UPDATE jobs SET result=? WHERE id=?", (json.dumps({"kind": "saved"}), identifier))
    repair = AutoRecovery(app)
    assert repair.tick(now=1000) == "scheduled"
    row = app.queue.list_jobs()[0]
    assert row["status"] == "retry_wait" and row["attempts"] == 4 and json.loads(row["result"]) == {"kind": "saved"}
    with app.queue.db() as db:
        db.execute("UPDATE jobs SET status='failed',available=2 WHERE id=?", (identifier,))
    assert AutoRecovery(app).tick(now=2000) == "idle"
    failed_job(app, "second")
    assert repair.tick(now=1100) == "backoff"
    assert repair.tick(now=2000) == "scheduled"


async def test_recovery_does_not_touch_active_jobs_or_unknown_delivery_and_ignores_unhandled_errors(tmp_path):
    app = recovery_app(tmp_path)
    failed_job(app, "bad", "ValueError: forged semantic evidence")
    assert AutoRecovery(app).tick(now=1000) == "idle"
    failed_job(app, "eligible")
    async with app.conversation._lock:
        assert AutoRecovery(app).tick(now=1000) == "busy"
    app.queue.enqueue(JobSpec(role="maintenance", input_refs=["active@1"]), now=0)
    assert AutoRecovery(app).tick(now=1000) == "busy"
    with closing(sqlite3.connect(app.queue.path)) as db:
        assert "deliveries" not in {r[0] for r in db.execute("SELECT name FROM sqlite_master")}

"""Authenticated local controls proxy to the running bridge, never write archives."""
import json
import sqlite3

from aiohttp import CookieJar, web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from myatri_bridge.context_viewer import create_app, library_list, proxy_control


SCOPE = "a" * 32
TOKEN = "fixture-owner-control-" + "x" * 40


def project(tmp_path, port=8766):
    root = tmp_path / "bridge"
    library = root / "var/groups" / SCOPE
    (library / "domains").mkdir(parents=True)
    with sqlite3.connect(library / "runtime.sqlite3") as db:
        db.executescript("CREATE TABLE protocols(payload TEXT); CREATE TABLE usage(id INTEGER PRIMARY KEY,payload TEXT);")
    (root / "config").mkdir()
    (root / "config/bridge.local.toml").write_text(
        '[bridge]\nhost="127.0.0.1"\nport=' + str(port) + '\ntoken_file="var/bridge-token.txt"\n', encoding="utf-8")
    (root / "var/bridge-token.txt").write_text(TOKEN, encoding="ascii")
    return root, library


def headers(client):
    return {"Origin": str(client.make_url("/")).rstrip("/"), "X-Context-Control": "1"}


async def test_owner_controls_proxy_scope_with_secret_header_and_no_local_writes(tmp_path):
    calls = []

    async def backend(request):
        calls.append((request.path, request.headers.get("Authorization"), await request.json()))
        return web.json_response({"ok": True, "action": request.match_info["action"], "scope": SCOPE,
            "persona_changed": True, "context_reset": {"boundary": "saved"}, "private_token": TOKEN})

    fake = web.Application()
    fake.router.add_post("/v1/admin/{action}", backend)
    async with TestServer(fake) as bridge:
        root, library = project(tmp_path, bridge.port)
        original = (library / "runtime.sqlite3").read_bytes()
        async with TestClient(TestServer(create_app(root)), cookie_jar=CookieJar(unsafe=True)) as client:
            await client.get("/")
            listing = await (await client.get("/api/libraries")).json()
            assert listing["libraries"][0]["runtime_controls"] is True
            for action in ("reload", "clear-context"):
                response = await client.post("/api/admin/" + action, json={"library": "group-1"}, headers=headers(client))
                assert response.status == 200
                data = await response.json()
                assert data["action"] == action and data["scope"] == SCOPE
                assert TOKEN not in json.dumps(data) and "private_token" not in data
        assert calls == [("/v1/admin/" + action, "Bearer " + TOKEN, {"scope": SCOPE})
                         for action in ("reload", "clear-context")]
        assert (library / "runtime.sqlite3").read_bytes() == original


async def test_cookie_origin_custom_header_and_supported_method_are_required(tmp_path):
    root, _ = project(tmp_path)
    async with TestClient(TestServer(create_app(root)), cookie_jar=CookieJar(unsafe=True)) as client:
        data = {"library": "group-1"}
        assert (await client.post("/api/admin/reload", json=data, headers=headers(client))).status == 403
        await client.get("/")
        assert (await client.post("/api/admin/reload", json=data)).status == 403
        assert (await client.post("/api/admin/reload", json=data,
                                 headers={"Origin": headers(client)["Origin"]})).status == 403
        assert (await client.post("/api/admin/reload", json=data,
                                 headers={**headers(client), "Origin": "https://evil.test"})).status == 403
        assert (await client.post("/api/admin/reload", json=data,
                                 headers={**headers(client), "Sec-Fetch-Site": "cross-site"})).status == 403
        assert (await client.get("/api/admin/reload")).status == 405
        assert (await client.post("/api/context", json=data, headers=headers(client))).status == 405
        assert (await client.post("/api/admin/reload", json={"scope": SCOPE}, headers=headers(client))).status == 400
        assert (await client.post("/api/admin/reload", json={"library": "../../private"},
                                 headers=headers(client))).status == 404


async def test_private_chat_controls_are_disabled_and_never_write_library(tmp_path):
    root, _ = project(tmp_path)
    private = root.parent / "MYATRI/var/xiaowennuan"
    private.mkdir(parents=True)
    (private / "runtime.sqlite3").write_bytes(b"not used by controls")
    row = next(row for row in library_list(root)["libraries"] if row["key"] == "xiaowennuan")
    assert row["runtime_controls"] is False
    result, status = await proxy_control(root, "xiaowennuan", "reload")
    assert status == 400 and "微信群" in result["error"]
    assert (private / "runtime.sqlite3").read_bytes() == b"not used by controls"


@pytest.mark.parametrize("status", [401, 404, 409, 500, 302])
async def test_backend_failure_is_clear_and_never_leaks_its_body(tmp_path, status):
    async def backend(request):
        return web.json_response({"error": TOKEN}, status=status,
                                 headers={"Location": "https://not-followed.invalid/"} if status == 302 else None)

    fake = web.Application()
    fake.router.add_post("/v1/admin/reload", backend)
    async with TestServer(fake) as bridge:
        root, _ = project(tmp_path, bridge.port)
        result, code = await proxy_control(root, "group-1", "reload")
    assert code >= 400 and TOKEN not in json.dumps(result)
    if status == 409:
        assert code == 409 and "生成或发送" in result["error"]


async def test_absent_bridge_does_not_fake_a_success(tmp_path):
    temporary = TestServer(web.Application())
    await temporary.start_server()
    port = temporary.port
    await temporary.close()
    root, _ = project(tmp_path, port)
    result, status = await proxy_control(root, "group-1", "clear-context")
    assert status == 503 and "启动或重启机器人" in result["error"]

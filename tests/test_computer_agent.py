"""Host boundaries plus real installed Harness loops, all model responses are offline fixtures."""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from myatri.contracts import digest
from myatri.providers import FixtureProvider, Gateway
from myatri.runtime.jobs import JobQueue
from myatri_bridge.computer_agent import ComputerAgent
from myatri_bridge.computer_control import ComputerControl
from myatri_bridge.computer_tasks import TaskStore
from myatri_bridge.computer_tools import ComputerTools

PROJECT = Path(__file__).resolve().parents[1]
NODE = PROJECT / "var/harness/node/node.exe"
RUNTIME = PROJECT / "var/harness/runtime"


def source(**changes):
    return {
        "platform_id": "fixture",
        "bot_id": "bot",
        "group_id": "room",
        "sender_id": "owner",
        "message_id": "m1",
        "text": "/pc 任务 创建一个 hello.txt，内容为你好",
        **changes,
    }


def task_key(s):
    return hashlib.sha256(
        json.dumps(
            [s[k] for k in ("platform_id", "bot_id", "group_id", "message_id")],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def agent(tmp_path, responses=(), *, delay=0, profile="bounded"):
    settings = {
        "enabled": True,
        "agent_enabled": True,
        "permission_profile": profile,
        "owner_id": "owner",
        "node_path": str(NODE),
        "runtime_root": str(RUNTIME),
    }
    computer = ComputerControl(
        tmp_path,
        settings,
        owner_id="owner",
        allowed_routes=["fixture|room", "fixture|other"],
        mode="live",
        executor=SimpleNamespace(run=lambda *args: None),
    )
    provider = FixtureProvider(list(responses), delay=delay)
    ledger = JobQueue(tmp_path / "budget.sqlite3", "bridge", "all-groups")
    usage = []
    gateway = Gateway(
        provider,
        budget_ledger=ledger,
        budget_scope="astrbot-all-groups",
        daily_token_limit=50000000,
        usage_sink=usage.append,
    )
    a = ComputerAgent(computer, settings, lambda scope: gateway)
    computer.agent = a
    a.provider, a.gateway, a.usage = provider, gateway, usage
    return a


def tool_response(name, args, identifier):
    return {
        "tool_calls": [
            {
                "id": identifier,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }
        ],
        "finish_reason": "tool_calls",
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    }


async def wait_state(a, task, states):
    async with asyncio.timeout(20):
        while a.store.get(task)["state"] not in states:
            await asyncio.sleep(0.05)
        while a.desktop_lock.locked():
            await asyncio.sleep(0.05)
    return a.store.get(task)


def test_path_boundary_and_no_clobber(tmp_path):
    t = ComputerTools({"workspace": tmp_path})
    for path in (
        "../escape",
        "C:/outside",
        "a:secret",
        "NUL",
        "CON.txt",
        "trailing.",
        "trailing ",
    ):
        with pytest.raises(ValueError):
            t.path("workspace", path)
    t.files("write", "workspace", "safe.txt", text="菅蘧")
    with pytest.raises(FileExistsError):
        t.files("write", "workspace", "safe.txt", text="overwrite")
    assert t.files("read", "workspace", "safe.txt")["text"] == "菅蘧"
    assert t.verify(
        {
            "kind": "file_contains",
            "root": "workspace",
            "path": "safe.txt",
            "text": "菅蘧",
        }
    )["passed"]
    with pytest.raises(ValueError):
        t.path("not_allowed", "safe.txt")


def test_store_claim_unknown_and_controls_survive_restart(tmp_path):
    store = TaskStore(tmp_path / "t.sqlite3")
    task = "a" * 64
    store.create(task, "owner", "scope", "goal", {}, "hash")
    assert store.next() == task
    assert store.claim_call(task, "call", "files", {}) == (True, None)
    store.recover()
    assert store.get(task)["state"] == "unknown"
    assert store.claim_call(task, "call", "files", {}) == (
        False,
        {"error": "outcome_unknown"},
    )
    with pytest.raises(ValueError):
        store.control(task, "resume", command_id="x")
    other = "b" * 64
    store.create(other, "owner", "scope", "goal", {}, "hash")
    paused = store.control(other, "pause", command_id="pause")
    assert paused["state"] == "paused"
    store.control(other, "resume", command_id="resume")
    assert store.control(other, "pause", command_id="pause") == paused
    assert store.get(other)["state"] == "queued" and store.get(other)["turn"] == 1


async def test_owner_scope_dedupe_and_no_group_private_results(tmp_path):
    a = agent(tmp_path)
    # Hold the shared desktop gate to inspect queued tasks without any model IO.
    a.desktop_lock.acquire()
    try:
        s = source()
        key = task_key(s)
        assert (
            a.submit(source(sender_id="imposter"), key)["error"]
            == "computer_owner_required"
        )
        first = a.submit(s, key)
        second = a.submit(s, key)
        assert first == second and len(a.store.list()) == 1
        assert "goal" not in first and "roots" not in first
        foreign = source(group_id="other", message_id="m2", text=f"/pc 状态 {key}")
        assert a.submit(foreign, task_key(foreign))["error"] == "task_scope_denied"
        assert a.provider.requests == []
    finally:
        a.desktop_lock.release()
        await a.close()


@pytest.mark.skipif(not NODE.exists(), reason="isolated Harness runtime not installed")
async def test_at_memory_request_reaches_real_native_shell_without_paid_model(tmp_path):
    a = agent(tmp_path, [
        tool_response("pwsh", {
            "description": "Read physical memory capacity",
            "command": "[pscustomobject]@{memory_bytes=(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory} | ConvertTo-Json -Compress",
        }, "memory-query"),
        tool_response("finish_task", {
            "status": "completed", "summary": "已用系统接口查询内存容量，数据留本机回执。",
            "checks": [{"kind": "receipt_success", "call_id": "memory-query"},
                       {"kind": "receipt_contains", "call_id": "memory-query", "text": "memory_bytes"}],
        }, "memory-finish"),
    ], profile="full")
    s = source(text="@小溫暖\u2005/pc 任务 查询当前内存容量")
    key = task_key(s)
    try:
        raw = SimpleNamespace(author_kind="human", payload={"channel_source": s})
        receipt = await a.computer.for_turn(raw)["computer_control"][1]()
        assert receipt["task_id"] == key and "memory_bytes" not in json.dumps(receipt)
        row = await wait_state(a, key, {"completed", "blocked", "unknown"})
        assert row["state"] == "completed", a.store.details(key)
        assert row["goal"] == "查询当前内存容量"
        actual = a.store.result(key, "memory-query")
        assert actual["isError"] is False and "memory_bytes" in json.dumps(actual)
        assert len(a.provider.requests) == 2
    finally:
        await a.close()


async def test_full_completion_errors_return_real_ids_and_allow_correction(tmp_path):
    from myatri_bridge.computer_full import FullComputerTools
    a = agent(tmp_path, profile='full')
    a.desktop_lock.acquire()
    s = source()
    key = task_key(s)
    try:
        a.submit(s, key)
        a.store.next()
        a.store.claim_call(key, 'actual-native-id', 'pwsh', {})
        a.store.receipt(key, 'actual-native-id', {'isError': False, 'value': {'exitCode': 0},
                                                'content': [{'type': 'text', 'text': 'memory_bytes: 123'}]})
        tools = FullComputerTools({'workspace': tmp_path}, a.store, key)
        for n, checks in enumerate(([{'kind': 'receipt_success', 'call_id': 'pwsh-1'}], [])):
            result = a._tool(key, {'name': 'finish_task', 'call_id': f'bad-{n}',
                'args': {'status': 'completed', 'summary': '查询结束', 'checks': checks}}, tools, n + 1)
            assert result['error'] == 'completion_verification_failed'
            assert result['available_receipt_ids'] == ['actual-native-id']
            assert result['end_turn'] is False and a.store.get(key)['state'] == 'running'
        result = a._tool(key, {'name': 'finish_task', 'call_id': 'corrected',
            'args': {'status': 'completed', 'summary': '查询已验证', 'checks': [
                {'kind': 'receipt_contains', 'call_id': 'actual-native-id', 'text': 'memory_bytes'}]}}, tools, 4)
        assert result['state'] == 'completed' and a.store.get(key)['state'] == 'completed'
    finally:
        a.desktop_lock.release()
        await a.close()


async def test_full_unverified_completion_stops_after_three_corrections(tmp_path):
    from myatri_bridge.computer_full import FullComputerTools
    a = agent(tmp_path, profile='full')
    a.desktop_lock.acquire()
    key = task_key(source())
    try:
        a.submit(source(), key)
        a.store.next()
        tools = FullComputerTools({'workspace': tmp_path}, a.store, key)
        for n in range(3):
            result = a._tool(key, {'name': 'finish_task', 'call_id': f'empty-{n}',
                'args': {'status': 'completed', 'summary': 'model claim', 'checks': []}}, tools, n + 1)
        assert result['state'] == 'blocked' and result['end_turn'] is True
        assert '校验' in a.store.get(key)['summary'] and '不代表用户缺少授权' in a.store.get(key)['summary']
    finally:
        a.desktop_lock.release()
        await a.close()


@pytest.mark.skipif(not NODE.exists(), reason='isolated Harness not installed')
async def test_real_harness_fixes_completion_reference_without_repeating_shell(tmp_path):
    a = agent(tmp_path, [
        tool_response('pwsh', {'description': 'Read memory',
            'command': '[pscustomobject]@{memory_bytes=(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory} | ConvertTo-Json -Compress'}, 'real-memory-id'),
        tool_response('finish_task', {'status': 'completed', 'summary': '查询结束',
            'checks': [{'kind': 'receipt_success', 'call_id': 'pwsh-1'}]}, 'wrong-id'),
        tool_response('finish_task', {'status': 'completed', 'summary': '查询结束', 'checks': []}, 'empty-check'),
        tool_response('finish_task', {'status': 'completed', 'summary': '已验证内存查询',
            'checks': [{'kind': 'receipt_contains', 'call_id': 'real-memory-id', 'text': 'memory_bytes'}]}, 'right-id'),
    ], profile='full')
    key = task_key(source())
    try:
        a.submit(source(), key)
        assert (await wait_state(a, key, {'completed', 'blocked', 'unknown'}))['state'] == 'completed', a.store.details(key)
        receipts = a.store.details(key)['receipts']
        assert len(receipts) == 4 and a.store.receipt_ids(key) == ['real-memory-id']
        response = json.loads(a.provider.requests[2].messages[-1]['content'])
        assert response['host_receipt_id'] == 'wrong-id'
        assert 'real-memory-id' in response['result']
    finally:
        await a.close()


@pytest.mark.skipif(not NODE.exists(), reason='isolated Harness not installed')
async def test_plain_report_gets_one_finish_only_closure_without_repeating_action(tmp_path):
    a = agent(tmp_path, [
        tool_response('pwsh', {'description': 'Query fixture data', 'command': 'Write-Output memory_marker'}, 'native-query'),
        {'content': '查询结果为memory_marker。'},
        tool_response('finish_task', {'status': 'completed', 'summary': '查询结果为memory_marker。',
            'checks': [{'kind': 'receipt_contains', 'call_id': 'native-query', 'text': 'memory_marker'}]}, 'closure'),
    ], profile='full')
    key = task_key(source())
    try:
        a.submit(source(), key)
        assert (await wait_state(a, key, {'completed', 'blocked', 'unknown'}))['state'] == 'completed', a.store.details(key)
        assert len(a.provider.requests) == 3
        assert [t['function']['name'] for t in a.provider.requests[-1].tools] == ['finish_task']
        assert a.store.receipt_ids(key) == ['native-query']
    finally:
        await a.close()


@pytest.mark.skipif(not NODE.exists(), reason='isolated Harness not installed')
async def test_missing_finish_still_preserves_report_and_stops_after_one_closure(tmp_path):
    a = agent(tmp_path, [{'content': '结果报告：未验证信息'}, {'content': '仍未提交校验的最终报告'}], profile='full')
    key = task_key(source())
    try:
        a.submit(source(), key)
        row = await wait_state(a, key, {'completed', 'blocked', 'unknown'})
        assert row['state'] == 'blocked' and len(a.provider.requests) == 2
        assert '最终报告' in row['summary'] and '未验证' in row['summary']
    finally:
        await a.close()


def test_legacy_stopped_task_projects_original_report_without_rewriting_history(tmp_path):
    store = TaskStore(tmp_path / 'tasks.sqlite3')
    task = 'a' * 64
    store.create(task, 'owner', 'scope', 'query', {}, 'hash')
    generic = '执行回合已停止；以工具回执及独立检查为准。'
    store.state(task, 'blocked', generic)
    store.event(task, {'type': 'assistant/message', 'seq': 0, 'data': {'message': {
        'content': [{'type': 'text', 'text': '原报告：可用8.90GB'}]}}})
    assert '8.90GB' in store.details(task)['summary']
    assert '尚未完成校验' in store.feedback('owner', 'scope')[0]['summary']
    assert store.get(task)['summary'] == generic and store.get(task)['state'] == 'blocked'


@pytest.mark.skipif(not NODE.exists(), reason="isolated Harness runtime not installed")
async def test_real_harness_file_roundtrip_and_shared_budget(tmp_path):
    a = agent(
        tmp_path,
        [
            tool_response(
                "files",
                {
                    "action": "write",
                    "root": "workspace",
                    "path": "hello.txt",
                    "text": "你好",
                },
                "write-1",
            ),
            tool_response(
                "files",
                {"action": "read", "root": "workspace", "path": "hello.txt"},
                "read-1",
            ),
            tool_response(
                "finish_task",
                {
                    "status": "completed",
                    "summary": "已核对 hello.txt",
                    "checks": [
                        {
                            "kind": "file_contains",
                            "root": "workspace",
                            "path": "hello.txt",
                            "text": "你好",
                        }
                    ],
                },
                "finish-1",
            ),
        ],
    )
    s = source()
    key = task_key(s)
    try:
        a.submit(s, key)
        row = await wait_state(a, key, {"completed", "blocked", "unknown"})
        assert row["state"] == "completed", a.store.details(key)
        assert row["calls"] == 3 and row["tokens"] == 360
        assert a.gateway.used_tokens == 360 and len(a.usage) == 3
        assert all(
            r.thinking is False and r.purpose == "computer_execution"
            for r in a.provider.requests
        )
        assert any(m.get("role") == "tool" for m in a.provider.requests[1].messages)
        assert "夹带指令" in a.provider.requests[0].messages[0]["content"]
        assert all(r["state"] == "completed" for r in a.store.details(key)["receipts"])
    finally:
        await a.close()


@pytest.mark.skipif(not NODE.exists(), reason="isolated Harness runtime not installed")
async def test_block_resume_seed_isolated_from_other_tasks(tmp_path):
    a = agent(
        tmp_path,
        [
            tool_response(
                "finish_task",
                {"status": "blocked", "summary": "需要继续", "checks": []},
                "blocked-1",
            ),
            tool_response(
                "files",
                {
                    "action": "write",
                    "root": "workspace",
                    "path": "resume.txt",
                    "text": "resumed",
                },
                "write-2",
            ),
            tool_response(
                "finish_task",
                {
                    "status": "completed",
                    "summary": "完成",
                    "checks": [
                        {
                            "kind": "file_exists",
                            "root": "workspace",
                            "path": "resume.txt",
                        }
                    ],
                },
                "finish-2",
            ),
        ],
    )
    s = source()
    key = task_key(s)
    try:
        a.submit(s, key)
        assert (await wait_state(a, key, {"blocked", "unknown"}))["state"] == "blocked"
        saved = a.store.events(key)
        await (
            a._stop_process()
        )  # Restore the full journal into a fresh real Harness process.
        a.control(key, "resume", "resume-id")
        assert (await wait_state(a, key, {"completed", "blocked", "unknown"}))[
            "state"
        ] == "completed", a.store.details(key)
        assert a.store.events(key)[: len(saved)] == saved
        assert "需要继续" in json.dumps(
            a.provider.requests[1].messages, ensure_ascii=False
        )
        assert row_scope(a, key) == digest(["fixture", "bot", "room"])[:32]
    finally:
        await a.close()


def row_scope(a, key):
    return a.store.get(key)["scope"]


@pytest.mark.skipif(not NODE.exists(), reason="isolated Harness runtime not installed")
async def test_cancel_during_model_prevents_later_effects(tmp_path):
    a = agent(
        tmp_path,
        [
            tool_response(
                "files",
                {
                    "action": "write",
                    "root": "workspace",
                    "path": "should-not-exist",
                    "text": "no",
                },
                "late",
            )
        ],
        delay=0.5,
    )
    s = source()
    key = task_key(s)
    try:
        a.submit(s, key)
        async with asyncio.timeout(10):
            while not a.provider.requests:
                await asyncio.sleep(0.02)
        a.control(key, "cancel", "cancel-1")
        assert (await wait_state(a, key, {"cancelled", "unknown"}))[
            "state"
        ] == "cancelled"
        assert not (a.root / "workspaces" / key / "should-not-exist").exists()
        assert a.gateway.used_tokens == 120
    finally:
        await a.close()


async def test_monitor_task_controls_require_local_cookie_and_origin(
    tmp_path, monkeypatch
):
    from aiohttp.test_utils import TestClient, TestServer
    from myatri_bridge.monitor import create_monitor_app

    calls = []

    async def proxy(project, payload=None, **kwargs):
        calls.append((payload, kwargs))
        return {"ok": True, "enabled": True, "tasks": []}, 200

    monkeypatch.setattr("myatri_bridge.monitor.runtime_settings_proxy", proxy)
    async with TestClient(TestServer(create_monitor_app(tmp_path))) as client:
        endpoint = "/api/computer-tasks"
        assert (await client.get(endpoint)).status == 403
        await client.get("/")
        assert (await client.get(endpoint)).status == 200
        payload = {"task_id": "a" * 64, "action": "pause", "command_id": "b" * 32}
        assert (await client.post(endpoint, json=payload)).status == 403
        origin = str(client.make_url("/")).rstrip("/")
        headers = {"Origin": origin, "X-Monitor-Control": "1"}
        assert (
            await client.post(
                endpoint, json={**payload, "goal": "new task"}, headers=headers
            )
        ).status == 400
        assert (
            await client.post(endpoint, json=payload, headers=headers)
        ).status == 200
        assert calls[-1][0] == payload and calls[-1][1]["computer"] is True
        assert (
            await client.get(endpoint, headers={"Sec-Fetch-Site": "cross-site"})
        ).status == 403


async def test_bridge_task_api_requires_bearer_and_never_accepts_new_goals(
    bridge, tmp_path, monkeypatch
):
    from aiohttp.test_utils import TestClient, TestServer
    from myatri_bridge.server import create_app

    a = agent(tmp_path)
    monkeypatch.setattr(bridge, "computer_agent", a)
    monkeypatch.setattr(bridge, "start", lambda: None)
    a.store.create("a" * 64, "owner", "scope", "private-goal", {}, "hash")
    token = "x" * 40
    headers = {"Authorization": "Bearer " + token}
    async with TestClient(TestServer(create_app(bridge, token))) as client:
        endpoint = "/v1/admin/computer-tasks"
        assert (await client.get(endpoint)).status == 401
        view = await (await client.get(endpoint, headers=headers)).json()
        assert "private-goal" not in json.dumps(view)
        detail = await (
            await client.get(endpoint + "?id=" + "a" * 64, headers=headers)
        ).json()
        assert detail["task"]["goal"] == "private-goal"
        payload = {"task_id": "a" * 64, "action": "pause", "command_id": "b" * 32}
        assert (
            await client.post(
                endpoint, headers=headers, json={**payload, "goal": "new goal"}
            )
        ).status == 400
        assert (
            await client.post(endpoint, headers=headers, json=payload)
        ).status == 200
        assert a.store.get("a" * 64)["state"] == "paused"


async def test_host_rejects_injected_paths_and_unverified_completion(tmp_path):
    a = agent(tmp_path)
    s = source()
    key = task_key(s)
    a.desktop_lock.acquire()
    try:
        a.submit(s, key)
        a.store.next()
        t = ComputerTools({"workspace": a.root / "workspaces" / key})
        # Even a compromised execution model cannot use file text to expand host permissions.
        for i, args in enumerate(
            [
                {
                    "action": "write",
                    "root": "workspace",
                    "path": "../escape",
                    "text": "bad",
                },
                {"action": "write", "root": "system", "path": "a", "text": "bad"},
            ]
        ):
            r = a._tool(key, {"name": "files", "args": args, "call_id": str(i)}, t, i)
            assert "error" in r
        result = a._tool(
            key,
            {
                "name": "finish_task",
                "call_id": "unverified",
                "args": {
                    "status": "completed",
                    "summary": "model claims success",
                    "checks": [],
                },
            },
            t,
            3,
        )
        assert result["state"] == "blocked" and a.store.get(key)["state"] == "blocked"
        assert not (a.root / "workspaces" / "escape").exists()
    finally:
        a.desktop_lock.release()
        await a.close()


async def test_execution_hard_call_limit_and_shared_daily_budget(tmp_path):
    a = agent(tmp_path)
    s = source()
    key = task_key(s)
    a.desktop_lock.acquire()
    try:
        a.submit(s, key)
        a.store.next()
        a.store.charge(key, 12, 0)
        options = {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "fixture"}]}
            ],
            "tools": [],
        }
        assert (await a._model(key, options))["error"] == "task_stopped_or_call_limit"
        assert not a.provider.requests
        a.store.charge(key, -12, 0)
        a.gateway.daily_token_limit = 1
        assert "error" in await a._model(key, options)
        assert not a.provider.requests and a.gateway.used_tokens == 0
    finally:
        a.desktop_lock.release()
        await a.close()


@pytest.mark.skipif(not NODE.exists(), reason="isolated Harness runtime not installed")
async def test_two_tasks_have_separate_real_sessions(tmp_path):
    a = agent(
        tmp_path,
        [
            tool_response(
                "finish_task",
                {"status": "blocked", "summary": "first-private", "checks": []},
                "first",
            ),
            tool_response(
                "finish_task",
                {"status": "blocked", "summary": "second", "checks": []},
                "second",
            ),
        ],
    )
    first = source(text="/pc 任务 first-private-goal")
    one = task_key(first)
    second = source(message_id="m2", text="/pc 任务 second-goal")
    two = task_key(second)
    try:
        a.submit(first, one)
        await wait_state(a, one, {"blocked", "unknown"})
        a.submit(second, two)
        await wait_state(a, two, {"blocked", "unknown"})
        assert len(a.provider.requests) == 2
        assert "first-private-goal" not in json.dumps(a.provider.requests[1].messages)
        assert a.store.events(one)[0]["seq"] == 0 and a.store.events(two)[0]["seq"] == 0
    finally:
        await a.close()


def test_fixed_action_does_not_queue_behind_active_desktop_task(tmp_path):
    a = agent(tmp_path)
    a.desktop_lock.acquire()
    try:
        assert a.computer._execute_once(
            "a" * 64, {"action": "open_notepad", "text": ""}
        ) == {"error": "computer_desktop_busy"}
        assert not a.computer.path.exists()
    finally:
        a.desktop_lock.release()


def test_desktop_tickets_bind_window_expire_and_verify_observed_text(
    tmp_path, monkeypatch
):
    t = ComputerTools({"workspace": tmp_path})
    requests = []

    def uia(request):
        requests.append(request)
        return {
            "process_start": "stamp",
            "elements": [{"runtime": "runtime", "name": "显示为42", "value": "42"}],
        }

    monkeypatch.setattr(t, "_uia", uia)
    observed = t.desktop("inspect", window="10")
    ticket = observed["elements"][0]["element"]
    assert "runtime" not in observed["elements"][0]
    with pytest.raises(ValueError, match="window_mismatch"):
        t.desktop("invoke", window="11", element=ticket)
    ticket = t.desktop("inspect", window="10")["elements"][0]["element"]
    t.observed[ticket]["expires"] = 0
    with pytest.raises(ValueError, match="inspect_again_required"):
        t.desktop("invoke", window="10", element=ticket)
    assert t.verify({"kind": "window_contains", "window": "10", "text": "42"})["passed"]
    assert not any(r["action"] == "invoke" for r in requests)


@pytest.mark.skipif(not NODE.exists(), reason="isolated Harness not installed")
async def test_full_profile_real_official_shell_and_absolute_file_edit(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("before", encoding="utf-8")
    responses = [
        tool_response("read", {"file_path": str(outside)}, "native-read"),
        tool_response(
            "edit",
            {"file_path": str(outside), "old_string": "before", "new_string": "after"},
            "native-edit",
        ),
        tool_response(
            "pwsh",
            {
                "description": "Verify edited text using native PowerShell",
                "command": f"Get-Content -LiteralPath '{outside}'",
            },
            "native-shell",
        ),
        tool_response(
            "finish_task",
            {
                "status": "completed",
                "summary": "已核对任意路径文件与PowerShell输出",
                "checks": [
                    {
                        "kind": "receipt_contains",
                        "call_id": "native-shell",
                        "text": "after",
                    },
                    {"kind": "file_contains", "path": str(outside), "text": "after"},
                ],
            },
            "native-finish",
        ),
    ]
    a = agent(tmp_path, responses, profile="full")
    s = source()
    key = task_key(s)
    try:
        a.submit(s, key)
        row = await wait_state(a, key, {"completed", "blocked", "unknown"})
        assert row["state"] == "completed", a.store.details(key)
        assert outside.read_text(encoding="utf-8") == "after"
        names = set(a.store.catalog(key))
        assert {
            "pwsh",
            "read",
            "write",
            "edit",
            "glob",
            "grep",
            "job_output",
            "job_list",
            "job_kill",
            "todo_write",
            "working_directory",
            "skill",
        } <= names
        assert a.store.result(key, "native-shell")["isError"] is False
        assert row["calls"] == 4 and a.gateway.used_tokens == 480
        assert not any(
            t["function"]["name"] == "files" for t in a.provider.requests[0].tools
        )
    finally:
        await a.close()


@pytest.mark.skipif(not NODE.exists(), reason="isolated Harness not installed")
async def test_full_cancel_reaches_real_running_shell_before_late_write(tmp_path):
    late = tmp_path / "late.txt"
    a = agent(
        tmp_path,
        [
            tool_response(
                "pwsh",
                {
                    "description": "Wait then write a test marker",
                    "command": f"Start-Sleep -Seconds 3; [IO.File]::WriteAllText('{late}', 'bad')",
                },
                "long-shell",
            )
        ],
        profile="full",
    )
    s = source()
    key = task_key(s)
    try:
        a.submit(s, key)
        async with asyncio.timeout(15):
            while True:
                with a.store.db() as db:
                    r = db.execute(
                        "SELECT state FROM receipts WHERE task=? AND call_id='long-shell'",
                        (key,),
                    ).fetchone()
                if r:
                    break
                await asyncio.sleep(0.03)
        a.control(key, "cancel", "stop-shell")
        assert (await wait_state(a, key, {"cancelled", "unknown"}))[
            "state"
        ] == "cancelled"
        await asyncio.sleep(3.2)
        assert not late.exists()
    finally:
        await a.close()


@pytest.mark.skipif(not NODE.exists(), reason="isolated Harness not installed")
async def test_full_background_job_and_nonzero_evidence(tmp_path):
    from myatri.contracts import ModelResponse

    a = agent(tmp_path, profile="full")
    s = source()
    key = task_key(s)
    requests = []

    async def generate(request):
        requests.append(request)
        i = len(requests)
        if i == 1:
            response = tool_response(
                "pwsh",
                {
                    "description": "Run a short background marker",
                    "command": "Start-Sleep -Milliseconds 200; Write-Output '后台完成'",
                    "run_in_background": True,
                },
                "start-job",
            )
        elif i == 2:
            receipt = a.store.result(key, "start-job")
            response = tool_response(
                "job_output",
                {"job_id": receipt["value"]["jobId"], "wait": True, "timeout_ms": 5000},
                "collect-job",
            )
        elif i == 3:
            response = tool_response(
                "pwsh",
                {
                    "description": "Return a deliberate nonzero exit",
                    "command": "exit 7",
                },
                "bad-exit",
            )
        else:
            response = tool_response(
                "finish_task",
                {
                    "status": "completed",
                    "summary": "收集后台结果",
                    "checks": [
                        {
                            "kind": "receipt_contains",
                            "call_id": "collect-job",
                            "text": "后台完成",
                        }
                    ],
                },
                "done-job",
            )
        return ModelResponse(provider="fixture", model="fixture", **response)

    a.provider.generate = generate
    try:
        a.submit(s, key)
        assert (await wait_state(a, key, {"completed", "blocked", "unknown"}))[
            "state"
        ] == "completed", a.store.details(key)
        from myatri_bridge.computer_full import FullComputerTools

        t = FullComputerTools({"workspace": tmp_path}, a.store, key)
        assert not t.verify({"kind": "receipt_success", "call_id": "start-job"})[
            "passed"
        ]
        assert not t.verify({"kind": "receipt_success", "call_id": "bad-exit"})[
            "passed"
        ]
        assert t.verify({"kind": "receipt_success", "call_id": "collect-job"})["passed"]
    finally:
        await a.close()


@pytest.mark.skipif(not NODE.exists(), reason="isolated Harness not installed")
async def test_full_official_search_and_mcp_extension(tmp_path):
    import sys

    (tmp_path / "search.txt").write_text("native-search-marker", encoding="utf-8")
    server = tmp_path / "fixture_mcp.py"
    server.write_text(
        """import sys,json
for line in sys.stdin:
 r=json.loads(line);method=r.get('method');identifier=r.get('id')
 if identifier is None:continue
 if method=='initialize':result={'protocolVersion':r['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
 elif method=='tools/list':result={'tools':[{'name':'echo','description':'Fixture local echo','inputSchema':{'type':'object','properties':{'text':{'type':'string'}},'required':['text']}}]}
 elif method=='tools/call':result={'content':[{'type':'text','text':r['params']['arguments']['text']}]}
 else:result={}
 print(json.dumps({'jsonrpc':'2.0','id':identifier,'result':result}),flush=True)
""",
        encoding="utf-8",
    )
    cfg = tmp_path / "native.local.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": [
                    {
                        "transport": "stdio",
                        "serverName": "fixture",
                        "command": sys.executable,
                        "args": ["-u", str(server)],
                        "cwd": str(tmp_path),
                        "env": {},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    a = agent(
        tmp_path,
        [
            tool_response(
                "glob", {"pattern": "*.txt", "path": str(tmp_path)}, "glob-test"
            ),
            tool_response(
                "grep",
                {
                    "pattern": "native-search-marker",
                    "path": str(tmp_path),
                    "include": "*.txt",
                },
                "grep-test",
            ),
            tool_response(
                "mcp__fixture__echo", {"text": "mcp-local-marker"}, "mcp-test"
            ),
            tool_response(
                "finish_task",
                {
                    "status": "completed",
                    "summary": "工具扩展通过",
                    "checks": [
                        {"kind": "receipt_success", "call_id": "glob-test"},
                        {
                            "kind": "receipt_contains",
                            "call_id": "grep-test",
                            "text": "native-search-marker",
                        },
                        {
                            "kind": "receipt_contains",
                            "call_id": "mcp-test",
                            "text": "mcp-local-marker",
                        },
                    ],
                },
                "finish-mcp",
            ),
        ],
        profile="full",
    )
    a.settings["native_config"] = str(cfg)
    s = source()
    key = task_key(s)
    try:
        a.submit(s, key)
        assert (await wait_state(a, key, {"completed", "blocked", "unknown"}))[
            "state"
        ] == "completed", a.store.details(key)
        assert a.store.result(key, "mcp-test")["isError"] is False
    finally:
        await a.close()


@pytest.mark.skipif(not NODE.exists(), reason="isolated Harness not installed")
async def test_full_native_todo_and_cwd_survive_process_restart(tmp_path):
    a = agent(
        tmp_path,
        [
            tool_response(
                "todo_write",
                {"todos": [{"content": "resume marker", "status": "in_progress"}]},
                "todo-before",
            ),
            tool_response("working_directory", {"cd": str(tmp_path)}, "cwd-before"),
            tool_response(
                "finish_task",
                {"status": "blocked", "summary": "等待继续", "checks": []},
                "pause-before",
            ),
            tool_response(
                "pwsh",
                {
                    "description": "Verify restored working directory",
                    "command": "(Get-Location).Path",
                },
                "resume-shell",
            ),
            tool_response(
                "finish_task",
                {
                    "status": "completed",
                    "summary": "恢复原目录",
                    "checks": [
                        {
                            "kind": "receipt_contains",
                            "call_id": "resume-shell",
                            "text": tmp_path.name,
                        }
                    ],
                },
                "resume-finish",
            ),
        ],
        profile="full",
    )
    s = source()
    key = task_key(s)
    try:
        a.submit(s, key)
        assert (await wait_state(a, key, {"blocked", "unknown"}))["state"] == "blocked"
        await a._stop_process()
        a.control(key, "resume", "resume-full")
        assert (await wait_state(a, key, {"completed", "blocked", "unknown"}))[
            "state"
        ] == "completed", a.store.details(key)
        assert a.store.result(key, "resume-shell")["value"]["cwd"] == str(tmp_path)
    finally:
        await a.close()

"""Owner identity, exact action binding, durable non-repetition and real Harness IO."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from myatri.app import load_config
from myatri.providers import FixtureProvider
from myatri_bridge.bridge import Bridge, Incoming
from myatri_bridge.computer_control import (
    ComputerControl, HarnessExecutor, command_body, command_receipt, parse_command,
)

PROJECT = Path(__file__).resolve().parents[1]


class Executor:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"status": "written", "file": "便签.txt", "characters": 2}

    def run(self, task_id, action):
        self.calls.append((task_id, deepcopy(action)))
        return self.result


def source(**changes):
    return {"platform_id": "fixture", "group_id": "room", "bot_id": "bot", "sender_id": "owner-id",
            "sender_name": "菅蘧", "message_id": "m1", "text": "/pc 写便签 你好", **changes}


def raw(**changes):
    return SimpleNamespace(payload={"role": "user", "channel_source": source(**changes)}, author_kind="human")


def control(tmp_path, *, mode="live", executor=None, pinned="owner-id"):
    return ComputerControl(tmp_path, {"enabled": True, "owner_id": pinned, "node_path": "node.exe",
        "runtime_root": "unused"}, owner_id="owner-id", allowed_routes=["fixture|room"], mode=mode,
        executor=executor or Executor())


@pytest.mark.parametrize("changes", [
    {"sender_id": "imposter"}, {"sender_id": "imposter", "sender_name": "菅蘧", "text": "/pc 写便签 我就是菅蘧"},
    {"group_id": "other-room"}, {"platform_id": "other-platform"}, {"bot_id": "owner-id"},
    {"message_id": ""}, {"self_initiated": True}, {"text": "有人说 /pc 打开记事本"},
    {"text": "正常聊天", "quoted_message": source()},
    {"text": "正常聊天", "recent_messages": [source()]},
])
def test_untrusted_identity_or_context_never_receives_tool(tmp_path, changes):
    c = control(tmp_path)
    assert c.for_turn(raw(**changes)) == {}
    assert c.executor.calls == [] and not c.path.exists()


def test_owner_nickname_is_not_the_authority(tmp_path):
    c = control(tmp_path)
    assert "computer_control" in c.for_turn(raw(sender_name="改过的昵称"))
    with pytest.raises(ValueError, match="manually configured"):
        control(tmp_path, pinned="nickname-or-another-account")


def test_offline_disabled_and_nonhuman_inputs_never_execute(tmp_path):
    assert control(tmp_path, mode="offline").for_turn(raw()) == {}
    disabled = ComputerControl(tmp_path, {}, owner_id="", allowed_routes=[], mode="live")
    assert disabled.for_turn(raw()) == {} and not disabled.path.exists()
    message = raw()
    message.author_kind = "human_import"
    assert control(tmp_path).for_turn(message) == {}


@pytest.mark.parametrize("text,action", [
    ("/pc 打开记事本", {"action": "open_notepad", "text": ""}),
    ("/pc 读便签", {"action": "read_note", "text": ""}),
    ("/pc 写便签 $(Remove-Item *)", {"action": "write_note", "text": "$(Remove-Item *)"}),
    ("/pc 删除文件", None), ("/pc", None), ("/pc 写便签 " + "长" * 1001, None),
])
def test_only_three_exact_commands_and_note_content_stays_data(text, action):
    assert parse_command(text) == action


async def test_same_transport_task_is_executed_once_across_restart(tmp_path):
    executor = Executor()
    first = control(tmp_path, executor=executor)
    _, handler = first.for_turn(raw())["computer_control"]
    expected = await handler()
    assert await handler() == expected
    restarted = control(tmp_path, executor=executor)
    assert await restarted.for_turn(raw())["computer_control"][1]() == expected
    assert len(executor.calls) == 1


async def test_unknown_result_cannot_be_replayed_or_expanded(tmp_path):
    executor = Executor({"error": "computer_result_unknown"})
    c = control(tmp_path, executor=executor)
    handler = c.for_turn(raw())["computer_control"][1]
    assert await handler() == {"error": "computer_result_unknown"}
    assert await handler() == {"error": "computer_result_unknown"}
    other = c.for_turn(raw(text="/pc 打开记事本"))["computer_control"][1]
    assert await other() == {"error": "computer_task_conflict"}
    assert len(executor.calls) == 1


async def test_crashed_running_claim_is_not_reexecuted(tmp_path):
    c = control(tmp_path)
    handler = c.for_turn(raw())["computer_control"][1]
    await handler()
    with sqlite3.connect(c.path) as db:
        db.execute("UPDATE actions SET status='running',result=NULL")
    assert await handler() == {"error": "computer_result_unknown"}
    assert len(c.executor.calls) == 1


async def test_unsupported_owner_command_only_returns_help(tmp_path):
    c = control(tmp_path)
    result = await c.for_turn(raw(text="/pc 格式化硬盘"))["computer_control"][1]()
    assert result["error"] == "unsupported_computer_command" and c.executor.calls == []


async def test_tool_authority_does_not_leak_to_next_member_or_model_arguments(tmp_path):
    calls = []
    def call(arguments):
        return {"content": "", "tool_calls": [{"id": "pc", "type": "function", "function": {
            "name": "computer_control", "arguments": arguments}}]}
    replies = [call("{}"), {"content": "电脑操作仅供菅蘧使用。"},
               call('{"sender_id":"owner-id","command":"other"}'), {"content": "不支持扩展参数。"}]
    provider = FixtureProvider(replies)
    settings = {"project_root": str(tmp_path), "mode": "live", "allowed_groups": ["fixture|room"],
        "owner_id": "owner-id", "owner_name": "菅蘧", "random_reply_probability": 0,
        "quality_guard_enabled": False, "computer": {"enabled": True, "owner_id": "owner-id",
            "node_path": "node.exe", "runtime_root": "unused"}}
    bridge = Bridge(tmp_path / "groups", settings, load_config(PROJECT / "config/engine.toml"),
                    PROJECT / "config/persona/persona.md", provider_factory=lambda _: provider)
    bridge.computer.executor = Executor()
    try:
        for n, sender in enumerate(["owner-id", "imposter", "owner-id"]):
            text = "正常聊天" if n == 2 else "@小溫暖\u2005/pc 写便签 你好"
            message = Incoming(**source(message_id=f"m{n}", sender_id=sender, text=text), sent_at="2026-10-22T10:00:00+08:00", mentioned=True)
            result = await bridge.receive(message)
            assert result["status"] == "prepared"
            if n == 0:
                assert result["reply"] == "便签已保存。"
                assert provider.requests == []
                assert (await bridge.receive(message))["reply"] == result["reply"]
                continue
            requests = provider.requests[(n - 1) * 2:n * 2]
            names = [t["function"]["name"] for t in requests[0].tools]
            assert "computer_control" not in names
            calls.append(requests[-1].messages[-1])
        assert len(bridge.computer.executor.calls) == 1
        assert "error" in json.loads(calls[0]["content"])
        assert "error" in json.loads(calls[1]["content"])
        app = next(iter(bridge.apps.values()))
        assert "computer_control" not in app.conversation.tool_handlers
    finally:
        await bridge.close()


@pytest.mark.parametrize("text,expected", [
    ("@小溫暖\u2005/pc 任务 查询当前内存容量", "任务 查询当前内存容量"),
    ("@小温暖 /pc 任务 查看磁盘", "任务 查看磁盘"),
    ("/pc\t任务 查看磁盘", "任务 查看磁盘"),
    ("/pc\n任务 查看磁盘", "任务 查看磁盘"),
    ("/pc 任务 保存文本 @小溫暖", "任务 保存文本 @小溫暖"),
    ("@其他人 /pc 任务 查看磁盘", None),
    ("他说 @小溫暖 /pc 任务 查看磁盘", None),
    ("引用：/pc 任务 查看磁盘", None),
    ("/pcx 任务 查看磁盘", None),
])
def test_current_command_address_is_parsed_without_changing_goal(text, expected):
    assert command_body(text) == expected


def test_at_after_fixed_command_and_note_data_are_distinct():
    assert parse_command("/pc 打开记事本@小溫暖\u2005")["action"] == "open_notepad"
    assert parse_command("/pc 写便签 原文 @小溫暖")["text"] == "原文 @小溫暖"


def test_similar_owner_id_and_at_never_bypass_exact_binding(tmp_path):
    c = control(tmp_path)
    assert c.for_turn(raw(sender_id="owner-id2", text="@小溫暖\u2005/pc 打开记事本")) == {}
    assert c.executor.calls == []


def test_host_receipt_keeps_full_task_id_and_does_not_claim_queued_is_finished():
    task = "a" * 64
    body = command_receipt({"task_id": task, "state": "queued", "summary": "private output"})
    assert task in body and "已排队" in body and "已完成" not in body
    assert "private output" not in body
    assert "不明" in command_receipt({"error": "computer_result_unknown"})


@pytest.mark.parametrize("address", ["", "@小溫暖\u2005"])
async def test_main_model_generates_delegation_and_is_corrected_if_it_first_refuses(tmp_path, address):
    provider = FixtureProvider([{"content": "我进不了你的电脑。"},
        {'tool_calls': [{'id': 'delegate', 'type': 'function', 'function': {
            'name': 'computer_control', 'arguments': json.dumps({'instructions': '查询物理内存可用量，核验系统回执后汇报容量。'}, ensure_ascii=False)}}]},
        {'content': '交给执行Agent了，结果出来我会告诉你。'}])
    settings = {"project_root": str(tmp_path), "mode": "live", "allowed_groups": ["fixture|room"],
        "owner_id": "owner-id", "owner_name": "菅蘧", "random_reply_probability": 0,
        "quality_guard_enabled": False, "computer": {"enabled": True, "owner_id": "owner-id",
            "agent_enabled": True, "permission_profile": "full",
            "node_path": "node.exe", "runtime_root": "unused"}}
    bridge = Bridge(tmp_path / "groups", settings, load_config(PROJECT / "config/engine.toml"),
                    PROJECT / "config/persona/persona.md", provider_factory=lambda _: provider)
    bridge.computer.lock.acquire()
    try:
        message = Incoming(**source(text=address + "/pc 任务 查询当前内存容量"),
                           sent_at="2026-10-22T10:00:00+08:00", mentioned=bool(address))
        result = await bridge.receive(message)
        assert result["status"] == "prepared"
        task, = bridge.computer_agent.store.list()
        row = bridge.computer_agent.store.get(task["id"])
        assert row["goal"] == "查询当前内存容量" and row["owner"] == "owner-id"
        assert json.loads(row["contract"])["profile"] == "full"
        assert len(provider.requests) == 3
        assert json.loads(row['contract'])['delegation'] == '查询物理内存可用量，核验系统回执后汇报容量。'
        assert json.loads(row['contract'])['report_to_chat'] is True
        assert (await bridge.receive(message))["reply"] == result["reply"]
        assert len(bridge.computer_agent.store.list()) == 1
    finally:
        await bridge.close()
        bridge.computer.lock.release()


async def test_delegation_error_can_be_reported_without_claiming_task_submission(tmp_path):
    provider = FixtureProvider([
        {'tool_calls': [{'id': 'delegate', 'type': 'function', 'function': {
            'name': 'computer_control', 'arguments': '{"instructions":"查询内存并核验"}'}}]},
        {'content': '执行器现在没有启用，暂时没有提交任务。'}])
    settings = {'project_root': str(tmp_path), 'mode': 'live', 'allowed_groups': ['fixture|room'],
        'owner_id': 'owner-id', 'quality_guard_enabled': False, 'computer': {'enabled': True,
            'owner_id': 'owner-id', 'node_path': 'node.exe', 'runtime_root': 'unused'}}
    bridge = Bridge(tmp_path/'groups', settings, load_config(PROJECT/'config/engine.toml'),
                    PROJECT/'config/persona/persona.md', provider_factory=lambda _: provider)
    try:
        result = await bridge.receive(Incoming(**source(text='/pc 任务 查询内存'),
            mentioned=True, sent_at='2026-10-22T12:00:00+08:00'))
        assert result['status'] == 'prepared' and '没有提交任务' in result['reply']
        assert len(provider.requests) == 2
    finally:
        await bridge.close()


def test_installed_harness_writes_and_reads_only_temporary_note(tmp_path):
    # Real official runtime composition, no LLM, credentials, network or GUI input.
    node = PROJECT / "var/harness/node/node.exe"
    runtime = PROJECT / "var/harness/runtime"
    if not node.is_file() or not (runtime / "node_modules/@deepseek-ai/dsh/package.json").exists():
        pytest.skip("optional local Harness installation absent")
    executor = HarnessExecutor(node, runtime, tmp_path / "notes")
    def key(text):
        return hashlib.sha256(text.encode()).hexdigest()
    assert executor.run(key("probe"), {"action": "probe", "text": ""})["status"] == "ready"
    text = "合成便签 $(not-a-command)"
    assert executor.run(key("write"), {"action": "write_note", "text": text})["status"] == "written"
    read = executor.run(key("read"), {"action": "read_note", "text": ""})
    assert read["status"] == "read" and read["text"] == text + "\n"
    assert (tmp_path / "notes/便签.txt").read_text(encoding="utf-8") == text + "\n"
    assert list((tmp_path / "notes").iterdir()) == [tmp_path / "notes/便签.txt"]


def test_harness_rejects_a_hard_linked_note(tmp_path):
    import os
    node = PROJECT / "var/harness/node/node.exe"
    if not node.is_file():
        pytest.skip("optional local Harness installation absent")
    original = tmp_path / "outside.txt"
    original.write_text("原文", encoding="utf-8")
    notes = tmp_path / "notes"
    notes.mkdir()
    os.link(original, notes / "便签.txt")
    executor = HarnessExecutor(node, PROJECT / "var/harness/runtime", notes)
    assert executor.run("0" * 64, {"action": "write_note", "text": "不能写入"}) == {"error": "computer_result_unknown"}
    assert original.read_text(encoding="utf-8") == "原文"


def test_harness_unicode_limit_matches_the_host_parser(tmp_path):
    node = PROJECT / "var/harness/node/node.exe"
    if not node.is_file():
        pytest.skip("optional local Harness installation absent")
    executor = HarnessExecutor(node, PROJECT / "var/harness/runtime", tmp_path / "notes")
    text = "😀" * 1000
    action = parse_command("/pc 写便签 " + text)
    assert action is not None
    result = executor.run("1" * 64, action)
    assert result["status"] == "written" and result["characters"] == 1000
    read = executor.run("2" * 64, {"action": "read_note", "text": ""})
    assert read["truncated"] is True and len(read["text"]) == 1000

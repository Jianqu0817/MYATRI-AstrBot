"""Main-persona task reports go through the real durable multipart outbox."""
from contextlib import closing
import ast
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import time
import logging

import pytest
import httpx

from myatri.app import load_config
from myatri.providers import FixtureProvider
from myatri_bridge.bridge import Bridge, Incoming
from myatri_bridge.server import pending_deliveries

PROJECT = Path(__file__).resolve().parents[1]


def setup(tmp_path, replies=None):
    provider = FixtureProvider(replies or [{'content': '查到了，可用内存8.90GB\n查询发生在12:07，当前数值可能有变化'}])
    settings = {'project_root': str(tmp_path), 'mode': 'live', 'allowed_groups': ['fixture|room'],
        'owner_id': 'owner', 'owner_name': '菅蘧', 'random_reply_probability': 0,
        'quality_guard_enabled': False, 'computer': {'enabled': True, 'agent_enabled': True,
            'permission_profile': 'full', 'owner_id': 'owner', 'node_path': 'node.exe', 'runtime_root': 'unused'}}
    bridge = Bridge(tmp_path / 'groups', settings, load_config(PROJECT / 'config/engine.toml'),
                    PROJECT / 'config/persona/persona.md', provider_factory=lambda _: provider)
    message = Incoming(platform_id='fixture', bot_id='bot', group_id='room', sender_id='owner',
        message_id='pc-original', text='/pc 任务 查看内存', mentioned=True,
        sent_at='2026-10-22T12:07:00+08:00')
    bridge.routes[message.scope] = message.model_dump()
    return bridge, provider, message


def task(bridge, message, *, terminal='completed', initial='waiting_for_computer', suffix='', envelope_extra=None):
    source = message.model_copy(update={'message_id': message.message_id + suffix})
    task_id = hashlib.sha256(json.dumps([source.platform_id, source.bot_id, source.group_id, source.message_id],
                                       ensure_ascii=False).encode()).hexdigest()
    bridge.computer_agent.store.create(task_id, source.sender_id, source.scope, '查看内存', {
        'report_to_chat': False, 'reply_mode': 'conversation', 'channel_source': source.model_dump()}, 'hash')
    bridge.computer_agent.store.state(task_id, terminal, '12:07可用内存8.90GB；查询已验证。')
    envelope = json.dumps(source.model_dump(exclude={'mentioned'}) | (envelope_extra or {}))
    with closing(sqlite3.connect(bridge.path)) as db, db:
        db.execute('INSERT INTO deliveries(id,scope,fingerprint,status,updated,envelope) VALUES(?,?,?,?,?,?)',
            (source.key, source.scope, 'fixture', initial, 1, envelope))
    app = bridge._application(source)
    app.store.append_message('user', envelope, message_id=source.key, channel_source=source.model_dump())
    app.queue.save_protocol(source.key, {'status': 'active', 'snapshot': app.store.snapshot(), 'steps': 1,
        'messages': [{'role': 'user', 'content': envelope},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'delegate', 'type': 'function',
                'function': {'name': 'computer_control', 'arguments': '{"instructions":"查询内存并核验"}'}}]},
            {'role': 'tool', 'tool_call_id': 'delegate', 'content': json.dumps({'task_id': task_id, 'state': 'queued'})}],
        'computer_wait': {'call_id': 'delegate', 'arguments': {'instructions': '查询内存并核验'}}})
    bridge.computer_reports.wait(source, task_id)
    return task_id, source


async def test_main_persona_receives_report_and_prepares_wechat_without_another_human_turn(tmp_path):
    bridge, provider, message = setup(tmp_path)
    task_id, source = task(bridge, message)
    try:
        await bridge.computer_reports.tick()
        assert len(provider.requests) == 1 and provider.requests[0].purpose == 'conversation'
        receipt = next(m for m in provider.requests[0].messages if m['role'] == 'tool')
        assert receipt['tool_call_id'] == 'delegate'
        assert '8.90GB' in receipt['content']
        assert json.loads(receipt['content'])['state'] == 'completed'
        protocol = bridge.apps[message.scope].queue.get_protocol(message.key)
        assert protocol['status'] == 'completed'
        assert any(m['role'] == 'tool' and '8.90GB' in m['content'] for m in protocol['messages'])
        assert len(bridge.apps[message.scope].store.list_records('raw_message')) == 2
        pending = pending_deliveries(bridge.path, message.scope)
        assert len(pending) == 1
        delivery = pending[0]
        assert len(delivery['reply_parts']) == 2 and delivery['id'] == message.key
        assert (await bridge.claim(delivery['id']))['claimed'] is True
        await bridge.acknowledge(delivery['id'], 'sent', 0)
        await bridge.acknowledge(delivery['id'], 'sent', 1)
        assert len(bridge.short_memory.recent_assistant(message.scope)) == 2
        await bridge.computer_reports.tick()
        assert len(provider.requests) == 1 and pending_deliveries(bridge.path, message.scope) == []
    finally:
        await bridge.close()


@pytest.mark.parametrize('terminal', ['completed', 'blocked', 'unknown', 'paused', 'cancelled'])
async def test_waiting_turn_survives_restart_and_returns_actual_state(tmp_path, terminal):
    bridge, provider, message = setup(tmp_path)
    task_id, _ = task(bridge, message, terminal=terminal)
    settings, engine = bridge.config, bridge.engine_config
    await bridge.close()
    restored = Bridge(tmp_path / 'groups', settings, engine, PROJECT / 'config/persona/persona.md',
                      provider_factory=lambda _: provider)
    try:
        await restored.computer_reports.tick()
        receipt = next(m for m in provider.requests[-1].messages if m['role'] == 'tool')
        assert json.loads(receipt['content'])['state'] == terminal
        assert json.loads(receipt['content'])['report']['task_id'] == task_id
        assert len(restored.computer_agent.store.list()) == 1
        assert pending_deliveries(restored.path, message.scope)[0]['id'] == message.key
    finally:
        await restored.close()


async def test_submission_checkpoint_recovers_missing_wait_registration(tmp_path):
    bridge, provider, message = setup(tmp_path)
    task_id, _ = task(bridge, message)
    with closing(sqlite3.connect(bridge.path)) as db, db:
        db.execute('DELETE FROM computer_reports')
        db.execute("UPDATE deliveries SET status='received',trigger_kind='explicit'")
    try:
        await bridge.computer_reports.tick()
        assert len(provider.requests) == 1
        assert bridge.computer_reports.status(task_id)['delivery_id'] == message.key
        assert len(bridge.computer_agent.store.list()) == 1
    finally:
        await bridge.close()


async def test_call_checkpoint_recovers_crash_before_task_submission(tmp_path):
    bridge, provider, message = setup(tmp_path)
    task_id, _ = task(bridge, message, envelope_extra={'context_note': 'background', 'recent_messages': []})
    with bridge.computer_agent.store.db() as db:
        db.execute('DELETE FROM inputs')
        db.execute('DELETE FROM tasks')
    # Simulate the gap between saving the call and registering its wait.
    with closing(sqlite3.connect(bridge.path)) as db, db:
        db.execute('DELETE FROM computer_reports')
        db.execute("UPDATE deliveries SET status='received',trigger_kind='explicit'")
    bridge.computer.lock.acquire()
    try:
        await bridge.computer_reports.tick()
        assert provider.requests == []  # Continue the saved call without a new plan.
        assert bridge.computer_agent.store.get(task_id)['state'] == 'queued'
        assert len(bridge.computer_agent.store.list()) == 1
        assert bridge.computer_reports.status(task_id)['attempts'] == 0
        assert not pending_deliveries(bridge.path, message.scope)
    finally:
        await bridge.close()
        bridge.computer.lock.release()


@pytest.mark.parametrize('old_state', ['prepared', 'sent', 'unknown'])
async def test_upgrade_supersedes_only_unsent_independent_drafts(tmp_path, old_state):
    bridge, provider, message = setup(tmp_path)
    task_id, _ = task(bridge, message, initial='sent')
    with closing(sqlite3.connect(bridge.path)) as db, db:
        db.execute('DELETE FROM computer_reports')
        db.execute('INSERT INTO deliveries(id,scope,fingerprint,status,reply,updated) VALUES(?,?,?,?,?,?)',
                   ('pc_report_old', message.scope, 'old', old_state, '旧的独立草稿', 1))
        db.execute('INSERT INTO computer_reports(task,turn,delivery_id,state) VALUES(?,?,?,?)',
                   (task_id, 0, 'pc_report_old', 'prepared'))
    settings, engine = bridge.config, bridge.engine_config
    await bridge.close()
    restored = Bridge(tmp_path / 'groups', settings, engine, PROJECT / 'config/persona/persona.md',
                      provider_factory=lambda _: provider)
    try:
        with closing(sqlite3.connect(restored.path)) as db:
            row = db.execute("SELECT status,reply FROM deliveries WHERE id='pc_report_old'").fetchone()
        assert row == ('superseded' if old_state == 'prepared' else old_state, '旧的独立草稿')
        await restored.computer_reports.tick()
        assert provider.requests == []
    finally:
        await restored.close()


async def test_old_background_task_does_not_generate_an_unrelated_catchup_reply(tmp_path):
    bridge, provider, message = setup(tmp_path)
    task_id, _ = task(bridge, message, initial='sent')
    with closing(sqlite3.connect(bridge.path)) as db, db:
        db.execute('DELETE FROM computer_reports')
    with bridge.computer_agent.store.db() as db:
        db.execute('UPDATE tasks SET contract=?', (json.dumps({'report_to_chat': True}),))
    try:
        await bridge.computer_reports.tick()
        assert provider.requests == [] and pending_deliveries(bridge.path, message.scope) == []
        assert bridge.computer_agent.store.get(task_id)['state'] == 'completed'
    finally:
        await bridge.close()


@pytest.mark.parametrize('outcome', ['sent', 'unknown'])
async def test_report_restart_never_replays_sent_or_unknown(tmp_path, outcome):
    bridge, provider, message = setup(tmp_path)
    task(bridge, message)
    try:
        await bridge.computer_reports.tick()
        key = pending_deliveries(bridge.path, message.scope)[0]['id']
        await bridge.claim(key)
        await bridge.acknowledge(key, outcome)
        settings, engine = bridge.config, bridge.engine_config
    finally:
        await bridge.close()
    restored = Bridge(tmp_path / 'groups', settings, engine, PROJECT / 'config/persona/persona.md',
                      provider_factory=lambda _: provider)
    try:
        restored.routes[message.scope] = message.model_dump()
        await restored.computer_reports.tick()
        assert len(provider.requests) == 1 and pending_deliveries(restored.path, message.scope) == []
    finally:
        await restored.close()


async def test_report_waits_for_result_and_rejects_foreign_owner(tmp_path):
    bridge, provider, message = setup(tmp_path)
    task_id, source = task(bridge, message, terminal='queued')
    try:
        await bridge.computer_reports.tick()
        assert provider.requests == []
        assert bridge._proactive_state(message.scope)['explicit_reply_pending']
        with bridge.computer_agent.store.db() as db:
            db.execute("UPDATE tasks SET state='completed',owner='imposter'")
        await bridge.computer_reports.tick()
        assert provider.requests == []
    finally:
        await bridge.close()


async def test_failed_report_generation_is_bounded_and_has_no_send_effect(tmp_path):
    bridge, provider, message = setup(tmp_path)
    task(bridge, message)
    async def fail(request):
        raise RuntimeError('fixture unavailable')
    try:
        app = bridge._application(message)
        app.gateway.generate = fail
        for _ in range(5):
            with closing(sqlite3.connect(bridge.path)) as db, db:
                db.execute('UPDATE computer_reports SET next_attempt=0')
            await bridge.computer_reports.tick()
        with closing(sqlite3.connect(bridge.path)) as db:
            assert db.execute('SELECT attempts FROM computer_reports').fetchone()[0] == 3
        assert pending_deliveries(bridge.path, message.scope) == []
    finally:
        await bridge.close()


@pytest.mark.skipif(not (PROJECT / 'var/harness/node/node.exe').exists(), reason='local Harness absent')
@pytest.mark.parametrize('text', ['/pc 任务 查看内存', '@小溫暖\u2005帮我查询内存容量空间'])
async def test_main_delegates_real_harness_then_generates_report_end_to_end(tmp_path, text):
    def call(name, args, identifier):
        return {'tool_calls': [{'id': identifier, 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(args, ensure_ascii=False)}}]}
    replies = [
        call('computer_control', {'instructions': '用PowerShell读取memory_marker并验证回执，将结果交回我向菅蘧汇报。'}, 'main-delegate'),
        call('pwsh', {'description': 'Fixture query', 'command': 'Write-Output memory_marker'}, 'native-data'),
        call('finish_task', {'status': 'completed', 'summary': '系统查询得到memory_marker，回执已验证。',
            'checks': [{'kind': 'receipt_contains', 'call_id': 'native-data', 'text': 'memory_marker'}]}, 'finish'),
        {'content': '查到了，结果是memory_marker\n这次已经核验过了'},
    ]
    bridge, provider, message = setup(tmp_path, replies)
    message = message.model_copy(update={'text': text})
    agent = bridge.computer_agent
    agent.settings['node_path'] = str(PROJECT / 'var/harness/node/node.exe')
    agent.settings['runtime_root'] = str(PROJECT / 'var/harness/runtime')
    agent.desktop_lock.acquire()
    released = False
    try:
        accepted = await bridge.receive(message)
        assert accepted['status'] == 'waiting_for_computer' and not accepted['reply_parts']
        row, = agent.store.list()
        assert 'PowerShell' in json.loads(agent.store.get(row['id'])['contract'])['delegation']
        assert len(provider.requests) == 1
        assert not (await bridge.claim(message.key))['claimed']
        agent.desktop_lock.release()
        released = True
        async with asyncio.timeout(20):
            while agent.store.get(row['id'])['state'] not in {'completed', 'blocked', 'unknown'}:
                await asyncio.sleep(.05)
        assert agent.store.get(row['id'])['state'] == 'completed', agent.store.details(row['id'])
        await bridge.computer_reports.tick()
        report, = pending_deliveries(bridge.path, message.scope, kind='computer_report')
        assert len(report['reply_parts']) == 2 and 'memory_marker' in report['reply_parts'][0]
        assert [r.purpose for r in provider.requests] == [
            'conversation', 'computer_execution', 'computer_execution', 'conversation']
        returned = next(m for m in provider.requests[-1].messages if m['role'] == 'tool')
        assert returned['tool_call_id'] == 'main-delegate' and 'memory_marker' in returned['content']
        assert report['id'] == message.key
        assert len(bridge.apps[message.scope].store.list_records('raw_message')) == 2
    finally:
        if not released:
            agent.desktop_lock.release()
        await bridge.close()


def plugin_methods():
    path = PROJECT / 'astrbot_plugin_myatri/main.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'MyatriPlugin')
    methods = [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name in {'_deliver_pending', '_send_parts'}]
    helpers = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'delivery_error_code']
    class Plain:
        def __init__(self, text):
            self.text = text
    namespace = {'asyncio': asyncio, 'time': time, 'httpx': httpx, 'logger': logging.getLogger('report-test'),
                 'Plain': Plain, 'MessageChain': lambda parts: parts}
    exec(compile(ast.Module(body=[*helpers, *methods], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace


async def test_plugin_sends_report_as_two_messages_with_proactive_disabled(tmp_path):
    bridge, provider, message = setup(tmp_path)
    task(bridge, message)
    sent, queries = [], []
    async def send(session, chain):
        sent.append(chain[0].text)
        return True
    async def handle(request):
        if request.url.path.endswith('/pending'):
            queries.append(dict(request.url.params))
            return httpx.Response(200, json={'deliveries': pending_deliveries(bridge.path, message.scope,
                kind=request.url.params.get('kind'))})
        key = request.url.path.split('/')[-2]
        if request.url.path.endswith('/claim'):
            result = await bridge.claim(key)
        else:
            args = json.loads(request.content)
            result = await bridge.acknowledge(key, args.pop('status'), **args)
        return httpx.Response(200, json=result)
    try:
        await bridge.computer_reports.tick()
        methods = plugin_methods()
        async with httpx.AsyncClient(base_url='http://127.0.0.1', transport=httpx.MockTransport(handle)) as client:
            plugin = SimpleNamespace(client=client, config={'proactive_enabled': False}, delivery_locks={},
                                     context=SimpleNamespace(send_message=send))
            plugin._send_parts = lambda *args: methods['_send_parts'](plugin, *args)
            target = {'scope': message.scope, 'session': 'fixture:GroupMessage:room'}
            await methods['_deliver_pending'](plugin, message.route, target)
            await methods['_deliver_pending'](plugin, message.route, target)
        assert queries[0]['kind'] == 'computer_report'
        assert len(sent) == 2 and all('\n' not in part for part in sent)
        assert bridge.computer_reports.status(next(iter(bridge.computer_agent.store.list()))['id'])['delivery_state'] == 'sent'
    finally:
        await bridge.close()

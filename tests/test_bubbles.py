"""One model call, ordered bubbles, durable partial delivery and no resend."""
import ast
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from myatri.conversation.reply import ReplyPolicy
from myatri.providers import FixtureProvider
from myatri_bridge.bridge import Bridge, Conflict, Incoming
from myatri_bridge.bubbles import BUBBLE_RULES, SEPARATOR, prepare_bubbles
from myatri_bridge.server import DeliveryAck


POLICY = ReplyPolicy({"reply_min_chars": 20, "reply_max_chars": 160, "reply_target_max_chars": 100})
FIRST = "回来啦，今天好像比平时晚一点。"
SECOND = "先把东西放下吧，我陪你歇会儿。"


@pytest.mark.parametrize("text, expected", [
    ("嗯，回来就好。", ["嗯，回来就好。"]),
    ("好" * 15, ["好" * 15]),
    (FIRST + SEPARATOR + SECOND, [FIRST, SECOND]),
    (SEPARATOR + FIRST + SEPARATOR + SEPARATOR + SECOND + SEPARATOR, [FIRST, SECOND]),
    (FIRST + SECOND, [FIRST + SECOND]),
    (FIRST + SECOND + "好。", [FIRST, SECOND, "好。"]),
    ("这句话没有自然断点所以应该保持完整才自然", ["这句话没有自然断点所以应该保持完整才自然"]),
    ("```python\nprint('请保留这段代码完整')\n```", ["```python\nprint('请保留这段代码完整')\n```"]),
    ("短" + SEPARATOR + "句", ["短", "句"]),
    ("我不指挥，听人喊就行\n\n你要指挥我倒也能跟", ["我不指挥，听人喊就行", "你要指挥我倒也能跟"]),
    ("先看结果\n再接着说", ["先看结果", "再接着说"]),
    ('你说“先休息\n明天再聊”\n那就好好睡', ['你说“先休息\n明天再聊”', '那就好好睡']),
    ('你说“今天有点累。明天再聊吧。”那就早点休息。', ['你说“今天有点累。明天再聊吧。”那就早点休息。']),
    ('这个链接是 https://example.com/?q=hello 先保存起来吧。', ['这个链接是 https://example.com/?q=hello 先保存起来吧。']),
])
def test_marker_and_safe_fallback_preserve_complete_meaning(text, expected):
    body, parts = prepare_bubbles(text, POLICY)
    assert parts == expected and SEPARATOR not in body
    assert len(parts) <= 3
    assert "".join(body.split()) == "".join("".join(parts).split())


def test_three_part_cap_and_whole_reply_length_cap():
    body, parts = prepare_bubbles(SEPARATOR.join([FIRST, SECOND, FIRST, SECOND] * 4), POLICY)
    assert 1 <= len(parts) <= 3
    assert len("".join(body.split())) <= 160
    assert all(SEPARATOR not in part and part for part in parts)
    assert "".join(body.split()) == "".join("".join(parts).split())


def provider_for(bridge):
    provider = FixtureProvider([{"content": FIRST + SEPARATOR + SECOND}])
    bridge.provider_factory = lambda scope: provider
    return provider


async def test_one_generation_one_archive_turn_and_per_part_confirmation(bridge, message):
    provider = provider_for(bridge)
    result = await bridge.receive(message)
    assert result["reply_parts"] == [FIRST, SECOND] and result["sent_parts"] == 0
    assert len(provider.requests) == 1 and BUBBLE_RULES in provider.requests[0].messages[-2]["content"]
    app = bridge.apps[message.scope]
    answer = app.store.get_record("answer_" + message.key)
    assert answer.payload["reply_parts"] == [FIRST, SECOND] and SEPARATOR not in answer.body_markdown
    assert len(app.store.list_records("raw_message")) == 2
    await bridge.claim(message.key)
    with pytest.raises(Conflict):
        await bridge.acknowledge(message.key, "sent", 1)
    assert (await bridge.acknowledge(message.key, "sent", 0))["status"] == "sending"
    assert (await bridge.acknowledge(message.key, "sent", 0))["sent_parts"] == 1
    assert (await bridge.acknowledge(message.key, "sent", 1))["status"] == "sent"
    with closing(sqlite3.connect(bridge.short_memory.path)) as db:
        own = [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM listening_assistant_context WHERE scope=? ORDER BY rowid", (message.scope,))]
    assert [row["text"] for row in own] == [FIRST, SECOND]
    assert len({row["message_id"] for row in own}) == 2
    # The next request replays the full confirmed answer once, not its bubbles again.
    await bridge.receive(message.model_copy(update={"message_id": "next", "text": "接着说吧"}))
    envelope = json.loads(provider.requests[-1].messages[-1]["content"].split("\n", 1)[1])
    assert not any(row.get("role") == "assistant" for row in envelope["recent_messages"])


async def test_restart_retains_partial_progress_and_does_not_resend(bridge, message):
    provider_for(bridge)
    await bridge.receive(message)
    await bridge.claim(message.key)
    await bridge.acknowledge(message.key, "sent", 0)
    await bridge.close()
    restored = Bridge(bridge.root, bridge.config, bridge.engine_config,
        Path(__file__).resolve().parents[1] / "config/persona/persona.md", provider_factory=bridge.provider_factory)
    try:
        state = await restored.receive(message)
        assert state["status"] == "unknown" and state["sent_parts"] == 1
        assert state["reply_parts"] == [FIRST, SECOND]
        assert (await restored.claim(message.key))["claimed"] is False
        assert not restored.apps
    finally:
        await restored.close()


class Plain:
    def __init__(self, text):
        self.text = text


class At:
    pass


class Reply:
    pass


class Event:
    def __init__(self, message, *, fail_second=False):
        self.message = message
        self.message_obj = SimpleNamespace(message_id=message.message_id,
            timestamp=datetime.fromisoformat(message.sent_at).timestamp(), raw_message={})
        self.sent, self.attempted, self.fail_second = [], 0, fail_second

    def stop_event(self):
        pass

    @property
    def unified_msg_origin(self):
        return f"{self.message.platform_id}:GroupMessage:{self.message.group_id}"

    def get_platform_id(self):
        return self.message.platform_id

    def get_platform_name(self):
        return "fixture"

    def get_group_id(self):
        return self.message.group_id

    def get_self_id(self):
        return self.message.bot_id

    def get_sender_id(self):
        return self.message.sender_id

    def get_sender_name(self):
        return self.message.sender_name

    def get_messages(self):
        return [Plain("/warm " + self.message.text)]

    def plain_result(self, text):
        return text

    async def send(self, text):
        self.attempted += 1
        await asyncio.sleep(0)
        if self.fail_second and self.attempted == 2:
            raise TimeoutError("ambiguous platform receipt")
        self.sent.append(text)


def plugin_callback():
    # Execute the actual callback with only AstrBot's UI surface stubbed out.
    path = Path(__file__).resolve().parents[1] / "astrbot_plugin_myatri/main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "delivery_error_code")
    scope_helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "group_scope")
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == "MyatriPlugin")
    method = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "group_message")
    method.decorator_list = []
    import hashlib
    import time
    namespace = {"asyncio": asyncio, "datetime": datetime, "timezone": timezone, "time": time,
                 "hashlib": hashlib, "json": json, "httpx": httpx,
                 "logger": logging.getLogger("bubbles-test"), "Plain": Plain, "At": At, "Reply": Reply,
                 "AstrMessageEvent": Event}
    exec(compile(ast.Module(body=[helper, scope_helper, method], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["group_message"]


@pytest.mark.parametrize("fail_second", [False, True])
async def test_plugin_sends_in_order_and_stops_on_ambiguous_failure(bridge, message, fail_second):
    provider = provider_for(bridge)

    async def handler(request):
        body = json.loads(request.content or b"{}")
        if request.url.path == "/v1/messages":
            result = await bridge.receive(Incoming.model_validate(body))
        elif request.url.path.endswith("/claim"):
            result = await bridge.claim(message.key)
        else:
            ack = DeliveryAck.model_validate(body)
            result = await bridge.acknowledge(message.key, ack.status, ack.part_index,
                reason_code=ack.reason_code, elapsed_seconds=ack.elapsed_seconds)
        return httpx.Response(200, json=result)

    async with httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.MockTransport(handler)) as client:
        plugin = SimpleNamespace(config={"enabled": True, "allowed_groups": [message.route]},
                                 client=client, inflight=set(), delivery_locks={},
                                 proactive_sessions={})
        event = Event(message, fail_second=fail_second)
        callback = plugin_callback()
        await callback(plugin, event)
        assert event.sent == ([FIRST] if fail_second else [FIRST, SECOND])
        await callback(plugin, event)
        assert event.attempted == 2  # A repeat callback never resends a partial or completed batch.
    with closing(sqlite3.connect(bridge.path)) as db:
        status, count = db.execute("SELECT status,sent_parts FROM deliveries WHERE id=?", (message.key,)).fetchone()
        telemetry = db.execute("SELECT status,reason_code,elapsed_seconds FROM delivery_events ORDER BY id").fetchall()
    assert status == ("unknown" if fail_second else "sent")
    assert count == (1 if fail_second else 2)
    assert len(telemetry) == 2 and all(row[2] >= 0 for row in telemetry)
    assert telemetry[-1][1] == ("send_error" if fail_second else None)
    assert len(provider.requests) == 1

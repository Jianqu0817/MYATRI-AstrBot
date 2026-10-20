"""Synthetic quoted-message transport checks; no WeChat send or paid calls."""
import asyncio
from contextlib import closing
from copy import deepcopy
import json
import os
import sqlite3
import time
from types import SimpleNamespace

import pytest

from myatri_bridge.bridge import Incoming
from myatri.providers import FixtureProvider
from conftest import PROJECT
from test_wechatagent import incoming, settings


def native_quote():
    return {"event": "message.new", "sessionType": "group", "sessionId": "room@chatroom",
            "talkerId": "wxid_actual", "sourceName": "真实发言者", "rawid": "native-reply",
            "type": 49, "timestamp": time.time(), "content": "@小溫暖\u2005我说的是这件事",
            "quoteReply": {"version": 1, "truncated": False, "reply": "@小溫暖\u2005我说的是这件事",
                "reference": {"conversationId": "room@chatroom", "kind": "1", "senderId": "wxid_quoted",
                    "senderName": "引用者", "messageId": "server-123", "time": "2026-10-21T12:00:00+08:00",
                    "text": "原来的完整文本", "truncated": False, "unavailable": False}}}


def native_client():
    return SimpleNamespace(group_id="room@chatroom", bot_id="wxid_bot", settings=settings())


@pytest.mark.parametrize("kind", [49, (57 << 32) | 49])
def test_complete_native_quote_preserves_reference_without_authenticating_quoted_sender(kind):
    payload = native_quote()
    payload["type"] = kind
    result = incoming(payload, native_client(), payload["timestamp"] - 1)
    assert result["sender_id"] == "wxid_actual"
    assert result["sender_name"] == "真实发言者"
    assert result["text"] == payload["quoteReply"]["reply"] == payload["content"]
    assert result["message_type"] == "quote" and result["mentioned"]
    assert result["quoted_message"] == {"sender_id": "wxid_quoted", "sender_name": "引用者",
        "server_message_id": "server-123", "sent_at": "2026-10-21T12:00:00+08:00",
        "text": "原来的完整文本", "complete": True}


@pytest.mark.parametrize("mutation", ["missing_version", "wrong_reply", "truncated", "missing_reference", "media"])
def test_incomplete_quote_or_nontext_media_is_rejected(mutation):
    payload = native_quote()
    if mutation == "missing_version":
        del payload["quoteReply"]["version"]
    elif mutation == "wrong_reply":
        payload["quoteReply"]["reply"] = "与原始content不同的文字"
    elif mutation == "truncated":
        payload["quoteReply"]["truncated"] = True
    elif mutation == "missing_reference":
        del payload["quoteReply"]["reference"]
    else:
        payload["type"] = 3
    assert incoming(payload, native_client(), payload["timestamp"] - 1) is None


@pytest.mark.parametrize("changes", [
    {"conversationId": "elsewhere@chatroom"}, {"kind": "3"},
    {"truncated": True}, {"unavailable": True}, {"senderId": ""},
])
def test_unusable_reference_does_not_enter_quote_but_current_reply_survives(changes):
    payload = native_quote()
    payload["quoteReply"]["reference"].update(changes)
    result = incoming(payload, native_client(), payload["timestamp"] - 1)
    assert result["sender_id"] == "wxid_actual"
    assert result["text"] == payload["content"]
    assert "quoted_message" not in result


def cached_payload(bridge, key):
    with closing(sqlite3.connect(bridge.path)) as db:
        return json.loads(db.execute("SELECT payload FROM recent_messages WHERE id=?", (key,)).fetchone()[0])


async def test_failed_reply_retry_keeps_first_quote_observation_when_original_arrives_later(bridge, message):
    class FailOnce(FixtureProvider):
        async def generate(self, request):
            if not getattr(self, "failed", False):
                self.failed = True
                raise RuntimeError("synthetic provider interruption")
            return await super().generate(request)

    bridge.provider_factory = lambda scope: FailOnce()
    original = message.model_copy(update={"message_id": "late-original", "mentioned": False,
        "sender_id": "other-member", "sender_name": "原作者", "text": "今晚去吃红烧肉"})
    quote = {"sender_id": original.sender_id, "sender_name": original.sender_name,
        "server_message_id": "unmapped-server-id", "sent_at": original.sent_at,
        "text": original.text, "complete": True}
    reply = Incoming.model_validate({**message.model_dump(), "message_id": "retry-quote",
        "text": "小温暖，这件事你怎么看", "message_type": "quote", "quoted_message": quote})
    with pytest.raises(RuntimeError, match="synthetic provider interruption"):
        await bridge.receive(reply)
    before = cached_payload(bridge, reply.key)
    assert before["reply_to_id"] is None
    assert before["quote_resolution"] == "unverified_quote_not_original_member_evidence"
    assert (await bridge.receive(original))["status"] == "ignored"
    assert (await bridge.receive(reply))["status"] == "prepared"
    assert cached_payload(bridge, reply.key) == before
    with closing(sqlite3.connect(bridge.path)) as db:
        assert db.execute("SELECT count(*) FROM recent_messages").fetchone()[0] == 2
    stored = bridge.apps[message.scope].store.get_record(reply.key)
    assert stored.payload["channel_source"]["reply_to_id"] is None


async def test_bridge_links_uniquely_observed_quote_to_real_native_message_id(bridge, message):
    original = message.model_copy(update={"message_id": "actual-native-original", "mentioned": False,
        "sender_id": "original-member", "sender_name": "原作者", "text": "那今晚还是去吃红烧肉吧"})
    assert (await bridge.receive(original))["status"] == "ignored"
    quote = {"sender_id": original.sender_id, "sender_name": "引用里伪造昵称",
             "server_message_id": "server-id-not-native-id", "sent_at": original.sent_at,
             "text": original.text, "complete": True}
    reply = Incoming.model_validate({**message.model_dump(), "message_id": "quote-reply",
        "mentioned": False, "text": "这个我赞成", "message_type": "quote", "quoted_message": quote})
    assert (await bridge.receive(reply))["status"] == "ignored"
    saved = cached_payload(bridge, reply.key)
    assert saved["reply_to_id"] == original.message_id
    assert saved["reply_to_id"] != quote["server_message_id"]
    assert saved["quote_resolution"] == "matched_one_original_transport_message_in_this_group"
    assert bridge.identities.resolve_name(message.scope, "引用里伪造昵称")["status"] == "unconfirmed"
    assert bridge.topics.lookup(message.scope, reply.message_id) == bridge.topics.lookup(message.scope, original.message_id)


@pytest.mark.parametrize("case", ["missing", "duplicate", "other_group"])
async def test_bridge_unmatched_or_ambiguous_quote_is_not_identity_evidence(bridge, message, case):
    original = message.model_copy(update={"message_id": "older", "mentioned": False,
        "sender_id": "unverified-person", "sender_name": "某个名字", "text": "我喜欢红茶"})
    if case == "duplicate":
        await bridge.receive(original)
        await bridge.receive(original.model_copy(update={"message_id": "repeated-same-text"}))
    elif case == "other_group":
        await bridge.receive(original.model_copy(update={"group_id": "room2"}))
    quote = {"sender_id": original.sender_id, "sender_name": "只在引用里出现的人",
             "server_message_id": "fake-server-1", "text": original.text, "complete": True}
    reply = Incoming.model_validate({**message.model_dump(), "message_id": "unverified-quote",
        "mentioned": False, "text": "原来如此", "message_type": "quote", "quoted_message": deepcopy(quote)})
    assert (await bridge.receive(reply))["status"] == "ignored"
    saved = cached_payload(bridge, reply.key)
    assert not saved.get("reply_to_id")
    assert saved["quote_resolution"] == "unverified_quote_not_original_member_evidence"
    assert saved["sender_id"] == message.sender_id
    assert bridge.identities.resolve_name(message.scope, "只在引用里出现的人")["status"] == "unconfirmed"
    if case != "duplicate":
        with closing(sqlite3.connect(bridge.identities.path)) as db:
            assert db.execute("SELECT COUNT(*) FROM members WHERE scope=? AND member_id=?",
                              (message.scope, original.sender_id)).fetchone()[0] == 0


async def test_actual_astrbot_at_and_reply_metadata_use_current_component_api(tmp_path):
    python = PROJECT / "upstream/AstrBot/.venv/Scripts/python.exe"
    if not python.exists():
        pytest.skip("Pinned AstrBot runtime is unavailable")
    script = r'''
import asyncio
import importlib.util
import sys
from pathlib import Path
import httpx
from astrbot.api.message_components import At, Reply
root = Path(sys.argv[1])
sys.path.insert(0, str(root))
spec = importlib.util.spec_from_file_location("smoke", root / "scripts/smoke_plugin.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)
spec = importlib.util.spec_from_file_location("astrbot_plugin_myatri.main", root / "astrbot_plugin_myatri/main.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
async def run():
    observed = []
    async def capture(request):
        observed.append(__import__("json").loads(request.content))
        return httpx.Response(200, json={"status": "ignored"})
    plugin = module.MyatriPlugin(None, {"enabled": True, "allowed_groups": ["fixture|room1"]})
    plugin.client = httpx.AsyncClient(base_url="http://fixture.invalid", transport=httpx.MockTransport(capture))
    event = smoke.make_event("metadata-only")
    event.message_obj.message = [Reply(id="prior-native-id"), At(qq="bot"), At(qq="other-member"), *event.message_obj.message]
    await plugin.group_message(event)
    assert event.is_stopped() and not event.sent
    assert len(observed) == 1
    payload = observed[0]
    assert payload["reply_to_id"] == "prior-native-id"
    assert payload["mentioned_ids"] == ["bot", "other-member"]
    assert payload["mentioned"] is True and payload["sender_id"] == "member1"
    await plugin.terminate()
asyncio.run(run())
'''
    env = {**os.environ, "ASTRBOT_ROOT": str(tmp_path / "astrbot"), "PYTHONUTF8": "1"}
    process = await asyncio.create_subprocess_exec(str(python), "-c", script, str(PROJECT),
        cwd=tmp_path, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await asyncio.wait_for(process.communicate(), 90)
    assert process.returncode == 0, (stdout + stderr).decode("utf-8", errors="replace")

import json
from pathlib import Path
import sys
import time

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astrbot_plugin_myatri.wechatagent_client import (  # noqa: E402
    NativeClient,
    NotReady,
    Settings,
    incoming,
    loopback_origin,
)


def settings():
    return Settings(
        "http://127.0.0.1:5031",
        "r" * 48,
        "http://127.0.0.1:30001",
        "a" * 64,
        "测试群",
        "小溫暖",
    )


def backend(
    *, version="4.1.10.27", accounts=None, duplicate=False, receipt=True, sent_flag=True
):
    calls = []

    def handler(request):
        calls.append(request)
        path = request.url.path
        if path == "/api/v1/reader/status":
            body = {
                "success": True,
                "backend": "weflow-cli-nt",
                "accountDirectory": "wxid_bot_abcd",
                "ownWxid": "wxid_bot",
            }
        elif path == "/WeChatAgent/health":
            body = {
                "integration": "WeChatAgent-1",
                "backend": "wechat-hook",
                "version": version,
                "databaseAccounts": accounts
                if accounts is not None
                else ["wxid_bot_abcd"],
            }
        elif path == "/api/v1/sessions":
            body = {
                "sessions": [{"username": "room@chatroom", "displayName": "测试群"}]
                + (
                    [{"username": "other@chatroom", "displayName": "测试群"}]
                    if duplicate
                    else []
                )
            }
        elif path == "/SendTextMsg":
            assert request.headers["X-WeChatAgent-Account"] == "wxid_bot_abcd"
            assert json.loads(request.content)["wxidorgid"] == "room@chatroom"
            body = {"ret": 0}
        elif path == "/api/v1/messages":
            sent = any(r.url.path == "/SendTextMsg" for r in calls)
            body = {
                "messages": (
                    [
                        {
                            "localId": 7,
                            "localType": 1,
                            "createTime": int(time.time()),
                            "isSend": sent_flag,
                            "fullText": "你好",
                            "serverId": "9007199254740993",
                        }
                    ]
                    if sent and receipt
                    else []
                )
            }
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler), calls


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"version": "4.1.15.13"}, "HOOK_VERSION_MISMATCH"),
        ({"accounts": ["someone_else"]}, "HOOK_ACCOUNT_MISMATCH"),
        ({"accounts": ["wxid_bot_abcd", "another"]}, "HOOK_ACCOUNT_MISMATCH"),
        ({"duplicate": True}, "GROUP_MAPPING_AMBIGUOUS_OR_MISSING"),
    ],
)
async def test_account_version_and_ambiguous_group_cannot_send(kwargs, code):
    transport, calls = backend(**kwargs)
    client = NativeClient(settings(), transport=transport)
    try:
        with pytest.raises(NotReady, match=code):
            await client.bind()
        assert not any(r.method == "POST" for r in calls)
    finally:
        await client.close()


async def test_receipt_and_unknown_outcome_submit_exactly_once():
    for receipt in (True, False):
        transport, calls = backend(receipt=receipt)
        client = NativeClient(settings(), transport=transport, receipt_timeout=0)
        try:
            await client.bind()
            if receipt:
                await client.send("你好")
            else:
                with pytest.raises(NotReady, match="SEND_RECEIPT_UNKNOWN"):
                    await client.send("你好")
            assert sum(r.url.path == "/SendTextMsg" for r in calls) == 1
        finally:
            await client.close()


@pytest.mark.parametrize("flag", [1, "1", "true"])
async def test_native_receipt_accepts_database_integer_but_not_strings(flag):
    transport, _ = backend(sent_flag=flag)
    client = NativeClient(settings(), transport=transport, receipt_timeout=0)
    try:
        await client.bind()
        if flag == 1:
            await client.send("你好")
        else:
            with pytest.raises(NotReady, match="SEND_RECEIPT_UNKNOWN"):
                await client.send("你好")
    finally:
        await client.close()


async def test_stable_binding_survives_group_rename():
    transport, _ = backend(duplicate=True)
    config = settings()
    config.group_id = "room@chatroom"
    config.group_name = "旧名称"
    client = NativeClient(config, transport=transport)
    try:
        assert await client.bind() == "room@chatroom"
    finally:
        await client.close()


async def test_event_identity_time_wake_and_rejection():
    transport, _ = backend()
    client = NativeClient(settings(), transport=transport)
    try:
        await client.bind()
        now = int(time.time())
        payload = {
            "event": "message.new",
            "sessionType": "group",
            "sessionId": "room@chatroom",
            "talkerId": "wxid_member",
            "rawid": "native-message-1",
            "type": 1,
            "timestamp": now,
            "content": "@小溫暖\u2005你好",
            "sourceName": "同名群友",
        }
        result = incoming(payload, client, now - 1)
        assert result["sender_id"] == "wxid_member"
        assert (
            result["timestamp"] == now
            and result["text"] == payload["content"]
            and result["mentioned"]
        )
        for update in [
            {"talkerId": "wxid_bot"},
            {"talkerId": ""},
            {"rawid": ""},
            {"sessionId": "other@chatroom"},
            {"timestamp": now - 10},
            {"timestamp": True},
            {"isSend": "false"},
            {"type": 3},
        ]:
            assert incoming({**payload, **update}, client, now - 1) is None
        result = incoming({**payload, "content": "@小溫暖abc"}, client, now - 1)
        assert not result["mentioned"]
    finally:
        await client.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1",
        "http://localhost:5031",
        "http://127.0.0.1@evil.test",
        "http://127.0.0.1:5031/secret",
        "http://127.0.0.1:5031?token=a",
    ],
)
def test_credentials_cannot_be_sent_to_other_origins(url):
    with pytest.raises(NotReady):
        loopback_origin(url)

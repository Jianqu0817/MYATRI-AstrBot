"""Run the actual AstrBot API against a loopback fixture bridge; never log in."""

import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.api.message_components import At, Plain
from astrbot.core.star.star_handler import star_handlers_registry


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class CapturedEvent(AstrMessageEvent):
    async def send(self, message):
        self.sent.append(message)


def make_event(mid, *, mentioned=True, fail=False):
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE
    message.self_id = "bot"
    message.session_id = "room1"
    message.message_id = mid
    message.group_id = "room1"
    message.sender = MessageMember("member1", "甲")
    message.message = ([At(qq="bot")] if mentioned else []) + [
        Plain("今天想安静待一会儿。")
    ]
    message.timestamp = 1792518125
    message.message_str = "今天想安静待一会儿。"
    event = CapturedEvent(
        message.message_str,
        message,
        PlatformMetadata("fixture", "fixture", "fixture"),
        "room1",
    )
    event.sent = []
    if fail:

        async def failed_send(body):
            raise TimeoutError("fixture send timeout")

        event.send = failed_send
    return event


async def run(args):
    spec = importlib.util.spec_from_file_location(
        "astrbot_plugin_myatri.main", ROOT / "astrbot_plugin_myatri/main.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = {
        "enabled": True,
        "allowed_groups": ["fixture|room1"],
        "bridge_url": args.url,
        "token_file": args.token_file,
        "trigger_prefix": "/warm ",
    }
    plugin = module.MyatriPlugin(None, config)
    await plugin.initialize()
    try:
        first = make_event("real-plugin-1")
        await plugin.group_message(first)
        assert first.is_stopped() and 1 <= len(first.sent) <= 3
        assert all("<|msg|>" not in part.text for sent in first.sent for part in sent.chain if isinstance(part, Plain))
        duplicate = make_event("real-plugin-1")
        await plugin.group_message(duplicate)
        assert duplicate.is_stopped() and not duplicate.sent
        ordinary = make_event("real-plugin-ignored", mentioned=False)
        await plugin.group_message(ordinary)
        assert ordinary.is_stopped() and not ordinary.sent
        failed = make_event("real-plugin-unknown", fail=True)
        await plugin.group_message(failed)
        replay = make_event("real-plugin-unknown")
        await plugin.group_message(replay)
        assert not replay.sent
        assert any(h.handler_name == "group_message" for h in star_handlers_registry)
        from astrbot_plugin_myatri.wechatagent_platform import WeChatAgentPlatform

        captured_native = []

        async def native_send(text):
            captured_native.append(text)

        platform = WeChatAgentPlatform({"id": "fixture"}, {}, asyncio.Queue())
        platform.native = SimpleNamespace(
            bot_id="bot",
            group_id="room1",
            settings=SimpleNamespace(bot_nickname="小溫暖"),
            send=native_send,
        )
        native_payload = {
            "event": "message.new",
            "sessionType": "group",
            "sessionId": "room1",
            "talkerId": "wxid_group_member",
            "rawid": "real-native-platform-1",
            "type": 1,
            "timestamp": int(time.time()),
            "content": "/warm 今天想喝红茶。",
            "sourceName": "同名群友",
        }
        native_event = platform.event(native_payload)
        assert native_event.get_sender_id() == "wxid_group_member"
        assert native_event.message_obj.timestamp == native_payload["timestamp"]
        await plugin.group_message(native_event)
        assert native_event.is_stopped() and 1 <= len(captured_native) <= 3
        assert all("<|msg|>" not in text for text in captured_native)
        native_count = len(captured_native)
        await plugin.group_message(platform.event(native_payload))
        assert len(captured_native) == native_count
        print(
            json.dumps(
                {
                    "actual_astrbot_plugin_loaded": True,
                    "captured_sends": len(first.sent),
                    "duplicate_sends": len(duplicate.sent),
                    "unknown_not_resent": True,
                    "external_messages_sent": 0,
                    "native_platform_captured_sends": len(captured_native),
                }
            )
        )
    finally:
        await plugin.terminate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--token-file", required=True)
    asyncio.run(run(parser.parse_args()))

"""AstrBot platform for the local WeChatAgent reader and native sender."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import time

import httpx
from astrbot.api import logger
from astrbot.api.message_components import At, Plain
from astrbot.api.platform import (
    AstrBotMessage,
    AstrMessageEvent,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)

from .wechatagent_client import NativeClient, NotReady, Settings, incoming


class WeChatAgentEvent(AstrMessageEvent):
    async def send(self, message):
        if not message.chain or any(
            not isinstance(part, Plain) for part in message.chain
        ):
            raise NotReady("TEXT_ONLY")
        # The bridge claims the durable delivery before reaching this method.
        # Unknown outcomes are never automatically retried by this adapter.
        await self.native_client.send("".join(part.text for part in message.chain))


@register_platform_adapter(
    "myatri_wechatagent",
    "WeChatAgent 本机数据库与原生发送接口",
    default_config_tmpl={
        "settings_file": "D:/DS_workspace/MYATRI-AstrBot/config/wechatagent.local.toml"
    },
    adapter_display_name="小温暖 · WeChatAgent",
    support_streaming_message=False,
)
class WeChatAgentPlatform(Platform):
    def __init__(self, platform_config, platform_settings, event_queue):
        super().__init__(platform_config, event_queue)
        self.native = None
        self.stopped = False
        self.started_at = int(time.time())
        self.observe_only = os.environ.get("MYATRI_WECHAT_OBSERVE_ONLY") == "1"
        self.metadata = PlatformMetadata(
            name="myatri_wechatagent",
            description="Local WeChatAgent transport",
            id=platform_config["id"],
            support_streaming_message=False,
            support_proactive_message=False,
        )

    def meta(self):
        return self.metadata

    def event(self, payload):
        item = incoming(payload, self.native, self.started_at)
        if item is None:
            return None
        message = AstrBotMessage()
        message.type = MessageType.GROUP_MESSAGE
        message.self_id = self.native.bot_id
        message.group_id = self.native.group_id
        message.session_id = self.native.group_id
        message.message_id = item["message_id"]
        message.sender = MessageMember(item["sender_id"], item["sender_name"])
        message.message = ([At(qq=message.self_id)] if item["mentioned"] else []) + [
            Plain(item["text"])
        ]
        message.message_str = item["text"]
        message.timestamp = item["timestamp"]
        # Preserve the original reader event locally; do not expose credentials.
        message.raw_message = payload
        event = WeChatAgentEvent(
            message.message_str, message, self.meta(), message.session_id
        )
        event.native_client = self.native
        return event

    async def run(self):
        settings = Settings.load(Path(self.config["settings_file"]))
        if not settings.group_id or not settings.bot_id:
            raise NotReady("RUN_CHECK_WECHAT_CONFIGURE_FIRST")
        self.native = NativeClient(settings)
        try:
            await self.native.bind()
            self.client_self_id = self.native.bot_id
            logger.info(
                "MYATRI WeChatAgent connected; observe_only=%s", self.observe_only
            )
            while not self.stopped:
                # Account/version failures stop the platform, rather than reconnecting blindly.
                await self.native.check()
                try:
                    async for payload in self.native.events():
                        event = self.event(payload)
                        if event:
                            await self.native.check()
                            if self.observe_only:
                                logger.info(
                                    "MYATRI WeChatAgent: observed target-group event; no pipeline dispatch"
                                )
                                continue
                            self.commit_event(event)
                    if not self.stopped:
                        raise httpx.ReadError("reader stream closed")
                except httpx.TransportError:
                    logger.warning("MYATRI WeChatAgent: reader stream interrupted")
                    await asyncio.sleep(3)
        finally:
            await self.native.close()

    async def terminate(self):
        self.stopped = True
        if self.native:
            await self.native.close()

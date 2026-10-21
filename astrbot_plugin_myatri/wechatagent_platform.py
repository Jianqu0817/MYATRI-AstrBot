"""AstrBot platform for the local WeChatAgent reader and native sender."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import time
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    # 仅用于类型标注：运行期导入会绑定到 AstrBot 的模块路径，避免插件加载顺序问题。
    from astrbot.api.event import MessageChain
    from astrbot.core.platform.message_session import MessageSesion


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
            support_proactive_message=True,
        )

    def meta(self):
        return self.metadata

    async def send_by_session(
        self, session: MessageSesion, message_chain: MessageChain
    ) -> None:
        """主动发送：不依赖事件对象，复用 NativeClient.send 的回执验证。

        Args:
            session: 目标会话（AstrBot 的 unified_msg_origin / MessageSesion）。
            message_chain: 消息链；本适配器只接受纯文本（Plain），其他类型抛
                ``NotReady("TEXT_ONLY")``。

        Raises:
            NotReady: 非文本消息、平台未绑定，或发送前账号/版本校验失败。
        """
        await super().send_by_session(session, message_chain)
        chain = getattr(message_chain, "chain", None)
        if not chain or any(not isinstance(part, Plain) for part in chain):
            raise NotReady("TEXT_ONLY")
        text = "".join(part.text for part in chain)
        if not text.strip():
            raise NotReady("TEXT_ONLY")
        if self.native is None:
            raise NotReady("NATIVE_NOT_BOUND")
        # 发送前再校验账号与版本；NativeClient.send 内部还会独立校验一次。
        await self.native.check()
        await self.native.send(text)

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
        message.raw_message = {**payload, "_myatri_quote": item.get("quoted_message"),
                               "_myatri_message_type": item.get("message_type", "text")}
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

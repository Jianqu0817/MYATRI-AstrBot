from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlparse

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.star import Context, Star

# Import registers the platform before AstrBot instantiates configured adapters.
from . import wechatagent_platform  # noqa: F401


def group_scope(platform_id, bot_id, group_id):
    """与桥接 ``Incoming.scope`` 完全一致的摘要。

    桥接侧用 ``myatri.contracts.digest([...])[:32]``；这里在插件进程内复算同一套
    规范化 JSON 与 SHA-256，避免两进程各算一套导致查不到待投递。

    Args:
        platform_id: AstrBot 平台实例 ID。
        bot_id: 机器人自身 ID。
        group_id: 群 ID。

    Returns:
        32 位小写十六进制的作用域摘要。
    """
    canonical = json.dumps(
        [platform_id, bot_id, group_id],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def delivery_error_code(error, stage):
    """Only enumerated diagnostics cross the bridge; never exception contents."""
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if stage == "ack":
        return "ack_error"
    if type(error).__name__ == "NotReady":
        code = str(error)
        if code == "SEND_RECEIPT_UNKNOWN":
            return "receipt_timeout"
        if code == "NATIVE_SEND_REJECTED":
            return "native_rejected"
        return "binding_unavailable"
    if isinstance(error, httpx.HTTPError):
        return "transport_error"
    return "send_error"


class MyatriPlugin(Star):
    """Forward allowed group text for local trigger selection and recent context."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.client = None
        self.inflight = set()
        self.delivery_locks = {}
        self.proactive_task = None
        # route -> {"scope": 32位摘要, "session": unified_msg_origin}
        # 只从真实群事件学得，不猜平台内部结构；群内安静时本来也不该主动开口。
        self.proactive_sessions = {}

    async def initialize(self):
        url = self.config.get("bridge_url", "http://127.0.0.1:8766").rstrip("/")
        parsed = urlparse(url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("bridge_url must be a localhost HTTP origin")
        if not self.config.get("enabled", False):
            return
        token = Path(self.config["token_file"]).read_text(encoding="ascii").strip()
        if len(token) < 32:
            raise ValueError("invalid bridge token")
        self.client = httpx.AsyncClient(
            base_url=url,
            headers={"Authorization": "Bearer " + token},
            timeout=600,
            trust_env=False,
            follow_redirects=False,
        )
        # Task reports are requested replies, independent of casual proactive speech.
        if self.proactive_task is None:
            self.proactive_task = asyncio.create_task(self._proactive_loop())

    async def _proactive_loop(self):
        """轮询桥接的待投递主动发言。

        桥接只把 ``status='prepared'`` 的投递交给插件；claim/ack 语义与被动回复
        完全一致，所以 ``unknown`` 一样不会被重投。
        """
        while True:
            await asyncio.sleep(10)
            for route, target in list(self.proactive_sessions.items()):
                try:
                    await self._deliver_pending(route, target)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # 只记录枚举诊断，不带任何消息内容。
                    logger.warning(
                        "MYATRI proactive poll stopped: %s",
                        delivery_error_code(exc, "poll"),
                    )

    async def _deliver_pending(self, route, target):
        """取出该 scope 的待投递并逐条 claim→发送→逐段 ack。"""
        if self.client is None or self.context is None:
            return
        params = {"scope": target["scope"]}
        if not self.config.get('proactive_enabled', False):
            params['kind'] = 'computer_report'
        response = await self.client.get("/v1/deliveries/pending", params=params)
        response.raise_for_status()
        deliveries = response.json().get("deliveries")
        if not isinstance(deliveries, list):
            raise ValueError("invalid pending response")
        for delivery in deliveries[:3]:
            if not isinstance(delivery, dict):
                raise ValueError("invalid pending delivery")
            delivery_id = delivery.get("id")
            replies = delivery.get("reply_parts")
            if (
                not isinstance(delivery_id, str)
                or not delivery_id
                or not isinstance(replies, list)
                or not 1 <= len(replies) <= 3
                or any(not isinstance(text, str) or not text.strip() for text in replies)
            ):
                raise ValueError("invalid pending delivery")
            async with self.delivery_locks.setdefault(route, asyncio.Lock()):
                claim = await self.client.post(f"/v1/deliveries/{delivery_id}/claim")
                claim.raise_for_status()
                if not claim.json().get("claimed"):
                    continue
                await self._send_parts(delivery_id, replies, target["session"])

    async def _send_parts(self, delivery_id, replies, session):
        """按段发送并逐段 ack；任一段失败立刻记 ``unknown``，绝不重投。"""
        part_started = None
        stage = "send"
        try:
            for index, text in enumerate(replies):
                part_started = time.monotonic()
                stage = "send"
                # 平台 send_by_session 只接受纯文本，并在 NativeClient.send 内做回执验证。
                sent = await self.context.send_message(
                    session, MessageChain([Plain(text)])
                )
                if not sent:
                    raise RuntimeError("platform session not found")
                elapsed = min(3600.0, time.monotonic() - part_started)
                stage = "ack"
                ack = await self.client.post(
                    f"/v1/deliveries/{delivery_id}/ack",
                    json={"status": "sent", "part_index": index, "elapsed_seconds": elapsed},
                )
                ack.raise_for_status()
        except (Exception, asyncio.CancelledError) as exc:
            reason = delivery_error_code(exc, stage)
            logger.warning("MYATRI proactive delivery stopped: %s", reason)
            try:
                await self.client.post(
                    f"/v1/deliveries/{delivery_id}/ack",
                    json={
                        "status": "unknown",
                        "reason_code": reason,
                        "elapsed_seconds": min(3600.0, time.monotonic() - part_started)
                        if part_started is not None
                        else None,
                    },
                )
            except Exception:
                pass
            if isinstance(exc, asyncio.CancelledError):
                raise

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10000)
    async def group_message(self, event: AstrMessageEvent):
        if not self.config.get("enabled", False):
            return
        route = f"{event.get_platform_id()}|{event.get_group_id()}"
        if route not in self.config.get("allowed_groups", []):
            return
        # Own the configured routes, including errors and untriggered messages.
        # Otherwise AstrBot's default LLM could produce a second response.
        event.stop_event()
        if self.client is None:
            logger.warning("MYATRI bridge is enabled but not initialized")
            return
        bot_id = event.get_self_id()
        sender_id = event.get_sender_id()
        message_id = getattr(event.message_obj, "message_id", "")
        if not bot_id or not sender_id or not message_id or sender_id == bot_id:
            return
        # 记住该群的会话与作用域，供主动发言轮询使用；没有群事件时不猜平台内部结构。
        self.proactive_sessions[route] = {
            "scope": group_scope(
                event.get_platform_id(), str(bot_id), event.get_group_id()
            ),
            "session": event.unified_msg_origin,
        }
        parts = event.get_messages()
        text = "".join(part.text for part in parts if isinstance(part, Plain))
        prefix = self.config.get("trigger_prefix", "/warm ")
        mentioned = any(
            isinstance(part, At) and str(part.qq) == str(bot_id) for part in parts
        )
        if prefix and text.startswith(prefix):
            mentioned = True
            text = text[len(prefix) :]
        if not text.strip():
            return
        fingerprint = (
            event.get_platform_id(),
            str(bot_id),
            event.get_group_id(),
            str(message_id),
        )
        if fingerprint in self.inflight:
            return
        timestamp = getattr(event.message_obj, "timestamp", None)
        if not isinstance(timestamp, (int, float)):
            return
        payload = {
            "platform_id": event.get_platform_id(),
            "bot_id": str(bot_id),
            "group_id": event.get_group_id(),
            "sender_id": str(sender_id),
            "sender_name": event.get_sender_name(),
            "message_id": str(message_id),
            "sent_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            "text": text,
            "mentioned": mentioned,
        }
        components = event.get_messages()
        reply = next((part for part in components if isinstance(part, Reply)), None)
        if reply and reply.id:
            payload["reply_to_id"] = str(reply.id)
        mentioned_ids = [str(part.qq) for part in components if isinstance(part, At) and part.qq]
        if mentioned_ids:
            payload["mentioned_ids"] = list(dict.fromkeys(mentioned_ids))[:30]
        raw = getattr(event.message_obj, "raw_message", None)
        if event.get_platform_name() == "myatri_wechatagent" and isinstance(raw, dict):
            if raw.get("_myatri_quote"):
                payload["quoted_message"] = raw["_myatri_quote"]
            payload["message_type"] = raw.get("_myatri_message_type", "text")
        self.inflight.add(fingerprint)
        delivery_id = None
        claimed = False
        part_started = None
        stage = "prepare"
        try:
            response = await self.client.post("/v1/messages", json=payload)
            response.raise_for_status()
            result = response.json()
            if result.get("status") != "prepared":
                return
            delivery_id = result["id"]
            replies = result.get("reply_parts", [result["reply"]])
            if (not isinstance(replies, list) or not 1 <= len(replies) <= 3
                    or any(not isinstance(text, str) or not text.strip() for text in replies)):
                raise ValueError("invalid reply parts")
            async with self.delivery_locks.setdefault(route, asyncio.Lock()):
                claim = await self.client.post(f"/v1/deliveries/{delivery_id}/claim")
                claim.raise_for_status()
                if not claim.json().get("claimed"):
                    return
                claimed = True
                # Hold the route lock for the whole batch. The WeChat adapter
                # awaits its receipt before event.send returns; do not resend on timeout.
                for index, text in enumerate(replies):
                    part_started = time.monotonic()
                    stage = "send"
                    await event.send(event.plain_result(text))
                    elapsed = min(3600., time.monotonic() - part_started)
                    stage = "ack"
                    ack = await self.client.post(
                        f"/v1/deliveries/{delivery_id}/ack",
                        json={"status": "sent", "part_index": index, "elapsed_seconds": elapsed}
                    )
                    ack.raise_for_status()
        except (Exception, asyncio.CancelledError) as exc:
            reason = delivery_error_code(exc, stage)
            logger.warning("MYATRI bridge stopped this reply: %s", reason)
            if claimed:
                try:
                    await self.client.post(
                        f"/v1/deliveries/{delivery_id}/ack", json={"status": "unknown", "reason_code": reason,
                            "elapsed_seconds": min(3600., time.monotonic() - part_started) if part_started is not None else None}
                    )
                except Exception:
                    pass
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            self.inflight.discard(fingerprint)

    async def terminate(self):
        if self.proactive_task is not None:
            # 干净取消：先请求取消，再等它退出，避免关闭时留下悬挂的 poll 请求。
            self.proactive_task.cancel()
            await asyncio.gather(self.proactive_task, return_exceptions=True)
            self.proactive_task = None
        if self.client:
            await self.client.aclose()

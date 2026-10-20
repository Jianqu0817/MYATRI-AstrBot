from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.star import Context, Star

# Import registers the platform before AstrBot instantiates configured adapters.
from . import wechatagent_platform  # noqa: F401


class MyatriPlugin(Star):
    """Forward allowed group text for local trigger selection and recent context."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.client = None
        self.inflight = set()

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
        try:
            response = await self.client.post("/v1/messages", json=payload)
            response.raise_for_status()
            result = response.json()
            if result.get("status") != "prepared":
                return
            delivery_id = result["id"]
            claim = await self.client.post(f"/v1/deliveries/{delivery_id}/claim")
            claim.raise_for_status()
            if not claim.json().get("claimed"):
                return
            claimed = True
            # Direct send is intentional: stop_event has already closed the pipeline.
            await event.send(event.plain_result(result["reply"]))
            ack = await self.client.post(
                f"/v1/deliveries/{delivery_id}/ack", json={"status": "sent"}
            )
            ack.raise_for_status()
        except (Exception, asyncio.CancelledError) as exc:
            logger.warning("MYATRI bridge stopped this reply: %s", type(exc).__name__)
            if claimed:
                try:
                    await self.client.post(
                        f"/v1/deliveries/{delivery_id}/ack", json={"status": "unknown"}
                    )
                except Exception:
                    pass
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            self.inflight.discard(fingerprint)

    async def terminate(self):
        if self.client:
            await self.client.aclose()

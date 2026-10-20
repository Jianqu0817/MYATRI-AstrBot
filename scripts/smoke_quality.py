"""At most four explicitly requested live API calls, synthetic data, zero channel sends."""
import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import tomllib

from myatri.app import load_config
from myatri.providers import DeepSeekProvider, Gateway
from myatri_bridge.bridge import Bridge, Incoming
from myatri_bridge.memory_review import review_summary


ROOT = Path(__file__).resolve().parents[1]


class LimitedProvider:
    name = "deepseek"

    def __init__(self, provider):
        self.provider, self.calls = provider, 0
        self.model = provider.model

    async def generate(self, request):
        if self.calls >= 4:
            raise RuntimeError("synthetic smoke API call cap reached")
        self.calls += 1
        return await self.provider.generate(request)


async def smoke():
    settings = tomllib.loads((ROOT / "config/bridge.local.toml").read_text(encoding="utf-8-sig"))["bridge"]
    if settings.get("mode") != "live":
        raise RuntimeError("existing local configuration has not enabled live mode")
    engine = load_config(ROOT / settings["engine_config"])
    engine["budget"]["daily_tokens"] = 50000
    provider = LimitedProvider(DeepSeekProvider(model=engine["chat"]["model"], endpoint=engine["chat"]["endpoint"],
        allow_paid=True, capabilities_verified=engine["chat"].get("capabilities_verified", False)))
    gateway = Gateway(provider, daily_token_limit=50000, retries=0)
    source = {"message_id": "synthetic1", "sender_id": "synthetic_a", "sender_name": "小禾",
              "sent_at": datetime.now(timezone.utc).isoformat(), "text": "我以前喜欢咖啡，但现在完全不喝咖啡了，改喝红茶。"}
    base = {"speaker_id": "synthetic_a", "subject_id": "synthetic_a", "attribution": "self_report",
            "source_message_ids": [source["message_id"]], "source_quotes": [{"message_id": source["message_id"], "quote": source["text"]}]}
    summary = {"items": [{**deepcopy(base), "text": "小禾现在喜欢喝咖啡"},
                          {**deepcopy(base), "text": "小禾自述现在不喝咖啡，改喝红茶"}],
               "generator": {"run_id": "synthetic_support_smoke"}}
    reviewed = await review_summary(summary, [source], gateway)
    checks = {row["index"]: row["verdict"] for row in reviewed["review"]["checks"]}
    report = {"data": "synthetic_only", "wechat_sends": 0, "support_review": checks,
              "support_check_passed": checks.get(0) != "supported" and checks.get(1) == "supported"}
    with tempfile.TemporaryDirectory(prefix="myatri-live-synthetic-") as folder:
        instance = Bridge(Path(folder), {"mode": "offline", "allowed_groups": ["synthetic|room"],
            "random_reply_probability": 0, "cooldown_seconds": 0, "quality_guard_enabled": True,
            "owner_id": "synthetic_owner", "owner_name": "合成原对话人"}, engine,
            ROOT / settings["persona_file"], provider_factory=lambda _: provider)
        try:
            member = Incoming(platform_id="synthetic", bot_id="synthetic_bot", group_id="room",
                sender_id="synthetic_a", sender_name="小禾", message_id="one", sent_at=source["sent_at"],
                text="我喜欢红茶。", mentioned=False)
            await instance.receive(member)
            trigger = member.model_copy(update={"sender_id": "synthetic_b", "sender_name": "小舟",
                "message_id": "two", "mentioned": True,
                "text": "我是小舟，喜欢咖啡，红茶是小禾喜欢的。记得把我们分开，简单接一句就好。"})
            answer = await instance.receive(trigger)
            app = instance.apps[trigger.scope]
            app.gateway.retries = 0
            report.update(reply=answer.get("reply"), generation_status=answer["status"],
                          reply_guard=getattr(app.conversation.reply_guard, "last_check", None))
        finally:
            await instance.close()
    report.update(api_calls=provider.calls, maximum_api_calls=4,
                  limitations="Small synthetic smoke only; not an accuracy, style or market comparison benchmark.")
    target = ROOT / "var/diagnostics/live-quality-smoke-0.2.0.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="explicitly permit the bounded synthetic API test")
    args = parser.parse_args()
    if not args.live:
        parser.error("No API calls made. Pass --live only to run the bounded test.")
    asyncio.run(smoke())

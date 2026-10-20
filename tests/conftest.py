from pathlib import Path
import pytest

from myatri.app import load_config
from myatri.providers import FixtureProvider
from myatri_bridge.bridge import Bridge, Incoming


PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture
def message():
    return Incoming(
        platform_id="fixture",
        bot_id="bot",
        group_id="room1",
        sender_id="member1",
        sender_name="甲",
        message_id="m1",
        sent_at="2026-10-21T01:42:05+08:00",
        text="今天想安静待一会儿。",
        mentioned=True,
    )


@pytest.fixture
async def bridge(tmp_path):
    providers = []

    def factory(scope):
        provider = FixtureProvider(
            [
                {
                    "content": "我陪你安静坐一会儿，今天就不忙着说那么多话了。窗外的事情先放一放，能稍微松口气也很好。"
                }
            ]
        )
        providers.append(provider)
        return provider

    settings = {
        "mode": "offline",
        "allowed_groups": ["fixture|room1", "fixture|room2"],
        "cooldown_seconds": 0,
        "random_reply_probability": 0,
    }
    instance = Bridge(
        tmp_path / "groups",
        settings,
        load_config(PROJECT / "config/engine.toml"),
        PROJECT / "config/persona/persona.md",
        provider_factory=factory,
    )
    instance.test_providers = providers
    yield instance
    await instance.close()

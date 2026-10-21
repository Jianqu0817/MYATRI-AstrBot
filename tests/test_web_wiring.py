"""Wire the out-of-process web search into the bridge, off by default."""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from myatri.app import load_config
from myatri.providers import FixtureProvider
from myatri_bridge.bridge import Bridge, Incoming

PROJECT = Path(__file__).resolve().parents[1]


def _message():
    return Incoming(
        platform_id="fixture",
        bot_id="bot",
        group_id="room1",
        sender_id="member1",
        sender_name="甲",
        message_id="m1",
        sent_at="2026-10-21T01:42:05+08:00",
        text="帮我查一下明天的天气。",
        mentioned=True,
    )


def _bridge(tmp_path, **web):
    providers = []

    def factory(scope):
        provider = FixtureProvider([{"content": "我先看一眼，等我一下。"}])
        providers.append(provider)
        return provider

    settings = {
        "mode": "offline",
        "allowed_groups": ["fixture|room1"],
        "cooldown_seconds": 0,
        "random_reply_probability": 0,
        "recent_context_messages": 20,
        "dialogue_turns": 15,
        "web": web,
    }
    bridge = Bridge(
        tmp_path / "groups",
        settings,
        load_config(PROJECT / "config/engine.toml"),
        PROJECT / "config/persona/persona.md",
        provider_factory=factory,
    )
    bridge.test_providers = providers
    return bridge


async def test_web_search_is_absent_by_default(tmp_path):
    bridge = _bridge(tmp_path)
    try:
        assert bridge.web_enabled is False
        assert bridge._web_tool(bridge.root / "anything") is None
        await bridge.receive(_message())
        app = bridge.apps[_message().scope]
        assert "web_search" not in app.conversation.tool_handlers
        assert not list((bridge.root / _message().scope).glob("web.sqlite3"))
    finally:
        await bridge.close()


def test_enabled_without_backend_paths_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="node_path"):
        _bridge(tmp_path, enabled=True, bin_path="x.js")


async def test_enabled_registers_an_awaitable_handler(tmp_path):
    bridge = _bridge(tmp_path, enabled=True, node_path="node.exe", bin_path="bin.js")
    try:
        await bridge.receive(_message())
        app = bridge.apps[_message().scope]
        tool, handler = app.conversation.tool_handlers["web_search"]
        assert tool["function"]["name"] == "web_search"
        # The handler must be awaitable so the blocking subprocess runs off-loop.
        assert inspect.iscoroutinefunction(handler)
    finally:
        await bridge.close()


async def test_credential_is_handed_to_the_child_explicitly(tmp_path, monkeypatch):
    import myatri.credentials as credentials

    monkeypatch.setattr(credentials, "read_deepseek_key", lambda: "sk-test-fake-0000000000")
    bridge = _bridge(tmp_path, enabled=True, node_path="node.exe", bin_path="bin.js")
    try:
        message = _message()
        await bridge.receive(message)
        _tool, search = bridge.web_tools[bridge.root / message.scope]
        env = search.backend.client.child_environment()
        assert env["DEEPSEEK_API_KEY"] == "sk-test-fake-0000000000"
        # The secret must never appear in the logged command line.
        assert not any("sk-test-fake" in part for part in search.backend.client.describe_command())
    finally:
        await bridge.close()


async def test_missing_credential_leaves_the_child_without_a_key(tmp_path, monkeypatch):
    import myatri.credentials as credentials

    monkeypatch.setattr(credentials, "read_deepseek_key", lambda: None)
    bridge = _bridge(tmp_path, enabled=True, node_path="node.exe", bin_path="bin.js")
    try:
        message = _message()
        await bridge.receive(message)
        _tool, search = bridge.web_tools[bridge.root / message.scope]
        assert "DEEPSEEK_API_KEY" not in search.backend.client.child_environment()
    finally:
        await bridge.close()


def _system_text(bridge):
    messages = bridge.test_providers[-1].requests[-1].messages
    return "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")


async def test_search_guidance_is_injected_only_when_enabled(tmp_path, monkeypatch):
    import myatri.credentials as credentials

    monkeypatch.setattr(credentials, "read_deepseek_key", lambda: None)
    enabled = _bridge(tmp_path / "on", enabled=True, node_path="node.exe", bin_path="bin.js")
    try:
        await enabled.receive(_message())
        assert "web_search 工具" in _system_text(enabled)
    finally:
        await enabled.close()

    disabled = _bridge(tmp_path / "off")
    try:
        await disabled.receive(_message())
        assert "web_search 工具" not in _system_text(disabled)
    finally:
        await disabled.close()

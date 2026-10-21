"""Exercise real provider serialization with local HTTP responses only."""
from copy import deepcopy
import json

import httpx
import pytest

from myatri.app import Application, load_config
from myatri.providers import DeepSeekProvider, FixtureProvider, ProviderError
from myatri_bridge.bridge import Bridge
from myatri_bridge.reading_conversation import ReadingConversation
from conftest import PROJECT


async def test_thinking_http_tools_and_sent_history_survive_restart(tmp_path, message):
    bodies = []

    def transport(request):
        body = json.loads(request.content)
        bodies.append(body)
        assistant = {"role": "assistant", "content": "这会儿陪你安静待着。",
                     "reasoning_content": "fixture-private-final"}
        if len(bodies) == 1:
            assistant = {"role": "assistant", "content": "", "reasoning_content": "fixture-private-tool",
                "tool_calls": [{"id": "read1", "type": "function", "function": {
                    "name": "recall", "arguments": '{"query":"安静"}'}}]}
        return httpx.Response(200, json={"model": "deepseek-flash", "choices": [
            {"message": assistant, "finish_reason": "stop"}], "usage": {
                "prompt_tokens": 2000, "completion_tokens": 520, "total_tokens": 2520,
                "completion_tokens_details": {"reasoning_tokens": 500}}})

    config = load_config(PROJECT / "config/engine.toml")
    # Explicit high remains supported; production chat now defaults to low.
    config["chat"]["reasoning_effort"] = "high"
    settings = {"mode": "offline", "allowed_groups": [message.route], "cooldown_seconds": 0,
                "random_reply_probability": 0, "quality_guard_enabled": False}
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        def factory(scope):
            return DeepSeekProvider(model="deepseek-flash", api_key="synthetic-not-a-secret",
                allow_paid=True, capabilities_verified=True, client=client)

        def build():
            return Bridge(tmp_path / "groups", settings, config,
                          PROJECT / "config/persona/persona.md", provider_factory=factory)

        bridge = build()
        try:
            result = await bridge.receive(message)
            assert result["status"] == "prepared" and len(bodies) == 2
            first, second = bodies
            assert first["thinking"] == {"type": "enabled"}
            assert first["reasoning_effort"] == "high" and first["max_tokens"] == 8192
            tool_round = [m for m in second["messages"] if m.get("tool_calls")][0]
            assert tool_round["reasoning_content"] == "fixture-private-tool"
            app = bridge.apps[message.scope]
            assert app.conversation.context.budget.output_reserve >= 8192
            assert app.gateway.used_tokens == 5040  # reasoning is already included in completion.
            protocol = app.queue.get_protocol(message.key)
            assert protocol["reasoning_content"] == "fixture-private-final"
            assert "messages" not in protocol  # Keep lightweight ordinary checkpoints.
            assert "fixture-private" not in json.dumps(
                [r.model_dump(mode="json") for r in app.store.list_records()], ensure_ascii=False)
            await bridge.claim(message.key)
            await bridge.acknowledge(message.key, "sent")
        finally:
            await bridge.close()

        bridge = build()
        try:
            await bridge.receive(message.model_copy(update={"message_id": "next", "text": "嗯，我回来了。"}))
            history = [m for m in bodies[-1]["messages"] if m["role"] == "assistant"]
            assert len(history) == 1
            assert history[0]["reasoning_content"] == "fixture-private-final"
            assert bodies[-1]["reasoning_effort"] == "high"
        finally:
            await bridge.close()


async def test_disabling_thinking_preserves_normal_output_limit(tmp_path):
    provider = FixtureProvider([{"content": "你好。"}])
    app = Application(tmp_path, provider=provider, config={"context": {
        "llm_activation": False, "llm_compaction": False,
        "reply_max_chars": 160, "reply_max_tokens": 512}})
    conversation = ReadingConversation.adapt(app.conversation, {"thinking": False})
    await conversation.respond("你好", message_id="q")
    request = provider.requests[0]
    assert not request.thinking and request.max_tokens == 512 and request.timeout == 45
    assert "reasoning_content" not in app.queue.get_protocol("q")


async def test_output_truncated_during_thinking_keeps_raw_and_rejects_answer(tmp_path):
    provider = FixtureProvider([{"content": "", "finish_reason": "length",
                                 "provider_fields": {"reasoning_content": "fixture-private"}}])
    app = Application(tmp_path, provider=provider, config={"context": {
        "limit": 100000, "llm_activation": False, "llm_compaction": False}})
    conversation = ReadingConversation.adapt(app.conversation, {"thinking": True})
    with pytest.raises(ProviderError, match="incomplete response"):
        await conversation.respond("你好", message_id="q")
    assert app.store.get_record("q").body_markdown == "你好"
    assert not any(r.payload.get("role") == "assistant" for r in app.store.list_records("raw_message"))


async def test_active_thinking_tool_protocol_resumes_after_restart(tmp_path):
    config = {"context": {"limit": 100000, "llm_activation": False, "llm_compaction": False}}
    provider = FixtureProvider([{"content": "", "provider_fields": {"reasoning_content": "fixture-tool"},
        "tool_calls": [{"id": "search", "type": "function", "function": {
            "name": "recall", "arguments": '{"query":"安静"}'}}]}])
    app = Application(tmp_path, provider=provider, config=config)
    conversation = ReadingConversation.adapt(app.conversation, {"thinking": True})
    conversation.max_steps = 1
    with pytest.raises(ProviderError, match="step limit"):
        await conversation.respond("安静待一会", message_id="q")
    provider = FixtureProvider([{"content": "好，陪你待着。",
                                 "provider_fields": {"reasoning_content": "fixture-final"}}])
    app = Application(tmp_path, provider=provider, config=config)
    conversation = ReadingConversation.adapt(app.conversation, {"thinking": True})
    await conversation.respond("安静待一会", message_id="q")
    request = provider.requests[0]
    assert request.thinking and request.reasoning_effort == "high"
    assert next(m for m in request.messages if m.get("tool_calls"))["reasoning_content"] == "fixture-tool"
    assert app.queue.get_protocol("q")["reasoning_content"] == "fixture-final"


@pytest.mark.parametrize("options", [
    {"thinking": "true"}, {"reasoning_effort": "medium"},
    {"thinking_max_tokens": 512}, {"thinking_timeout": float("nan")},
])
def test_invalid_thinking_settings_fail_before_api(tmp_path, options):
    app = Application(tmp_path, provider=FixtureProvider())
    with pytest.raises(ValueError, match="thinking settings"):
        ReadingConversation.adapt(app.conversation, deepcopy(options))

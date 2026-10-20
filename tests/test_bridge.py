import asyncio
from contextlib import closing
import json
import sqlite3

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError

from myatri_bridge.bridge import Bridge, Conflict, Incoming
from myatri_bridge.server import create_app
from myatri.persona.patch import PersonaPatcher


async def test_only_explicit_allowed_group_triggers_touch_engine(bridge, message):
    for update in [{"mentioned": False}, {"group_id": "other"}, {"sender_id": "bot"}]:
        assert (await bridge.receive(message.model_copy(update=update)))[
            "status"
        ] == "ignored"
    assert not bridge.apps and not bridge.test_providers


async def test_group_memory_source_identity_time_and_exact_text_survive_restart(
    bridge, message
):
    result = await bridge.receive(message)
    app = bridge.apps[message.scope]
    user = app.store.get_record(message.key)
    envelope = json.loads(user.body_markdown)
    assert envelope["text"] == message.text
    assert envelope["sender_id"] == "member1" and envelope["sent_at"] == message.sent_at
    assert result["status"] == "prepared" and 40 <= len(result["reply"]) <= 80
    assert (
        app.conversation.context.trace_every_turns == 30 and app.maintenance.turns == 50
    )
    assert app.gateway.daily_token_limit == 50000000
    assert app.persona.read_state().payload["entries"]["P15_group_scope"]
    await bridge.close()
    reopened = Bridge(
        bridge.root,
        bridge.config,
        bridge.engine_config,
        __import__("conftest").PROJECT / "config/persona/persona.md",
        provider_factory=bridge.provider_factory,
    )
    try:
        replay = await reopened.receive(message)
        assert replay == result and not reopened.apps
    finally:
        await reopened.close()


async def test_two_rooms_are_isolated_but_members_share_room_context(bridge, message):
    await bridge.receive(message)
    second = message.model_copy(
        update={"message_id": "m2", "sender_id": "member2", "text": "我喜欢红茶。"}
    )
    await bridge.receive(second)
    other = message.model_copy(
        update={"group_id": "room2", "message_id": "m3", "text": "另一个群的消息。"}
    )
    await bridge.receive(other)
    first_app, second_app = bridge.apps[message.scope], bridge.apps[other.scope]
    assert len(first_app.store.list_records("raw_message")) == 4
    assert len(second_app.store.list_records("raw_message")) == 2
    contexts = bridge.test_providers[0].requests[-1].messages
    assert any('"sender_id": "member1"' in m["content"] for m in contexts)
    assert not any("另一个群的消息" in m["content"] for m in contexts)
    assert first_app.gateway.budget_ledger is second_app.gateway.budget_ledger
    assert first_app.gateway.budget_scope == second_app.gateway.budget_scope


async def test_repeated_callbacks_generate_once_and_delivery_claim_is_atomic(
    bridge, message
):
    results = await asyncio.gather(bridge.receive(message), bridge.receive(message))
    assert results[0] == results[1]
    assert len(bridge.test_providers[0].requests) == 1
    claims = await asyncio.gather(bridge.claim(message.key), bridge.claim(message.key))
    assert sum(c["claimed"] for c in claims) == 1
    await bridge.acknowledge(message.key, "sent")
    assert (await bridge.receive(message))["status"] == "sent"
    assert not (await bridge.claim(message.key))["claimed"]
    with pytest.raises(Conflict):
        await bridge.receive(message.model_copy(update={"text": "同一个ID却换了内容"}))


async def test_process_loss_during_delivery_becomes_unknown_never_resends(
    bridge, message
):
    await bridge.receive(message)
    await bridge.claim(message.key)
    await bridge.close()
    reopened = Bridge(
        bridge.root,
        bridge.config,
        bridge.engine_config,
        __import__("conftest").PROJECT / "config/persona/persona.md",
        provider_factory=bridge.provider_factory,
    )
    try:
        assert (await reopened.receive(message))["status"] == "unknown"
        assert not (await reopened.claim(message.key))["claimed"]
    finally:
        await reopened.close()


async def test_http_auth_validation_and_transport_status(bridge, message):
    async with TestClient(
        TestServer(create_app(bridge, "fixture-token-" + "a" * 40))
    ) as client:
        assert (await client.get("/health")).status == 401
        client.session.headers["Authorization"] = "Bearer fixture-token-" + "a" * 40
        assert (await client.get("/health")).status == 200
        assert (
            await client.post("/v1/messages", json={"text": "incomplete"})
        ).status == 400
        response = await client.post("/v1/messages", json=message.model_dump())
        assert (
            response.status == 200 and (await response.json())["status"] == "prepared"
        )
        claim = await client.post(f"/v1/deliveries/{message.key}/claim")
        assert (await claim.json())["claimed"]
        ack = await client.post(
            f"/v1/deliveries/{message.key}/ack", json={"status": "sent"}
        )
        assert (await ack.json())["status"] == "sent"


@pytest.mark.parametrize(
    "change",
    [
        {"sender_id": "unknown"},
        {"sender_id": ""},
        {"sent_at": "2026-10-21T01:42:05"},
        {"platform_id": "x|y"},
    ],
)
def test_unknown_identity_or_naive_time_is_rejected(message, change):
    with pytest.raises(ValidationError):
        Incoming.model_validate({**message.model_dump(), **change})


async def test_cooldown_never_calls_model_for_second_message(bridge, message):
    bridge.config["cooldown_seconds"] = 3600
    await bridge.receive(message)
    assert (await bridge.receive(message.model_copy(update={"message_id": "fast"})))[
        "reason"
    ] == "cooldown"
    assert len(bridge.test_providers[0].requests) == 1


async def test_sent_does_not_mean_the_recipient_read_it(bridge, message):
    await bridge.receive(message)
    with closing(sqlite3.connect(bridge.path)) as db:
        assert db.execute("SELECT status FROM deliveries").fetchone()[0] == "prepared"
    assert not bridge.config.get("allow_proactive", False)


async def test_restart_preserves_group_persona_changes_instead_of_reinitializing(
    bridge, message
):
    await bridge.receive(message)
    app = bridge.apps[message.scope]
    changed = PersonaPatcher(app.store).human_edit(
        "P03_personality", "这是一条群内人工测试修改。", "离线重启回归"
    )
    await bridge.close()
    from conftest import PROJECT

    reopened = Bridge(
        bridge.root,
        bridge.config,
        bridge.engine_config,
        PROJECT / "config/persona/persona.md",
        provider_factory=bridge.provider_factory,
    )
    try:
        await reopened.receive(
            message.model_copy(update={"message_id": "after-restart"})
        )
        assert reopened.apps[message.scope].persona.read_state().ref == changed.ref
        assert (
            reopened.apps[message.scope]
            .persona.read_state()
            .payload["entries"]["P03_personality"]
            == "这是一条群内人工测试修改。"
        )
    finally:
        await reopened.close()

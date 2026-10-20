"""Offline transport and attention regressions; no service or paid API calls."""
import asyncio
import json
import sqlite3
import zlib
from contextlib import closing

import pytest

from myatri.providers import FixtureProvider
from myatri_bridge.cold_archive import ColdArchive
from myatri_bridge.topics import TopicTracker, select_summaries


class GateProvider(FixtureProvider):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.foreground_calls = 0

    async def generate(self, request):
        if request.role == "foreground":
            self.foreground_calls += 1
            self.entered.set()
            await self.release.wait()
        return await super().generate(request)


def gate_factory(bridge):
    providers = {}

    def factory(scope):
        providers[scope] = GateProvider()
        return providers[scope]

    bridge.provider_factory = factory
    return providers


async def enter_generation(bridge, providers, message):
    task = asyncio.create_task(bridge.receive(message))
    for _ in range(200):
        if message.scope in providers:
            await asyncio.wait_for(providers[message.scope].entered.wait(), 5)
            return task
        await asyncio.sleep(.01)
    raise AssertionError("fixture generation never started")


async def test_slow_generation_does_not_block_listening_claim_or_ack(bridge, message, tmp_path):
    prepared = await bridge.receive(message)
    bridge.cold_archive = ColdArchive(tmp_path / "cold.sqlite3", 30)
    providers = gate_factory(bridge)
    other = message.model_copy(update={"group_id": "room2", "message_id": "slow"})
    generation = await enter_generation(bridge, providers, other)
    ordinary = other.model_copy(update={"message_id": "ordinary", "mentioned": False,
                                       "text": "只旁听保存这条消息"})
    result = await asyncio.wait_for(bridge.receive(ordinary), 1)
    assert result["status"] == "ignored"
    assert any(row["message_id"] == "ordinary" for row in bridge.short_memory.recent(other.scope))
    assert bridge.cold_archive.read(other.scope, "ordinary")["available"]
    assert await asyncio.wait_for(bridge.claim(prepared["id"]), 1) == {"claimed": True}
    assert await asyncio.wait_for(bridge.acknowledge(prepared["id"], "sent"), 1) == {"status": "sent"}
    assert not generation.done()
    providers[other.scope].release.set()
    assert (await generation)["status"] == "prepared"


async def test_groups_generate_in_parallel_and_duplicate_callbacks_join_once(bridge, message):
    providers = gate_factory(bridge)
    first = await enter_generation(bridge, providers, message)
    duplicate = asyncio.create_task(bridge.receive(message))
    other = message.model_copy(update={"group_id": "room2"})
    second = await enter_generation(bridge, providers, other)
    assert not first.done() and not second.done()
    assert providers[message.scope].foreground_calls == 1
    assert providers[other.scope].foreground_calls == 1
    for provider in providers.values():
        provider.release.set()
    results = await asyncio.gather(first, duplicate, second)
    assert results[0] == results[1]
    assert all(row["status"] == "prepared" for row in results)
    assert bridge.short_memory.stats(message.scope)["total_messages"] == 1


async def test_new_explicit_message_supersedes_inflight_random_reply(bridge, message):
    providers = gate_factory(bridge)
    bridge.random_probability = 1
    random = message.model_copy(update={"mentioned": False, "message_id": "random"})
    first = await enter_generation(bridge, providers, random)
    explicit = message.model_copy(update={"message_id": "explicit"})
    second = asyncio.create_task(bridge.receive(explicit))
    for _ in range(200):
        if bridge.short_memory.stats(message.scope)["total_messages"] == 2:
            break
        await asyncio.sleep(.01)
    else:
        raise AssertionError("new explicit message was blocked by generation")
    providers[message.scope].release.set()
    assert (await first)["reason"] == "stale_random_reply"
    assert (await second)["status"] == "prepared"
    assert await bridge.claim(random.key) == {"claimed": False}


async def test_random_ttl_expires_before_send_claim(bridge, message):
    bridge.random_probability = 1
    bridge.config["random_reply_ttl_seconds"] = 45
    random = message.model_copy(update={"mentioned": False})
    assert (await bridge.receive(random))["status"] == "prepared"
    with closing(sqlite3.connect(bridge.path)) as db, db:
        # A recent state update must not reset a random reply's original TTL.
        db.execute("UPDATE deliveries SET received_at=received_at-46 WHERE id=?", (random.key,))
    assert await bridge.claim(random.key) == {"claimed": False}
    with closing(sqlite3.connect(bridge.path)) as db:
        assert db.execute("SELECT status FROM deliveries WHERE id=?", (random.key,)).fetchone()[0] == "superseded"


async def test_prepared_random_is_not_claimed_after_new_explicit_request(bridge, message):
    bridge.random_probability = 1
    random = message.model_copy(update={"mentioned": False})
    assert (await bridge.receive(random))["status"] == "prepared"
    explicit = message.model_copy(update={"message_id": "explicit-later"})
    assert (await bridge.receive(explicit))["status"] == "prepared"
    assert await bridge.claim(random.key) == {"claimed": False}
    assert await bridge.claim(explicit.key) == {"claimed": True}


async def test_transport_cancellation_does_not_duplicate_inflight_generation(bridge, message):
    providers = gate_factory(bridge)
    caller = await enter_generation(bridge, providers, message)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    retry = asyncio.create_task(bridge.receive(message))
    await asyncio.sleep(.01)
    assert providers[message.scope].foreground_calls == 1
    providers[message.scope].release.set()
    assert (await retry)["status"] == "prepared"
    assert providers[message.scope].foreground_calls == 1
    await bridge.close()
    assert not bridge.generations


async def test_repeated_sent_ack_does_not_duplicate_cold_or_assistant_context(bridge, message, tmp_path):
    bridge.cold_archive = ColdArchive(tmp_path / "confirmed-cold.sqlite3", 30)
    result = await bridge.receive(message)
    assert await bridge.claim(result["id"]) == {"claimed": True}
    assert await bridge.acknowledge(result["id"], "sent") == {"status": "sent"}
    assert await bridge.acknowledge(result["id"], "sent") == {"status": "sent"}
    with closing(sqlite3.connect(bridge.cold_archive.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM cold_messages WHERE scope=?", (message.scope,)).fetchone()[0] == 2
    reply = bridge.cold_archive.read(message.scope, "reply_" + message.key)
    assert reply["message"]["role"] == "assistant"
    assert reply["message"]["text"] == result["reply"]
    assert bridge.short_memory.stats(message.scope)["total_messages"] == 1
    with closing(sqlite3.connect(bridge.short_memory.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM listening_assistant_context WHERE scope=?", (message.scope,)).fetchone()[0] == 1


def test_cold_archive_compressed_original_scope_paging_and_expiry(tmp_path, monkeypatch, message):
    now = [1_000_000.0]
    monkeypatch.setattr("myatri_bridge.cold_archive.time.time", lambda: now[0])
    cold = ColdArchive(tmp_path / "cold.sqlite3", 30)
    payload = message.model_dump()
    payload["text"] = "这是需要逐页核对的群消息。" * 150
    assert cold.add("group-a", payload)
    assert not cold.add("group-a", payload)
    with closing(sqlite3.connect(cold.path)) as db:
        blob = db.execute("SELECT payload FROM cold_messages").fetchone()[0]
    assert isinstance(blob, bytes)
    assert json.loads(zlib.decompress(blob))["text"] == payload["text"]
    assert not cold.read("group-b", message.message_id)["available"]
    chunks, offset = [], 0
    while True:
        page = cold.read("group-a", message.message_id, offset=offset, limit=317)
        assert page["available"] and len(page["message"]["text"]) <= 317
        chunks.append(page["message"]["text"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert "".join(chunks) == payload["text"]
    now[0] += 30 * 86400 + 1
    assert not cold.read("group-a", message.message_id)["available"]
    cold.prune()
    with closing(sqlite3.connect(cold.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM cold_messages").fetchone()[0] == 0


def test_disabled_cold_archive_and_invalid_pages(tmp_path, message):
    cold = ColdArchive(tmp_path / "disabled.sqlite3", 0)
    assert not cold.add("s", message.model_dump())
    assert cold.read("s", "m1")["reason"] == "cold_archive_disabled"
    for args in ({"offset": -1}, {"limit": 801}, {"limit": 0}, {"offset": True}):
        with pytest.raises(ValueError):
            cold.read("s", "m1", **args)


def test_topic_explicit_quote_beats_lexical_match_and_isolates_groups(tmp_path, message):
    tracker = TopicTracker(tmp_path / "topics.sqlite3")
    food = message.model_dump()
    food.update(message_id="food", sender_id="a", text="今晚吃火锅还是去烧烤店", sent_at="2026-10-21T10:00:00+08:00")
    game = {**food, "message_id": "game", "sender_id": "b", "text": "游戏服务器升级后存档读取出错", "sent_at": "2026-10-21T10:00:01+08:00"}
    food_topic, game_topic = tracker.add("one", food), tracker.add("one", game)
    assert food_topic != game_topic
    mixed = {**game, "message_id": "mixed", "text": "游戏服务器升级后存档读取出错，顺便说今晚吃什么", "reply_to_id": "food", "sent_at": "2026-10-21T10:00:02+08:00"}
    assert tracker.add("one", mixed) == food_topic
    assert tracker.lookup("two", "food") is None
    assert tracker.add("two", mixed) != food_topic
    assert tracker.describe("one", food_topic)["source_message_ids"] == ["food", "mixed"]


def summary_item(text, subject="a"):
    return {"text": text, "speaker_id": subject, "subject_id": subject,
            "attribution": "self_report", "source_message_ids": [text[:4]]}


def test_summary_selection_budget_prefers_current_member_correction_and_deduplicates():
    correction = summary_item("更正：我现在不再喝咖啡，改为红茶")
    irrelevant = summary_item("游戏服务器升级了", "b")
    old = summary_item("我喜欢咖啡")
    summaries = [{"window_end": 100, "created_at": "2026-10-21", "items": [old, irrelevant]},
                 {"window_end": 200, "created_at": "2026-10-22", "items": [correction, correction]}]
    result = select_summaries(summaries, "你记得我的饮品偏好吗", "a", max_chars=300)
    assert correction["text"] in [row["text"] for row in result["items"]]
    assert len({row["text"] for row in result["items"]}) == len(result["items"])
    assert sum(len(json.dumps(row, ensure_ascii=False)) for row in result["items"]) <= 300
    assert select_summaries(summaries, "咖啡", "a", max_chars=0)["items"] == []

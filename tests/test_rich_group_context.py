"""Offline long dialogue, identity, quotation and shared context-budget checks."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3

from myatri.contracts import Record


def seeded(bridge, message, count=100, width=20):
    bridge.engine_config["embedding"] = {"backend": "tfidf"}
    app = bridge._application(message)
    app.conversation.context.dialogue_turns = 80
    records, rows, timeline = [], [], []
    start = datetime(2026, 10, 21, tzinfo=timezone.utc)
    for number in range(count):
        text = ("纸飞机是我们没聊完的玩笑" if number == 5 else f"群内往返{number}，") + "日常闲聊。" * width
        original = {**message.model_dump(exclude={"mentioned"}), "message_id": f"chat{number}",
            "sender_id": "member1" if number == 5 else "other_member",
            "sender_name": "甲" if number == 5 else "乙", "text": text}
        user = Record(record_id=f"chat{number}", record_kind="raw_message", agent_id=app.store.agent_id,
            user_id=app.store.user_id, author_kind="human", created_at=(start+timedelta(seconds=number*2)).isoformat(),
            body_markdown=json.dumps(original, ensure_ascii=False), payload={"role": "user"})
        answer = Record(record_id=f"answer_chat{number}", record_kind="raw_message", agent_id=app.store.agent_id,
            user_id=app.store.user_id, author_kind="system", created_at=(start+timedelta(seconds=number*2+1)).isoformat(),
            body_markdown=f"回应第{number}轮，" + "接着之前的话聊。" * width,
            payload={"role": "assistant", "reply_to": user.ref})
        records.extend((user, answer))
        rows.append((user.record_id, message.scope, str(number), "unknown" if number == 4 else "sent", 1))
        timeline.extend((original, {"message_id": "reply_"+user.record_id, "sender_id": message.bot_id,
                                  "role": "assistant", "text": answer.body_markdown}))
    app.store.commit(records, idempotency_key="synthetic_long_dialogue")
    with closing(sqlite3.connect(bridge.path)) as db, db:
        db.executemany("INSERT INTO deliveries(id,scope,fingerprint,status,updated) VALUES(?,?,?,?,?)", rows)
    return app, timeline


def build(app, message, timeline, **extra):
    data = {**message.model_dump(exclude={"mentioned"}), "message_id": "new_question",
            "text": "还记得纸飞机那个玩笑吗", "recent_messages": timeline, **extra}
    messages, _ = app.conversation.context.build_context(json.dumps(data, ensure_ascii=False))
    return messages, app.conversation.context.explain_context()


async def test_eighty_complete_pairs_and_related_member_survive_busy_timeline(bridge, message):
    app, timeline = seeded(bridge, message)
    messages, info = build(app, message, timeline[-100:])
    assert info["ongoing_dialogue_turns"] == 80
    pairs = [m for m in messages if m["role"] in {"user", "assistant"}][:-1]
    assert len(pairs) == 160
    assert all(pairs[i]["role"] == "user" and pairs[i+1]["role"] == "assistant" for i in range(0,160,2))
    ids = {json.loads(pairs[i]["content"])["message_id"] for i in range(0,160,2)}
    assert "chat5" in ids
    assert "chat4" not in ids  # Unknown send is never replayed.
    current = json.loads(messages[-1]["content"].split("\n",1)[1])
    assert not any(m["message_id"] in ids for m in current["recent_messages"])
    assert info["context_stats"]["dialogue_omitted_turns"] == 19
    assert app.store.verify_archive() == []


async def test_explicit_old_quote_is_kept_without_authenticating_another_member(bridge, message):
    app, timeline = seeded(bridge, message, count=120)
    messages, info = build(app, message, timeline[-100:], reply_to_id="chat0")
    previous = [json.loads(m["content"]) for m in messages[:-1] if m["role"] == "user"]
    assert any(m["message_id"] == "chat0" and m["sender_id"] == "other_member" for m in previous)
    assert info["ongoing_dialogue_turns"] == 80
    policy = next(m["content"] for m in messages if "<group_channel_data>" in m["content"])
    data = json.loads(policy.split("<group_channel_data>\n", 1)[1].split("\n</group_channel_data>", 1)[0])
    assert data["channel_derived_identity"]["current_reply_to_id"] == "member1"


async def test_small_shared_budget_removes_complete_units_and_keeps_current_and_rules(bridge, message):
    app, timeline = seeded(bridge, message, count=50, width=100)
    app.conversation.context.working_context_tokens = 45000
    messages, info = build(app, message, timeline[-100:], text="我现在这句话必须完整保留")
    assert "我现在这句话必须完整保留" in messages[-1]["content"]
    assert "当前人格" in messages[0]["content"]
    assert info["context_stats"]["budget_trimmed_units"] > 0
    assert info["ongoing_dialogue_turns"] < 50
    dialogue = [m for m in messages[:-1] if m["role"] in {"user","assistant"}]
    assert len(dialogue) % 2 == 0
    assert app.conversation.context.budget.fits(messages)
    assert len(app.store.list_records("raw_message")) == 100


async def test_default_window_settings_and_numeric_metrics(bridge, message):
    app, timeline = seeded(bridge, message, count=6)
    _, info = build(app, message, timeline)
    stats = info["context_stats"]
    encoded = json.dumps(stats, ensure_ascii=False)
    assert "other_member" not in encoded and "纸飞机" not in encoded
    assert stats["timeline_deduplicated_messages"] == 10  # Unknown send remains timeline evidence only.
    assert stats["components"]["timeline_and_current"]["messages"] == 1
    assert "token_budget" in stats["omission_reasons"]

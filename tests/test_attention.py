import json
import sqlite3

from myatri_bridge.attention import followup


def test_window_is_one_to_four_group_local_and_expires():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE attention_windows(scope TEXT PRIMARY KEY,remaining INTEGER,expires REAL,wake_key TEXT)"
    )
    counts = set()
    for i in range(50):
        result = followup(db, "one", str(i), wake=True, now=0)
        n = result["remaining"]
        counts.add(n)
        assert 1 <= n <= 4
        assert not followup(db, "other", "x", wake=False, now=1)["active"]
        for j in range(n):
            assert followup(db, "one", str(j), wake=False, now=1)["active"]
        assert not followup(db, "one", "end", wake=False, now=1)["active"]
    assert counts == {1, 2, 3, 4}
    followup(db, "one", "wake", wake=True, now=0)
    assert not followup(db, "one", "expired", wake=False, now=601)["active"]
    db.close()


async def test_attention_bypasses_normal_draw_cooldown_and_deduplicates(
    bridge, message
):
    bridge.config["attention_followup_enabled"] = True
    bridge.attention_probability = 1
    bridge.config["cooldown_seconds"] = 999
    await bridge.receive(message)
    with sqlite3.connect(bridge.path) as db:
        rounds = db.execute(
            "SELECT remaining FROM attention_windows WHERE scope=?", (message.scope,)
        ).fetchone()[0]
    for i in range(rounds):
        follow = message.model_copy(
            update={
                "mentioned": False,
                "message_id": f"attention-{i}",
                "text": "继续刚才的话题。",
            }
        )
        result = await bridge.receive(follow)
        assert result["status"] == "prepared"
        assert await bridge.receive(follow) == result
        with sqlite3.connect(bridge.path) as db:
            row = db.execute(
                "SELECT trigger_kind,envelope FROM deliveries WHERE id=?", (follow.key,)
            ).fetchone()
            assert (
                row[0] == "attention"
                and json.loads(row[1])["followup_attention"]["active"]
            )
            assert (
                db.execute(
                    "SELECT remaining FROM attention_windows WHERE scope=?",
                    (message.scope,),
                ).fetchone()[0]
                == rounds - i - 1
            )
    extra = message.model_copy(
        update={"mentioned": False, "message_id": "after-window", "text": "普通消息。"}
    )
    assert (await bridge.receive(extra))["reason"] == "not_a_trigger"
    wake = message.model_copy(
        update={
            "mentioned": False,
            "message_id": "named-wake",
            "text": "小温暖，来聊聊",
        }
    )
    assert (await bridge.receive(wake))["status"] == "prepared"
    with sqlite3.connect(bridge.path) as db:
        assert (
            1
            <= db.execute(
                "SELECT remaining FROM attention_windows WHERE scope=?", (wake.scope,)
            ).fetchone()[0]
            <= 4
        )

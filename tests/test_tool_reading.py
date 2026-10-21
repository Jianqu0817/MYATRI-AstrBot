"""Offline protocol acceptance: sources, actual returned text, final claims."""
import json

import pytest

from myatri.app import Application
from myatri.contracts import RecallBundle, Record
from myatri.providers import FixtureProvider, ProviderError
from myatri.storage import ArchiveStore
from myatri_bridge.reading_conversation import ReadingConversation
from myatri_bridge.tool_reading import (
    FIELDS, byte_size, cold_page, guard_reading_claim, read_audit, read_page,
    recall_directory,
)


LONG = ("甲的长原文。" + "这是无关背景，不要从背景猜偏好。" * 65
        + "甲更正：我以前喝咖啡，现在不喝咖啡了，改喝红茶。" + "这只是背景。" * 500)


def commit(store, *records):
    records = [r.model_copy(update={"agent_id": store.agent_id, "user_id": store.user_id}) for r in records]
    store.commit(list(records), idempotency_key="seed-" + records[0].record_id,
                 expected={r.record_id: 0 for r in records})


def call(name, **args):
    return {"id": f"{name}-{len(str(args))}-{args.get('offset', 0)}-{args.get('ref', '')}",
            "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def roundtrip(call_value, result):
    return [{"role": "assistant", "content": "", "tool_calls": [call_value]},
            {"role": "tool", "tool_call_id": call_value["id"],
             "content": json.dumps(result, ensure_ascii=False)}]


def app_at(path, responses):
    provider = FixtureProvider(responses)
    app = Application(path, provider=provider, config={"context": {
        "trace_every_turns": 30, "limit": 100000, "reply_min_chars": 1,
        "reply_max_chars": 0, "llm_activation": False, "llm_compaction": False}})
    app.conversation = ReadingConversation.adapt(app.conversation)
    return app, provider


def test_directory_bounded_keeps_locators_and_internal_bundle(tmp_path):
    store = ArchiveStore(tmp_path)
    records = [Record(record_id=f"raw{i}", record_kind="raw_message",
                      body_markdown='中文😀"\\\n\t' * 2000) for i in range(12)]
    commit(store, *records)
    bundle = RecallBundle(snapshot=store.snapshot(), records=records,
                          reasons={r.ref: ["诊断" * 5000] for r in records},
                          limitations=["诊断" * 20000])
    before = bundle.model_dump_json()
    result = recall_directory(bundle, store)
    assert byte_size(result) <= 6000 and result["results"]
    assert result["has_more"] and result["omitted_count"] == 7
    assert all(row["read_record"]["ref"] == row["ref"] and not row["original_read"]
               for row in result["results"])
    assert bundle.model_dump_json() == before


def test_explicit_omitted_source_reachable_but_reasons_cannot_expand(tmp_path):
    store = ArchiveStore(tmp_path)
    a = Record(record_id="earliest", record_kind="raw_message", body_markdown=LONG)
    b = Record(record_id="other_member", record_kind="raw_message", body_markdown="乙喜欢咖啡")
    commit(store, a, b)
    bundle = RecallBundle(snapshot=store.snapshot(), reasons={b.ref: ["ranked before subject filter"]},
                          paths=[[a.ref, b.ref]], limitations=[
                              f"早期原文定位：{a.ref}。正文未注入时可read_record分页读取。"])
    result = recall_directory(bundle, store)
    assert [r["ref"] for r in result["results"]] == [a.ref]
    assert read_page(store, a.ref, bundle.snapshot, [])["content"] == LONG[:800]


@pytest.mark.parametrize("field", FIELDS)
def test_every_field_pages_are_reachable_and_exact(tmp_path, field):
    store = ArchiveStore(tmp_path)
    r = Record(record_id="long", record_kind="raw_message", body_markdown='中😀"\\\n\x01' * 2200,
               payload={"text": '长😀"\\\n' * 1200}, generator={"huge": "元信息" * 10000},
               source_refs=["source@1"] * 1000)
    commit(store, Record(record_id="source", record_kind="raw_message"), r)
    messages, parts, offset = [], [], 0
    while True:
        page = read_page(store, r.ref, store.snapshot(), read_audit(messages), offset=offset, field=field)
        assert byte_size(page) <= 6000
        parts.append(page["content"])
        messages += roundtrip(call("read_record", ref=r.ref, field=field, offset=offset), page)
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert page["read_progress"]["complete"]
    expected = r.body_markdown if field == "body_markdown" else (
        json.dumps(r.payload, ensure_ascii=False, sort_keys=True) if field == "payload_json" else
        json.dumps(r.source_refs, ensure_ascii=False) if field == "source_refs_json" else
        json.dumps({k: v for k, v in r.model_dump(mode="json").items()
                    if k not in {"body_markdown", "payload", "source_refs"}}, ensure_ascii=False, sort_keys=True))
    assert "".join(parts) == expected


def test_skip_to_end_is_incomplete_then_fill_gap_and_separate_fields(tmp_path):
    store = ArchiveStore(tmp_path)
    r = Record(record_id="long", record_kind="raw_message", body_markdown=LONG,
               payload={"text": "另一字段" * 300})
    commit(store, r)
    messages = []
    for offset in (0, 3200, 4000):
        page = read_page(store, r.ref, store.snapshot(), read_audit(messages), offset=offset)
        messages += roundtrip(call("read_record", ref=r.ref, offset=offset), page)
    assert page["next_offset"] is None
    assert not page["read_progress"]["complete"]
    assert page["read_progress"]["first_unread_offset"] == 800
    separate = read_page(store, r.ref, store.snapshot(), read_audit(messages), field="payload_json", offset=800)
    assert separate["read_progress"]["first_unread_offset"] == 0
    for offset in (800, 1600, 2400):
        page = read_page(store, r.ref, store.snapshot(), read_audit(messages), offset=offset)
        messages += roundtrip(call("read_record", ref=r.ref, offset=offset), page)
    assert page["read_progress"]["complete"]


def test_audit_and_local_guard_are_selective():
    failure = roundtrip(call("read_record", ref="missing@1"), {"error": "KeyError"})
    audit = read_audit(failure)
    assert not audit[0]["success"] and audit[0]["range"] is None
    assert guard_reading_claim("我已经查到确切原话了。", audit)[1] == "failed_read_claim"
    guarded, reason = guard_reading_claim("我已经读完全文了。", audit)
    assert reason == "unread_source_claim" and "只读到了部分" not in guarded
    partial = [{"tool": "read_record", "ref": "a@1", "field": "body_markdown",
                "success": True, "complete": False}]
    assert guard_reading_claim("我读了a@1的全文，没有任何偏好。", partial)[1]
    assert guard_reading_claim("我读了a@1全文（4070字），没有任何相关内容。", partial)[1]
    assert guard_reading_claim("我读完了a@1。", partial)[1]
    assert guard_reading_claim("我还没读完a@1，需要核对缺页。", partial)[1] is None
    assert guard_reading_claim("目前看到的片段里，甲说改喝红茶了。", partial)[1] is None
    assert guard_reading_claim("已读片段里没有任何相关内容，后面还没核对。", partial)[1] is None
    assert guard_reading_claim("本轮给出的原话里，甲说喜欢红茶。", [])[1] is None
    assert guard_reading_claim("我读完了a@1全文。", [{"tool": "recall", "success": True, "refs": ["a@1"]}])[1]


def test_page_preserves_speaker_and_subject_without_merging_them(tmp_path):
    store = ArchiveStore(tmp_path)
    record = Record(record_id="reported", record_kind="raw_message", body_markdown="乙喜欢咖啡。",
                    payload={"role": "user", "channel_source": {"sender_id": "a", "sender_name": "同名"},
                             "attribution": {"speaker_id": "a", "subject_id": "b", "attribution": "reported"}})
    commit(store, record)
    page = read_page(store, record.ref, store.snapshot(), [])
    assert page["payload_context"]["channel_source"]["sender_id"] == "a"
    assert page["payload_context"]["attribution"] == {"speaker_id": "a", "subject_id": "b", "attribution": "reported"}
    assert not page["payload_context_complete"] and byte_size(page) <= 6000


def test_large_identity_projection_cannot_stall_page_progress(tmp_path):
    store = ArchiveStore(tmp_path)
    identity = {key: "😀" * 160 for key in ("sender_id", "sender_name", "message_id", "sent_at",
                "speaker_id", "subject_id", "attribution", "source_message_id")}
    record = Record(record_id="wide", record_kind="raw_message", body_markdown=LONG,
                    payload={key: identity for key in ("channel_source", "group_attribution", "attribution")})
    commit(store, record)
    page = read_page(store, record.ref, store.snapshot(), [])
    assert page["content"] and page["next_offset"] == 800 and byte_size(page) <= 6000


def test_cold_page_budget_and_gap_progress():
    audit = []
    for offset in (0, 3200, 4000):
        result = cold_page({"available": True, "message": {"text": LONG[offset:offset+800],
                           "huge_metadata": "资料" * 10000}, "offset": offset,
                           "total_chars": len(LONG)}, "m1", audit)
        assert byte_size(result) <= 6000
        audit = read_audit(roundtrip(call("read_group_message", message_id="m1", offset=offset), result)) + audit
    assert result["next_offset"] is None and not result["read_progress"]["complete"]
    assert result["read_progress"]["first_unread_offset"] == 800


@pytest.mark.parametrize("query", ["最早聊天", "ATRI时期", "你答应保留记忆的那次"])
async def test_natural_queries_return_source_then_original_then_answer(tmp_path, query):
    raw = Record(record_id="retained", record_kind="raw_message", author_kind="human_import",
                 created_at="2020-01-01T00:00:00+00:00", body_markdown=
                 "最早聊天，ATRI时期，你答应保留记忆的那次：我答应保留这些记忆。" + "背景。" * 3000,
                 payload={"role": "user"})
    app, provider = app_at(tmp_path, [
        {"tool_calls": [call("recall", query=query)]},
        {"tool_calls": [call("read_record", ref=raw.ref)]},
        {"content": "在已读取的原文片段里，确实有保留这些记忆的约定。"}])
    commit(app.store, raw)
    answer = await app.conversation.respond(query, message_id="q")
    results = [json.loads(m["content"]) for m in provider.requests[-1].messages if m["role"] == "tool"]
    assert results[0]["results"][0]["ref"] == raw.ref
    assert results[1]["content"].startswith("最早聊天，ATRI时期")
    assert all(byte_size(r) <= 6000 for r in results)
    assert "约定" in answer.body_markdown and answer.payload["reading_guard"] is None
    protocol = app.queue.get_protocol("q")
    assert len(protocol["tool_audit"]) == 2 and not protocol["full_checkpoint"]
    assert "messages" not in protocol and "约定" not in json.dumps(protocol["tool_audit"], ensure_ascii=False)


async def test_bridge_end_to_end_blocks_skip_page_claim_after_semantic_guard(bridge, message):
    provider = FixtureProvider([
        {"tool_calls": [call("read_record", ref="long@1", offset=0),
                        call("read_record", ref="long@1", offset=3200),
                        call("read_record", ref="long@1", offset=4000)]},
        {"content": "我读了long@1的全文，没有任何关于甲喝什么的内容。"}])
    bridge.provider_factory = lambda scope: provider
    app = bridge._application(message)
    commit(app.store, Record(record_id="long", record_kind="raw_message", body_markdown=LONG, payload={"role": "user"}))

    async def semantic_guard(text, raw, messages):
        return "long@1的全文我已经读完，没有任何偏好。"

    app.conversation.reply_guard = semantic_guard
    result = await bridge.receive(message)
    assert "尚未核对完整" in result["reply"]
    protocol = app.queue.get_protocol(message.key)
    assert protocol["reading_guard"] == "incomplete_read_claim"
    assert not protocol["tool_audit"][-1]["complete"]
    assert len(provider.requests) == 2


async def test_completed_read_can_answer_correction_and_keep_people_separate(tmp_path):
    calls = [call("read_record", ref="long@1", offset=i) for i in range(0, len(LONG), 800)]
    app, provider = app_at(tmp_path, [{"tool_calls": calls}, {"content":
        "long@1全文已核对：甲更正为现在喝红茶；乙喜欢咖啡，不能混成甲的偏好。"}])
    commit(app.store, Record(record_id="long", record_kind="raw_message", body_markdown=LONG, payload={"role": "user"}),
           Record(record_id="other", record_kind="raw_message", body_markdown="乙自述喜欢咖啡，甲和乙是不同的人。", payload={"role": "user"}))
    answer = await app.conversation.respond("甲现在喝什么？", message_id="q")
    assert "甲更正" in answer.body_markdown and answer.payload["reading_guard"] is None
    pages = [json.loads(m["content"]) for m in provider.requests[-1].messages if m["role"] == "tool"]
    assert any("甲更正" in p["content"] for p in pages)
    assert pages[-1]["read_progress"]["complete"]


async def test_active_protocol_restart_keeps_gap_evidence(tmp_path):
    app, _ = app_at(tmp_path, [{"tool_calls": [call("read_record", ref="long@1", offset=3200)]}])
    commit(app.store, Record(record_id="long", record_kind="raw_message", body_markdown=LONG, payload={"role": "user"}))
    app.conversation.max_steps = 1
    with pytest.raises(ProviderError, match="step limit"):
        await app.conversation.respond("读一下原文", message_id="q")
    assert app.queue.get_protocol("q")["tool_audit"][0]["range"] == [3200, 4000]
    reopened, provider = app_at(tmp_path, [
        {"tool_calls": [call("read_record", ref="long@1", offset=4000)]},
        {"content": "我已经读完long@1的全文了，没有任何偏好。"}])
    answer = await reopened.conversation.respond("读一下原文", message_id="q")
    assert "尚未核对完整" in answer.body_markdown
    protocol = reopened.queue.get_protocol("q")
    assert len(protocol["tool_audit"]) == 2
    assert not protocol["tool_audit"][-1]["complete"]
    assert len(provider.requests) == 2


def test_snapshot_and_invalid_offset_do_not_leak_future_records(tmp_path):
    store = ArchiveStore(tmp_path)
    commit(store, Record(record_id="past", record_kind="raw_message", body_markdown="以前"))
    snapshot = store.snapshot()
    commit(store, Record(record_id="future", record_kind="raw_message", body_markdown="以后"))
    with pytest.raises(KeyError):
        read_page(store, "future@1", snapshot, [])
    with pytest.raises(ValueError):
        read_page(store, "past@1", snapshot, [], offset=100)
    page = read_page(store, "past@1", snapshot, [])
    assert page["ref"] == "past@1" and page["read_progress"]["complete"]


async def test_plain_chat_has_no_extra_model_calls(bridge, message):
    result = await bridge.receive(message)
    assert result["status"] == "prepared"
    assert len(bridge.test_providers[0].requests) == 1
    protocol = bridge.apps[message.scope].queue.get_protocol(message.key)
    assert protocol["tool_audit"] == [] and not protocol["full_checkpoint"]


@pytest.mark.parametrize("arguments", ["{broken", "[]", '{"ref": []}', '{"ref":"missing@1","field":{}}'])
async def test_bad_tool_arguments_still_allow_honest_final_and_audit(tmp_path, arguments):
    bad_call = {"id": "bad", "type": "function", "function": {"name": "read_record", "arguments": arguments}}
    app, _ = app_at(tmp_path, [{"tool_calls": [bad_call]}, {"content": "尚未查到确切原话。"}])
    answer = await app.conversation.respond("查原话", message_id="q")
    assert answer.body_markdown == "尚未查到确切原话。"
    row = app.queue.get_protocol("q")["tool_audit"][0]
    assert not row["success"] and row["range"] is None and row["error"]


def test_real_group_subject_filter_survives_directory_projection(tmp_path):
    from myatri_bridge.group_memory import GroupRecall
    from myatri_bridge.identity import IdentityRegistry

    store = ArchiveStore(tmp_path / "archive")
    identities = IdentityRegistry(tmp_path / "identity.db")
    for who, preference in (("a", "红茶"), ("b", "咖啡")):
        message = {"message_id": who, "sender_id": who, "sender_name": "同名",
                   "sent_at": "2026-10-21T10:00:00+08:00", "text": "我的喜好：" + preference + LONG}
        identities.observe("room", message)
        commit(store, Record(record_id=who, record_kind="raw_message", body_markdown=message["text"],
                             payload={"role": "user", "channel_source": message}))
    recall = GroupRecall(store, None, identities, "room")
    recall.current_sender = "a"
    result = recall_directory(recall.recall("我的喜好", budget=1800), store)
    assert [r["ref"] for r in result["results"]] == ["a@1"]
    assert "咖啡" not in result["results"][0]["excerpt"]


async def test_bridge_cannot_read_other_groups_version(bridge, message):
    provider = FixtureProvider([{ "tool_calls": [call("read_record", ref="secret@1")]},
                                {"content": "尚未查到确切原话。"}])
    bridge.provider_factory = lambda scope: provider
    app = bridge._application(message)
    commit(app.store, Record(record_id="secret", record_kind="raw_message", body_markdown="本群独有资料"))
    other = message.model_copy(update={"group_id": "room2", "message_id": "m2"})
    result = await bridge.receive(other)
    assert result["reply"] == "尚未查到确切原话。"
    rows = [json.loads(m["content"]) for m in provider.requests[-1].messages if m["role"] == "tool"]
    assert rows[0]["error"] == "KeyError"
    assert not any("本群独有资料" in m["content"] for m in provider.requests[-1].messages)


def test_progress_is_pinned_to_revision(tmp_path):
    store = ArchiveStore(tmp_path)
    original = Record(record_id="versioned", record_kind="cognitive_run", body_markdown="旧材料")
    commit(store, original)
    first = read_page(store, original.ref, store.snapshot(), [])
    audit = read_audit(roundtrip(call("read_record", ref=original.ref), first))
    revised = Record(record_id="versioned", revision=2, supersedes_revision=1,
                     record_kind="cognitive_run", body_markdown=LONG)
    store.commit([revised], idempotency_key="revision", expected={"versioned": 1})
    page = read_page(store, revised.ref, store.snapshot(), audit, offset=4000)
    assert page["read_progress"]["first_unread_offset"] == 0
    assert not page["read_progress"]["complete"]

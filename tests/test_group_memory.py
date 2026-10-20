from copy import deepcopy

import pytest

from myatri.cognition.experience import ExperienceOrganizer
from myatri.contracts import RecallBundle, digest
from myatri.providers import Gateway
from myatri.retrieval import RecallService
from myatri.storage import ArchiveStore
from myatri_bridge.group_memory import GroupRecall, SummaryArchive
from myatri_bridge.identity import IdentityRegistry


def messages():
    return [
        {"message_id": "m1", "sender_id": "a", "sender_name": "同名",
         "sent_at": "2026-10-21T10:00:00+08:00", "text": '我喜欢"红茶"。\n今天也想喝。'},
        {"message_id": "m2", "sender_id": "b", "sender_name": "同名",
         "sent_at": "2026-10-21T10:00:01+08:00", "text": "我喜欢咖啡。"},
        {"message_id": "m3", "sender_id": "a", "sender_name": "同名",
         "sent_at": "2026-10-21T10:00:02+08:00", "text": "乙喜欢咖啡，这是他以前告诉我的。"},
    ]


def item(speaker="a", subject="a", mid="m1", quote='我喜欢"红茶"', attribution="self_report"):
    return {"text": "该成员自述喜欢红茶。" if attribution == "self_report" else "乙喜欢咖啡。",
            "speaker_id": speaker, "subject_id": subject, "attribution": attribution,
            "source_message_ids": [mid], "source_quotes": [{"message_id": mid, "quote": quote}]}


def summary(items=None, *, end=100):
    return {"items": items if items is not None else [item()], "window_start": end - 99,
            "window_end": end, "created_at": "2026-10-21T02:01:00+00:00", "kind": "llm",
            "generator": {"provider": "offline-test", "model": "script", "prompt_version": "test/v1", "run_id": "summary_test"}}


@pytest.fixture
def setup(tmp_path):
    store = ArchiveStore(tmp_path / "archive", agent_id="xiaowennuan", user_id="group")
    identities = IdentityRegistry(tmp_path / "identities.sqlite3", owner_id="owner", owner_name="原私聊本人")
    for message in messages():
        identities.observe("room", message)
    return store, identities, SummaryArchive(store, identities, "room")


def test_summary_persists_actual_member_id_and_exact_original_quote_span(setup):
    store, identities, archive = setup
    quote = '"红茶"。\n今天'
    archive.save(summary([item(quote=quote)]), messages())
    event = store.list_records("event")[0]
    assert event.payload["participants"] == ["a"]
    span = event.payload["source_spans"][0]
    raw = store.get_record(span["message_ref"].rsplit("@", 1)[0])
    assert raw.body_markdown[span["start"]:span["end"]] == span["quote"] == quote
    assert raw.payload["channel_source"]["message_id"] == "m1"
    assert raw.payload["logical_source_key"] == digest(["room", "m1"])
    assert raw.payload["evidence_not_independent_of_same_message_id"] is True
    claim = store.list_records("interpretation")[0].payload["group_attribution"]
    assert claim["speaker_id"] == claim["subject_id"] == "a"
    assert identities.resolve_name("room", "同名")["status"] == "ambiguous"
    assert store.verify_archive() == []


def test_same_summary_retry_is_idempotent_and_overlapping_source_reused(setup):
    store, _, archive = setup
    archive.save(summary(), messages())
    count = len(store.list_records())
    archive.save(deepcopy(summary()), deepcopy(messages()))
    assert len(store.list_records()) == count
    archive.save(summary(end=200), messages())
    assert len(store.list_records("raw_message")) == 1
    assert len(store.list_records("event")) == 2
    assert store.verify_archive() == []


@pytest.mark.parametrize("change", ["summary", "source"])
def test_same_window_cannot_silently_accept_changed_content(setup, change):
    store, _, archive = setup
    archive.save(summary(), messages())
    original_count = len(store.list_records())
    value, originals = summary(), messages()
    if change == "summary":
        value["items"][0]["text"] = "篡改成喜欢咖啡"
    else:
        originals[0]["text"] += "内容已改变"
    with pytest.raises(ValueError, match="changed|different content"):
        archive.save(value, originals)
    assert len(store.list_records()) == original_count


@pytest.mark.parametrize("bad_item,reason", [
    (item(speaker="b", subject="b"), "evidence_speaker_mismatch"),
    (item(subject="b"), "self_report_subject_mismatch"),
    (item(quote="我喜欢不存在的东西"), "quote_not_found"),
    ({**item(), "source_message_ids": ["m2"]}, "source IDs"),
    ({**item(), "source_message_ids": ["missing"]}, "source IDs"),
])
def test_false_or_cross_person_evidence_is_rejected_atomically(setup, bad_item, reason):
    store, _, archive = setup
    with pytest.raises(ValueError, match=reason):
        archive.save(summary([bad_item]), messages())
    assert store.list_records() == []


def test_conflicting_duplicate_message_ids_rejected(setup):
    store, _, archive = setup
    originals = messages()
    originals.append({**originals[0], "sender_id": "b"})
    with pytest.raises(ValueError, match="duplicate"):
        archive.save(summary(), originals)
    assert store.list_records() == []


def test_third_person_claim_is_reported_without_external_confirmation(setup):
    store, _, archive = setup
    reported = item("a", "b", "m3", "乙喜欢咖啡", "reported")
    archive.save(summary([reported]), messages())
    event = store.list_records("event")[0]
    assert event.payload["participants"] == ["a", "b"]
    assert event.payload["reality_scope"] == "reported"
    interpretation = store.list_records("interpretation")[0]
    assert interpretation.payload["group_attribution"]["attribution"] == "reported"
    assert interpretation.payload["verified_external_fact"] is False


def test_personal_recall_filters_reporter_from_subject_and_hides_bundle_paths(setup, monkeypatch):
    store, identities, archive = setup
    own_a = item()
    own_b = item("b", "b", "m2", "我喜欢咖啡")
    reported = item("a", "b", "m3", "乙喜欢咖啡", "reported")
    archive.save(summary([own_a, own_b, reported]), messages())
    all_records = store.list_records()
    events = store.list_records("event")
    refs = [record.ref for record in events]
    bundle = RecallBundle(snapshot=store.snapshot(), records=all_records,
        reasons={ref: ["test"] for ref in refs}, paths=[refs], counter_refs=refs)
    monkeypatch.setattr(RecallService, "recall", lambda *args, **kwargs: bundle)
    recall = GroupRecall(store, None, identities, "room")
    recall.current_sender = "a"
    result = recall.recall("我的喜好", budget=20000)
    kept_events = [record for record in result.records if record.record_kind == "event"]
    assert len(kept_events) == 1
    assert kept_events[0].payload["participants"] == ["a"]
    kept = {record.ref for record in result.records}
    assert all(ref in kept for ref in result.reasons)
    assert all(ref in kept for ref in result.counter_refs)
    assert all(ref in kept for path in result.paths for ref in path)
    recall.current_sender = "b"
    theirs = recall.recall("我的喜好", budget=20000)
    assert len([record for record in theirs.records if record.record_kind == "event"]) == 2
    assert recall.recall("咖啡", budget=20000) is bundle


async def test_summary_checkpoint_stops_core_from_reextracting_same_raw_source(setup):
    store, _, archive = setup
    archive.save(summary(), messages())

    class NeverCall:
        name = "offline-no-call"

        async def generate(self, request):
            raise AssertionError("already covered raw evidence must not call a provider")

    organizer = ExperienceOrganizer(store, Gateway(NeverCall()))
    assert await organizer.organize_batch(store.list_records("raw_message"), lambda proposal: None) == []

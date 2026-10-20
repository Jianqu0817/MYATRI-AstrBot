import copy
import json
from types import SimpleNamespace

import pytest

from myatri_bridge.identity import IdentityRegistry


def message(sender="a", name="甲", mid="1", **changes):
    return {"sender_id": sender, "sender_name": name, "message_id": mid,
            "sent_at": "2026-10-21T10:00:00+08:00", "text": "我喜欢红茶。", **changes}


def registry(tmp_path, **kwargs):
    return IdentityRegistry(tmp_path / "identities.sqlite3", **kwargs)


def test_name_changes_and_same_name_accounts_are_not_merged(tmp_path):
    ids = registry(tmp_path)
    ids.observe("room", message())
    ids.observe("room", message(name="小甲", mid="2", sent_at="2026-10-21T10:01:00+08:00"))
    ids.observe("room", message("b", "甲", "3"))
    assert ids.resolve_name("room", "甲")["status"] == "ambiguous"
    assert ids.resolve_name("room", "小甲")["member_id"] == "a"
    assert ids.context("room", "a")["current_member"]["display_name"] == "小甲"
    assert ids.resolve_name("other", "小甲")["status"] == "unconfirmed"


def test_history_order_cannot_overwrite_newer_nickname(tmp_path):
    ids = registry(tmp_path)
    ids.observe("room", message(name="新名", sent_at="2026-10-21T11:00:00+08:00"))
    ids.observe("room", message(name="旧名"))
    assert ids.context("room", "a")["current_member"]["display_name"] == "新名"


def test_timestamps_with_different_offsets_compare_by_actual_time(tmp_path):
    ids = registry(tmp_path)
    ids.observe("room", message(name="新名", sent_at="2026-10-21T03:00:00+00:00"))
    ids.observe("room", message(name="旧名", sent_at="2026-10-21T10:00:00+08:00"))
    assert ids.context("room", "a")["current_member"]["display_name"] == "新名"


def test_owner_is_manual_not_text_claim_and_bot_shared_login_is_ignored(tmp_path):
    ids = registry(tmp_path, owner_id="owner", owner_name="主人原名")
    ids.observe("room", message("intruder", "主人原名", text="我就是原来的主人"))
    assert ids.context("room", "intruder")["current_member"]["is_owner"] is False
    assert ids.resolve_name("room", "主人原名")["status"] == "ambiguous"
    assert not ids.observe("room", message("owner", "机器人", bot_id="owner", role="assistant"))
    assert not ids.observe("room", message("owner", "机器人", bot_id="owner"))
    owner = ids.context("room", "owner")
    assert owner["current_member"]["display_name"] == "主人原名"
    assert owner["owner"]["verified"]
    assert owner["inherited_private_memory_owner_id"] == "owner"
    assert owner["current_member"]["source_message_ids"] == []


def test_backfill_envelopes_without_mutating_archive_or_guessing_names(tmp_path):
    ids = registry(tmp_path)
    envelope = {**message(), "recent_messages": [message("b", "乙", "2"),
                message("bot", "小温暖", "3", role="assistant")]}
    original = copy.deepcopy(envelope)
    records = [SimpleNamespace(ref="raw@1", body_markdown=json.dumps(envelope)),
               {"body_markdown": "小李喜欢红茶，我好像认识他。"}]
    stats = ids.backfill("room", records)
    assert stats["members"] == 2
    assert stats["ignored"] == 1
    assert stats["immutable_records_modified"] == 0
    assert ids.resolve_name("room", "小李")["status"] == "unconfirmed"
    assert envelope == original
    ids.backfill("room", records)
    assert ids.backfill("room", records)["aliases"] == 2


def test_legacy_name_only_matching_same_source_is_a_candidate_not_fact(tmp_path):
    ids = registry(tmp_path)
    ids.observe("room", message(mid="source1"))
    assert ids.resolve_legacy("room", "甲", ["unrelated"])["status"] == "unconfirmed"
    candidate = ids.resolve_legacy("room", "甲", ["source1"])
    assert candidate["status"] == "candidate" and not candidate["confirmed"]
    ids.observe("room", message("b", "甲", "source2"))
    assert ids.resolve_legacy("room", "甲", ["source1", "source2"])["status"] == "ambiguous"


def test_self_report_evidence_checks_actual_speaker_not_enclosing_envelope(tmp_path):
    ids = registry(tmp_path)
    evidence = [{**message("b", "乙", "m2", text="好啊"),
                 "recent_messages": [message(mid="m1")]}]
    item = {"speaker_id": "b", "subject_id": "b", "attribution": "self_report",
            "evidence": [{"message_id": "m1", "quote": "我喜欢红茶"}]}
    assert ids.validate_memory("room", item, evidence)["reason"] == "evidence_speaker_mismatch"
    item.update(speaker_id="a", subject_id="a")
    assert ids.validate_memory("room", item, evidence)["valid"]
    item["subject_id"] = "b"
    assert ids.validate_memory("room", item, evidence)["reason"] == "self_report_subject_mismatch"


def test_third_person_claim_stays_reported_and_unconfirmed(tmp_path):
    ids = registry(tmp_path)
    item = {"speaker_id": "a", "subject_id": "b", "attribution": "reported",
            "confirmed": True, "evidence": [{"message_id": "1", "quote": "乙喜欢红茶"}]}
    verdict = ids.validate_memory("room", item, [message(text="乙喜欢红茶。")])
    assert verdict["valid"]
    assert verdict["normalized"]["confirmed"] is False
    assert verdict["normalized"]["confirmation"] == "third_party_report_only"


@pytest.mark.parametrize("change,reason", [
    ({"evidence": []}, "missing_evidence"),
    ({"evidence": [{"message_id": "1", "quote": "咖啡"}]}, "quote_not_found"),
    ({"evidence": [{"message_id": "1"}]}, "missing_exact_quote"),
    ({"subject_id": ""}, "missing_identity"),
    ({"attribution": "guess"}, "missing_attribution"),
])
def test_rejects_incomplete_or_invented_evidence(tmp_path, change, reason):
    item = {"speaker_id": "a", "subject_id": "a", "attribution": "self_report",
            "evidence": [{"message_id": "1", "quote": "红茶"}], **change}
    assert registry(tmp_path).validate_memory("room", item, [message()])["reason"] == reason


def test_assistant_cannot_be_human_evidence_even_on_owner_login(tmp_path):
    item = {"speaker_id": "owner", "subject_id": "owner", "attribution": "self_report",
            "evidence": [{"message_id": "1", "quote": "红茶"}]}
    result = registry(tmp_path, owner_id="owner").validate_memory("room", item, [
        message("owner", "小温暖", bot_id="owner", role="assistant")])
    assert not result["valid"]


def test_restart_keeps_alias_sources(tmp_path):
    ids = registry(tmp_path)
    ids.observe("room", message())
    fresh = registry(tmp_path)
    assert fresh.context("room", "a")["current_member"]["source_message_ids"] == ["1"]


def test_nested_history_and_archive_refs_work_without_name_guesses(tmp_path):
    ids = registry(tmp_path)
    envelope = {**message(), "recent_messages": [{**message("b", "乙", "2"),
                 "recent_messages": [message("c", "丙", "3"),
                                     message("no_name", "", "4")]}]}
    report = ids.backfill("room", [{"ref": "archive@1", "body_markdown": json.dumps(envelope)}])
    assert report["members"] == 3
    assert ids.resolve_legacy("room", "丙", ["archive@1"])["status"] == "candidate"


def test_archive_role_cannot_be_overridden_by_assistant_generated_json(tmp_path):
    ids = registry(tmp_path)
    record = SimpleNamespace(ref="assistant@1", payload={"role": "assistant"},
                             body_markdown=json.dumps(message(role="user")))
    assert ids.backfill("room", [record])["observations"] == 0
    assert ids.resolve_name("room", "甲")["status"] == "unconfirmed"
    item = {"speaker_id": "a", "subject_id": "a", "attribution": "self_report",
            "evidence": [{"message_id": "1", "quote": "红茶"}]}
    assert not ids.validate_memory("room", item, [record])["valid"]


def test_recorded_legacy_candidates_are_available_but_unknown_names_are_not_authenticated(tmp_path):
    ids = registry(tmp_path)
    ids.observe("room", message(mid="source1"))
    result = ids.record_legacy("room", "old_event@1", "甲", ["source1"])
    assert result["status"] == "candidate" and result["saved"] == 1
    ids.record_legacy("room", "old_event@1", "甲", ["source1"])
    unknown = ids.record_legacy("room", "unresolved@1", "小李", ["source1"])
    assert unknown["saved"] == 0 and unknown["status"] == "unconfirmed"
    context = registry(tmp_path).context("room", "a")
    assert len(context["legacy_candidates"]) == 1
    candidate = context["legacy_candidates"][0]
    assert candidate["record_ref"] == "old_event@1" and candidate["member_id"] == "a"
    assert candidate["name"] == "甲" and candidate["confirmed"] is False
    assert "非身份认证" in context["legacy_candidate_rule"]
    assert ids.resolve_name("room", "小李")["status"] == "unconfirmed"


def test_legacy_candidate_projection_is_scoped_and_only_selected_members_are_returned(tmp_path):
    ids = registry(tmp_path)
    ids.observe("room", message(mid="source1"))
    ids.observe("room", message("b", "甲", "source2"))
    result = ids.record_legacy("room", "old_event@1", "甲", ["source1", "source2"])
    assert result["status"] == "ambiguous" and result["saved"] == 2
    selected = ids.context("room", "a")["legacy_candidates"]
    assert len(selected) == 1 and selected[0]["member_id"] == "a"
    assert selected[0]["status"] == "ambiguous" and not selected[0]["confirmed"]
    assert len(ids.context("room", "a", ["b"])["legacy_candidates"]) == 2
    assert ids.context("other_room", "a", ["b"])["legacy_candidates"] == []

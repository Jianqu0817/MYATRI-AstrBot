"""Fixture integrity and bounded real-chain replay, without model generation."""
from collections import Counter
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/evaluate_group_memory.py"
SPEC = importlib.util.spec_from_file_location("quality_evaluation", MODULE_PATH)
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


def case(template):
    return next(row for row in evaluation.generate_cases() if row["template_id"].endswith("/" + template))


def test_committed_dataset_is_deterministic_and_explicitly_synthetic():
    generated = evaluation.generate_cases()
    assert evaluation.load_cases() == generated
    evaluation.validate_cases(generated)
    assert len(generated) == 200
    assert len({row["template_id"] for row in generated}) == 40
    assert Counter(row["category"] for row in generated) == dict.fromkeys({row["category"] for row in generated}, 20)
    assert Counter(row["split"] for row in generated) == {"development": 150, "heldout": 50}
    assert all(row["synthetic"] for row in generated)
    for template in {row["template_id"] for row in generated}:
        variants = [row for row in generated if row["template_id"] == template]
        assert len({tuple(message["text"] for message in row["messages"]) for row in variants}) == 5
        assert len({row["split"] for row in variants}) == 1


@pytest.mark.parametrize("fault", ["duplicate_case", "duplicate_message", "unknown_expected", "overlap", "missing_text", "bad_time", "wrong_quote", "wrong_speaker", "split_leak"])
def test_dataset_validator_rejects_broken_evidence_or_split(fault):
    rows = deepcopy(evaluation.generate_cases()[:2])
    row = rows[0]
    if fault == "duplicate_case":
        rows[1]["case_id"] = row["case_id"]
    elif fault == "duplicate_message":
        row["messages"][1]["message_id"] = row["messages"][0]["message_id"]
    elif fault == "unknown_expected":
        row["expected"]["source_ids"] = ["missing"]
    elif fault == "overlap":
        row["expected"]["forbidden_source_ids"] = row["expected"]["source_ids"]
    elif fault == "missing_text":
        row["messages"][0]["text"] = ""
    elif fault == "bad_time":
        row["messages"][0]["sent_at"] = "next Tuesday"
    elif fault == "wrong_quote":
        row["ingestion_claims"][0]["source_quotes"][0]["quote"] = "made up"
    elif fault == "wrong_speaker":
        row["ingestion_claims"][0]["speaker_id"] = "impostor"
    else:
        rows[1]["split"] = "heldout"
    with pytest.raises(ValueError):
        evaluation.validate_cases(rows)


def test_blind_export_has_no_answers_or_ingestion_annotations(tmp_path):
    rows = [case("preference_change"), case("missing_person")]
    evaluation.export_blind(rows, tmp_path)
    public = [json.loads(line) for line in (tmp_path / "inputs.jsonl").read_text(encoding="utf-8").splitlines()]
    gold = [json.loads(line) for line in (tmp_path / "WITHHELD_scoring_key.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(set(row) == {"case_id", "messages", "query", "current_sender"} for row in public)
    assert [row["expected"] for row in gold] == [row["expected"] for row in rows]


def test_cli_embedding_defaults_and_local_model_selection(tmp_path):
    parser = evaluation.build_parser()
    args = parser.parse_args([])
    assert args.embedding == "tfidf"
    assert args.model_path is None
    assert evaluation.load_embedding(args.embedding, args.model_path) is None
    assert evaluation.embedding_metadata(None)["implicit_download"] is False
    args = parser.parse_args(["--embedding", "bge", "--model-path", str(tmp_path)])
    assert args.embedding == "bge"
    assert args.model_path == tmp_path
    with pytest.raises(SystemExit):
        parser.parse_args(["--embedding", "untrusted-model"])
    with pytest.raises(ValueError, match="requires --model-path"):
        evaluation.load_embedding("bge", None)


async def test_selected_provider_is_reused_across_case_indexes(tmp_path, monkeypatch):
    class Provider:
        model = "test-provider"
        model_revision = "pinned-test-revision"
        space_id = "test-space"
        dimension = 2
        normalization = "l2"

    provider = Provider()
    seen = []

    async def replay(row, directory, top_k, *, embedding_provider=None):
        seen.append(embedding_provider)
        return {"source_ids": [], "claim_subject_ids": [], "limitations": []}

    monkeypatch.setattr(evaluation, "replay_case", replay)
    result = await evaluation.evaluate([case("same_name"), case("preference_change")], embedding_provider=provider)
    assert seen == [provider, provider]
    assert result["embedding"]["revision"] == "pinned-test-revision"
    assert result["llm_api_calls"] == 0
    assert result["local_embedding_inference"] is True


def test_metrics_distinguish_source_misses_and_cross_member_exposure():
    row = case("same_name")
    expected = row["expected"]["source_ids"][0]
    forbidden = row["expected"]["forbidden_source_ids"][0]
    metrics = evaluation.score_sources(row, {"source_ids": [expected, forbidden], "claim_subject_ids": ["member_a", "member_b"]})
    assert metrics["source_recall"] == 1
    assert metrics["source_precision"] == 0.5
    assert metrics["forbidden_source_exposure"] is True
    assert metrics["cross_member_claim_exposure"] is True
    # Raw-message baseline has no claims; unavailable is not a successful zero.
    assert evaluation.score_sources(row, {"source_ids": []})["cross_member_claim_exposure"] is None
    assert evaluation.score_sources(case("missing_person"), {"source_ids": []})["source_recall"] is None


async def test_real_chain_does_not_read_scoring_key_and_filters_same_name(tmp_path):
    class NoGold(dict):
        def __getitem__(self, key):
            if key == "expected":
                raise AssertionError("retrieval must not inspect gold expected values")
            return super().__getitem__(key)

    for template in ("same_name", "reporter_not_subject", "quotes_not_self"):
        row = case(template)
        result = await evaluation.replay_case(NoGold(row), tmp_path / template)
        assert set(result["source_ids"]) == set(row["expected"]["source_ids"])
        assert result["claim_subject_ids"] == ["member_a"]


@pytest.mark.parametrize("template,status", [("preference_change", "superseded"), ("typo", "superseded"), ("retraction", "retracted")])
async def test_real_claim_projection_preserves_correction_evidence(tmp_path, template, status):
    row = case(template)
    result = await evaluation.replay_case(row, tmp_path)
    projection = result["claim_projection"]
    assert len(projection["active"]) == 1
    assert projection["active"][0]["source_message_ids"] == [row["messages"][1]["message_id"]]
    assert len(projection["history"]) == 2
    assert projection["history"][0]["status"] == status


async def test_explicit_quoted_context_keeps_original_without_other_member_claims(tmp_path):
    for template in ("short_accept", "rejection", "two_invitations"):
        row = case(template)
        result = await evaluation.replay_case(row, tmp_path / template)
        metrics = evaluation.score_sources(row, result)
        assert metrics["source_recall"] == 1
        assert metrics["cross_member_claim_exposure"] is False
        assert all(message_id in {message["message_id"] for message in row["messages"]}
                   for message_id in result["source_ids"])


async def test_evaluation_emits_separate_baseline_and_mixed_aggregates(tmp_path):
    result = await evaluation.evaluate([case("missing_person"), case("preference_change")])
    assert result["model_calls"] == 0
    assert result["synthetic"] is True
    assert "not answer quality" in result["scope"]
    assert {row["method"] for row in result["aggregates"]} == {"member_mixed", "unfiltered_lexical"}

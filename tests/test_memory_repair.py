from copy import deepcopy
import json
from types import SimpleNamespace
import ast
import inspect
import textwrap

import pytest

from myatri.contracts import ModelResponse, Record
from myatri.storage import ArchiveStore
from myatri_bridge.memory_repair import ExperienceOrganizer, AssociationAgent, UnresolvedCandidates


def test_maintenance_orchestration_is_identical_to_frozen_core_only_factories_differ():
    from myatri.runtime.maintenance import Maintenance
    from myatri_bridge.maintenance import BridgeMaintenance
    def tree(method):
        return ast.dump(ast.parse(textwrap.dedent(inspect.getsource(method))), include_attributes=False)
    assert tree(Maintenance.run) == tree(BridgeMaintenance.run)


def source(store, rid="raw", sender="member", text="我喜欢红茶，也喜欢散步。"):
    row = Record(record_id=rid, record_kind="raw_message", agent_id=store.agent_id, user_id=store.user_id,
        body_markdown=json.dumps({"sender_id": sender, "text": text}, ensure_ascii=False),
        payload={"role": "user", "channel_source": {"sender_id": sender, "known_member_ids": ["member", "other"]}})
    store.commit([row], rid, expected={rid: 0})
    return row


def candidate(message, **extra):
    return {"message_ref": message.ref, "summary": "喜欢红茶", "quote": "我喜欢红茶", "reality_scope": "reported",
        "occurred_at": None, "event_key": "tea", "speaker_id": "member", "subject_id": "member",
        "attribution": "self_report", "participants": ["member"], **extra}


def gateway(value):
    requests = []
    async def generate(request):
        requests.append(request)
        return ModelResponse(content=json.dumps(value, ensure_ascii=False), provider="offline-script", model="fixture")
    return SimpleNamespace(generate=generate, provider=SimpleNamespace(name="offline-script"), requests=requests)


async def test_mixed_a1_bad_quote_and_unknown_identity_are_audited_without_fabrication(tmp_path):
    store = ArchiveStore(tmp_path, "xiaowennuan", "group")
    raw = source(store)
    good = candidate(raw)
    g = gateway({"events": [good, candidate(raw, quote="捏造的引文"), candidate(raw, subject_id="unknown")]})
    organizer = ExperienceOrganizer(store, g)
    await organizer.organize_batch([raw], lambda proposal: store.commit(proposal.records, proposal.proposal_id,
        expected=proposal.read_versions))
    assert len(store.list_records("event")) == 1
    assert store.list_records("event")[0].payload["participants"] == ["member"]
    audits = [r for r in store.list_records("cognitive_run") if r.payload.get("role") == "bridge_memory_validation"]
    assert len(audits) == 1 and len(audits[0].payload["rejected_candidates"]) == 2
    run = next(r for r in store.list_records("cognitive_run") if r.payload.get("role") == "A1")
    assert not run.payload["source_coverage"]["validation_complete"]
    assert run.payload["source_coverage"]["quarantined_candidates"] == 2
    assert g.requests[0].max_tokens == 8192
    count = len(g.requests)
    await ExperienceOrganizer(store, g).organize_batch([raw], lambda p: None)
    assert len(g.requests) == count  # Durable organization/checkpoints survive restarts.
    assert store.verify_archive() == []


async def test_all_bad_a1_candidates_leave_source_pending_and_audit_deduplicates(tmp_path):
    store = ArchiveStore(tmp_path, "xiaowennuan", "group")
    raw = source(store)
    organizer = ExperienceOrganizer(store, gateway({"events": [candidate(raw, quote="invented")]}))
    for _ in range(2):
        with pytest.raises(UnresolvedCandidates):
            await organizer.organize_batch([raw], lambda p: None)
    assert not organizer._organized(raw) and store.list_records("event") == []
    assert len(store.list_records("cognitive_run")) == 1


async def test_merge_ownership_is_never_fixed_by_reassigning_member_or_dropping_old_ref(tmp_path):
    store = ArchiveStore(tmp_path, "xiaowennuan", "group")
    old = source(store, "old", "other")
    old_g = gateway({"events": [candidate(old, speaker_id="other", subject_id="other", participants=["other"])]})
    await ExperienceOrganizer(store, old_g).organize_batch([old], lambda p: store.commit(p.records,p.proposal_id,
        expected=p.read_versions))
    prior = store.list_records("event")[0]
    raw = source(store, "new")
    item = candidate(raw, existing_event_ref=prior.ref)
    original = deepcopy(item)
    organizer = ExperienceOrganizer(store, gateway({"events": [item]}))
    with pytest.raises(UnresolvedCandidates):
        await organizer.organize_batch([raw], lambda p: None)
    assert item == original and len(store.list_records("event")) == 1
    assert store.list_records("event")[0].payload["participants"] == ["other"]


@pytest.mark.parametrize("only_bad", [False, True])
async def test_association_bad_edges_defer_or_keep_good_peers_without_relaxing_bounds(tmp_path, only_bad):
    store = ArchiveStore(tmp_path)
    a,b,c = source(store,"a"),source(store,"b"),source(store,"c")
    good = {"edge_id": "edge_good", "from_ref": a.ref, "to_ref": b.ref, "type": "supports", "evidence_refs": [a.ref]}
    bad = {**good, "edge_id": "edge_bad", "to_ref": c.ref}
    index = SimpleNamespace(graph=SimpleNamespace(edges=[{"type":"similar","from_ref":a.ref,"to_ref":b.ref}]))
    g = gateway({"decision":"propose","reason":"offline fixture","edges":[bad] if only_bad else [good,bad]})
    proposal = await AssociationAgent(store,g,index).propose([a.ref])
    store.commit(proposal.records,proposal.proposal_id,expected=proposal.read_versions)
    assert len(store.list_records("edge")) == (0 if only_bad else 1)
    run = store.list_records("cognitive_run")[0]
    assert run.payload["decision"] == ("defer_with_quarantine" if only_bad else "propose_with_quarantine")
    assert len(run.payload["rejected_candidates"]) == 1
    assert store.verify_archive() == []

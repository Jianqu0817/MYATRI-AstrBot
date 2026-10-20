"""Offline, idempotent projections from existing group envelopes; no model/sender."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import tomllib
from contextlib import closing

from myatri.storage import ArchiveStore
from myatri_bridge.bridge import Incoming
from myatri_bridge.identity import IdentityRegistry, _messages
from myatri_bridge.short_memory import ShortMemory
from myatri_bridge.cold_archive import ColdArchive
from myatri_bridge.topics import TopicTracker


PROJECT = Path(__file__).resolve().parents[1]


def archive_view(root):
    """Read committed manifests without constructor recovery/projection writes."""
    for domain in (root / "domains").iterdir():
        manifests = sorted((domain / "commits").glob("*.json"))
        if not manifests:
            continue
        header = json.loads(manifests[0].read_text(encoding="utf-8"))
        if (header["agent_id"], header["user_id"]) != ("xiaowennuan", "group"):
            continue
        store = ArchiveStore.__new__(ArchiveStore)
        store.root, store.domain_path = root, domain
        store.agent_id, store.user_id = "xiaowennuan", "group"
        store.manifest_path = domain / "commits"
        store._thread_lock = threading.RLock()
        return store
    raise ValueError("no committed group archive")


def backfill(project=PROJECT):
    config = tomllib.loads((project / "config/bridge.local.toml").read_text(encoding="utf-8-sig"))["bridge"]
    root = project / config["state_root"]
    identities = IdentityRegistry(root / "identities.sqlite3", owner_id=config.get("owner_id", ""),
                                  owner_name=config.get("owner_name", ""))
    memory = ShortMemory(root / "short-memory.sqlite3")
    cold = ColdArchive(root / "cold-archive.sqlite3", config.get("cold_retention_days", 0))
    topics = TopicTracker(root / "topics.sqlite3")
    reports = []
    for group in sorted(root.iterdir()):
        if not group.is_dir() or not (group / "domains").exists():
            continue
        store = archive_view(group)
        snapshot = store.snapshot()
        records = store.list_records(snapshot=snapshot)
        raw = [r for r in records if r.record_kind == "raw_message"]
        identity_report = identities.backfill(group.name, raw)
        recovered, conflicts, routes = {}, [], []
        for record in raw:
            for item in _messages(record):
                try:
                    message = Incoming.model_validate({k: item[k] for k in Incoming.model_fields if k != "mentioned" and k in item}
                                                       | {"mentioned": False})
                except (KeyError, ValueError):
                    continue
                if message.route not in config.get("allowed_groups", []) or message.scope != group.name:
                    continue
                if item.get("role") == "assistant" or message.sender_id == message.bot_id:
                    continue
                previous = recovered.get(message.message_id)
                if previous and previous.model_dump() != message.model_dump():
                    conflicts.append(message.message_id)
                else:
                    recovered[message.message_id] = message
        for native_id in set(conflicts):
            recovered.pop(native_id, None)
        added = 0
        ordered = sorted(recovered.values(), key=lambda m: (datetime.fromisoformat(m.sent_at), m.message_id))
        for message in ordered:
            identities.observe(group.name, message.model_dump())
            added += memory.add(group.name, message.model_dump())
            payload = message.model_dump(exclude={"mentioned"})
            payload["topic_id"] = topics.add(group.name, payload)
            cold.add(group.name, payload)
            routes.append(message)
        with closing(sqlite3.connect(root / "deliveries.sqlite3")) as db:
            confirmed = db.execute("SELECT id,reply,updated,envelope FROM deliveries WHERE scope=? AND status='sent'",
                                   (group.name,)).fetchall()
        for key, reply, stamp, envelope in confirmed:
            if not envelope or not reply:
                continue
            original = json.loads(envelope)
            own = {k: original[k] for k in ("platform_id", "bot_id", "group_id")}
            own.update(sender_id=original["bot_id"], sender_name="小温暖", message_id="reply_"+key,
                       reply_to_id=original["message_id"], text=reply, role="assistant",
                       sent_at=datetime.fromtimestamp(stamp, timezone.utc).isoformat())
            own["topic_id"] = topics.add(group.name, own)
            memory.add_assistant_context(group.name, own)
            if not cold.read(group.name, own["message_id"]).get("available"):
                cold.add(group.name, own)
        if routes:
            with closing(sqlite3.connect(root / "deliveries.sqlite3")) as db, db:
                db.execute("CREATE TABLE IF NOT EXISTS group_routes(scope TEXT PRIMARY KEY,payload TEXT NOT NULL)")
                old = db.execute("SELECT payload FROM group_routes WHERE scope=?", (group.name,)).fetchone()
                if not old or datetime.fromisoformat(json.loads(old[0])["sent_at"]) <= datetime.fromisoformat(routes[-1].sent_at):
                    db.execute("INSERT OR REPLACE INTO group_routes VALUES (?,?)",
                               (group.name, json.dumps(routes[-1].model_dump(), ensure_ascii=False)))
        legacy = []
        for record in records:
            original = record.payload.get("original", {})
            payload = original.get("payload", {}) if isinstance(original, dict) else {}
            names = payload.get("participants", record.payload.get("participants", []))
            for name in names:
                if not isinstance(name, str) or name.startswith("wxid_"):
                    continue
                resolved = identities.record_legacy(group.name, record.ref, name, record.source_refs)
                legacy.append({"record_ref": record.ref, "name": name, **resolved})
        with closing(sqlite3.connect(identities.path)) as db:
            members = [dict(zip(("member_id", "display_name", "last_seen"), row, strict=True)) for row in db.execute(
                "SELECT member_id,display_name,last_seen FROM members WHERE scope=?", (group.name,))]
        reports.append({"scope": group.name, "read_snapshot": snapshot, "identity_backfill": identity_report,
            "members": members, "native_messages_recovered": len(recovered), "new_listening_messages": added,
            "conflicting_message_ids_skipped": sorted(set(conflicts)), "listening": memory.stats(group.name),
            "legacy_name_candidates": legacy, "immutable_records_modified": 0})
    memory.close()
    result = {"created_at": datetime.now(timezone.utc).isoformat(), "owner_id": identities.owner_id,
              "owner_name": identities.owner_name, "groups": reports,
              "limitations": ["Only native IDs already present in channel envelopes can be recovered.",
                              "Old private memories belong to the configured owner; name-only third parties remain unconfirmed.",
                              "No model call, archive rewrite, persona modification or message send performed."]}
    target = project / "var/diagnostics/group-memory-backfill.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    result = backfill()
    print(json.dumps({"groups": [{"scope": group["scope"], "members": len(group["members"]),
        "recovered": group["native_messages_recovered"], "new": group["new_listening_messages"],
        "listening": group["listening"]} for group in result["groups"]]}, ensure_ascii=False))

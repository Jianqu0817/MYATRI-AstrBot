import importlib.util
from pathlib import Path

import pytest

from myatri.app import Application
from myatri.contracts import Record


def migration_module():
    file = Path(__file__).resolve().parents[1] / "scripts/migrate_private_memory.py"
    spec = importlib.util.spec_from_file_location("memory_migration", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_keeps_both_archives_and_backups_with_explicit_history_scope(tmp_path):
    module = migration_module()
    module.PROJECT = tmp_path / "bridge"
    source = Application(tmp_path / "private")
    source.store.commit([Record(record_id="old", record_kind="raw_message", author_kind="human",
                                body_markdown="我喜欢草莓。", payload={"role": "user"})], idempotency_key="old")
    target = Application(module.PROJECT / "var/groups/room", "xiaowennuan", "group")
    current = Record(record_id="group", record_kind="raw_message", agent_id="xiaowennuan", user_id="group",
                     body_markdown="我是其他群成员。", payload={"role": "user"})
    target.store.commit([current], idempotency_key="group")
    snapshot = source.store.snapshot()
    report = module.migrate(source.root, target.root)
    assert source.store.snapshot() == snapshot
    assert Path(report["source_backup"]).is_file() and Path(report["target_backup"]).is_file()
    assert Path(report["frozen_archive"]).is_dir()
    assert target.store.get_record("group") == current
    assert target.store.list_records("raw_message") == [current]
    assert report["raw_messages"] == 1 and report["archives_verified"]
    persona = target.store.get_record("persona")
    assert "不代表当前发言者就是原对话人" in persona.payload["entries"]["P16_private_memory_import"]
    assert not target.store.verify_archive()
    assert not source.gateway.provider.requests and not target.gateway.provider.requests


def test_migration_refuses_an_unscoped_target(tmp_path):
    module = migration_module()
    module.PROJECT = tmp_path / "bridge"
    with pytest.raises(ValueError, match="one explicit group"):
        module.migrate(tmp_path / "private", tmp_path / "unrelated")

import importlib.util
import json
from pathlib import Path

from myatri_bridge.cold_archive import ColdArchive


async def test_old_envelopes_without_new_optional_fields_backfill_to_cold(bridge, message):
    result = await bridge.receive(message)
    await bridge.claim(result["id"])
    await bridge.acknowledge(result["id"], "sent")
    await bridge.close()
    app = bridge.apps[message.scope]
    # Existing immutable record still has the original envelope/source. Optional
    # fields missing from an older release must use Incoming's defaults.
    project = bridge.root.parent
    (project / "config").mkdir()
    (project / "config/bridge.local.toml").write_text(
        '[bridge]\nstate_root="groups"\nallowed_groups=["fixture|room1"]\ncold_retention_days=30\n', encoding="utf-8")
    spec = importlib.util.spec_from_file_location("backfill_test", Path(__file__).parents[1] / "scripts/backfill_group_memory.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    first = module.backfill(project)
    assert first["groups"][0]["native_messages_recovered"] == 1
    assert first["groups"][0]["new_listening_messages"] == 0
    cold = ColdArchive(bridge.root / "cold-archive.sqlite3", 30)
    assert cold.read(message.scope, message.message_id)["message"]["text"] == message.text
    assert cold.read(message.scope, "reply_"+message.key)["message"]["role"] == "assistant"
    before = app.store.snapshot()
    second = module.backfill(project)
    assert second["groups"][0]["native_messages_recovered"] == 1
    assert app.store.snapshot() == before
    assert json.loads((project / "var/diagnostics/group-memory-backfill.json").read_text(encoding="utf-8"))["groups"]

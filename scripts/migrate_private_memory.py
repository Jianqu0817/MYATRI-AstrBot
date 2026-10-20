"""Explicit one-group, offline snapshot migration; never starts a model or sender."""
import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace

from myatri.interfaces.inspect import backup, restore_backup
from myatri.memory.imports import import_history
from myatri.memory import MemoryIndex
from myatri.persona.patch import PersonaPatcher
from myatri.persona.state import PersonaState
from myatri.storage import ArchiveStore
from myatri.storage.archive import ConflictError


PROJECT = Path(__file__).resolve().parents[1]


def view(root, agent, user):
    return SimpleNamespace(root=root, store=ArchiveStore(root, agent, user),
                           queue=SimpleNamespace(path=root / "runtime.sqlite3"))


def migrate(source, target, *, owner="菅蘧", source_agent="atri", source_user="local"):
    source, target = Path(source).resolve(), Path(target).resolve()
    if not target.is_relative_to((PROJECT / "var/groups").resolve()):
        raise ValueError("target must be one explicit group inside this project")
    if source == target or not all((p / "runtime.sqlite3").is_file() for p in [source, target]):
        raise ValueError("both source and target must be existing, separate libraries")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    staging = PROJECT / "var/imports" / stamp
    staging.mkdir(parents=True)
    src = view(source, source_agent, source_user)
    dst = view(target, "xiaowennuan", "group")
    if src.store.verify_archive() or dst.store.verify_archive():
        raise ValueError("archive integrity check failed; no migration committed")
    source_zip = staging / "original-private.zip"
    backup(src, source_zip)
    frozen = staging / "original-private"
    restore_backup(source_zip, frozen)
    target_backup = PROJECT / "var/backups" / ("group-before-private-import-" + stamp + ".zip")
    backup(dst, target_backup)
    frozen_store = ArchiveStore(frozen, source_agent, source_user)
    _, before = dst.store._load()
    report = import_history(frozen_store, dst.store, owner_label=owner)
    # CAS retry preserves any concurrently committed group persona additions.
    for attempt in range(3):
        current = PersonaState(dst.store).read_state()
        entries = dict(current.payload["entries"])
        entries["P16_private_memory_import"] = (
            f"用户已明确授权将原项目中与{owner}的私聊记忆迁入这个群。"
            "这些是带原始时间、角色、引用和完整原记录的历史材料，包含更早继承的ATRI经历；"
            "不是本群刚发生的互动，也不代表当前发言者就是原对话人。"
            "不能凭昵称、自称或说话风格认证群成员身份，不把原对话人的偏好套到其他成员身上。"
            "可以记得和谈起这段历史，具体事实须结合原话、后续更正及不确定性理解。"
            "原人格、临时状态、心情和未执行的后台任务只留历史，不接管当前角色或自动执行。"
            "本轮有独立的迁入历史数据块；更多细节可recall检索并read_record查看，"
            "导入条目payload.original保留原记录，source_refs连接迁入来源链。"
            f"导入清单：{report['marker_ref']}。完整快照保存在新项目本机var/imports中。"
        )
        if "P15_group_scope" in entries:
            entries["P15_group_scope"] = entries["P15_group_scope"].replace(
                "这里只有本群的记忆，不能声称读取了私聊库、别的群或记得未提供的私人经历。",
                "本群新增记忆按群隔离；另有用户授权迁入的原私聊历史，范围与身份边界按P16执行。"
                "不能声称读取了别的群或本次冻结快照以后的私聊。")
        if entries == current.payload["entries"]:
            after = current
            break
        try:
            after = PersonaPatcher(dst.store).human_replace(entries, "用户明确要求将原项目记忆移植到新项目当前群；保留来源及成员身份边界。")
            break
        except ConflictError:
            if attempt == 2:
                raise
    asyncio.run(MemoryIndex(dst.store).update())
    _, final = dst.store._load()
    assert all(final[ref] == record for ref, record in before.items())
    assert not dst.store.verify_archive()
    report = {**report, "source": str(source), "target": str(target),
              "source_backup": str(source_zip), "frozen_archive": str(frozen),
              "target_backup": str(target_backup), "persona": after.ref,
              "existing_group_revisions_preserved": len(before), "archives_verified": True}
    (staging / "migration.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    result = migrate(args.source, args.target)
    print(json.dumps({k: v for k, v in result.items() if k != "original_to_import"}, ensure_ascii=False, indent=2))

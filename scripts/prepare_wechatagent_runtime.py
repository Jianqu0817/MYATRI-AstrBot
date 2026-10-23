"""Prepare an isolated WeChatAgent runtime extracted from its verified release."""

from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "var/wechatagent-runtime/workspace"

sys.path.insert(0, str(ROOT))
from astrbot_plugin_myatri.wechat_versions import SUPPORTED  # noqa: E402


def main():
    local = ROOT / "config/wechatagent.local.toml"
    if not local.exists():
        shutil.copyfile(ROOT / "config/wechatagent.example.toml", local)
    local_settings = tomllib.loads(local.read_text(encoding="utf-8-sig"))["wechatagent"]
    distribution = json.loads(
        (WORKSPACE / "config/distribution.json").read_text(encoding="utf-8-sig")
    )
    config_file = WORKSPACE / "config/wechatagent.json"
    config = json.loads(config_file.read_text(encoding="utf-8-sig"))
    # We do not launch upstream's model, OneBot server or adapter services.
    config["ports"]["adapter"] = 8767
    config["account"]["nicknames"] = [local_settings["bot_nickname"]]
    config["wechat"]["wake_words"] = [local_settings["bot_nickname"]]
    for feature in (
        "proactive",
        "search",
        "context",
        "stickers",
        "memberMemory",
        "webReader",
    ):
        if feature in config["wechat"]:
            config["wechat"][feature]["enabled"] = False
    config_file.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    subprocess.run(
        [distribution["node"], str(WORKSPACE / "scripts/project-config.mjs")],
        check=True,
        cwd=WORKSPACE,
        capture_output=True,
    )
    native = WORKSPACE / "state/hook/native"
    build = json.loads((native / "build.json").read_text(encoding="utf-8-sig"))
    dll = native / "version.dll"
    spec = SUPPORTED.get(build["targetVersion"])
    if (
        spec is None
        or hashlib.sha256(dll.read_bytes()).hexdigest() != build["sha256"]
    ):
        raise RuntimeError("Native sender hash/version mismatch")
    client = WORKSPACE / spec.client_dir
    destination = client / "version.dll"
    if destination.exists():
        if destination.read_bytes() != dll.read_bytes():
            raise RuntimeError("Another native sender is already installed")
        # Already installed; skip rewriting a file the running client may hold open.
    else:
        shutil.copyfile(dll, destination)
    shutil.copyfile(
        WORKSPACE / "state/hook/native-token.txt", client / "wechatagent-hook.token"
    )
    print(
        "Isolated WeChat runtime and local target settings prepared. No account login or send."
    )


if __name__ == "__main__":
    main()

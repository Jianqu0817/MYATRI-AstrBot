"""Create local bridge configuration and install only our plugin files."""

from pathlib import Path
import json
import secrets
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def main():
    locked = json.loads((ROOT / "UPSTREAM_LOCK.json").read_text(encoding="utf-8"))[
        "AstrBot"
    ]
    upstream = ROOT / "upstream/AstrBot"
    if not upstream.exists():
        upstream.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", locked["repository"], str(upstream)], check=True
        )
        subprocess.run(
            ["git", "-C", str(upstream), "checkout", "--detach", locked["commit"]],
            check=True,
        )
    head = subprocess.check_output(
        ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True
    ).strip()
    if head != locked["commit"]:
        raise RuntimeError(
            "AstrBot HEAD differs from UPSTREAM_LOCK.json; inspect before upgrading"
        )
    dependency_lock = ROOT / "locks/astrbot.uv.lock"
    upstream_lock = upstream / "uv.lock"
    if not upstream_lock.exists():
        shutil.copyfile(dependency_lock, upstream_lock)
    elif upstream_lock.read_bytes() != dependency_lock.read_bytes():
        raise RuntimeError("AstrBot dependency lock differs; inspect before upgrading")
    local = ROOT / "config/bridge.local.toml"
    if not local.exists():
        shutil.copyfile(ROOT / "config/bridge.example.toml", local)
    token = ROOT / "var/bridge-token.txt"
    token.parent.mkdir(parents=True, exist_ok=True)
    if not token.exists():
        with token.open("x", encoding="ascii") as stream:
            stream.write(secrets.token_urlsafe(48))
    # Copy a known fixed set; never overwrite AstrBot's user configuration/data.
    target = upstream / "data/plugins/astrbot_plugin_myatri"
    target.mkdir(parents=True, exist_ok=True)
    for name in [
        "__init__.py",
        "main.py",
        "metadata.yaml",
        "requirements.txt",
        "_conf_schema.json",
        "wechatagent_client.py",
        "wechatagent_platform.py",
        "wechat_versions.py",
        "wechat-versions.json",
    ]:
        shutil.copyfile(ROOT / "astrbot_plugin_myatri" / name, target / name)
    print(
        "Plugin files prepared; existing mode and channel settings preserved. No login or model call."
    )


if __name__ == "__main__":
    main()

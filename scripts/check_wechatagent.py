"""Check local WeChatAgent; bind configuration only after authenticating it."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import socket
import sys
import tomllib

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from astrbot_plugin_myatri.wechatagent_client import (  # noqa: E402
    NativeClient,
    NotReady,
    Settings,
    incoming,
)


HINTS = {
    "READER_HTTP_UNAVAILABLE": "本机消息读取服务未连接。请运行 Start-WeChat.cmd 或 Start-WeChat-Live.cmd；若启动仍失败，查看 var/reader.stderr.log。单独运行 Check-WeChat.cmd 不会启动读取服务。",
    "NATIVE_HTTP_UNAVAILABLE": "配套微信接口未连接（默认端口 30001）。请从托盘完整退出普通微信，用 Open-WeChat.cmd 打开配套 4.1.10.27，完成手机确认并进入聊天列表，再启动 Start-WeChat-Live.cmd。直接打开普通微信无法提供该接口。",
    "HOOK_INITIALIZATION_PENDING": "配套微信接口仍在初始化。请完成手机登录确认并进入聊天列表，稍等后重新启动。",
    "SET_WECHATAGENT_DIRECTORY": "尚未安装并配置 WeChatAgent 的读取/原生发送后端。请先取得可运行后端，再在 config/wechatagent.local.toml 填运行目录。克隆源码不会生成发送 DLL。",
    "WECHATAGENT_RUNTIME_FILES_MISSING": "目录中缺少 state/wechat-io-config.json 或 state/hook/native-token.txt；需要已完成首次配置的 WeChatAgent 运行数据目录。",
    "HOOK_VERSION_MISMATCH": "参考接口只验证了微信 4.1.10.27；不能仅把版本号改成 4.1.15.13。",
    "GROUP_MAPPING_AMBIGUOUS_OR_MISSING": "未找到唯一测试群。请确认已同步目标群；同名群需要先区分。",
    "STOP_SERVICES_BEFORE_CONFIGURE": "写配置前请关闭旧的两个 CMD，再运行本检查。",
}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temp.replace(path)


def load_json(path, default):
    return (
        json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default
    )


def configure(client, settings_file):
    for port in (6185, 8766):
        with socket.socket() as probe:
            probe.settimeout(0.3)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise NotReady("STOP_SERVICES_BEFORE_CONFIGURE")
    bridge_file = ROOT / "config/bridge.local.toml"
    bridge_text = bridge_file.read_text(encoding="utf-8-sig")
    bridge = tomllib.loads(bridge_text)["bridge"]
    if bridge.get("mode") not in {"offline", "live"}:
        raise NotReady("BRIDGE_CONFIG_INVALID")
    platform_id = "xiaowennuan-wechat"
    route = platform_id + "|" + client.group_id
    allowed = list(dict.fromkeys([*bridge.get("allowed_groups", []), route]))
    # TOML arrays of strings use JSON-compatible quoting.
    bridge_text, count = re.subn(
        r"(?m)^allowed_groups\s*=\s*\[[^\]]*\]",
        lambda _: "allowed_groups = " + json.dumps(allowed, ensure_ascii=False),
        bridge_text,
        count=1,
    )
    if count != 1:
        raise NotReady("BRIDGE_CONFIG_INVALID")
    settings_text = settings_file.read_text(encoding="utf-8-sig")
    for key, value in (("group_id", client.group_id), ("bot_id", client.bot_id)):
        settings_text, count = re.subn(
            rf"(?m)^{key}\s*=.*$",
            lambda _: key + " = " + json.dumps(value),
            settings_text,
            count=1,
        )
        if count != 1:
            raise NotReady("WECHAT_CONFIG_INVALID")
    data = ROOT / "upstream/AstrBot/data"
    core_file = data / "cmd_config.json"
    if not core_file.exists():
        raise NotReady("START_ASTRBOT_ONCE_FIRST")
    core = load_json(core_file, {})
    platforms = core.setdefault("platform", [])
    existing = [p for p in platforms if p.get("id") == platform_id]
    if existing and (
        len(existing) != 1 or existing[0].get("type") != "myatri_wechatagent"
    ):
        raise NotReady("PLATFORM_ID_CONFLICT")
    replacement = {
        "id": platform_id,
        "type": "myatri_wechatagent",
        "enable": True,
        "settings_file": str(settings_file),
    }
    # This launcher owns a dedicated WeChat instance; keep other platform
    # definitions, but do not start unrelated QQ/ClawBot network connections.
    other_platforms = []
    for platform in platforms:
        if platform.get("id") != platform_id:
            other_platforms.append({**platform, "enable": False})
    core["platform"] = other_platforms + [replacement]
    core.setdefault("provider_settings", {})["enable"] = False
    plugin_file = data / "config/astrbot_plugin_myatri_config.json"
    plugin = load_json(plugin_file, {})
    plugin.update(
        {
            "enabled": True,
            "allowed_groups": list(
                dict.fromkeys([*plugin.get("allowed_groups", []), route])
            ),
            "bridge_url": "http://127.0.0.1:8766",
            "token_file": str(ROOT / "var/bridge-token.txt"),
            "trigger_prefix": "/warm ",
        }
    )
    # Everything is validated before modifying files. Keep local pre-change copies.
    backup = (
        ROOT
        / "var/setup-backups"
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    )
    backup.mkdir(parents=True)
    for file, body in (
        (settings_file, settings_text),
        (bridge_file, bridge_text),
        (core_file, core),
        (plugin_file, plugin),
    ):
        if file.exists():
            (backup / file.name).write_bytes(file.read_bytes())
        if isinstance(body, str):
            temp = file.with_suffix(file.suffix + ".tmp")
            temp.write_text(body, encoding="utf-8")
            temp.replace(file)
        else:
            write_json(file, body)


async def check(args):
    file = ROOT / "config/wechatagent.local.toml"
    client = None
    code = "READY"
    observed = False
    try:
        settings = Settings.load(file)
        client = NativeClient(settings)
        await client.bind()
        if args.configure:
            configure(client, file)
        print("微信后端身份、版本、测试群绑定检查通过；未发送消息，未调用模型。")
        if args.observe_seconds:
            import time

            started = int(time.time())

            async def observe():
                async for payload in client.events():
                    item = incoming(payload, client, started)
                    if item and (
                        item["mentioned"] or item["text"].startswith("/warm ")
                    ):
                        await client.check()
                        return True

            print("只读等待其他群成员的 @小溫暖 或 /warm 消息；不会回复。", flush=True)
            try:
                observed = bool(await asyncio.wait_for(observe(), args.observe_seconds))
            except TimeoutError:
                pass
            print(
                "已收到目标群触发消息，ID与原发送时间校验通过。"
                if observed
                else "观察时间内未收到目标群触发消息。"
            )
    except NotReady as exc:
        code = str(exc)
    except httpx.HTTPError:
        code = "LOCAL_BACKEND_HTTP_UNAVAILABLE"
    except (OSError, ValueError, KeyError, TypeError):
        code = "LOCAL_CONFIG_INVALID"
    finally:
        if client:
            await client.close()
    write_json(
        ROOT / "var/wechatagent-readiness.json",
        {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "status": code,
            "configured": code == "READY" and args.configure,
            "external_messages_sent": 0,
            "model_calls": 0,
            "real_incoming_observed": observed,
        },
    )
    if code != "READY":
        print("尚未接通：" + code)
        print(
            HINTS.get(
                code,
                "请检查本机 WeChatAgent 后端及配置；详情见 docs/WeChatAgent接入.md。",
            )
        )
        return 2
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--configure", action="store_true")
    parser.add_argument(
        "--observe-seconds",
        type=int,
        choices=range(1, 121),
        default=0,
        metavar="1..120",
    )
    raise SystemExit(asyncio.run(check(parser.parse_args())))

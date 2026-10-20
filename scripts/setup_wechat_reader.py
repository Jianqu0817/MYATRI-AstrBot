"""Bind the isolated reader to a user-authenticated WeChat account.

Run with the Python bundled in the verified WeChatAgent distribution.
Credentials stay in memory and Windows current-user DPAPI storage.
"""

import importlib.util
import json
from pathlib import Path
import re
import sys
import tomllib
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "var/wechatagent-runtime/workspace"
sys.path.insert(0, str(ROOT))


def module(name, file):
    sys.path.insert(0, str(file.parent))
    spec = importlib.util.spec_from_file_location(name, file)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


def read(file, default=None):
    return (
        json.loads(file.read_text(encoding="utf-8-sig")) if file.exists() else default
    )


def write(file, body):
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_suffix(file.suffix + ".tmp")
    temporary.write_text(
        json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(file)


def native_health():
    token = (
        (WORKSPACE / "state/hook/native-token.txt").read_text(encoding="ascii").strip()
    )
    req = urllib.request.Request(
        "http://127.0.0.1:30001/WeChatAgent/health",
        headers={"Authorization": "Bearer " + token},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=4) as response:
        result = json.load(response)
    if (
        result.get("integration") != "WeChatAgent-1"
        or result.get("version") != "4.1.10.27"
    ):
        raise RuntimeError("HOOK_VERSION_MISMATCH")
    return result


def main():
    setup = module("myatri_vendor_reader_setup", WORKSPACE / "scripts/reader_setup.py")
    settings_file = WORKSPACE / "state/weflow/WeFlow-config.json"
    previous = read(settings_file, {})
    health = native_health()
    accounts = [a for a in setup.discover_accounts() if a["ready"]]
    live = health.get("databaseAccounts", [])
    if len(live) > 1:
        raise RuntimeError("HOOK_MULTIPLE_ACCOUNT_DATABASES")
    if len(live) == 1:
        accounts = [a for a in accounts if a["myWxid"].casefold() == live[0].casefold()]
    if not accounts:
        raise RuntimeError("ACCOUNT_SELECTION_REQUIRED")
    account = accounts[0] if len(accounts) == 1 else None
    protected_key = ""
    if (
        account
        and previous.get("myWxid") == account["myWxid"]
        and previous.get("decryptKey", "").startswith("userdpapi:")
    ):
        try:
            setup.verify_database(
                account["dbPath"], setup.credential(previous["decryptKey"])
            )
            protected_key = previous["decryptKey"]
        except setup.SetupError:
            pass
    if not protected_key:
        executable = WORKSPACE / "state/hook/wechat-4.1.10.27/Weixin.exe"
        print(
            "等待你在配套微信中完成登录；若已登录，请在配套微信中退出账号后重新登录。",
            flush=True,
        )
        if account:
            key = setup.capture_key(str(executable), account["dbPath"], timeout=180)
        else:
            # The login-time key must authenticate exactly one local account.
            # Do not choose a database by mtime or a previously used nickname.
            verify_database = setup.verify_database
            selected = []

            def verify_candidate(unused, candidate_key):
                matches = []
                for candidate in accounts:
                    try:
                        verify_database(candidate["dbPath"], candidate_key)
                        matches.append(candidate)
                    except setup.SetupError as error:
                        if str(error) != "READER_KEY_INVALID":
                            raise
                if len(matches) != 1:
                    raise setup.SetupError("READER_KEY_INVALID")
                selected[:] = matches

            setup.verify_database = verify_candidate
            try:
                key = setup.capture_key(str(executable), "", timeout=180)
            finally:
                setup.verify_database = verify_database
            account = selected[0]
        protected_key = setup.credential(key, protect=True)
        key = None
    health = native_health()
    live = health.get("databaseAccounts", [])
    if (
        health.get("backend") != "wechat-hook"
        or len(live) != 1
        or live[0].casefold() != account["myWxid"].casefold()
    ):
        raise RuntimeError("HOOK_ACCOUNT_MISMATCH")
    # This isolated workspace supports one account. Refuse replacement of another.
    if previous.get("myWxid") and previous["myWxid"] != account["myWxid"]:
        raise RuntimeError("ACCOUNT_REPLACEMENT_REQUIRES_REVIEW")
    next_settings = {
        **previous,
        "dbPath": account["dbPath"],
        "myWxid": account["myWxid"],
        "decryptKey": protected_key,
    }
    write(settings_file, next_settings)
    io_file = WORKSPACE / "state/wechat-io-config.json"
    io = read(io_file)
    # Use exactly the vendor reader's account normalization rule.
    own = re.sub(r"_[A-Za-z0-9]{4}$", "", account["myWxid"])
    io["self_wxid"] = own
    write(io_file, io)
    reader_module = module("myatri_vendor_reader", WORKSPACE / "scripts/reader-api.py")
    reader = reader_module.Reader()
    try:
        local = tomllib.loads(
            (ROOT / "config/wechatagent.local.toml").read_text(encoding="utf-8")
        )["wechatagent"]
        pinned = local.get("group_id")
        matches = [
            u
            for u, name in reader.names.items()
            if u.endswith("@chatroom")
            and (u == pinned if pinned else name == local["group_name"])
        ]
        if len(matches) != 1:
            raise RuntimeError("GROUP_MAPPING_AMBIGUOUS_OR_MISSING")
        group_id = matches[0]
        identity = reader.conversation_id(group_id)
        if not identity:
            raise RuntimeError("GROUP_METADATA_NOT_READY")
        project_file = WORKSPACE / "config/wechatagent.json"
        project = read(project_file)
        project["wechat"]["whitelist"] = {"groups": [int(identity)], "private": []}
        write(project_file, project)
        next_settings["messagePushFilterList"] = [group_id]
        write(settings_file, next_settings)
    finally:
        reader.stop.set()
        for name in ("image_executor", "sticker_executor", "sticker_review_executor"):
            getattr(reader, name).shutdown(wait=False, cancel_futures=True)
    print(
        "读取凭据已用当前 Windows 用户加密保存；仅选定测试群。未发送消息，未调用模型。",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        code = str(error)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", code):
            code = type(error).__name__
        print("微信读取配置未完成：" + code, flush=True)
        raise SystemExit(2)

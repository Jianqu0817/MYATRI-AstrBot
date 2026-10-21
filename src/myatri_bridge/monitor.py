"""Read-only, loopback-only context telemetry. No model or bridge credentials."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import time
import tomllib
import webbrowser

from aiohttp import web


def _read_config(path):
    try:
        return tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def _connect(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.3)
    db.execute("PRAGMA query_only=ON")
    return db


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return value if not isinstance(value, float) or math.isfinite(value) else None


def _usage_view(row):
    """Project a fixed allowlist; never expose prompts, IDs, credentials or reasoning."""
    if not row:
        return None
    usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
    details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    reasoning = _number(details.get("reasoning_tokens", usage.get("reasoning_tokens")))
    effort = row.get("reasoning_effort")
    return {"model": str(row.get("model", ""))[:120], "recorded_at": row.get("recorded_at"),
        "prompt_tokens": _number(usage.get("prompt_tokens")),
        "completion_tokens": _number(usage.get("completion_tokens")),
        "reasoning_tokens": reasoning,
        "cached_input_tokens": _number(usage.get("prompt_cache_hit_tokens",
            prompt_details.get("cached_tokens"))),
        "elapsed_seconds": _number(row.get("elapsed_seconds")),
        "finish_reason": row.get("finish_reason"), "status": row.get("status"),
        "thinking_enabled": row.get("thinking_enabled") if isinstance(row.get("thinking_enabled"), bool) else None,
        "reasoning_effort": effort if isinstance(effort, str) and effort in {"low", "high", "max"} else None,
        "request_max_tokens": _number(row.get("request_max_tokens")),
        "estimated_input_tokens": _number(row.get("estimated_input_tokens"))}


def _context_view(value):
    """Only numerical, named counters may leave the local protocol store."""
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        return None
    keys = ("history_messages", "dialogue_turns", "recall_records", "recall_omitted_chars",
        "imported_records", "summary_count", "dialogue_candidates", "dialogue_omitted_turns",
        "recent_group_messages", "timeline_candidates", "timeline_omitted_messages",
        "timeline_deduplicated_messages", "summary_items", "summary_omitted_items",
        "budget_trimmed_units", "working_context_tokens", "configured_dialogue_turns", "configured_recent_messages")
    result = {key: _number(value[key]) for key in keys if key in value}
    components = value.get("components", {})
    result["components"] = {name: {key: _number(components[name].get(key))
        for key in ("messages", "chars", "estimated_tokens")}
        for name in ("rules", "dialogue", "recall", "summary", "imported_history", "state",
                     "group_policy_and_summary", "timeline_and_current")
        if isinstance(components, dict) and isinstance(components.get(name), dict)}
    result["measurement"] = "local_utf8_upper_estimate_not_api_tokens"
    reasons = value.get("omission_reasons", {})
    result["omission_reasons"] = {key: _number(reasons.get(key))
        for key in ("dialogue_window", "summary_attention", "token_budget", "timeline_deduplication")
        if isinstance(reasons, dict) and key in reasons}
    return result


def read_library(path, label, config, *, key):
    options = config.get("context", {})
    result = {"key": key, "label": label, "configured_context_limit": options.get("limit"),
        "compaction_threshold": options.get("compact_at"),
        "default_thinking_enabled": config.get("chat", {}).get("thinking", False),
        "status": "no_data", "latest_conversation": None, "recent_conversations": [],
        "last_conversation_completed_at": None}
    result["context_composition"] = None
    runtime = path / "runtime.sqlite3"
    if not runtime.exists():
        return result
    try:
        with closing(_connect(runtime)) as db:
            rows = []
            for (value,) in db.execute("SELECT payload FROM usage ORDER BY id DESC LIMIT 2000"):
                try:
                    row = json.loads(value)
                except (ValueError, TypeError):
                    continue
                if isinstance(row, dict):
                    rows.append(row)
            conversations = [row for row in rows if row.get("purpose") == "conversation"
                and row.get("role", "foreground") == "foreground"]
            result["latest_conversation"] = _usage_view(conversations[0]) if conversations else None
            result["recent_conversations"] = [_usage_view(row) for row in conversations[:30]][::-1]
            summaries = [row for row in rows if row.get("purpose") == "group_short_memory"]
            result["latest_summary"] = _usage_view(summaries[0]) if summaries else None
            for (value,) in db.execute("SELECT payload FROM protocols ORDER BY rowid DESC LIMIT 100"):
                try:
                    protocol = json.loads(value)
                except (ValueError, TypeError):
                    continue
                if isinstance(protocol, dict) and protocol.get("status") == "completed" and protocol.get("replied_at"):
                    result["last_conversation_completed_at"] = protocol["replied_at"]
                    result["last_turn_model_requests"] = protocol.get("steps")
                    result["context_composition"] = _context_view(protocol.get("context_stats"))
                    break
        prompt = (result["latest_conversation"] or {}).get("prompt_tokens")
        limit, threshold = options.get("limit"), options.get("compact_at")
        result["context_usage_percent"] = round(prompt / limit * 100, 2) if prompt is not None and limit else None
        result["compaction_usage_percent"] = round(prompt / threshold * 100, 2) if prompt is not None and threshold else None
        result["status"] = "ok"
    except (sqlite3.Error, OSError, ValueError):
        result["status"] = "temporarily_unavailable"
    return result


def _budget(path, limit):
    result = {"daily_limit": limit, "used_tokens": None, "reserved_tokens": None,
        "day_utc": datetime.now(timezone.utc).date().isoformat(), "reset_beijing": "每日08:00"}
    if not path.exists():
        return result
    try:
        with closing(_connect(path)) as db:
            reserved, used = db.execute("SELECT COALESCE(SUM(CASE WHEN status='reserved' THEN reserved_tokens ELSE 0 END),0),"
                "COALESCE(SUM(CASE WHEN status!='reserved' THEN charged_tokens ELSE 0 END),0) "
                "FROM budget_reservations WHERE day=?", (result["day_utc"],)).fetchone()
            result.update(used_tokens=used, reserved_tokens=reserved)
    except sqlite3.Error:
        pass
    return result


def snapshot(project):
    project = Path(project).resolve()
    settings = _read_config(project / "config/bridge.local.toml").get("bridge", {})
    engine = _read_config(project / settings.get("engine_config", "config/engine.toml"))
    state = project / settings.get("state_root", "var/groups")
    # Locations come from local configuration only, never from HTTP arguments.
    if not state.resolve().is_relative_to(project):
        raise ValueError("invalid state root")
    libraries = []
    if state.exists():
        groups = [p for p in sorted(state.iterdir()) if p.is_dir() and (p / "runtime.sqlite3").exists()
                  and (p / "domains").exists()]
        for index, group in enumerate(groups, 1):
            libraries.append(read_library(group, f"微信群聊 {index}", engine, key=f"group-{index}"))
    core = project.parent / "MYATRI"
    private_config = _read_config(core / "config/live.local.toml")
    defaults = _read_config(core / "config/defaults.toml")
    merged = {**defaults, **private_config,
        "context": {**defaults.get("context", {}), **private_config.get("context", {})}}
    for folder, title in (("xiaowennuan", "小温暖私聊"), ("myatri-main", "ATRI旧私聊")):
        path = core / "var" / folder
        if (path / "runtime.sqlite3").exists():
            libraries.append(read_library(path, title, merged, key=folder))
    pending, summaries, listening = [], None, None
    short = state / "short-memory.sqlite3"
    if short.exists():
        try:
            with closing(_connect(short)) as db:
                pending = [{"window_end": end, "attempts": attempts, "retry_at": retry, "error_type": error}
                    for end, attempts, retry, error in db.execute("SELECT end_seq,attempts,next_attempt,last_error "
                        "FROM listening_pending ORDER BY end_seq LIMIT 30")]
                summaries = db.execute("SELECT count(*) FROM listening_summaries").fetchone()[0]
                listening = db.execute("SELECT COALESCE(SUM(total),0) FROM listening_state").fetchone()[0]
        except sqlite3.Error:
            pass
    return {"updated_at": datetime.now(timezone.utc).isoformat(), "refresh_seconds": 2,
        "libraries": libraries, "group_daily_budget": _budget(state / "budget.sqlite3",
            engine.get("budget", {}).get("daily_tokens")),
        "listening": {"messages": listening, "summaries": summaries, "pending": pending},
        "measurement": "API确认的最近一次请求用量；生成中不提供逐Token读数。缓存命中仍占上下文；每日累计不等于单次上下文。",
        "privacy": "只读本机计数，不读取或返回聊天正文、密钥、推理文本或账号ID。"}


def create_monitor_app(project):
    cached = None
    cached_at = 0.0
    lock = asyncio.Lock()

    @web.middleware
    async def local_only(request, handler):
        # Reject DNS rebinding and cross-site browser requests; no control endpoints.
        host = request.host.rsplit(":", 1)[0]
        if host not in {"127.0.0.1", "localhost"}:
            raise web.HTTPForbidden()
        origin = request.headers.get("Origin")
        if origin and origin != f"http://{request.host}":
            raise web.HTTPForbidden()
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise web.HTTPForbidden()
        if request.method != "GET":
            raise web.HTTPMethodNotAllowed(request.method, ["GET"])
        response = await handler(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})
        return response

    async def metrics(request):
        nonlocal cached, cached_at
        async with lock:
            if cached is None or time.monotonic() - cached_at >= 2:
                cached = await asyncio.to_thread(snapshot, project)
                cached_at = time.monotonic()
        return web.json_response(cached)

    async def index(request):
        return web.Response(text=Path(__file__).with_name("monitor.html").read_text(encoding="utf-8"),
                            content_type="text/html")

    app = web.Application(middlewares=[local_only])
    app.router.add_get("/", index)
    app.router.add_get("/api/metrics", metrics)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    app = create_monitor_app(args.project)
    if args.open_browser:
        async def open_browser(app):
            asyncio.get_running_loop().call_later(.8, webbrowser.open, f"http://127.0.0.1:{args.port}/")
        app.on_startup.append(open_browser)
    web.run_app(app, host="127.0.0.1", port=args.port, access_log=None)

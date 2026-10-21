"""联网搜索模块的离线验收：全部 mock，不联网、不调用真实后端。

覆盖设计文档 2.9 的测试点：超时/非零退出/输出超预算/缓存命中/日限额/默认关闭，
外加子进程的环境变量白名单、工作目录隔离与审计只写哈希。
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from myatri_bridge import dsh_client as dsh_module
from myatri_bridge.dsh_client import (
    ERROR_BACKEND,
    ERROR_TIMEOUT,
    DshClient,
    DshResult,
    default_workdir,
)
from myatri_bridge.tool_reading import TOOL_BYTES, byte_size
from myatri_bridge.web_tools import (
    ERROR_OUTPUT,
    ERROR_QUOTA,
    WEB_SEARCH_TOOL,
    DshBackend,
    WebSearch,
    WebSearchError,
    build_prompt,
    cache_key,
    fit_budget,
    parse_output,
    query_hash_of,
    tool_registration,
)

NOW = 1_700_000_000.0


class FakeBackend:
    """可编程后端：记录每次调用，返回预置文本或抛预置错误码。"""

    def __init__(self, text: str = '{"results":[]}', error: str | None = None) -> None:
        self.text = text
        self.error = error
        self.calls: list[dict[str, object]] = []

    def __call__(self, query: str, *, max_results: int, recency_days: int | None,
                 timeout_seconds: float) -> str:
        self.calls.append({"query": query, "max_results": max_results,
                           "recency_days": recency_days, "timeout_seconds": timeout_seconds})
        if self.error is not None:
            raise WebSearchError(self.error)
        return self.text


def rows_text(*rows: dict[str, str]) -> str:
    return json.dumps({"results": list(rows)}, ensure_ascii=False)


def a_row(index: int = 1, snippet: str = "一句话摘录") -> dict[str, str]:
    return {"title": f"标题{index}", "url": f"https://example.com/{index}", "snippet": snippet}


def make_search(tmp_path: Path, backend: FakeBackend, **kwargs: object) -> WebSearch:
    options: dict[str, object] = {"clock": lambda: NOW}
    options.update(kwargs)
    return WebSearch(tmp_path / "web.sqlite3", backend=backend, **options)  # type: ignore[arg-type]


# ---- 工具契约 ---------------------------------------------------------------


def test_tool_schema_matches_design_contract() -> None:
    assert WEB_SEARCH_TOOL["type"] == "function"
    function = WEB_SEARCH_TOOL["function"]
    assert function["name"] == "web_search"
    parameters = function["parameters"]
    assert parameters["required"] == ["query"]
    assert parameters["additionalProperties"] is False
    assert set(parameters["properties"]) == {"query", "max_results", "recency_days"}
    assert parameters["properties"]["query"]["maxLength"] == 200
    assert parameters["properties"]["max_results"]["minimum"] == 1
    assert parameters["properties"]["max_results"]["maximum"] == 5
    assert parameters["properties"]["recency_days"]["maximum"] == 30


def test_registration_is_schema_and_callable(tmp_path: Path) -> None:
    web = make_search(tmp_path, FakeBackend(rows_text(a_row())))
    schema, handler = tool_registration(web)
    assert schema is WEB_SEARCH_TOOL
    assert handler(query="工具注册")["results"][0]["url"] == "https://example.com/1"


# ---- 默认关闭：构造不产生副作用 --------------------------------------------


def test_construction_does_not_touch_disk(tmp_path: Path) -> None:
    database = tmp_path / "nested" / "web.sqlite3"
    WebSearch(database, backend=FakeBackend())
    assert not database.exists()
    assert not database.parent.exists()


# ---- 正常返回 ---------------------------------------------------------------


def test_successful_search_shape_and_backend_arguments(tmp_path: Path) -> None:
    backend = FakeBackend(rows_text(a_row(1), a_row(2)))
    web = make_search(tmp_path, backend)
    result = web.search("  上海  今天  气温 ", max_results=2, recency_days=7)
    assert result["query"] == "上海 今天 气温"
    assert result["truncated"] is False
    assert isinstance(result["fetched_at"], str) and result["fetched_at"]
    assert [row["url"] for row in result["results"]] == [
        "https://example.com/1", "https://example.com/2"]
    assert backend.calls == [{"query": "上海 今天 气温", "max_results": 2,
                              "recency_days": 7, "timeout_seconds": 25.0}]


def test_empty_results_are_not_an_error(tmp_path: Path) -> None:
    web = make_search(tmp_path, FakeBackend('{"results": []}'))
    result = web.search("查不到的主题")
    assert result["results"] == [] and "error" not in result


# ---- 缓存 -------------------------------------------------------------------


def test_second_identical_query_uses_cache(tmp_path: Path) -> None:
    backend = FakeBackend(rows_text(a_row()))
    web = make_search(tmp_path, backend)
    first = web.search("同一个  查询")
    second = web.search("  同一个   查询 ")
    # 命中缓存时返回的是当初那次抓取的 payload，本次不再外呼。
    assert second["query"] == first["query"] == "同一个 查询"
    assert second["fetched_at"] == first["fetched_at"]
    assert second["results"] == first["results"]
    assert len(backend.calls) == 1
    assert [row["cached"] for row in web.audit_rows(5)] == [False, True]


def test_cache_expires_after_ttl(tmp_path: Path) -> None:
    backend = FakeBackend(rows_text(a_row()))
    now = [NOW]
    web = WebSearch(tmp_path / "web.sqlite3", backend=backend, cache_ttl_hours=24,
                    clock=lambda: now[0])
    web.search("会过期的查询")
    now[0] += 24 * 3600 + 1
    web.search("会过期的查询")
    assert len(backend.calls) == 2


def test_cache_key_separates_result_count(tmp_path: Path) -> None:
    backend = FakeBackend(rows_text(a_row(1), a_row(2), a_row(3)))
    web = make_search(tmp_path, backend)
    web.search("参数不同", max_results=2)
    web.search("参数不同", max_results=3)
    assert len(backend.calls) == 2
    assert cache_key("参数不同", 2, None) != cache_key("参数不同", 3, None)


def test_cache_hit_does_not_consume_quota(tmp_path: Path) -> None:
    backend = FakeBackend(rows_text(a_row()))
    web = make_search(tmp_path, backend, daily_limit=1)
    assert "results" in web.search("唯一一次外呼")
    assert "results" in web.search("唯一一次外呼")
    assert web.search("第二个查询") == {"error": ERROR_QUOTA}
    assert len(backend.calls) == 1


# ---- 日限额 -----------------------------------------------------------------


def test_daily_limit_is_persisted_in_the_same_database(tmp_path: Path) -> None:
    database = tmp_path / "web.sqlite3"
    backend = FakeBackend(rows_text(a_row()))
    web = WebSearch(database, backend=backend, daily_limit=2, clock=lambda: NOW)
    assert "results" in web.search("查询一")
    assert "results" in web.search("查询二")
    assert web.search("查询三") == {"error": ERROR_QUOTA}
    assert len(backend.calls) == 2
    # 换一个实例（进程重启）仍然算同一天已用满。
    restarted = WebSearch(database, backend=backend, daily_limit=2, clock=lambda: NOW)
    assert restarted.quota_used(NOW) == 2
    assert restarted.search("查询四") == {"error": ERROR_QUOTA}


# ---- 错误语义 ---------------------------------------------------------------


@pytest.mark.parametrize("code", [ERROR_TIMEOUT, ERROR_BACKEND, ERROR_OUTPUT])
def test_backend_failures_return_only_the_error_code(tmp_path: Path, code: str) -> None:
    web = make_search(tmp_path, FakeBackend(error=code))
    assert web.search("会失败的查询") == {"error": code}


@pytest.mark.parametrize("text", [
    "", "抱歉，我查不到。", '{"results": "不是数组"}', "[]", '{"results": [1, 2]}',
])
def test_invalid_backend_output_never_fabricates(tmp_path: Path, text: str) -> None:
    web = make_search(tmp_path, FakeBackend(text))
    assert web.search("输出不合法") == {"error": ERROR_OUTPUT}


def test_unexpected_backend_exception_is_backend_failure(tmp_path: Path) -> None:
    def explode(query: str, *, max_results: int, recency_days: int | None,
                timeout_seconds: float) -> str:
        raise RuntimeError("boom")

    web = WebSearch(tmp_path / "web.sqlite3", backend=explode, clock=lambda: NOW)
    assert web.search("异常查询") == {"error": ERROR_BACKEND}


def test_error_results_are_not_cached(tmp_path: Path) -> None:
    backend = FakeBackend(error=ERROR_TIMEOUT)
    web = make_search(tmp_path, backend)
    web.search("超时查询")
    web.search("超时查询")
    assert len(backend.calls) == 2


# ---- 解析与预算 -------------------------------------------------------------


def test_parse_output_tolerates_fences_and_drops_items_without_url() -> None:
    text = ("```json\n" + json.dumps({"results": [
        {"title": "  甲  ", "url": "https://a.example/1", "snippet": " 摘  要 "},
        {"title": "没有链接", "snippet": "丢弃"},
        {"url": "ftp://a.example/2", "snippet": "协议不对"},
    ]}, ensure_ascii=False) + "\n```")
    assert parse_output(text, max_results=5) == [
        {"title": "甲", "url": "https://a.example/1", "snippet": "摘 要"}]


def test_parse_output_respects_max_results() -> None:
    text = rows_text(a_row(1), a_row(2), a_row(3))
    assert len(parse_output(text, max_results=2)) == 2


def test_long_results_are_truncated_under_tool_bytes(tmp_path: Path) -> None:
    backend = FakeBackend(rows_text(*[a_row(index, "长" * 1200) for index in range(1, 6)]))
    web = make_search(tmp_path, backend)
    result = web.search("需要截断的查询", max_results=5)
    assert result["truncated"] is True
    assert byte_size(result) <= TOOL_BYTES
    assert 0 < len(result["results"]) < 5


def test_fit_budget_keeps_a_single_oversized_result_readable() -> None:
    payload = {"query": "q", "fetched_at": "2026-10-21T20:00:00+08:00", "truncated": False,
               "results": [{"title": "标题", "url": "https://example.com/long",
                            "snippet": "字" * 4000}]}
    fitted = fit_budget(dict(payload))
    assert fitted["truncated"] is True
    assert byte_size(fitted) <= TOOL_BYTES
    assert fitted["results"] and fitted["results"][0]["url"] == "https://example.com/long"


def test_short_result_is_not_marked_truncated(tmp_path: Path) -> None:
    web = make_search(tmp_path, FakeBackend(rows_text(a_row())))
    result = web.search("短结果")
    assert result["truncated"] is False
    assert byte_size(result) <= TOOL_BYTES


# ---- 输入校验 ---------------------------------------------------------------


@pytest.mark.parametrize("query", ["", "   ", 123, None])
def test_invalid_query_raises(tmp_path: Path, query: object) -> None:
    web = make_search(tmp_path, FakeBackend())
    with pytest.raises(ValueError):
        web.search(query)  # type: ignore[arg-type]


def test_out_of_range_arguments_raise(tmp_path: Path) -> None:
    web = make_search(tmp_path, FakeBackend())
    with pytest.raises(ValueError):
        web.search("太长", 6)
    with pytest.raises(ValueError):
        web.search("太短", 0)
    with pytest.raises(ValueError):
        web.search("时效", recency_days=365)
    with pytest.raises(ValueError):
        web.search("超长查询" * 60)


# ---- 审计 -------------------------------------------------------------------


def test_audit_records_hash_only(tmp_path: Path) -> None:
    records: list[dict[str, object]] = []
    web = make_search(tmp_path, FakeBackend(rows_text(a_row())), audit_sink=records.append)
    web.search("绝不能写进日志的原话")
    assert len(records) == 1
    record = records[0]
    assert record["query_hash"] == query_hash_of("绝不能写进日志的原话")
    assert set(record) == {"tool", "query_hash", "duration_ms", "result_count",
                           "cached", "truncated", "error"}
    assert "绝不能写进日志的原话" not in json.dumps(record, ensure_ascii=False)
    assert web.audit_rows(1) == records


def test_audit_log_line_has_no_query_text(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("myatri_bridge.web_tools")
    caplog.set_level(logging.INFO, logger="myatri_bridge.web_tools")
    web = make_search(tmp_path, FakeBackend(rows_text(a_row())), logger=logger)
    web.search("日志里不该出现的原话")
    assert "日志里不该出现的群聊原话" not in caplog.text
    assert "日志里不该出现的原话" not in caplog.text
    assert query_hash_of("日志里不该出现的原话") in caplog.text


def test_audit_sink_failure_does_not_break_the_result(tmp_path: Path) -> None:
    def sink(record: dict[str, object]) -> None:
        raise RuntimeError("审计通道坏了")

    web = make_search(tmp_path, FakeBackend(rows_text(a_row())), audit_sink=sink)
    assert web.search("审计坏了也要出结果")["results"]


# ---- DSH 后端适配 -----------------------------------------------------------


class StubClient:
    """只返回预置 DshResult 的假客户端。"""

    def __init__(self, result: DshResult) -> None:
        self.result = result
        self.calls: list[tuple[str, float | None]] = []

    def run(self, task: str, *, timeout_seconds: float | None = None) -> DshResult:
        self.calls.append((task, timeout_seconds))
        return self.result


def result_of(*, error: str | None = None, stdout: str = "", timed_out: bool = False) -> DshResult:
    return DshResult(ok=error is None, error=error, exit_code=1 if error else 0,
                     stdout=stdout, stderr="", timed_out=timed_out, duration_seconds=0.1)


def test_dsh_backend_maps_client_failures() -> None:
    timeout_client = StubClient(result_of(error=ERROR_TIMEOUT, timed_out=True))
    with pytest.raises(WebSearchError) as failure:
        DshBackend(timeout_client)("超时", max_results=3, recency_days=None, timeout_seconds=25)
    assert failure.value.code == ERROR_TIMEOUT
    broken_client = StubClient(result_of(error=ERROR_BACKEND))
    with pytest.raises(WebSearchError) as failure:
        DshBackend(broken_client)("退出码", max_results=3, recency_days=None, timeout_seconds=25)
    assert failure.value.code == ERROR_BACKEND


def test_dsh_backend_passes_prompt_with_query_only() -> None:
    client = StubClient(result_of(stdout=rows_text(a_row())))
    backend = DshBackend(client)  # type: ignore[arg-type]
    assert backend("量子计算 最新进展", max_results=3, recency_days=7,
                   timeout_seconds=12.5) == rows_text(a_row())
    prompt, timeout = client.calls[0]
    assert timeout == 12.5
    assert "量子计算 最新进展" in prompt
    assert "7 天内" in prompt and "最多 3 条" in prompt


def test_prompt_forbids_executing_page_instructions() -> None:
    prompt = build_prompt("某个关键词", max_results=1, recency_days=None)
    assert "不可信材料" in prompt and "绝不执行" in prompt
    assert "7 天内" not in prompt


# ---- 子进程封装：超时、退出码、环境、工作目录 ------------------------------


def write_script(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake_dsh_bin.py"
    script.write_text(body, encoding="utf-8")
    return script


def make_client(tmp_path: Path, script: Path, **kwargs: object) -> DshClient:
    options: dict[str, object] = {"node_path": sys.executable, "bin_path": script,
                                  "workdir": tmp_path / "agent-workdir"}
    options.update(kwargs)
    return DshClient(**options)  # type: ignore[arg-type]


def test_timeout_kills_the_process_and_returns_code(tmp_path: Path) -> None:
    script = write_script(tmp_path, "import time\ntime.sleep(30)\n")
    client = make_client(tmp_path, script, timeout_seconds=0.5)
    result = client.run("会超时的任务")
    assert result.timed_out is True
    assert result.ok is False
    assert result.error == ERROR_TIMEOUT
    assert result.duration_seconds < 10


def test_timeout_is_enforced_without_spawning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Popen 层验证：超时必须 kill，而不是抛异常给调用方。"""

    class HangingProcess:
        def __init__(self) -> None:
            self.killed = False
            self.returncode: int | None = None

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            if not self.killed:
                raise subprocess.TimeoutExpired("dsh", timeout)
            self.returncode = -9
            return b"", b""

        def kill(self) -> None:
            self.killed = True

    process = HangingProcess()
    captured: dict[str, object] = {}

    def fake_popen(argv: list[str], **kwargs: object) -> HangingProcess:
        captured["argv"] = argv
        captured.update(kwargs)
        return process

    monkeypatch.setattr(dsh_module.subprocess, "Popen", fake_popen)
    client = make_client(tmp_path, tmp_path / "unused.py", timeout_seconds=0.01)
    result = client.run("任务")
    assert process.killed is True
    assert result.error == ERROR_TIMEOUT
    assert captured["shell"] is False
    assert captured["cwd"] == str((tmp_path / "agent-workdir").resolve())


def test_non_zero_exit_maps_to_backend_failed(tmp_path: Path) -> None:
    script = write_script(tmp_path, "import sys\nsys.stderr.write('dsh: boom\\n')\nsys.exit(3)\n")
    result = make_client(tmp_path, script).run("会失败的任务")
    assert result.ok is False
    assert result.error == ERROR_BACKEND
    assert result.exit_code == 3
    assert "boom" in result.stderr


def test_successful_run_returns_stdout(tmp_path: Path) -> None:
    # 子进程按字节写 UTF-8：DSH 的 node 也是按 UTF-8 写管道。
    script = write_script(
        tmp_path, "import sys\nsys.stdout.buffer.write('答案\\n'.encode('utf-8'))\n")
    result = make_client(tmp_path, script).run("正常任务")
    assert result.ok is True and result.error is None
    assert result.exit_code == 0
    assert result.stdout == "答案\n"


def test_inherited_credentials_and_proxies_are_dropped(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-inherited-should-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_inherited_should_not_leak")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:8080")
    script = write_script(tmp_path, (
        "import json, os, sys\n"
        "sys.stdout.write(json.dumps({'env': dict(os.environ), 'argv': sys.argv[1:],"
        " 'cwd': os.getcwd()}))\n"))
    result = make_client(tmp_path, script, extra_env={"DEEPSEEK_API_KEY": "sk-from-config"}).run("查询")
    payload = json.loads(result.stdout)
    environment = payload["env"]
    assert environment["DEEPSEEK_API_KEY"] == "sk-from-config"
    assert "GITHUB_TOKEN" not in environment
    assert "HTTPS_PROXY" not in environment and "http_proxy" not in environment
    assert environment["DSH_PERMISSION_MODE"] == "read-only"
    assert payload["argv"] == ["--profile", "headless", "查询"]
    assert payload["cwd"] == str((tmp_path / "agent-workdir").resolve())


def test_danger_full_access_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        make_client(tmp_path, tmp_path / "unused.py", permission_mode="danger-full-access")


def test_project_directory_is_refused_as_workdir(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    with pytest.raises(ValueError):
        DshClient(node_path=sys.executable, bin_path=tmp_path / "unused.py", workdir=tmp_path)


def test_default_workdir_is_an_isolated_directory(tmp_path: Path) -> None:
    assert default_workdir(tmp_path) == tmp_path / "var" / "agents" / "websearch"
    assert default_workdir(tmp_path) != tmp_path


@pytest.mark.parametrize("task", ["", "   ", "-查询"])
def test_invalid_task_is_rejected(tmp_path: Path, task: str) -> None:
    client = make_client(tmp_path, tmp_path / "unused.py")
    with pytest.raises(ValueError):
        client.run(task)


def test_describe_command_contains_no_task_text(tmp_path: Path) -> None:
    client = make_client(tmp_path, tmp_path / "bin.js")
    command = client.describe_command()
    assert command[2:] == ["--profile", "headless"]
    assert str(tmp_path) not in " ".join(command[2:])


def test_probe_uses_help_without_a_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class OkProcess:
        returncode = 0

        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            return b"Usage: dsh --profile headless\n", b""

        def kill(self) -> None:
            raise AssertionError("不应 kill")

    def fake_popen(argv: list[str], **kwargs: object) -> OkProcess:
        captured["argv"] = argv
        return OkProcess()

    monkeypatch.setattr(dsh_module.subprocess, "Popen", fake_popen)
    result = make_client(tmp_path, tmp_path / "bin.js").probe()
    assert result.ok is True
    assert captured["argv"][-1] == "--help"

"""Official Search wire contract and failure boundaries, using MockTransport only."""
from concurrent.futures import ThreadPoolExecutor
import json

import httpx
import pytest

from myatri_bridge.deepseek_web import DeepSeekSearchBackend, MAX_RESPONSE_BYTES, SEARCH_ENDPOINT
from myatri_bridge.web_tools import (
    ERROR_BACKEND, ERROR_OUTPUT, ERROR_QUOTA, ERROR_TIMEOUT, TOOL_BYTES,
    WebSearch, WebSearchError, byte_size,
)


def source(**changes):
    return {"title": "官方更新", "url": "https://example.com/news", "snippet": "短摘录",
            "summary": "较完整的搜索摘要", "date": "2026-10-20T00:00:00Z", **changes}


def backend(handler, **kwargs):
    return DeepSeekSearchBackend(allow_network=True, key_resolver=lambda: "fake-credential",
                                transport=httpx.MockTransport(handler), **kwargs)


def test_wire_request_and_source_projection(tmp_path):
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"results": [source()]})
    web = WebSearch(tmp_path / "web.sqlite3", backend=backend(handle))
    result = web.search("官方更新", max_results=2)
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == SEARCH_ENDPOINT and request.method == "POST"
    assert request.headers["Authorization"] == "Bearer fake-credential"
    assert json.loads(request.content) == {"query": "官方更新", "count": 2, "need_content": False}
    assert result["results"] == [{"title": "官方更新", "url": "https://example.com/news",
                                 "snippet": "较完整的搜索摘要"}]
    assert "fake-credential" not in json.dumps(result)
    assert "fake-credential" not in (tmp_path / "web.sqlite3").read_bytes().decode("latin1")


def test_recency_uses_documented_operator_and_fits_limit():
    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"results": []})
    api = backend(handle, clock=lambda: 1792627200)  # 2026-10-22 UTC
    api("词" * 111, max_results=3, recency_days=7, timeout_seconds=25)
    assert requests[0]["query"] == "词" * 111 + " after:2026-10-15"
    assert len(requests[0]["query"]) == 128
    assert "recency_days" not in requests[0]


def test_credential_resolution_is_lazy_and_offline_never_dispatches():
    def forbidden():
        raise AssertionError("credential must not be read")
    api = DeepSeekSearchBackend(key_resolver=forbidden)
    with pytest.raises(WebSearchError) as error:
        api("离线", max_results=3, recency_days=None, timeout_seconds=25)
    assert error.value.code == ERROR_BACKEND


def test_missing_key_fails_before_transport(tmp_path):
    def forbidden(request):
        raise AssertionError("must not dispatch")
    api = DeepSeekSearchBackend(allow_network=True, key_resolver=lambda: None,
                                transport=httpx.MockTransport(forbidden))
    web = WebSearch(tmp_path / "web.sqlite3", backend=api)
    assert web.search("没有凭据") == {"error": ERROR_BACKEND}


@pytest.mark.parametrize("status,code", [(301, ERROR_BACKEND), (401, ERROR_BACKEND),
    (402, ERROR_BACKEND), (429, ERROR_BACKEND), (500, ERROR_BACKEND),
    (408, ERROR_TIMEOUT), (504, ERROR_TIMEOUT)])
def test_http_failures_never_retry_redirect_or_leak_body(tmp_path, status, code, caplog):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://other.example/"},
                              text="fake-credential private-query")
    web = WebSearch(tmp_path / "web.sqlite3", backend=backend(handle))
    assert web.search("private-query") == {"error": code}
    assert len(calls) == 1
    assert "fake-credential" not in caplog.text and "private-query" not in caplog.text


@pytest.mark.parametrize("kind,code", [(httpx.ReadTimeout, ERROR_TIMEOUT),
                                      (httpx.ConnectError, ERROR_BACKEND)])
def test_transport_failures_are_sanitized(tmp_path, kind, code):
    def handle(request):
        raise kind("fake-credential", request=request)
    web = WebSearch(tmp_path / "web.sqlite3", backend=backend(handle))
    assert web.search("传输失败") == {"error": code}


@pytest.mark.parametrize("value", [[], {}, {"results": "wrong"}, {"results": [None]},
    {"results": [source(url="file:///private")]}, {"results": [source(url="https://u:p@example.com/")]},
    {"results": [source(snippet=None)]}, {"results": [source(date=123)]}])
def test_malformed_response_is_not_a_success(tmp_path, value):
    web = WebSearch(tmp_path / "web.sqlite3", backend=backend(lambda _: httpx.Response(200, json=value)))
    assert web.search("格式错误") == {"error": ERROR_OUTPUT}


@pytest.mark.parametrize("body", [b"not json", b"x" * (MAX_RESPONSE_BYTES + 1)],
                         ids=["invalid-json", "oversized"])
def test_invalid_or_oversized_json_is_rejected(tmp_path, body):
    web = WebSearch(tmp_path / "web.sqlite3", backend=backend(lambda _: httpx.Response(200, content=body)))
    assert web.search("过大结果") == {"error": ERROR_OUTPUT}


def test_direct_results_are_bounded_cached_and_do_not_reuse_dsh_cache(tmp_path):
    path = tmp_path / "web.sqlite3"
    old = WebSearch(path, backend=lambda *a, **k: json.dumps({"results": [source(title="old model")]}))
    old.search("同一问题", max_results=5)
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"results": [source(summary="长" * 8000)] * 5})
    web = WebSearch(path, backend=backend(handle), cache_namespace="deepseek-search-v1")
    first = web.search("同一问题", max_results=5)
    assert byte_size(first) <= TOOL_BYTES
    assert all(row["title"] != "old model" for row in first["results"])
    assert first == web.search("同一问题", max_results=5)
    assert len(calls) == 1 and web.quota_used() == 2


def test_parallel_searches_cannot_overspend_daily_attempt_limit(tmp_path):
    web = WebSearch(tmp_path / "web.sqlite3", daily_limit=1,
                    backend=backend(lambda _: httpx.Response(200, json={"results": []})))
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda n: web.search(f"查询{n}"), range(6)))
    assert sum("results" in row for row in results) == 1
    assert results.count({"error": ERROR_QUOTA}) == 5
    assert web.quota_used() == 1


def test_overlong_query_is_rejected_without_silent_truncation():
    calls = []
    api = backend(lambda request: calls.append(request))
    with pytest.raises(WebSearchError) as error:
        api("词" * 129, max_results=3, recency_days=None, timeout_seconds=25)
    assert error.value.code == ERROR_OUTPUT and calls == []


def test_recency_date_uses_beijing_midnight():
    from datetime import datetime, timezone
    calls = []
    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"results": []})
    # UTC is still Oct 21; Beijing has already reached Oct 22.
    moment = datetime(2026, 10, 21, 16, 30, tzinfo=timezone.utc).timestamp()
    api = backend(handle, clock=lambda: moment)
    api("日期边界", max_results=3, recency_days=7, timeout_seconds=25)
    assert calls[0]["query"] == "日期边界 after:2026-10-15"


def test_direct_tool_schema_reserves_filter_space_without_changing_dsh():
    from myatri_bridge.deepseek_web import DIRECT_WEB_SEARCH_TOOL
    from myatri_bridge.web_tools import WEB_SEARCH_TOOL
    assert DIRECT_WEB_SEARCH_TOOL["function"]["parameters"]["properties"]["query"]["maxLength"] == 111
    assert WEB_SEARCH_TOOL["function"]["parameters"]["properties"]["query"]["maxLength"] == 200

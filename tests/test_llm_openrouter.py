"""Lớp OpenRouter: chọn chế độ, vòng lặp gọi tool, xử lý lỗi. Không gọi mạng thật: urlopen được thay bằng bản giả."""
import io
import json
import urllib.error

import pytest

from app import llm


class FakeHTTP:
    """Thay urllib.request.urlopen: trả lần lượt các phản hồi đã chuẩn bị và ghi lại request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(json.loads(req.data.decode("utf-8")))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return io.BytesIO(json.dumps(item).encode("utf-8"))


def _msg(content=None, tool_calls=None):
    return {"choices": [{"message": {"role": "assistant", "content": content, "tool_calls": tool_calls}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


def _call(name, args, cid="c1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def _http_error(code, body="{}"):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body.encode("utf-8")))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.setattr(llm.time, "sleep", lambda *_: None)
    return llm.OpenRouter({"llm_model": "claude-opus-5-5"})


CTX = {"anchor": "2026-11-09", "role": "analyst", "rls": {}, "memory": {}, "fewshots": "(chưa có)"}


def test_mode_resolution(monkeypatch):
    monkeypatch.setattr(llm, "credentials_available", lambda: False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert llm.resolve_mode({"llm_mode": "auto"}) == "offline"
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert llm.resolve_mode({"llm_mode": "auto"}) == "openrouter"
    assert llm.resolve_mode({"llm_mode": "offline"}) == "offline"
    monkeypatch.setattr(llm, "credentials_available", lambda: True)
    assert llm.resolve_mode({"llm_mode": "auto"}) == "claude"


def test_model_name_for_openrouter(client, monkeypatch):
    assert client.model == llm.OPENROUTER_DEFAULT_MODEL            # tên Claude trần không dùng cho OpenRouter
    assert llm.effective_model({"llm_model": "google/gemini-flash"}, "openrouter") == "google/gemini-flash"
    monkeypatch.setenv("OPENROUTER_MODEL", "meta/llama")
    assert llm.effective_model({"llm_model": ""}, "openrouter") == "meta/llama"


def test_missing_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(llm.LLMUnavailable):
        llm.OpenRouter({})


def test_plan_runs_tools_then_returns_submitted_answer(client, monkeypatch):
    final = {"intent": "metric_query", "kind": "semantic", "semantic_plan": {"metrics": ["doanh_thu_thuan"]}}
    http = FakeHTTP([_msg(tool_calls=[_call("search_schema", {"query": "doanh thu"})]),
                     _msg(tool_calls=[_call("submit_answer", final, "c2")])])
    monkeypatch.setattr(llm.urllib.request, "urlopen", http)
    seen, trace = [], []
    out = client.plan("Doanh thu thuần năm 2025", CTX, {"search_schema": lambda query: seen.append(query) or {"tables": []}}, trace=trace)
    assert out == final and seen == ["doanh thu"]
    assert [t["tool"] for t in trace] == ["search_schema"] and trace[0]["error"] is False
    first, second = http.requests
    assert first["tool_choice"] == "auto" and first["temperature"] == 0
    assert {t["function"]["name"] for t in first["tools"]} >= {"search_schema", "dry_run_sql", "submit_answer"}
    assert second["messages"][-1] == {"role": "tool", "tool_call_id": "c1", "content": '{"tables": []}'}
    assert client.usage["calls"] == 2 and client.usage["input_tokens"] == 20


def test_plan_reports_tool_errors_and_bad_arguments_back_to_model(client, monkeypatch):
    def broken(**_):
        raise ValueError("bảng không tồn tại")
    bad_args = {"id": "c1", "type": "function", "function": {"name": "describe_table", "arguments": "{not json"}}
    http = FakeHTTP([_msg(tool_calls=[bad_args, _call("describe_table", {"table": "dw.x"}, "c2"), _call("khong_co", {}, "c3")]),
                     _msg(tool_calls=[_call("submit_answer", {"intent": "help", "kind": "answer"}, "c4")])])
    monkeypatch.setattr(llm.urllib.request, "urlopen", http)
    out = client.plan("?", CTX, {"describe_table": broken}, trace=[])
    assert out["kind"] == "answer"
    tool_msgs = [m["content"] for m in http.requests[1]["messages"] if m["role"] == "tool"]
    assert "JSON" in tool_msgs[0] and "bảng không tồn tại" in tool_msgs[1] and "khong_co" in tool_msgs[2]


def test_plan_gives_up_after_max_turns(client, monkeypatch):
    monkeypatch.setattr(llm.urllib.request, "urlopen", FakeHTTP([_msg(content="chưa biết")] * 3))
    with pytest.raises(llm.LLMError):
        client.plan("?", CTX, {}, max_turns=3)


@pytest.mark.parametrize("code, exc", [(401, llm.LLMUnavailable), (402, llm.LLMUnavailable), (400, llm.LLMError)])
def test_http_errors_are_mapped(client, monkeypatch, code, exc):
    monkeypatch.setattr(llm.urllib.request, "urlopen", FakeHTTP([_http_error(code)]))
    with pytest.raises(exc):
        client._create([{"role": "user", "content": "x"}])


def test_retries_transient_errors_then_succeeds(client, monkeypatch):
    http = FakeHTTP([_http_error(429), _http_error(503), _msg(content="ok")])
    monkeypatch.setattr(llm.urllib.request, "urlopen", http)
    assert client._create([{"role": "user", "content": "x"}])["content"] == "ok"
    assert len(http.requests) == 3


def test_rate_limit_is_waited_out_instead_of_failing(client, monkeypatch):
    """Tài khoản bị giới hạn số lượt gọi mỗi phút: client chờ rồi gọi lại, không bỏ cuộc sau vài giây."""
    http = FakeHTTP([_http_error(429)] * 4 + [_msg(content="ok")])
    monkeypatch.setattr(llm.urllib.request, "urlopen", http)
    assert client._create([{"role": "user", "content": "x"}])["content"] == "ok"
    assert len(http.requests) == 5


def test_rate_limit_wait_follows_provider_headers():
    assert llm._rate_limit_wait({"Retry-After": "7"}, 0) == 7.0
    assert llm._rate_limit_wait({"Retry-After": "600"}, 0) == 30.0            # không chờ quá 30 giây mỗi lần
    reset_ms = (llm.time.time() + 12) * 1000
    assert 11 <= llm._rate_limit_wait({"X-RateLimit-Reset": str(reset_ms)}, 0) <= 13
    assert llm._rate_limit_wait({}, 2) == 15.0 and llm._rate_limit_wait(None, 0) == 5.0


def test_narrate_rejects_invented_numbers_and_unknown_placeholders(client, monkeypatch):
    facts = {"dt": {"text": "12,5 tỷ"}}
    replies = [_msg(content='{"text": "Doanh thu đạt {dt}."}'), _msg(content='{"text": "Doanh thu đạt 12 tỷ."}'),
               _msg(content='{"text": "Doanh thu đạt {khac}."}')]
    monkeypatch.setattr(llm.urllib.request, "urlopen", FakeHTTP(replies))
    assert client.narrate("q", facts, "t") == "Doanh thu đạt {dt}."
    assert client.narrate("q", facts, "t") is None
    assert client.narrate("q", facts, "t") is None


def test_repair_sql_requires_sql(client, monkeypatch):
    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        FakeHTTP([_msg(content='```json\n{"sql": "SELECT 1", "explanation": "sửa tên cột"}\n```'), _msg(content='{"explanation": "x"}')]))
    assert client.repair_sql("SELEC 1", [], "", "q") == {"sql": "SELECT 1", "explanation": "sửa tên cột"}
    with pytest.raises(llm.LLMError):
        client.repair_sql("SELEC 1", [], "", "q")

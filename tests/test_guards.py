"""Chốt chặn bằng code quanh LLM: tham số tool, plan sai kiểu, giá trị lọc sai chính tả, câu mơ hồ, câu hỏi "vì sao".

Không gọi mạng: client LLM được thay bằng bản giả trả về kết quả định sẵn.
"""
import io
import json

import pytest

from app import llm
from app.agent import get_agent
from app.governance import UserCtx
from app.nlu import get_nlu, is_why_question
from app.semantic import SemanticError, get_semantic
from app.store import get_store

ADMIN = UserCtx("admin", "admin", {})
CTX = {"anchor": "2026-11-09", "role": "analyst", "rls": {}, "memory": {}, "fewshots": "(chưa có)"}


class FakeClient:
    """Thay cho llm.OpenRouter trong agent: trả một kết quả submit_answer định sẵn và ghi lại ngữ cảnh nhận được."""
    model, effort = "fake/model", "mặc định"

    def __init__(self, answer):
        self.answer, self.contexts = answer, []
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "calls": 1}

    def plan(self, question, context, tool_impl, max_turns=8, trace=None):
        self.contexts.append(context)
        return self.answer(tool_impl) if callable(self.answer) else self.answer


def ask(monkeypatch, question, answer, memory=None, clarification=None):
    client = FakeClient(answer)
    monkeypatch.setattr(llm, "get_client", lambda settings, mode: client)
    store = get_store()
    cid = store.create_conversation("admin", "test")
    if memory:
        store.update_conversation(cid, memory=memory)
    try:
        return get_agent().ask(ADMIN, cid, question, clarification=clarification, eval_mode=True, mode="openrouter"), client
    finally:
        store.delete_conversation(cid)


# ------------------------------------------------------------------ tool: tham số và kết quả nộp
def test_tool_argument_error_tells_the_model_the_expected_parameters():
    content, is_err = llm.run_tool({"describe_table": lambda table: {}}, "describe_table", {"ten_bang": "x"})
    assert is_err and "table" in content and "Lỗi tham số" in content


def test_compile_tool_accepts_plan_without_wrapper():
    tools = get_agent()._tool_impls(ADMIN)
    wrapped = tools["compile_semantic_plan"](plan={"metrics": ["so_don"]})
    flat = tools["compile_semantic_plan"](metrics=["so_don"])
    assert wrapped["ok"] and flat["ok"] and flat["plan"] == wrapped["plan"]
    assert "submit_answer" in flat["next"] and "sql" not in flat


def test_compile_tool_warns_when_plan_drops_a_value_named_in_the_question():
    draft = get_nlu().draft_for_llm("Tồn kho điện thoại theo kho")
    assert draft["plan"]["filters"][0]["values"] == ["Điện thoại"]
    compile_plan = get_agent()._tool_impls(ADMIN, draft)["compile_semantic_plan"]
    without = compile_plan(plan={"metrics": ["ton_kho_sl"], "dimensions": ["kho"]})
    assert without["ok"] and "Điện thoại" in without["warnings"][0] and "cảnh báo" in without["next"]
    assert "warnings" not in compile_plan(plan=draft["plan"])
    assert "warnings" not in compile_plan(plan={"metrics": ["ton_kho_sl"], "dimensions": ["kho", "nganh_hang"]})   # chia theo chính chiều đó


def test_compile_tool_warns_when_a_ranking_has_nothing_to_rank():
    """“Top 10 khách hàng…”: semantic layer không có chiều khách hàng, plan chỉ còn limit -> một dòng tổng."""
    compile_plan = get_agent()._tool_impls(ADMIN)["compile_semantic_plan"]
    lonely = compile_plan(plan={"metrics": ["doanh_thu_thuan"], "limit": 10,
                                "order_by": [{"field": "doanh_thu_thuan", "direction": "desc"}]})
    assert lonely["ok"] and "MỘT dòng tổng" in lonely["warnings"][0] and "CHƯA nên nộp" in lonely["next"]
    ranked = compile_plan(plan={"metrics": ["doanh_thu_thuan"], "dimensions": ["chi_nhanh"], "limit": 10})
    assert "warnings" not in ranked and "Nộp ngay" in ranked["next"]


def test_normalize_answer_recovers_misplaced_plan_and_missing_kind():
    out = llm.normalize_answer({"intent": "metric_query", "metrics": ["so_don"], "dimensions": ["thang"]})
    assert out["kind"] == "semantic" and out["semantic_plan"] == {"metrics": ["so_don"], "dimensions": ["thang"]}
    assert llm.normalize_answer({"intent": "metric_query", "plan": {"metrics": ["so_don"]}})["semantic_plan"] == {"metrics": ["so_don"]}
    assert llm.normalize_answer({"intent": "metric_query", "sql": "SELECT 1"})["kind"] == "sql"
    assert llm.normalize_answer({"intent": "help"})["kind"] == "answer"


class FakeHTTP:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, req, timeout=None):
        self.requests.append(json.loads(req.data.decode("utf-8")))
        return io.BytesIO(json.dumps(self.responses.pop(0)).encode("utf-8"))


def _reply(name, args, cid="c"):
    call = {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}], "usage": {}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    return llm.OpenRouter({})


def test_same_failing_call_stops_early_instead_of_using_every_turn(client, monkeypatch):
    http = FakeHTTP([_reply("describe_table", {"sai": 1})] * 8)
    monkeypatch.setattr(llm.urllib.request, "urlopen", http)
    with pytest.raises(llm.LLMError, match="lặp lại"):
        client.plan("?", CTX, {"describe_table": lambda table: {}})
    assert len(http.requests) == llm.MAX_SAME_FAILURE


def test_repeated_successful_call_is_not_rerun_and_then_submission_is_forced(client, monkeypatch):
    runs = []
    http = FakeHTTP([_reply("describe_table", {"table": "t"})] * 3 + [_reply("submit_answer", {"intent": "help", "kind": "answer"})])
    monkeypatch.setattr(llm.urllib.request, "urlopen", http)
    out = client.plan("?", CTX, {"describe_table": lambda table: runs.append(table) or {"ok": True}})
    assert out["kind"] == "answer" and runs == ["t"]
    assert http.requests[2]["messages"][-1]["content"] == llm.REPEATED_CALL
    assert http.requests[2]["tool_choice"] == "auto"
    assert http.requests[3]["tool_choice"] == {"type": "function", "function": {"name": "submit_answer"}}


def test_last_turn_forces_submission(client, monkeypatch):
    http = FakeHTTP([_reply("describe_table", {"table": f"t{i}"}) for i in range(2)])
    monkeypatch.setattr(llm.urllib.request, "urlopen", http)
    with pytest.raises(llm.LLMError):
        client.plan("?", CTX, {"describe_table": lambda table: {}}, max_turns=2)
    assert http.requests[0]["tool_choice"] == "auto" and http.requests[1]["tool_choice"]["function"]["name"] == "submit_answer"


def test_hints_are_added_to_the_user_message():
    msg = llm.user_message("Vì sao giảm?", dict(CTX, hints=["Đây là câu hỏi tìm nguyên nhân"]))
    assert "Lưu ý từ hệ thống" in msg and msg.endswith("Câu hỏi: Vì sao giảm?")
    assert "Lưu ý từ hệ thống" not in llm.user_message("x", CTX)


# ------------------------------------------------------------------ plan: chuẩn hóa và kiểm giá trị lọc
def test_plan_is_normalized_and_filter_values_are_put_in_catalog_spelling():
    p = get_semantic().validate_plan({"metrics": "doanh_thu_thuan", "dimensions": "thang",
                                      "filters": [{"dimension": "vung", "op": "eq", "values": "mien bac"}],
                                      "time_range": {"start": "2026-10-01T00:00:00", "end": "2026-11-01"}})
    assert p["metrics"] == ["doanh_thu_thuan"] and p["dimensions"] == ["thang"]
    assert p["filters"][0]["values"] == ["Miền Bắc"] and p["time_range"]["start"] == "2026-10-01"


@pytest.mark.parametrize("value", ["B", "T", "DT"])
def test_filter_value_that_is_a_code_is_rejected_with_the_valid_names(value):
    """Mô hình hay lọc theo mã ('B', 'DT') thay cho tên hiển thị -> trước đây trả kết quả rỗng mà không báo gì."""
    dim = "nganh_hang" if value == "DT" else "vung"
    with pytest.raises(SemanticError) as e:
        get_semantic().compile({"metrics": ["doanh_thu_thuan"], "filters": [{"dimension": dim, "op": "in", "values": [value]}]})
    assert "Giá trị hợp lệ" in str(e.value) and ("Miền Bắc" in str(e.value) or "Điện thoại" in str(e.value))


@pytest.mark.parametrize("period", [{"start": "2026-10", "end": "2026-11-01"}, {"start": "2026-11-01", "end": "2026-10-01"}, "tháng 10"])
def test_bad_period_is_rejected(period):
    with pytest.raises(SemanticError):
        get_semantic().validate_plan({"metrics": ["so_don"], "time_range": period})


def test_filter_on_time_dimension_is_rejected_and_points_to_time_range():
    """Mô hình nhét thời gian vào filters (thang = '10') -> truy vấn rỗng. Phải báo lỗi và chỉ sang time_range."""
    with pytest.raises(SemanticError, match="time_range"):
        get_semantic().validate_plan({"metrics": ["so_don"], "filters": [{"dimension": "thang", "op": "eq", "values": ["10"]}]})


def test_column_values_tool_accepts_dimension_name_and_explains_unknown_column():
    tool = get_agent()._tool_impls(ADMIN)["get_column_values"]
    assert tool(table="dw.dim_vung", column="vung")["values"] == tool(table="dw.dim_vung", column="ten_vung")["values"]
    assert "Miền Bắc" in tool(table="dw.fct_tk_tuan", column="vung")["values"]
    with pytest.raises(ValueError, match="ten_vung"):
        tool(table="dw.dim_vung", column="khong_co")
    with pytest.raises(ValueError, match="time_range"):
        tool(table="dw.fct_ct_dh", column="thang")


def test_prompt_lists_filter_values_of_small_dimensions():
    text = llm._semantic_summary()
    assert "- vung: Vùng miền = dw.dim_vung.ten_vung [" in text and "Miền Bắc" in text
    assert "- thang: Tháng (time grain" in text
    assert "look up with get_column_values" in text              # chiều nhiều giá trị chỉ nêu ví dụ


def test_order_by_and_having_given_as_single_objects_are_accepted():
    c = get_semantic().compile({"metrics": ["doanh_thu_thuan"], "dimensions": ["chi_nhanh"], "limit": "3",
                                "order_by": {"field": "doanh_thu_thuan", "direction": "desc"},
                                "having": {"metric": "doanh_thu_thuan", "op": ">", "value": 15000000000}})
    assert "HAVING" in c["sql"] and "ORDER BY doanh_thu_thuan DESC" in c["sql"] and c["sql"].endswith("LIMIT 3")
    with pytest.raises(SemanticError, match="having"):
        get_semantic().validate_plan({"metrics": ["so_don"], "having": [{"metric": "khong_co", "value": 1}]})


def test_like_filter_and_open_ended_period_are_left_alone():
    p = get_semantic().validate_plan({"metrics": ["so_don"], "filters": [{"dimension": "san_pham", "op": "like", "values": ["iphone"]}],
                                      "time_range": {"start": "2026-01-01"}})
    assert p["filters"][0]["values"] == ["iphone"] and p["time_range"] == {"start": "2026-01-01"}


# ------------------------------------------------------------------ thuật ngữ mơ hồ (khai báo trong semantic layer)
@pytest.mark.parametrize("question, amb_id", [
    ("Doanh thu tháng trước là bao nhiêu?", "revenue_definition"),
    ("Tháng 10/2026 bán được bao nhiêu tiền?", "revenue_definition"),
    ("Sản phẩm bán chạy nhất tháng 10/2026", "best_criteria"),
    ("Vì sao doanh thu tháng 9/2026 giảm?", "revenue_definition"),
])
def test_ambiguous_question_is_detected(question, amb_id):
    c = get_nlu().detect_ambiguity(question)
    assert c and c["id"] == amb_id and len(c["options"]) >= 2


@pytest.mark.parametrize("question", [
    "Doanh thu thuần năm 2025 là bao nhiêu?",
    "Top 5 cửa hàng có nhiều nhân viên nhất",            # "top" là trigger yếu: để LLM tự hiểu tiêu chí
    "Doanh thu bán hàng thực nhận sau chiết khấu",       # đã có từ phân biệt (unless_any)
    "Doanh thu là gì?",                                  # hỏi định nghĩa, không phải hỏi số
    "Tồn kho điện thoại theo kho",
])
def test_clear_question_is_not_blocked(question):
    assert get_nlu().detect_ambiguity(question) is None


def test_stored_preference_answers_the_ambiguity():
    assert get_nlu().detect_ambiguity("Doanh thu tháng trước", {"prefs": {"revenue_metric": "doanh_thu_gop"}}) is None
    assert get_nlu().pref_key_for(["doanh_thu_thuan", "doanh_thu_gop"]) == "revenue_metric"
    assert get_nlu().pref_key_for(["so_don", "chi_phi_mkt"]) is None
    assert get_nlu().ambiguity_metrics() == ["doanh_thu_thuan", "doanh_thu_gop"]


def test_llm_mode_asks_back_without_calling_the_model(monkeypatch):
    resp, client = ask(monkeypatch, "Doanh thu tháng trước là bao nhiêu?", {"intent": "metric_query", "kind": "sql", "sql": "SELECT 1"})
    assert resp["type"] == "clarification" and client.contexts == []
    assert len(resp["clarification"]["options"]) == 2


def test_chosen_definition_is_passed_to_the_model_as_a_hint(monkeypatch):
    store, agent = get_store(), get_agent()
    client = FakeClient({"intent": "metric_query", "kind": "semantic", "semantic_plan": {"metrics": ["doanh_thu_gop"]}})
    monkeypatch.setattr(llm, "get_client", lambda settings, mode: client)
    cid = store.create_conversation("admin", "test")
    try:
        first = agent.ask(ADMIN, cid, "Doanh thu tháng trước là bao nhiêu?", eval_mode=True, mode="openrouter")
        second = agent.ask(ADMIN, cid, "", clarification={"id": first["clarification"]["id"], "option": 1},
                           eval_mode=True, mode="openrouter")
    finally:
        store.delete_conversation(cid)
    assert second["type"] == "result" and "Doanh thu gộp" in client.contexts[0]["hints"][0]


# ------------------------------------------------------------------ bản nháp plan của bộ luật cho LLM kiểm tra
def test_rule_draft_reports_the_words_it_did_not_use():
    nlu = get_nlu()
    clean = nlu.draft_for_llm("Doanh thu thuần theo miền tháng 9/2026")
    assert clean["plan"]["metrics"] == ["doanh_thu_thuan"] and clean["plan"]["dimensions"] == ["vung"] and clean["unparsed"] == []
    assert nlu.draft_for_llm("Doanh thu thuần theo quý năm 2025 so với cùng kỳ")["unparsed"] == []
    partial = nlu.draft_for_llm("Top 10 khách hàng có doanh thu thuần cao nhất năm 2025")
    assert partial["unparsed"] == ["khách", "hàng"]                      # bộ luật không có chiều "khách hàng"
    assert "trung" in nlu.draft_for_llm("Số đơn trung bình mỗi ngày theo thứ trong tuần năm 2025")["unparsed"]
    assert nlu.draft_for_llm("Doanh thu tháng trước là bao nhiêu?") is None            # mơ hồ -> không nháp
    assert nlu.draft_for_llm("Vì sao doanh thu thuần tháng 9/2026 giảm?") is None       # câu "vì sao" đi nhánh riêng


def test_rule_draft_is_given_to_the_model_as_a_hint(monkeypatch):
    plan = {"metrics": ["doanh_thu_thuan"], "dimensions": ["vung"],
            "time_range": {"start": "2026-09-01", "end": "2026-10-01"}}
    resp, client = ask(monkeypatch, "Doanh thu thuần theo miền tháng 9/2026",
                       {"intent": "metric_query", "kind": "semantic", "semantic_plan": plan})
    hint = client.contexts[0]["hints"][0]
    assert resp["type"] == "result" and '"dimensions": ["vung"]' in hint and "đã dùng hết" in hint
    _, client = ask(monkeypatch, "Top 10 khách hàng có doanh thu thuần cao nhất năm 2025",
                    {"intent": "metric_query", "kind": "semantic", "semantic_plan": plan})
    partial = client.contexts[0]["hints"][0]
    assert "MỘT PHẦN" in partial and "khách, hàng" in partial and "viết SQL" in partial


# ------------------------------------------------------------------ câu hỏi "vì sao"
def test_why_question_detection():
    assert is_why_question("Vì sao doanh thu thuần tháng 9/2026 giảm?") and is_why_question("Nguyên nhân tỷ lệ hủy tăng")
    assert not is_why_question("Doanh thu thuần tháng 9/2026")


def test_why_question_answered_with_plain_sql_falls_back_to_variance_plan(monkeypatch):
    sql = "SELECT SUM(f.tt) AS x FROM dw.fct_ct_dh f WHERE f.trang_thai = 'HOAN_THANH'"
    resp, client = ask(monkeypatch, "Vì sao doanh thu thuần tháng 9/2026 giảm so với cùng kỳ năm trước?",
                       {"intent": "metric_query", "kind": "sql", "sql": sql})
    assert resp["type"] == "variance" and resp["variance"]["best_dimension"]
    assert any("nguyên nhân" in h for h in client.contexts[0]["hints"])
    assert any("bằng luật" in n for n in resp["notes"])


def test_why_question_is_not_derailed_by_a_clarification_the_business_never_declared(monkeypatch):
    """Thuật ngữ mơ hồ đã được code kiểm trước khi gọi LLM; mô hình tự hỏi lại ở câu bộ luật hiểu trọn thì bỏ qua."""
    resp, _ = ask(monkeypatch, "Vì sao doanh thu thuần Miền Trung tháng 6/2026 giảm?",
                  {"intent": "variance", "kind": "clarify",
                   "clarification": {"question": "Bạn muốn so với kỳ nào?", "options": [{"label": "Tháng trước"}, {"label": "Cùng kỳ"}]}})
    assert resp["type"] == "variance" and resp["variance"]["best_dimension"]


def test_plain_comparison_labelled_variance_by_the_model_returns_the_comparison_table(monkeypatch):
    plan = {"metrics": ["doanh_thu_gop"], "dimensions": ["vung"],
            "time_range": {"start": "2026-07-01", "end": "2026-10-01", "label": "Q3/2026"},
            "compare": {"start": "2025-07-01", "end": "2025-10-01", "label": "Q3/2025"}}
    resp, _ = ask(monkeypatch, "Doanh thu gộp quý 3 năm 2026 so với cùng kỳ theo miền",
                  {"intent": "variance", "kind": "semantic", "semantic_plan": plan})
    assert resp["type"] == "result" and resp["intent"] == "metric_query"
    assert resp["columns"][:3] == ["vung", "doanh_thu_gop_ky_nay", "doanh_thu_gop_ky_truoc"] and len(resp["rows"]) == 3


def test_model_clarification_is_still_honoured_for_ordinary_questions(monkeypatch):
    resp, _ = ask(monkeypatch, "Cửa hàng nào đáng chú ý nhất tháng 10/2026?",
                  {"intent": "metric_query", "kind": "clarify",
                   "clarification": {"question": "Theo tiêu chí nào?", "options": [{"label": "Doanh thu thuần", "metric": "doanh_thu_thuan"},
                                                                                    {"label": "Số đơn", "metric": "so_don"}]}})
    assert resp["type"] == "clarification" and resp["clarification"]["question"] == "Theo tiêu chí nào?"


def test_why_question_with_valid_variance_plan_from_model_is_used(monkeypatch):
    plan = {"metrics": ["doanh_thu_thuan"], "filters": [{"dimension": "vung", "op": "in", "values": ["Miền Trung"]}],
            "time_range": {"start": "2026-06-01", "end": "2026-07-01", "label": "T6/2026"},
            "compare": {"start": "2026-05-01", "end": "2026-06-01", "label": "T5/2026"}}
    resp, _ = ask(monkeypatch, "Vì sao doanh thu thuần Miền Trung tháng 6/2026 giảm?",
                  {"intent": "variance", "kind": "semantic", "semantic_plan": plan})
    assert resp["type"] == "variance" and not any("bằng luật" in n for n in resp["notes"])

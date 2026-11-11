"""
Tích hợp Claude (Anthropic SDK) cho agent:
  - plan(): vòng lặp function calling — Claude tự tra schema, xem giá trị cột, dry-run SQL, biên dịch semantic plan,
    rồi nộp kết quả qua tool submit_answer (semantic plan | SQL | câu hỏi làm rõ | câu trả lời).
  - repair_sql(): sửa SQL lỗi dựa trên thông báo lỗi + schema (structured output).
  - narrate(): viết lời diễn giải CHỈ dùng placeholder {fact_id}; code kiểm tra không có chữ số "tự chế".
Mọi đầu ra của LLM vẫn phải qua governance/validator bằng code trước khi chạy.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import time

from . import config
from .catalog import get_catalog
from .semantic import get_semantic

BETAS = ["server-side-fallback-2026-07-01"]


class LLMUnavailable(Exception):
    pass


class LLMError(Exception):
    pass


def credentials_available() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_PROFILE"):
        return True
    return (pathlib.Path.home() / ".config" / "anthropic").exists()


def resolve_mode(settings: dict) -> str:
    m = settings.get("llm_mode", "auto")
    if m == "offline":
        return "offline"
    if m == "claude":
        return "claude"
    return "claude" if credentials_available() else "offline"


SYSTEM = """You are the planning brain of a self-service analytics agent for "Công ty CP Bán lẻ Điện máy ABC", a Vietnamese
electronics retail chain. Users are business staff who ask questions in Vietnamese. The warehouse is DuckDB with schema `dw`
(cleaned marts: fct_* facts and dim_* dimensions; column names are Vietnamese abbreviations — always check descriptions).

How to work:
1. Prefer a SEMANTIC PLAN whenever the question is about a named metric in the semantic layer below. The semantic layer is the
   single source of truth for metric definitions (e.g. "doanh thu thuần" vs "doanh thu gộp" differ). Never re-implement a named
   metric in raw SQL with a different formula or status filter.
2. Use raw SQL only when the semantic layer cannot express the question (e.g. customer-level rankings, product lists with
   conditions, set operations). Raw SQL rules: DuckDB dialect; fully-qualified tables (dw.fct_ct_dh); only SELECT; never SELECT *;
   filter time with sargable ranges on partition columns (f.ngay_dh >= DATE 'YYYY-MM-DD' AND f.ngay_dh < DATE 'YYYY-MM-DD');
   apply order-status filters consistent with metric definitions; do NOT join dw.fct_dh with dw.fct_ct_dh and then SUM header
   columns (fan-out); inventory (dw.fct_tk_tuan) is semi-additive — pick one ngay_chot per period. Always call dry_run_sql on raw
   SQL and fix all errors before submitting.
3. Ask a clarifying question (kind=clarify, 2–4 options) ONLY when the request is genuinely ambiguous in a way that changes the
   numbers — e.g. "doanh thu" without gộp/thuần when the conversation has no stored preference, or a ranking with no criterion.
   Otherwise make a reasonable assumption and list it in `assumptions` (in Vietnamese).
4. Time: the data anchor date (latest data) is given in the user message. "tháng này" = anchor month up to the anchor date;
   "tháng trước" = previous full month; ranges are [start, end) with end exclusive. Comparison questions ("so với", "tăng
   trưởng", "cùng kỳ") use semantic_plan.compare. "Vì sao/tại sao ... giảm/tăng" questions use intent=variance with
   semantic_plan.time_range (current period) and semantic_plan.compare (comparison period).
5. Columns marked restricted (PII/confidential) for the user's role must not be used. If asked for them, explain the restriction
   in answer_text and offer an aggregate alternative.
6. Conversation memory (previous plan, preferences) is provided — resolve follow-ups ("còn Miền Nam thì sao?") against it.
7. Finish by calling submit_answer exactly once. Keep `assumptions` and `answer_text` in Vietnamese.
"""


def _semantic_summary() -> str:
    sem = get_semantic()
    lines = ["SEMANTIC LAYER — metrics (name: label [model] — definition):"]
    for n, m in sem.metrics.items():
        defi = m.get("sql") or m.get("expr")
        filt = f" WHERE {m['filter']}" if m.get("filter") else ""
        lines.append(f"- {n}: {m['label']} [{m.get('model', 'derived')}] — {defi}{filt}. {m.get('description', '')}")
    lines.append("Dimensions (name: label): " + "; ".join(f"{n}: {d['label']}" for n, d in sem.dims.items()))
    lines.append("Model → supported dimensions:")
    for mn in sem.models:
        ds = [d for d in sem.dims if sem.dim_supported(mn, d)]
        lines.append(f"- {mn} ({sem.models[mn]['table']}): {', '.join(ds)}")
    lines.append('Filter ops: in, not_in, like (contains, for product names), eq, gt, lt. Time grains as dimensions: ngay, tuan, thang, quy, nam.')
    return "\n".join(lines)


def _schema_overview() -> str:
    cat = get_catalog()
    out = ["WAREHOUSE TABLES (dw):"]
    for fq, t in cat.tables.items():
        if t["schema"] == "dw":
            out.append(f"- {fq} ({t['rows']:,} rows): {t['description']}")
    return "\n".join(out)


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "metrics": {"type": "array", "items": {"type": "string"}},
        "dimensions": {"type": "array", "items": {"type": "string"}},
        "filters": {"type": "array", "items": {"type": "object", "properties": {
            "dimension": {"type": "string"}, "op": {"type": "string"}, "values": {"type": "array", "items": {"type": "string"}}},
            "required": ["dimension", "op", "values"]}},
        "time_range": {"type": ["object", "null"], "properties": {"start": {"type": "string"}, "end": {"type": "string"},
                                                                   "label": {"type": "string"}}},
        "compare": {"type": ["object", "null"], "properties": {"start": {"type": "string"}, "end": {"type": "string"},
                                                                "label": {"type": "string"}}},
        "having": {"type": "array", "items": {"type": "object", "properties": {
            "metric": {"type": "string"}, "op": {"type": "string"}, "value": {"type": "number"}}}},
        "order_by": {"type": "array", "items": {"type": "object", "properties": {
            "field": {"type": "string"}, "direction": {"type": "string", "enum": ["asc", "desc"]}}}},
        "limit": {"type": ["integer", "null"]},
    },
    "required": ["metrics"],
}

TOOLS = [
    {"name": "search_schema", "description": "Tìm bảng/cột/chỉ số liên quan tới một cụm từ (BM25 trên catalog + semantic layer). "
     "Trả về mô tả schema dạng văn bản.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "describe_table", "description": "Mô tả đầy đủ một bảng: cột, kiểu, ý nghĩa, giá trị mẫu, cột bị hạn chế theo vai trò.",
     "input_schema": {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]}},
    {"name": "get_column_values", "description": "Liệt kê giá trị phân biệt của một cột dạng văn bản (để khớp đúng chính tả bộ lọc).",
     "input_schema": {"type": "object", "properties": {"table": {"type": "string"}, "column": {"type": "string"},
                                                       "contains": {"type": "string"}}, "required": ["table", "column"]}},
    {"name": "dry_run_sql", "description": "Kiểm tra SQL KHÔNG chạy: cú pháp, phân quyền (RBAC/RLS), lint logic, ước lượng bytes quét.",
     "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}},
    {"name": "compile_semantic_plan", "description": "Biên dịch semantic plan thành SQL để xem trước và kiểm tra lỗi.",
     "input_schema": {"type": "object", "properties": {"plan": PLAN_SCHEMA}, "required": ["plan"]}},
    {"name": "submit_answer", "description": "Nộp kết quả cuối cùng. Gọi đúng một lần.",
     "input_schema": {"type": "object", "properties": {
         "intent": {"type": "string", "enum": ["metric_query", "variance", "schema_info", "metric_info", "dq", "help"]},
         "kind": {"type": "string", "enum": ["semantic", "sql", "clarify", "answer"]},
         "semantic_plan": PLAN_SCHEMA,
         "sql": {"type": "string"},
         "clarification": {"type": "object", "properties": {
             "question": {"type": "string"},
             "options": {"type": "array", "items": {"type": "object", "properties": {
                 "label": {"type": "string"}, "metric": {"type": "string"}, "rewrite": {"type": "string"}},
                 "required": ["label"]}}}},
         "answer_text": {"type": "string"},
         "assumptions": {"type": "array", "items": {"type": "string"}},
         "chart_hint": {"type": "string", "enum": ["line", "bar", "pie", "table", "auto"]},
         "restated_question": {"type": "string"}},
         "required": ["intent", "kind"]}},
]


class Claude:
    def __init__(self, settings: dict):
        import anthropic
        self.anthropic = anthropic
        self.model = settings.get("llm_model") or "claude-opus-5-5"
        self.effort = settings.get("llm_effort") or "medium"
        self.client = anthropic.Anthropic(max_retries=2, timeout=180)
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "calls": 0}
        self._fallbacks = True

    def _create(self, **kw):
        a = self.anthropic
        oc = {"effort": self.effort}
        oc.update(kw.pop("output_config", None) or {})
        params = dict(model=self.model, max_tokens=16000, output_config=oc, **kw)
        for attempt in range(2):
            try:
                if self._fallbacks:
                    resp = self.client.beta.messages.create(betas=BETAS, extra_body={"fallbacks": "default"}, **params)
                else:
                    resp = self.client.beta.messages.create(**params)
                break
            except a.BadRequestError as e:
                if self._fallbacks and "fallback" in str(e).lower():
                    self._fallbacks = False      # tài khoản/nền tảng không hỗ trợ fallbacks -> gửi lại không kèm
                    continue
                raise LLMError(f"Yêu cầu không hợp lệ: {e}")
            except a.AuthenticationError as e:
                raise LLMUnavailable(f"Xác thực Anthropic thất bại: {e}")
            except a.PermissionDeniedError as e:
                raise LLMUnavailable(f"Không có quyền dùng model {self.model}: {e}")
            except a.NotFoundError as e:
                raise LLMError(f"Không tìm thấy model {self.model}: {e}")
            except a.RateLimitError as e:
                raise LLMError(f"Vượt giới hạn tốc độ API: {e}")
            except a.APIStatusError as e:
                raise LLMError(f"Lỗi API ({e.status_code}): {e}")
            except a.APIConnectionError as e:
                raise LLMUnavailable(f"Không kết nối được Anthropic API: {e}")
        u = getattr(resp, "usage", None)
        if u is not None:
            self.usage["input_tokens"] += getattr(u, "input_tokens", 0) or 0
            self.usage["output_tokens"] += getattr(u, "output_tokens", 0) or 0
            self.usage["cache_read_input_tokens"] += getattr(u, "cache_read_input_tokens", 0) or 0
        self.usage["calls"] += 1
        if resp.stop_reason == "refusal":
            raise LLMError("Model từ chối yêu cầu (refusal).")
        return resp

    def _system(self):
        return [{"type": "text", "text": SYSTEM + "\n\n" + _semantic_summary() + "\n\n" + _schema_overview(),
                 "cache_control": {"type": "ephemeral"}}]

    # ------------------------------------------------------------------ lập kế hoạch (function calling)
    def plan(self, question: str, context: dict, tool_impl: dict, max_turns: int = 8, trace=None) -> dict:
        user_msg = (
            f"Ngày dữ liệu mới nhất (anchor): {context['anchor']}. Vai trò người dùng: {context['role']}"
            f"{' (RLS: ' + json.dumps(context['rls'], ensure_ascii=False) + ')' if context.get('rls') else ''}.\n"
            f"Bộ nhớ hội thoại: {json.dumps(context.get('memory', {}), ensure_ascii=False, default=str)[:3000]}\n"
            f"Ví dụ đã được xác nhận (few-shot):\n{context.get('fewshots', '(chưa có)')}\n\n"
            f"Câu hỏi: {question}")
        messages = [{"role": "user", "content": user_msg}]
        for turn in range(max_turns):
            resp = self._create(system=self._system(), tools=TOOLS, tool_choice={"type": "auto"}, messages=messages)
            if resp.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": resp.content})
                continue
            messages.append({"role": "assistant", "content": resp.content})
            uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if not uses:
                text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")
                m = re.search(r"\{.*\}", text, re.S)
                if m:
                    try:
                        return json.loads(m.group(0))
                    except json.JSONDecodeError:
                        pass
                messages.append({"role": "user", "content": "Hãy nộp kết quả bằng tool submit_answer."})
                continue
            results = []
            final = None
            for u in uses:
                inp = u.input if isinstance(u.input, dict) else json.loads(u.input or "{}")
                if u.name == "submit_answer":
                    final = inp
                    results.append({"type": "tool_result", "tool_use_id": u.id, "content": "OK"})
                    continue
                t0 = time.time()
                try:
                    out = tool_impl[u.name](**inp)
                    content, is_err = json.dumps(out, ensure_ascii=False, default=str)[:12000], False
                except Exception as e:  # lỗi tool -> báo lại cho model
                    content, is_err = f"Lỗi: {e}", True
                if trace is not None:
                    trace.append({"tool": u.name, "input": inp, "ms": int((time.time() - t0) * 1000), "error": is_err})
                results.append({"type": "tool_result", "tool_use_id": u.id, "content": content, "is_error": is_err})
            if final is not None:
                return final
            messages.append({"role": "user", "content": results})
        raise LLMError("Agent không hoàn thành trong số vòng cho phép.")

    # ------------------------------------------------------------------ sửa SQL
    def repair_sql(self, sql: str, errors: list, schema_text: str, question: str) -> dict:
        schema = {"type": "object", "properties": {"sql": {"type": "string"}, "explanation": {"type": "string"}},
                  "required": ["sql", "explanation"], "additionalProperties": False}
        prompt = (f"Câu hỏi: {question}\nSQL (DuckDB) bị lỗi:\n{sql}\n\nLỗi kiểm duyệt/thực thi:\n"
                  f"{json.dumps(errors, ensure_ascii=False, default=str)}\n\nSchema liên quan:\n{schema_text}\n\n"
                  "Hãy sửa SQL để đúng schema, chỉ SELECT, giữ nguyên ý nghĩa câu hỏi. explanation bằng tiếng Việt, 1 câu.")
        resp = self._create(system=self._system(), messages=[{"role": "user", "content": prompt}],
                            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}})
        text = next(b.text for b in resp.content if getattr(b, "type", None) == "text")
        return json.loads(text)

    # ------------------------------------------------------------------ viết lời (placeholder)
    def narrate(self, question: str, facts: dict, template: str) -> str | None:
        schema = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}
        listing = "\n".join(f"{{{k}}} = {v['text']}" for k, v in facts.items())
        prompt = (f"Câu hỏi của người dùng: {question}\nCác dữ kiện (đã kiểm chứng) và placeholder:\n{listing}\n\n"
                  f"Bản nháp tự động: {template}\n\nViết lại thành 2–4 câu tiếng Việt tự nhiên, súc tích cho người làm nghiệp vụ. "
                  "QUY TẮC BẮT BUỘC: mọi con số, ngày tháng, nhãn kỳ chỉ được xuất hiện thông qua placeholder dạng {ten_fact}; "
                  "không viết bất kỳ chữ số nào trực tiếp; không thêm số liệu không có trong danh sách; giữ đúng chiều tăng/giảm.")
        resp = self._create(messages=[{"role": "user", "content": prompt}],
                            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}})
        text = json.loads(next(b.text for b in resp.content if getattr(b, "type", None) == "text"))["text"]
        stripped = re.sub(r"\{[a-z0-9_]+\}", "", text)
        if re.search(r"\d", stripped):
            return None          # LLM tự chế số -> bỏ, dùng bản template
        unknown = [p for p in re.findall(r"\{([a-z0-9_]+)\}", text) if p not in facts]
        if unknown:
            return None
        return text

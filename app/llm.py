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
import urllib.error
import urllib.request

from . import config
from .catalog import get_catalog
from .semantic import get_semantic

BETAS = ["server-side-fallback-2026-07-01"]
LLM_MODES = ("claude", "openrouter")          # các chế độ có gọi mô hình ngôn ngữ
MODE_LABEL = {"claude": "Claude", "openrouter": "OpenRouter", "offline": "Offline NLU"}
OPENROUTER_DEFAULT_MODEL = "openai/gpt-6-luna"    # đo 12/11/2026: đúng hơn và rẻ hơn gpt-4o-mini trên cả hai bộ câu hỏi mẫu
INLINE_VALUES = 15            # chiều có tối đa từng này giá trị thì liệt kê thẳng trong prompt để mô hình không đoán mã


class LLMUnavailable(Exception):
    pass


class LLMError(Exception):
    pass


def credentials_available() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_PROFILE"):
        return True
    return (pathlib.Path.home() / ".config" / "anthropic").exists()


def openrouter_available() -> bool:
    return bool(os.environ.get("OPENROUTER_API_KEY"))


def any_credentials() -> bool:
    return credentials_available() or openrouter_available()


def resolve_mode(settings: dict) -> str:
    """auto: Claude nếu có credentials Anthropic, kế đến OpenRouter nếu có OPENROUTER_API_KEY, cuối cùng là offline."""
    m = settings.get("llm_mode", "auto")
    if m in ("offline", "claude", "openrouter"):
        return m
    if credentials_available():
        return "claude"
    return "openrouter" if openrouter_available() else "offline"


def effective_model(settings: dict, mode: str | None = None) -> str:
    mode = mode or resolve_mode(settings)
    m = settings.get("llm_model") or ""
    if mode == "openrouter":       # model OpenRouter có dạng "nhà-cung-cấp/tên"; tên Claude trần thì bỏ qua
        return m if "/" in m else os.environ.get("OPENROUTER_MODEL", OPENROUTER_DEFAULT_MODEL)
    return m or "claude-opus-5-5"


def get_client(settings: dict, mode: str):
    return OpenRouter(settings) if mode == "openrouter" else Claude(settings)


def _json_obj(text: str | None) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise LLMError("Mô hình không trả về JSON.")
    try:
        out = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise LLMError(f"JSON của mô hình không hợp lệ: {e}")
    if not isinstance(out, dict):
        raise LLMError("JSON của mô hình không phải object.")
    return out


def system_prompt() -> str:
    """Prompt hệ thống = phần mở đầu dựng từ hồ sơ doanh nghiệp (config/profile.yml) + bộ quy tắc chung."""
    from .profile import get_profile
    p = get_profile()
    c, w = p["company"], p["warehouse"]
    head = f'You are the planning brain of a self-service analytics agent for "{c["name"]}"'
    if c.get("industry"):
        head += f' (industry: {c["industry"]})'
    head += (f'. Users are business staff who ask questions in {p["assistant"]["language"]}. '
             f'The warehouse is {w["engine"]} with schema `{w["schema"]}`')
    head += f' ({w["schema_note"]}).' if w.get("schema_note") else "."
    if c.get("description"):
        head += f'\nAbout the business: {c["description"].strip()}'
    notes = "".join(f"\n   - {n}" for n in (w.get("notes") or []))
    rules = (SYSTEM_RULES.replace("__ENGINE__", w["engine"]).replace("__SCHEMA__", w["schema"])
             .replace("__WAREHOUSE_NOTES__", ("\n   Rules specific to this warehouse:" + notes) if notes else ""))
    return head + "\n\n" + rules


SYSTEM_RULES = """How to work:
1. Prefer a SEMANTIC PLAN (kind=semantic) whenever the question is about a named metric in the semantic layer below and only
   needs listed dimensions. The semantic layer is the single source of truth for metric definitions (two metrics with similar
   names can differ in formula or filter). Never re-implement a named metric in raw SQL with a different formula or filter.
   compile_semantic_plan takes {"plan": {...}}; every filter is {"dimension", "op", "values": [...]} with values copied
   exactly from the dimension list below — display names, not codes. Periods go in time_range/compare, never in filters. Do
   not add filters that a metric's definition already contains. When compile_semantic_plan returns ok, submit that plan as
   it is.
2. Use raw SQL only when the semantic layer cannot express the question: it needs a grouping or column that is not a listed
   dimension, a calculation over groups (e.g. an average per day or per entity), entity-level rankings, lists with conditions
   or set operations. In raw SQL still reuse the exact formula and filter of every named metric involved, and return one row
   per entity the user asks about (GROUP BY that entity and aggregate lower-level rows; do not repeat an entity once per
   store, warehouse or day unless the user asks for that breakdown). When ranking or listing entities, leave out rows whose
   entity key is NULL (unidentified). Raw SQL rules: __ENGINE__ dialect; fully-qualified tables (__SCHEMA__.table_name); only SELECT; never
   SELECT *; filter time with sargable ranges on the date/partition column (col >= DATE 'YYYY-MM-DD' AND col < DATE
   'YYYY-MM-DD'); apply status filters consistent with metric definitions; do not sum header-level columns after joining to a
   line-level table (fan-out); do not sum snapshot (semi-additive) measures across time. Look up tables and columns with
   search_schema / describe_table instead of guessing names. Call dry_run_sql once on raw SQL and fix what it reports as
   errors; lint items of severity warning or info are advisory and need no change. Then submit.__WAREHOUSE_NOTES__
3. Ask a clarifying question (kind=clarify, 2–4 options) ONLY when the request is genuinely ambiguous in a way that changes the
   numbers — e.g. a business term that maps to several metrics with different definitions when the conversation has no stored
   preference, or a ranking with no criterion. Otherwise make a reasonable assumption and list it in `assumptions` (in Vietnamese).
4. Time: the data anchor date (latest data) is given in the user message. "tháng này" = anchor month up to the anchor date;
   "tháng trước" = previous full month; ranges are [start, end) with end exclusive. Comparison questions ("so với", "tăng
   trưởng", "cùng kỳ") use semantic_plan.compare. "Vì sao/tại sao ... giảm/tăng" questions use intent=variance with
   semantic_plan.time_range (current period) and semantic_plan.compare (comparison period); never answer a why-question
   with a plain comparison query.
5. Columns marked restricted (PII/confidential) for the user's role must not be used. If asked for them, explain the restriction
   in answer_text and offer an aggregate alternative.
6. Conversation memory (previous plan, preferences) is provided — resolve follow-ups ("còn Miền Nam thì sao?") against it.
7. If the user asks what you are, what you can do or how to use you, submit kind=answer with intent=help and leave answer_text
   empty — the application writes the introduction from the business profile. If the question is outside this business's
   data (general knowledge, forecasts, advice), submit kind=answer with a short Vietnamese answer_text saying it is out of scope
   and what you can help with instead; never invent numbers.
8. Finish by calling submit_answer exactly once. Keep `assumptions` and `answer_text` in Vietnamese.
"""


def _semantic_summary() -> str:
    sem = get_semantic()
    lines = ["SEMANTIC LAYER — metrics (name: label [model] — definition):"]
    for n, m in sem.metrics.items():
        defi = m.get("sql") or m.get("expr")
        filt = f" WHERE {m['filter']}" if m.get("filter") else ""
        lines.append(f"- {n}: {m['label']} [{m.get('model', 'derived')}] — {defi}{filt}. {m.get('description', '')}")
    lines.append("Dimensions (name: label = physical column [values to use in filters, exactly as written]):")
    for n, d in sem.dims.items():
        if d.get("kind") == "time":
            lines.append(f"- {n}: {d['label']} (time grain for grouping; never a filter — put periods in time_range)")
            continue
        loc = sem.dim_column(n)
        s = f"- {n}: {d['label']}" + (f" = {loc[0]}.{loc[1]}" if loc else "")
        vals = sem.dim_values(n)
        if vals and len(vals) <= INLINE_VALUES:
            s += " [" + " | ".join(map(str, vals)) + "]"
        elif vals:
            s += f" [{len(vals)} values, e.g. " + " | ".join(map(str, vals[:4])) + "; look up with get_column_values]"
        lines.append(s)
    lines.append("Model → supported dimensions:")
    for mn in sem.models:
        ds = [d for d in sem.dims if sem.dim_supported(mn, d)]
        lines.append(f"- {mn} ({sem.models[mn]['table']}): {', '.join(ds)}")
    lines.append('Filter ops: in, not_in, like (contains, for product names), eq, gt, lt. Time grains as dimensions: ngay, tuan, thang, quy, nam.')
    return "\n".join(lines)


def _schema_overview() -> str:
    from .profile import get_profile
    schema = get_profile()["warehouse"]["schema"]
    cat = get_catalog()
    out = [f"WAREHOUSE TABLES ({schema}):"]
    for fq, t in cat.tables.items():
        if t["schema"] == schema:
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
    {"name": "get_column_values", "description": "Liệt kê giá trị phân biệt của một cột dạng văn bản (để khớp đúng chính tả bộ lọc). "
     "column nhận tên cột vật lý của bảng, hoặc tên một chiều phân tích trong semantic layer.",
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


PLAN_KEYS = tuple(PLAN_SCHEMA["properties"])
_TOOL_SCHEMA = {t["name"]: t["input_schema"] for t in TOOLS}
MAX_SAME_FAILURE = 3          # cùng một lời gọi tool lỗi lặp lại từng này lần thì dừng, không đốt thêm lượt gọi
REPEATED_CALL = ("Bạn đã gọi tool này với đúng các tham số này và đã có kết quả ở trên. Không gọi lại. "
                 "Hãy nộp kết quả bằng submit_answer.")


def run_tool(tool_impl: dict, name: str, inp: dict, trace: list | None = None) -> tuple[str, bool]:
    """Chạy một tool của agent, trả (nội dung gửi lại mô hình, có lỗi không). Lỗi được viết sao cho mô hình tự sửa được."""
    t0 = time.time()
    try:
        content, is_err = json.dumps(tool_impl[name](**inp), ensure_ascii=False, default=str)[:12000], False
    except TypeError as e:      # sai tên hoặc thiếu tham số
        sch = _TOOL_SCHEMA.get(name, {})
        content, is_err = (f"Lỗi tham số: tool {name} nhận các tham số {list(sch.get('properties', {}))}, "
                           f"bắt buộc {sch.get('required', [])}. Chi tiết: {e}"), True
    except Exception as e:      # lỗi nghiệp vụ của tool -> báo lại cho mô hình
        content, is_err = f"Lỗi: {e}", True
    if trace is not None:
        trace.append({"tool": name, "input": inp, "ms": int((time.time() - t0) * 1000), "error": is_err})
    return content, is_err


def normalize_answer(out: dict) -> dict:
    """Chuẩn hóa kết quả submit_answer: mô hình nhỏ hay đặt plan sai chỗ hoặc bỏ trống `kind`."""
    out = dict(out)
    if not isinstance(out.get("semantic_plan"), dict):
        out.pop("semantic_plan", None)
        alt = out.pop("plan", None)
        if isinstance(alt, dict):
            out["semantic_plan"] = alt
        elif out.get("metrics"):
            out["semantic_plan"] = {k: out.pop(k) for k in PLAN_KEYS if k in out}
    if out.get("kind") not in ("semantic", "sql", "clarify", "answer"):
        out["kind"] = ("sql" if out.get("sql") else "semantic" if out.get("semantic_plan")
                       else "clarify" if out.get("clarification") else "answer")
    return out


def user_message(question: str, context: dict) -> str:
    """Tin nhắn người dùng gửi cho mô hình: ngữ cảnh do code cung cấp + câu hỏi."""
    msg = (f"Ngày dữ liệu mới nhất (anchor): {context['anchor']}. Vai trò người dùng: {context['role']}"
           f"{' (RLS: ' + json.dumps(context['rls'], ensure_ascii=False) + ')' if context.get('rls') else ''}.\n"
           f"Bộ nhớ hội thoại: {json.dumps(context.get('memory', {}), ensure_ascii=False, default=str)[:3000]}\n"
           f"Ví dụ đã được xác nhận (few-shot):\n{context.get('fewshots', '(chưa có)')}\n\n")
    hints = [h for h in (context.get("hints") or []) if h]
    if hints:       # chỉ dẫn do code suy ra từ câu hỏi (vd người dùng vừa chọn định nghĩa, câu hỏi dạng "vì sao")
        msg += "Lưu ý từ hệ thống:\n" + "\n".join(f"- {h}" for h in hints) + "\n\n"
    return msg + f"Câu hỏi: {question}"


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
        return [{"type": "text", "text": system_prompt() + "\n\n" + _semantic_summary() + "\n\n" + _schema_overview(),
                 "cache_control": {"type": "ephemeral"}}]

    # ------------------------------------------------------------------ lập kế hoạch (function calling)
    def plan(self, question: str, context: dict, tool_impl: dict, max_turns: int = 8, trace=None) -> dict:
        messages = [{"role": "user", "content": user_message(question, context)}]
        last_fail, repeats = None, 0
        for turn in range(max_turns):
            choice = {"type": "tool", "name": "submit_answer"} if turn == max_turns - 1 else {"type": "auto"}   # lượt cuối: buộc nộp
            resp = self._create(system=self._system(), tools=TOOLS, tool_choice=choice, messages=messages)
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
                    final = normalize_answer(inp)
                    results.append({"type": "tool_result", "tool_use_id": u.id, "content": "OK"})
                    continue
                if u.name not in tool_impl:
                    content, is_err = f"Lỗi: không có tool tên '{u.name}'.", True
                else:
                    content, is_err = run_tool(tool_impl, u.name, inp, trace)
                key = (u.name, json.dumps(inp, sort_keys=True, default=str))
                repeats = (repeats + 1 if key == last_fail else 1) if is_err else 0
                last_fail = key if is_err else None
                results.append({"type": "tool_result", "tool_use_id": u.id, "content": content, "is_error": is_err})
            if final is not None:
                return final
            if repeats >= MAX_SAME_FAILURE:
                raise LLMError("Mô hình lặp lại cùng một lời gọi tool bị lỗi.")
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


def _rate_limit_wait(headers, attempt: int) -> float:
    """Số giây nên chờ sau lỗi 429: theo Retry-After hoặc X-RateLimit-Reset (mốc thời gian, mili giây) nếu có, không thì
    tăng dần 5, 10, 15, 20 giây. Không chờ quá 30 giây mỗi lần để người dùng không phải đợi vô hạn."""
    try:
        if headers and headers.get("Retry-After"):
            return min(30.0, max(1.0, float(headers["Retry-After"])))
        if headers and headers.get("X-RateLimit-Reset"):
            return min(30.0, max(1.0, float(headers["X-RateLimit-Reset"]) / 1000.0 - time.time() + 0.5))
    except (TypeError, ValueError):
        pass
    return 5.0 * (attempt + 1)


class OpenRouter:
    """Cùng giao diện với Claude (plan / repair_sql / narrate) nhưng gọi API tương thích OpenAI của OpenRouter.

    Dùng thư viện chuẩn (urllib) nên không thêm phụ thuộc. Mọi đầu ra vẫn qua governance/validator bằng code như Claude.
    """

    def __init__(self, settings: dict):
        self.api_key = os.environ.get("OPENROUTER_API_KEY")
        if not self.api_key:
            raise LLMUnavailable("Thiếu OPENROUTER_API_KEY.")
        self.base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
        self.model = effective_model(settings, "openrouter")
        self.effort = "mặc định"
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "calls": 0}

    def _create(self, messages: list, tools: list | None = None, json_mode: bool = False,
                force_tool: str | None = None) -> dict:
        body = {"model": self.model, "messages": messages, "temperature": 0}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = {"type": "function", "function": {"name": force_tool}} if force_tool else "auto"
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(self.base_url + "/chat/completions", data=json.dumps(body).encode("utf-8"),
                                     headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        data = None
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = json.loads(r.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if e.code in (401, 403):
                    raise LLMUnavailable(f"Xác thực OpenRouter thất bại ({e.code}): {detail}")
                if e.code == 402:
                    raise LLMUnavailable(f"Tài khoản OpenRouter không đủ số dư: {detail}")
                if e.code == 429 and attempt < 4:
                    # vượt giới hạn số lượt gọi mỗi phút (tài khoản mới: 20 lượt/phút): chờ đúng thời gian nhà cung cấp báo
                    time.sleep(_rate_limit_wait(e.headers, attempt))
                    continue
                if e.code in (408, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise LLMError(f"Lỗi API OpenRouter ({e.code}): {detail}")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise LLMUnavailable(f"Không kết nối được OpenRouter: {e}")
        if not isinstance(data, dict) or data.get("error") or not data.get("choices"):
            raise LLMError(f"Phản hồi OpenRouter không hợp lệ: {json.dumps((data or {}).get('error') or data, ensure_ascii=False)[:300]}")
        u = data.get("usage") or {}
        self.usage["input_tokens"] += u.get("prompt_tokens", 0) or 0
        self.usage["output_tokens"] += u.get("completion_tokens", 0) or 0
        self.usage["cache_read_input_tokens"] += ((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)) or 0
        self.usage["calls"] += 1
        return data["choices"][0].get("message") or {}

    def _system_text(self) -> str:
        return system_prompt() + "\n\n" + _semantic_summary() + "\n\n" + _schema_overview()

    # ------------------------------------------------------------------ lập kế hoạch (function calling)
    def plan(self, question: str, context: dict, tool_impl: dict, max_turns: int = 8, trace=None) -> dict:
        tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                   "parameters": t["input_schema"]}} for t in TOOLS]
        messages = [{"role": "system", "content": self._system_text()},
                    {"role": "user", "content": user_message(question, context)}]
        last_fail, repeats = None, 0
        seen, dupes = set(), 0
        for _turn in range(max_turns):
            # lượt cuối, hoặc khi mô hình cứ gọi lại tool đã có kết quả: buộc nộp bài thay vì bỏ phí cả chuỗi lượt gọi
            force = "submit_answer" if (_turn == max_turns - 1 or dupes >= 2) else None
            msg = self._create(messages, tools=tools, force_tool=force)
            calls = msg.get("tool_calls") or []
            if not calls:
                text = msg.get("content") or ""
                try:
                    out = _json_obj(text)
                    if out.get("kind"):
                        return normalize_answer(out)
                except LLMError:
                    pass
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": "Hãy nộp kết quả bằng tool submit_answer."})
                continue
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            final = None
            for c in calls:
                fn = c.get("function") or {}
                name = fn.get("name") or ""
                try:
                    inp = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    inp = None
                if not isinstance(inp, dict):
                    content = "Lỗi: arguments không phải JSON object hợp lệ. Hãy gọi lại tool với JSON đúng."
                elif name == "submit_answer":
                    final, content = normalize_answer(inp), "OK"
                elif name not in tool_impl:
                    content = f"Lỗi: không có tool tên '{name}'."
                else:
                    key = (name, json.dumps(inp, sort_keys=True, default=str))
                    if key in seen:
                        content, dupes = REPEATED_CALL, dupes + 1
                    else:
                        content, is_err = run_tool(tool_impl, name, inp, trace)
                        if not is_err:
                            seen.add(key)
                        repeats = (repeats + 1 if key == last_fail else 1) if is_err else 0
                        last_fail = key if is_err else None
                messages.append({"role": "tool", "tool_call_id": c.get("id"), "content": content})
            if final is not None:
                return final
            if repeats >= MAX_SAME_FAILURE:
                raise LLMError("Mô hình lặp lại cùng một lời gọi tool bị lỗi.")
        raise LLMError("Agent không hoàn thành trong số vòng cho phép.")

    # ------------------------------------------------------------------ sửa SQL
    def repair_sql(self, sql: str, errors: list, schema_text: str, question: str) -> dict:
        prompt = (f"Câu hỏi: {question}\nSQL (DuckDB) bị lỗi:\n{sql}\n\nLỗi kiểm duyệt/thực thi:\n"
                  f"{json.dumps(errors, ensure_ascii=False, default=str)}\n\nSchema liên quan:\n{schema_text}\n\n"
                  "Hãy sửa SQL để đúng schema, chỉ SELECT, giữ nguyên ý nghĩa câu hỏi. Trả về đúng một JSON object "
                  'dạng {"sql": "...", "explanation": "..."}; explanation bằng tiếng Việt, 1 câu.')
        msg = self._create([{"role": "system", "content": self._system_text()}, {"role": "user", "content": prompt}], json_mode=True)
        out = _json_obj(msg.get("content"))
        if not isinstance(out.get("sql"), str) or not out["sql"].strip():
            raise LLMError("Mô hình không trả về SQL đã sửa.")
        out.setdefault("explanation", "LLM đề xuất bản sửa.")
        return out

    # ------------------------------------------------------------------ viết lời (placeholder)
    def narrate(self, question: str, facts: dict, template: str) -> str | None:
        listing = "\n".join(f"{{{k}}} = {v['text']}" for k, v in facts.items())
        prompt = (f"Câu hỏi của người dùng: {question}\nCác dữ kiện (đã kiểm chứng) và placeholder:\n{listing}\n\n"
                  f"Bản nháp tự động: {template}\n\nViết lại thành 2–4 câu tiếng Việt tự nhiên, súc tích cho người làm nghiệp vụ. "
                  "QUY TẮC BẮT BUỘC: mọi con số, ngày tháng, nhãn kỳ chỉ được xuất hiện thông qua placeholder dạng {ten_fact}; "
                  "không viết bất kỳ chữ số nào trực tiếp; không thêm số liệu không có trong danh sách; giữ đúng chiều tăng/giảm. "
                  'Trả về đúng một JSON object dạng {"text": "..."}.')
        msg = self._create([{"role": "user", "content": prompt}], json_mode=True)
        text = _json_obj(msg.get("content")).get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        stripped = re.sub(r"\{[a-z0-9_]+\}", "", text)
        if re.search(r"\d", stripped):
            return None          # LLM tự chế số -> bỏ, dùng bản template
        unknown = [p for p in re.findall(r"\{([a-z0-9_]+)\}", text) if p not in facts]
        if unknown:
            return None
        return text

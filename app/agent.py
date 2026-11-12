"""
Agent Text-to-SQL đa tác tử, điều phối bằng đồ thị trạng thái (tương đương LangGraph StateGraph):

  load_context ─► planner ─┬─► clarify ─────────────────────────────────────────────┐
                           ├─► responder (định nghĩa chỉ số / schema / DQ / trợ giúp) ┤
                           ├─► variance_analyst (vì sao tăng/giảm) ──────────────────┤
                           └─► sql_generator ─► optimizer ─► validator ◄──┐            │
                                                              │    │      │            │
                                                              │    └─► repairer        │
                                                              ▼           ▲            │
                                                         cost_guard ──(HITL: chờ duyệt)┤
                                                              ▼           │            │
                                                          executor ───────┘(lỗi)       │
                                                              ▼                        │
                                                    analyst ─► explainer ─► finalize ◄─┘
Mỗi node ghi trace (thời gian, chi tiết) để hiển thị trên giao diện.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import re
import time
import uuid

from . import config, cost, dq, explain, llm, profile, repair, sqlcheck
from .catalog import get_catalog
from .governance import UserCtx, validate
from .nlu import get_nlu, is_why_question
from .retriever import get_retriever
from .semantic import SemanticError, get_semantic
from .store import get_store
from .textutil import fmt_money, human_bytes, norm
from .warehouse import QueryError, get_warehouse

END = "__end__"
INFO_INTENTS = {"metric_info", "schema_info", "dq", "help", "unknown"}
NON_REPAIRABLE = {"FORBIDDEN_STATEMENT", "FORBIDDEN_FUNCTION", "FORBIDDEN_SCHEMA", "FORBIDDEN_COLUMN", "MULTI_STATEMENT", "EMPTY"}


class Graph:
    """Đồ thị trạng thái tối giản: node là hàm (state) -> tên node kế tiếp."""

    def __init__(self):
        self.nodes = {}

    def add(self, name, fn):
        self.nodes[name] = fn

    def run(self, state: dict, start: str, max_steps: int = 40) -> dict:
        name = start
        steps = 0
        while name != END and steps < max_steps:
            steps += 1
            t0 = time.time()
            state["_detail"] = ""
            try:
                nxt = self.nodes[name](state)
            except Exception as e:  # lỗi không lường trước -> kết thúc an toàn
                state["status"] = "error"
                state["error"] = f"Lỗi nội bộ ở bước {name}: {type(e).__name__}: {e}"
                nxt = "finalize" if name != "finalize" else END
            state["trace"].append({"node": name, "ms": int((time.time() - t0) * 1000), "detail": state.pop("_detail", "")})
            name = nxt
        return state


SAMPLE_QUESTIONS = profile.sample_questions()      # câu hỏi mẫu của doanh nghiệp đang nạp (config/profile.yml)


class Agent:
    def __init__(self):
        self.store = get_store()
        self.graph = Graph()
        for n in ("load_context", "planner", "clarify", "responder", "variance_analyst", "sql_generator", "optimizer",
                  "validator", "repairer", "cost_guard", "executor", "analyst", "explainer", "finalize"):
            self.graph.add(n, getattr(self, "n_" + n))
        self._seed_fewshots()

    def _seed_fewshots(self):
        import yaml
        if any(f["source"] == "seed" for f in self.store.list_fewshots()):
            return
        path = config.CONFIG / "fewshots.yml"
        if path.exists():
            for item in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
                self.store.add_fewshot(item["question"], item["sql"].strip(), source="seed", verified=True)
        get_retriever().reload_fewshots()

    # ================================================================== API công khai
    def new_state(self, user: UserCtx, conversation_id: str, question: str, **kw) -> dict:
        s = {"run_id": uuid.uuid4().hex[:12], "user": user, "conversation_id": conversation_id, "question": question,
             "t0": time.time(), "trace": [], "assumptions": [], "notes": [], "repairs": [], "attempts": 0,
             "status": None, "plan": None, "sql": None, "sql_source": None, "columns_meta": None, "intent": None,
             "approved": False, "approved_by": None, "chart_hint": None, "llm_trace": [], "executed": [],
             "clarification_answer": kw.get("clarification"), "eval_mode": kw.get("eval_mode", False),
             "force_mode": kw.get("mode")}
        return s

    def ask(self, user: UserCtx, conversation_id: str, question: str, clarification: dict | None = None,
            eval_mode: bool = False, mode: str | None = None) -> dict:
        s = self.new_state(user, conversation_id, question, clarification=clarification, eval_mode=eval_mode, mode=mode)
        self.graph.run(s, "load_context")
        return s["response"]

    def run_sql(self, user: UserCtx, conversation_id: str, sql: str, question: str | None = None) -> dict:
        s = self.new_state(user, conversation_id, question or "[SQL Console]")
        s["sql"], s["sql_source"], s["intent"] = sql, "user", "sql"
        s["console"] = True
        self.n_load_context(s, skip_clarify=True)
        s["trace"].append({"node": "load_context", "ms": 0, "detail": "SQL do người dùng nhập"})
        self.graph.run(s, "validator")
        return s["response"]

    def approve(self, user: UserCtx, run_id: str, action: str, edited_sql: str | None = None) -> dict:
        p = self.store.pop_pending(run_id)
        if not p:
            return {"type": "error", "text": "Không tìm thấy truy vấn đang chờ duyệt (có thể đã được xử lý)."}
        st = p["state"]
        if p["username"] != user.username and user.role != "admin":
            return {"type": "error", "text": "Chỉ người tạo truy vấn (hoặc Admin) được duyệt."}
        s = self.new_state(user, p["conversation_id"], st["question"])
        for k in ("plan", "sql", "sql_source", "columns_meta", "intent", "assumptions", "notes", "chart_hint", "repairs",
                  "attempts", "mode", "console"):
            if k in st:
                s[k] = st[k]
        s["run_id"] = run_id
        s["trace"] = st.get("trace", [])
        s["_from_approval_msg"] = True
        self.n_load_context(s, skip_clarify=True)
        if action == "reject":
            s["status"] = "rejected"
            s["trace"].append({"node": "hitl", "ms": 0, "detail": f"{user.username} từ chối chạy SQL"})
            self.graph.run(s, "finalize")
            return s["response"]
        s["approved"], s["approved_by"] = True, user.username
        detail = f"{user.username} đã duyệt"
        if edited_sql and edited_sql.strip() != (s["sql"] or "").strip():
            s["sql"], s["sql_source"] = edited_sql, "user_edit"
            s["columns_meta"] = None
            detail += " (đã chỉnh sửa SQL — kiểm duyệt lại từ đầu)"
        s["trace"].append({"node": "hitl", "ms": 0, "detail": detail})
        self.graph.run(s, "validator")
        return s["response"]

    # ================================================================== NODES
    def n_load_context(self, s, skip_clarify=False):
        conv = self.store.get_conversation(s["conversation_id"]) or {"memory": {}}
        s["memory"] = conv.get("memory") or {}
        s["settings"] = self.store.get_settings()
        s.setdefault("mode", None)
        s["mode"] = s.get("force_mode") or s.get("mode") or llm.resolve_mode(s["settings"])
        s["anchor"] = dt.date.fromisoformat(get_catalog().anchor_date())
        if skip_clarify:
            return "planner"
        pc = s["memory"].get("pending_clarification")
        ans = s.get("clarification_answer")
        chosen = None
        if pc:
            if ans and ans.get("id") == pc["id"] and ans.get("option") is not None and 0 <= int(ans["option"]) < len(pc["options"]):
                chosen = pc["options"][int(ans["option"])]
            else:
                qn = norm(s["question"])
                for o in pc["options"]:
                    if norm(o["label"]).split(" —")[0] and (norm(o["label"]).startswith(qn) or qn in norm(o["label"])) and len(qn) >= 4:
                        chosen = o
                        break
            s["memory"].pop("pending_clarification", None)
            if chosen:
                prefs = s["memory"].setdefault("prefs", {})
                if chosen.get("metric") and pc.get("pref_key"):
                    prefs[pc["pref_key"]] = chosen["metric"]
                s["question_display"] = s["question"]
                s["question"] = chosen.get("rewrite") or pc["question"]
                s["_detail"] = f"Người dùng chọn: {chosen['label']}"
                s["clarified"] = chosen
        mem = s["memory"]
        s["_detail"] = s.get("_detail") or (f"Bộ nhớ: {len(mem.get('history', []))} lượt trước, ưu tiên {mem.get('prefs', {})}"
                                            if mem else "Hội thoại mới")
        return "planner"

    def n_planner(self, s):
        q = s["question"]
        ret = get_retriever()
        # 0) "bạn là gì / hỗ trợ gì": trả lời từ hồ sơ doanh nghiệp, không gọi LLM nên không bịa tính năng
        if not s.get("clarified") and profile.is_intro_question(q):
            s["intent"] = "help"
            s["_detail"] = "Câu hỏi giới thiệu: trả lời từ hồ sơ doanh nghiệp và semantic layer, không gọi LLM"
            return "responder"
        # 1) tái sử dụng SQL đã được xác nhận (học từ phản hồi)
        if not s.get("clarified"):
            hit =ret.reusable(q, threshold=float(s["settings"].get("fewshot_reuse_threshold", 0.8)))
            if hit and hit.get("sql"):
                s["sql"], s["sql_source"], s["intent"] = hit["sql"], "fewshot", "metric_query"
                s["fewshot_hit"] = {"id": hit["id"], "question": hit["question"], "similarity": hit["similarity"], "source": hit["source"]}
                self.store.touch_fewshot(hit["id"])
                s["notes"].append(f"Tái sử dụng SQL đã được xác nhận cho câu hỏi tương tự: “{hit['question']}” "
                                  f"(độ tương đồng {hit['similarity']:.2f}).")
                s["_detail"] = f"Few-shot hit #{hit['id']} ({hit['source']})"
                return "optimizer"
        # 2) LLM function calling (Claude hoặc OpenRouter)
        if s["mode"] in llm.LLM_MODES:
            # chốt chặn bằng code: thuật ngữ mơ hồ do doanh nghiệp khai báo thì luôn hỏi lại, không phụ thuộc mô hình
            if not s.get("clarified"):
                amb = get_nlu().detect_ambiguity(q, s["memory"])
                if amb:
                    s["clarification"] = dict(amb, question_text=amb["question"])
                    s["_detail"] = f"Thuật ngữ mơ hồ theo semantic layer ({amb['id']}): hỏi lại trước khi gọi LLM"
                    return "clarify"
            name = llm.MODE_LABEL[s["mode"]]
            try:
                return self._plan_with_claude(s)
            except (llm.LLMUnavailable, llm.LLMError) as e:
                s["notes"].append(f"Không dùng được {name} ({e}). Chuyển sang bộ hiểu ngôn ngữ offline.")
                s["mode"], s["llm_fallback"] = "offline", f"{name}: {e}"
            except Exception as e:  # pragma: no cover
                s["notes"].append(f"Lỗi khi gọi {name} ({type(e).__name__}). Chuyển sang chế độ offline.")
                s["mode"], s["llm_fallback"] = "offline", f"{name}: {type(e).__name__}"
        # 3) NLU offline
        r = get_nlu().parse(q, s["memory"])
        s["intent"], s["plan"], s["chart_hint"] = r.intent, r.plan, r.chart_hint
        s["assumptions"] += r.assumptions
        s["follow_up"] = r.follow_up
        s["metric_topic"] = r.metric_topic
        s["_detail"] = (f"NLU offline: ý định={r.intent}, chỉ số={r.plan.get('metrics')}, chiều={r.plan.get('dimensions')}, "
                        f"lọc={[(f['dimension'], f['values']) for f in r.plan.get('filters', [])]}, "
                        f"thời gian={(r.plan.get('time_range') or {}).get('label')}" + (" [câu hỏi nối tiếp]" if r.follow_up else ""))
        if r.clarification:
            s["clarification"] = dict(r.clarification, question_text=r.clarification["question"])
            return "clarify"
        if r.intent == "variance":
            return "variance_analyst"
        if r.intent in INFO_INTENTS:
            return "responder"
        return "sql_generator"

    def _plan_with_claude(self, s):
        user = s["user"]
        ret = get_retriever()
        shots = ret.search_fewshots(s["question"], k=int(s["settings"].get("fewshot_k", 4)))
        fs_txt = "\n".join(f"- Q: {f['question']}\n  SQL: {re.sub(r'\s+', ' ', f['sql'])}" for f in shots) or "(chưa có)"
        mem = {k: s["memory"].get(k) for k in ("prefs", "last_plan", "last_question", "last_sql") if s["memory"].get(k)}
        hints = []
        why = is_why_question(s["question"])
        if s.get("clarified"):
            c = s["clarified"]
            hints.append(f"Người dùng vừa làm rõ câu hỏi bằng lựa chọn: “{c['label']}”"
                         + (f" (chỉ số {c['metric']})" if c.get("metric") else "") + ". Dùng đúng lựa chọn này, không hỏi lại.")
        if why:
            hints.append("Đây là câu hỏi tìm nguyên nhân biến động: nộp intent=variance, kind=semantic với semantic_plan gồm "
                         "metrics (một chỉ số), filters, time_range (kỳ đang hỏi) và compare (kỳ so sánh; nếu người dùng "
                         "không nêu thì dùng kỳ liền trước có cùng độ dài).")
        draft = None if why else get_nlu().draft_for_llm(s["question"], s["memory"])
        if draft:       # bộ luật đề xuất, LLM kiểm tra: câu hỏi thông thường đi đúng định nghĩa chỉ số, đỡ lượt gọi
            plan_txt = json.dumps(draft["plan"], ensure_ascii=False)
            if draft["unparsed"]:
                hints.append(
                    f"Bộ phân tích bằng luật chỉ hiểu MỘT PHẦN câu hỏi. Phần nó hiểu được: {plan_txt}. "
                    f"Những từ nó chưa hiểu: {', '.join(draft['unparsed'])}. Trước khi dùng plan này hãy xét các từ chưa hiểu: "
                    "nếu chúng nói tới một đối tượng người dùng muốn xem từng dòng (như “nhân viên” trong “top 5 nhân viên”), "
                    "một điều kiện lọc, hay một phép tính (trung bình theo nhóm, tỷ trọng, theo thứ trong tuần) mà danh sách "
                    "dimension không có, thì plan này KHÔNG trả lời đúng câu hỏi: hãy viết SQL, chỉ tái dùng công thức và bộ "
                    "lọc của chỉ số. Nếu chúng chỉ là từ đệm thì biên dịch rồi nộp plan (kind=semantic).")
            else:
                hints.append(
                    f"Bộ phân tích bằng luật đã dùng hết các từ có nghĩa trong câu hỏi và đề xuất plan: {plan_txt}. "
                    "Biên dịch rồi nộp đúng plan đó (kind=semantic), trừ khi bạn thấy nó sai ý câu hỏi.")
        ctx = {"anchor": s["anchor"].isoformat(), "role": user.role, "rls": user.rls, "memory": mem, "fewshots": fs_txt,
               "hints": hints}
        client = llm.get_client(s["settings"], s["mode"])
        out = client.plan(s["question"], ctx, self._tool_impls(user, draft), trace=s["llm_trace"])
        s["llm_usage"] = client.usage
        s["claude"] = client
        s["assumptions"] += out.get("assumptions") or []
        s["chart_hint"] = out.get("chart_hint") if out.get("chart_hint") not in (None, "auto", "table") else None
        s["intent"] = out.get("intent") or "metric_query"
        kind = out.get("kind")
        s["_detail"] = f"{llm.MODE_LABEL[s['mode']]} ({client.model}, effort={client.effort}): kind={kind}, intent={s['intent']}, " \
                       f"{len(s['llm_trace'])} lần gọi tool, {client.usage['calls']} lượt gọi API"
        # Phân tích nguyên nhân chỉ dành cho câu hỏi "vì sao" (nhận diện bằng code), không theo nhãn intent của mô hình:
        # - câu "vì sao" mà mô hình không lập được plan phân tích (viết truy vấn so sánh, hoặc hỏi lại dù các thuật ngữ mơ hồ
        #   đã được code kiểm ở bước trước) -> dùng plan của bộ luật nếu bộ luật hiểu trọn câu hỏi;
        # - câu so sánh thường ("so với cùng kỳ") mà mô hình gắn nhãn variance -> trả về bảng so sánh như người dùng hỏi.
        if why and not (s["intent"] == "variance" and kind != "clarify" and (out.get("semantic_plan") or {}).get("metrics")):
            r = get_nlu().parse(s["question"], s["memory"])
            if r.intent == "variance" and r.plan.get("metrics") and not r.clarification:
                s["intent"], s["plan"] = "variance", r.plan
                s["assumptions"] += r.assumptions
                s["notes"].append("Mô hình không lập kế hoạch phân tích nguyên nhân; dùng kế hoạch của bộ phân tích bằng luật.")
                return "variance_analyst"
        if not why and s["intent"] == "variance":
            s["intent"] = "metric_query"
        if kind == "clarify" and out.get("clarification"):
            c = out["clarification"]
            s["clarification"] = {"id": "llm_" + uuid.uuid4().hex[:6], "question": s["question"], "question_text": c.get("question"),
                                  "pref_key": get_nlu().pref_key_for([o.get("metric") for o in c.get("options", [])]),
                                  "options": c.get("options", [])}
            return "clarify"
        if kind == "answer" or s["intent"] in ("help", "schema_info", "dq") and not out.get("sql") and not out.get("semantic_plan"):
            # lời giới thiệu luôn do code dựng từ hồ sơ doanh nghiệp, không dùng văn bản của LLM
            s["info_text"] = None if s["intent"] == "help" else out.get("answer_text")
            return "responder"
        if s["intent"] == "variance" and out.get("semantic_plan"):
            p = out["semantic_plan"]
            p["variance_compare"] = p.pop("compare", None)
            if not p.get("variance_compare") and p.get("time_range"):
                from .nlu import previous_period
                st, en = dt.date.fromisoformat(p["time_range"]["start"]), dt.date.fromisoformat(p["time_range"]["end"])
                ps, pe, lab = previous_period(st, en)
                p["variance_compare"] = {"start": ps.isoformat(), "end": pe.isoformat(), "label": lab}
            s["plan"] = p
            return "variance_analyst"
        if kind == "sql" and out.get("sql"):
            s["sql"], s["sql_source"] = out["sql"], "llm"
            return "optimizer"
        if out.get("semantic_plan"):
            s["plan"] = out["semantic_plan"]
            return "sql_generator"
        s["info_text"] = out.get("answer_text") or "Mình chưa hiểu câu hỏi, bạn có thể diễn đạt lại?"
        s["intent"] = "unknown"
        return "responder"

    def _tool_impls(self, user: UserCtx, draft: dict | None = None) -> dict:
        """Các tool cho LLM. draft = bản nháp plan của bộ luật (nếu có), dùng để cảnh báo khi plan bỏ sót bộ lọc."""
        cat, ret, sem = get_catalog(), get_retriever(), get_semantic()
        role = user.role_cfg

        def search_schema(query: str):
            r = ret.search_schema(query)
            tables = [t for t in r["tables"] if cat.get(t) and cat.get(t)["schema"] in role["schemas"]]
            return {"schema": cat.schema_text(tables[:6], role),
                    "metrics": [sem.describe_metric(m) for m in r["metrics"][:5]]}

        def describe_table(table: str):
            fq = cat.resolve(table)
            if not fq or cat.get(fq)["schema"] not in role["schemas"]:
                raise ValueError(f"Bảng {table} không tồn tại hoặc không được phép.")
            return cat.describe(fq, role, max_values=25)

        def get_column_values(table: str = "", column: str = "", contains: str | None = None):
            fq = cat.resolve(table or "")
            if column in sem.dims and not (fq and column in cat.get(fq)["columns"]):
                loc = sem.dim_column(column)        # mô hình hay truyền tên chiều phân tích thay cho tên cột
                if not loc:
                    raise ValueError(f"'{column}' là chiều thời gian hoặc biểu thức, không có danh mục giá trị; "
                                     "khoảng thời gian đặt ở time_range.")
                fq, column = loc
            if not fq or cat.get(fq)["schema"] not in role["schemas"]:
                raise ValueError(f"Bảng '{table}' không tồn tại hoặc không được phép. Tìm bảng bằng search_schema.")
            c = cat.get(fq)["columns"].get(column)
            if not c:
                raise ValueError(f"Cột '{column}' không có trong {fq}. Các cột của bảng: {', '.join(cat.get(fq)['columns'])}. "
                                 "Cũng có thể truyền tên một chiều phân tích của semantic layer vào column.")
            if c.get("sensitivity") in role.get("deny_sensitivity", []):
                raise ValueError("Cột bị hạn chế với vai trò hiện tại.")
            vals = c.get("values")
            if vals:
                if contains:
                    vals = [v for v in vals if norm(contains) in norm(str(v))]
                return {"values": vals[:60]}
            sql = f"SELECT DISTINCT {column} FROM {fq}" + (f" WHERE {column} ILIKE '%{contains.replace(chr(39), '')}%'" if contains else "") + " LIMIT 40"
            v = validate(sql, user)
            if not v.ok:
                raise ValueError(v.errors[0]["message"])
            return {"values": [r[0] for r in get_warehouse().execute(v.sql_exec)["rows"]]}

        def dry_run_sql(sql: str):
            sql2, notes = repair.optimize_partition_filters(sql)
            v = validate(sql2, user)
            out = {"ok": v.ok, "errors": v.errors, "optimizations": notes}
            if v.ok:
                est = cost.estimate(v.qualified, v.rls_applied)
                out["estimated_bytes"] = est["human"]
                out["decision"] = cost.decide(est, role)["decision"]
                out["lint"] = sqlcheck.lint(v, dq.known_issues())
                out["rls_applied"] = v.rls_applied
                must_fix = [l["code"] for l in out["lint"] if l["severity"] == "error"]
                out["next"] = (f"Sửa các mục lint mức error ({', '.join(must_fix)}) rồi dry_run lại." if must_fix else
                               "SQL hợp lệ. Lint mức warning/info chỉ để tham khảo, không cần sửa hay chạy lại. "
                               "Nộp ngay bằng submit_answer (kind=sql).")
            else:
                out["next"] = "Sửa lỗi rồi gọi lại dry_run_sql."
            return out

        def compile_semantic_plan(plan: dict | None = None, **fields):
            # mô hình nhỏ hay gửi thẳng {"metrics": [...], ...} thay vì bọc trong {"plan": {...}} -> nhận cả hai dạng
            plan = plan if isinstance(plan, dict) else fields
            c = sem.compile(plan)
            v = validate(c["sql"], user)
            keep = {k: x for k, x in c["plan"].items() if x not in (None, [], {})}
            out = {"ok": v.ok, "errors": v.errors, "notes": c["notes"], "plan": keep,
                   "next": ("Plan hợp lệ. Nộp ngay bằng submit_answer với kind=semantic và semantic_plan là đúng plan này; "
                            "không cần dry_run_sql.") if v.ok else "Sửa plan theo lỗi rồi biên dịch lại."}
            # câu hỏi có nhắc đúng tên một giá trị trong kho mà plan không lọc theo nó -> nhắc mô hình xem lại
            used = {f["dimension"] for f in c["plan"].get("filters") or []} | set(c["plan"].get("dimensions") or [])
            dropped = [f for f in ((draft or {}).get("plan") or {}).get("filters") or [] if f["dimension"] not in used]
            warnings = [f"Câu hỏi có nhắc “{', '.join(map(str, f['values']))}” ({sem.dims[f['dimension']]['label']}) "
                        "nhưng plan không lọc theo giá trị này. Nếu người dùng muốn giới hạn theo giá trị đó thì thêm filter."
                        for f in dropped]
            # xếp hạng (limit/order_by) mà không nhóm theo chiều nào thì kết quả chỉ có một dòng tổng: vô nghĩa
            if (c["plan"].get("limit") or c["plan"].get("order_by")) and not c["plan"].get("dimensions"):
                warnings.append("Plan có limit/order_by nhưng không có dimension nào nên chỉ trả về MỘT dòng tổng, không phải bảng "
                                "xếp hạng. Nếu đối tượng cần xếp hạng không nằm trong danh sách dimension của semantic layer thì "
                                "phải viết SQL: GROUP BY mã của đối tượng đó, tái dùng đúng công thức và bộ lọc của chỉ số.")
            if v.ok and warnings:
                out["warnings"] = warnings
                out["next"] = "Plan biên dịch được nhưng CHƯA nên nộp: xử lý các cảnh báo trước (sửa plan hoặc chuyển sang SQL)."
            return out

        return {"search_schema": search_schema, "describe_table": describe_table, "get_column_values": get_column_values,
                "dry_run_sql": dry_run_sql, "compile_semantic_plan": compile_semantic_plan}

    def n_clarify(self, s):
        c = s["clarification"]
        s["memory"]["pending_clarification"] = {"id": c["id"], "question": s["question"], "pref_key": c.get("pref_key"),
                                                "options": c["options"]}
        s["status"] = "clarification"
        s["_detail"] = f"Hỏi lại: {c.get('question_text')}"
        return "finalize"

    def n_responder(self, s):
        sem, cat = get_semantic(), get_catalog()
        intent = s["intent"]
        s["status"] = "info"
        if s.get("info_text"):
            s["text"] = s["info_text"]
            return "finalize"
        if intent == "metric_info":
            topic = s.get("metric_topic")
            names = [topic] if topic in sem.metrics else get_nlu().ambiguity_metrics(topic)
            cards = [sem.describe_metric(n) for n in names if n in sem.metrics]
            s["metric_cards"] = cards
            parts = []
            for c in cards:
                parts.append(f"**{c['label']}** (`{c['name']}`, chủ sở hữu: {c['owner']}): {c['description']} "
                             f"Công thức: `{c['sql']}`" + (f" với điều kiện `{c['filter']}`." if c.get("filter") else "."))
            s["text"] = "\n\n".join(parts) or "Chưa có định nghĩa cho chỉ số này trong semantic layer."
        elif intent == "schema_info":
            r = get_retriever().search_schema(s["question"])
            tabs = [t for t in r["tables"] if cat.get(t)["schema"] in s["user"].role_cfg["schemas"]][:5]
            s["schema_tables"] = [cat.describe(t, s["user"].role_cfg) for t in tabs]
            s["text"] = "Các bảng liên quan nhất: " + "; ".join(f"`{t}` — {cat.get(t)['description']}" for t in tabs)
        elif intent == "dq":
            rep = dq.latest_report()
            open_f = [f for f in rep["findings"] if f["status"] == "open" and f["severity"] in ("critical", "warning")]
            s["text"] = (f"Báo cáo chất lượng dữ liệu ({rep['generated_at']}): {rep['summary']['critical']} vấn đề nghiêm trọng, "
                         f"{rep['summary']['warning']} cảnh báo.\n\n" + "\n".join(f"- **{f['title']}**: {f['message']}" for f in open_f[:6]))
        elif intent == "help":
            s["text"] = profile.intro_text()
            s["suggestions"] = profile.sample_questions()[:6]
        else:
            labels = [m["label"].lower() for m in sem.metrics.values()]
            s["text"] = ("Mình chưa hiểu câu này muốn xem số liệu nào. Mình trả lời được các câu hỏi về: " +
                         ", ".join(labels[:12]) + ("…" if len(labels) > 12 else "") +
                         ". Bạn thử nêu rõ chỉ số và thời gian, hoặc bắt đầu từ một câu mẫu bên dưới.")
            if s["mode"] == "offline":
                s["text"] += ("\n\nHiện trợ lý đang trả lời bằng bộ luật có sẵn nên chỉ hiểu các câu về chỉ số đã định nghĩa. "
                              "Quản trị viên có thể bật mô hình ngôn ngữ trong trang Cài đặt để hỏi tự do hơn.")
            s["suggestions"] = SAMPLE_QUESTIONS[:5]
        s["_detail"] = f"Trả lời thông tin ({intent})"
        return "finalize"

    def n_sql_generator(self, s):
        try:
            c = get_semantic().compile(s["plan"])
        except SemanticError as e:
            s["status"], s["error"] = "error", str(e)
            return "finalize"
        s["sql"], s["sql_source"], s["columns_meta"] = c["sql"], "semantic", c["columns"]
        s["notes"] += c["notes"]
        s["_detail"] = f"Biên dịch semantic plan → SQL (models: {', '.join(c['models'])})"
        return "optimizer"

    def n_optimizer(self, s):
        if s["sql_source"] in ("llm", "fewshot"):
            sql2, notes = repair.optimize_partition_filters(s["sql"])
            if notes:
                s["sql"] = sql2
                s["notes"] += notes
                s["_detail"] = "; ".join(notes)
                return "validator"
        s["_detail"] = "Không cần tối ưu"
        return "validator"

    def n_validator(self, s):
        user = s["user"]
        v = validate(s["sql"], user)
        s["validation"] = v.to_dict()
        s["_v"] = v
        if not v.ok:
            e = v.errors[0]
            s["_detail"] = f"✗ {e['code']}: {e['message'][:160]}"
            if e["code"] in NON_REPAIRABLE:
                s["status"], s["error"] = "blocked", e["message"]
                return "finalize"
            s["last_error"] = e
            return "repairer"
        tr = (s.get("plan") or {}).get("time_range") or {}
        s["lint"] = sqlcheck.lint(v, dq.known_issues(), (tr.get("start"), tr.get("end")) if tr else None)
        s["metrics_matched"] = sqlcheck.matched_metrics(v)
        try:
            s["lineage"] = sqlcheck.query_lineage(v)
        except Exception:
            s["lineage"] = []
        errs = [l for l in s["lint"] if l["severity"] == "error"]
        s["_detail"] = f"✓ {len(v.checks)} kiểm tra đạt; lint: {len(s['lint'])} ({', '.join(l['code'] for l in s['lint']) or 'sạch'})" + \
                       (f"; RLS: {', '.join(v.rls_applied)}" if v.rls_applied else "")
        if errs and s["sql_source"] != "semantic":
            s["last_error"] = {"code": errs[0]["code"], "message": errs[0]["message"], "hint": errs[0]["hint"]}
            return "repairer"
        return "cost_guard"

    def n_repairer(self, s):
        e = s.get("last_error") or {}
        maxa = int(s["settings"].get("max_repair_attempts", 3))
        if s["attempts"] >= maxa:
            s["status"], s["error"] = "error", f"Không tự sửa được sau {s['attempts']} lần thử. Lỗi cuối: {e.get('message')}"
            return "finalize"
        s["attempts"] += 1
        before = s["sql"]
        tables = (s.get("validation") or {}).get("tables") or []
        new, how = repair.rule_repair(before, e, tables or profile.core_tables())
        agent = "rule"
        if new is None and s["mode"] in llm.LLM_MODES:
            try:
                client = s.get("claude") or llm.get_client(s["settings"], s["mode"])
                cat = get_catalog()
                schema_txt = cat.schema_text(tables or profile.core_tables(), s["user"].role_cfg)
                out = client.repair_sql(before, [e], schema_txt, s["question"])
                new, how, agent = out["sql"], out["explanation"], s["mode"]
            except Exception as ex:
                how = f"{llm.MODE_LABEL[s['mode']]} không sửa được: {ex}"
        s["repairs"].append({"attempt": s["attempts"], "error": e, "agent": agent, "fix": how, "sql_before": before, "sql_after": new})
        s["_detail"] = f"Lần {s['attempts']}: {e.get('code')} → {how}"
        if new is None or new.strip() == before.strip():
            s["status"], s["error"] = "error", f"{e.get('message')} — {how}" + (f" Gợi ý: {e['hint']}" if e.get("hint") else "")
            return "finalize"
        s["sql"] = new
        s["columns_meta"] = None if s["sql_source"] != "semantic" else s["columns_meta"]
        if s["sql_source"] == "semantic":
            s["sql_source"] = "semantic+repair"
        return "validator"

    def n_cost_guard(self, s):
        v = s["_v"]
        est = cost.estimate(v.qualified, v.rls_applied)
        dec = cost.decide(est, s["user"].role_cfg, bool(s["settings"].get("always_review")) and not s["eval_mode"])
        s["cost"] = dict(est, **dec)
        s["_detail"] = f"Ước tính quét {est['human']} → {dec['decision']}" + (f" ({'; '.join(dec['reasons'])})" if dec["reasons"] else "")
        if dec["decision"] == "blocked":
            s["status"], s["error"] = "blocked", " ".join(dec["reasons"])
            return "finalize"
        if dec["decision"] == "approval" and not s["approved"] and not s["eval_mode"]:
            s["status"] = "pending_approval"
            keep = {k: s.get(k) for k in ("question", "plan", "sql", "sql_source", "columns_meta", "intent", "assumptions",
                                          "notes", "chart_hint", "repairs", "attempts", "mode", "trace", "console")}
            self.store.save_pending(s["run_id"], s["conversation_id"], s["user"].username, copy.deepcopy(keep))
            return "finalize"
        return "executor"

    def n_executor(self, s):
        v = s["_v"]
        st = s["settings"]
        try:
            r = get_warehouse().execute(v.sql_exec, timeout_s=float(st.get("query_timeout_s", 20)),
                                        max_rows=int(s["user"].role_cfg.get("max_rows", 5000)))
        except QueryError as e:
            s["_detail"] = f"✗ {e.kind}: {str(e)[:160]}"
            if e.kind == "timeout":
                s["status"], s["error"] = "error", str(e)
                return "finalize"
            s["last_error"] = {"code": "BINDER", "message": str(e).split("\n")[0], "raw": str(e)}
            return "repairer"
        s["result"] = r
        s["executed"].append({"sql": v.sql_exec, "bytes": s["cost"]["total_bytes"], "rows": r["row_count"], "ms": r["elapsed_ms"]})
        s["_detail"] = f"✓ {r['row_count']} dòng trong {r['elapsed_ms']} ms" + (" (đã cắt bớt)" if r["truncated"] else "")
        return "analyst"

    def n_analyst(self, s):
        r = s["result"]
        meta = s.get("columns_meta")
        if not meta or [m["name"] for m in meta] != r["columns"]:
            meta = infer_meta(r["columns"], r["types"], r["rows"])
            s["columns_meta"] = meta
        e = explain.explain(meta, r["columns"], r["rows"], s.get("plan") or {}, r["truncated"])
        s["facts"], s["template"], s["text"], s["highlights"] = e["facts"], e["template"], e["text"], e["highlights"]
        s["chart"] = explain.recommend_chart(meta, r["columns"], r["rows"], s.get("chart_hint"), e["highlights"])
        s["_detail"] = f"{len(e['facts'])} dữ kiện; biểu đồ: {(s['chart'] or {}).get('type', 'không')}"
        return "explainer"

    def n_explainer(self, s):
        r = s["result"]
        if s["mode"] in llm.LLM_MODES and s["facts"] and not s["eval_mode"]:
            try:
                client = s.get("claude") or llm.get_client(s["settings"], s["mode"])
                txt = client.narrate(s["question"], s["facts"], s["template"])
                if txt:
                    s["text"] = _render(txt, s["facts"])
                    s["narrator"] = s["mode"]
                else:
                    s["notes"].append("Lời diễn giải của LLM chứa số không kiểm chứng được — dùng bản diễn giải tự động.")
            except Exception:
                pass
        s["audit"] = explain.audit(s["text"], s["facts"], r["columns"], r["rows"])
        if not s["audit"]["ok"]:
            s["text"] = _render(s["template"], s["facts"])
            s["audit"] = explain.audit(s["text"], s["facts"], r["columns"], r["rows"])
            s["notes"].append("Đã thay lời diễn giải bằng bản tự động vì phát hiện số liệu không khớp.")
        s["status"] = "success"
        a = s["audit"]
        s["_detail"] = f"Kiểm chứng số liệu: {a['facts_matched']}/{a['facts_checked']} dữ kiện khớp, {len(a['unverified_numbers'])} số lạ"
        return "finalize"

    def n_variance_analyst(self, s):
        plan = s["plan"]
        if not plan.get("metrics"):
            s["status"], s["error"] = "error", "Chưa xác định được chỉ số cần phân tích biến động."
            return "finalize"
        user = s["user"]
        role = user.role_cfg
        budget = {"bytes": 0}

        def run(sql, purpose):
            v = validate(sql, user)
            if not v.ok:
                raise ValueError(f"{purpose}: {v.errors[0]['message']}")
            est = cost.estimate(v.qualified, v.rls_applied)
            budget["bytes"] += est["total_bytes"]
            if budget["bytes"] > role["cost"]["max_bytes"]:
                raise ValueError("Phân tích vượt hạn mức chi phí quét của vai trò.")
            res = get_warehouse().execute(v.sql_exec, timeout_s=30, max_rows=20000)
            return {"columns": res["columns"], "rows": res["rows"], "sql": v.sql_exec, "bytes": est["total_bytes"], "ms": res["elapsed_ms"]}

        try:
            out = insights.analyze(plan, run, s["anchor"])
        except Exception as e:
            s["status"], s["error"] = "error", f"Không phân tích được biến động: {e}"
            return "finalize"
        s["variance"] = out
        s["text"], s["facts"], s["audit"], s["chart"] = out["text"], out["facts"], out["audit"], out["chart"]
        s["sql"] = out["queries"][0]["sql"] if out["queries"] else None
        s["executed"] = [{"sql": q["sql"], "bytes": q["bytes"], "rows": q["rows"], "ms": q["ms"]} for q in out["queries"]]
        s["cost"] = {"total_bytes": budget["bytes"], "human": human_bytes(budget["bytes"]), "decision": "auto", "reasons": [],
                     "tables": [], "large_tables": []}
        s["status"] = "success"
        s["_detail"] = f"{len(out['queries'])} truy vấn phân tích; chiều giải thích tốt nhất: {out['best_dimension']}; " \
                       f"{len(out['root_causes'])} kiểm tra nguyên nhân"
        return "finalize"

    def n_finalize(self, s):
        user = s["user"]
        resp = self._response(s)
        s["response"] = resp
        mem = s["memory"]
        if s["status"] == "success" and s.get("plan") and s["intent"] in ("metric_query", "variance"):
            mem["last_plan"] = s["plan"]
        if s["status"] in ("success", "pending_approval"):
            mem["last_sql"] = s.get("sql")
            mem["last_question"] = s["question"]
        hist = mem.setdefault("history", [])
        hist.append({"q": s.get("question_display") or s["question"], "intent": s.get("intent"), "status": s["status"],
                     "summary": (s.get("text") or s.get("error") or "")[:200]})
        mem["history"] = hist[-8:]
        self.store.update_conversation(s["conversation_id"], memory=mem)
        if s["eval_mode"]:
            return END
        conv = self.store.get_conversation(s["conversation_id"])
        if conv and conv["title"] == "Hội thoại mới" and not s.get("console"):
            self.store.update_conversation(s["conversation_id"], title=(s.get("question_display") or s["question"])[:60])
        if not s.get("_from_approval_msg"):
            self.store.add_message(s["conversation_id"], "user", {"text": s.get("question_display") or s["question"],
                                                                  "clarification": s.get("clarification_answer")})
        self.store.add_message(s["conversation_id"], "assistant", resp)
        if s["status"] not in ("clarification", "info") or s.get("sql"):
            self.store.log_query(run_id=s["run_id"], username=user.username, role=user.role, conversation_id=s["conversation_id"],
                                 question=s.get("question_display") or s["question"], sql=s.get("sql"), status=s["status"],
                                 error=s.get("error"), est_bytes=(s.get("cost") or {}).get("total_bytes"),
                                 rows=(s.get("result") or {}).get("row_count") or (len(s.get("executed", [])) if s.get("variance") else None),
                                 latency_ms=int((time.time() - s["t0"]) * 1000), retries=s["attempts"], llm_mode=s["mode"],
                                 source=s.get("sql_source") or ("variance" if s.get("variance") else s.get("intent")),
                                 plan=s.get("plan"), lint=s.get("lint"), approved_by=s.get("approved_by"))
        return END

    def _response(self, s) -> dict:
        r = s.get("result") or {}
        v = s.get("validation") or {}
        typ = {"success": "variance" if s.get("variance") else "result", "clarification": "clarification",
               "pending_approval": "pending_approval", "error": "error", "blocked": "blocked", "info": "info",
               "rejected": "rejected"}.get(s["status"], "error")
        resp = {
            "type": typ, "run_id": s["run_id"], "question": s.get("question_display") or s["question"],
            "text": s.get("text") or s.get("error"), "error": s.get("error"), "mode": s["mode"], "intent": s.get("intent"),
            "sql": s.get("sql"), "sql_exec": v.get("sql_exec"), "sql_source": s.get("sql_source"), "plan": s.get("plan"),
            "assumptions": s.get("assumptions", []), "notes": s.get("notes", []),
            "columns": r.get("columns"), "columns_meta": s.get("columns_meta"), "rows": r.get("rows"),
            "row_count": r.get("row_count"), "truncated": r.get("truncated"), "chart": s.get("chart"),
            "facts": s.get("facts"), "audit": s.get("audit"), "validation": v or None, "lint": s.get("lint"),
            "cost": _cost_public(s.get("cost")), "lineage": s.get("lineage"), "metrics_matched": s.get("metrics_matched"),
            "repairs": s.get("repairs"), "trace": s["trace"], "llm_trace": s.get("llm_trace"), "llm_usage": s.get("llm_usage"),
            "executed": s.get("executed"), "fewshot_hit": s.get("fewshot_hit"), "elapsed_ms": int((time.time() - s["t0"]) * 1000),
            "approved_by": s.get("approved_by"), "narrator": s.get("narrator", "template"),
        }
        if s["status"] == "clarification":
            c = s["clarification"]
            resp["clarification"] = {"id": c["id"], "question": c.get("question_text"), "options": [o["label"] for o in c["options"]]}
            resp["text"] = c.get("question_text")
        if s["status"] == "pending_approval":
            resp["text"] = ("Truy vấn cần bạn xem lại SQL và xác nhận trước khi chạy: " + " ".join(s["cost"].get("reasons", [])))
        if s.get("variance"):
            resp["variance"] = {k: s["variance"][k] for k in ("metric_label", "format", "period_now", "period_prev", "total",
                                                                  "significance", "drivers", "best_dimension", "drill",
                                                                  "decomposition", "root_causes", "queries")}
        for k in ("metric_cards", "schema_tables", "suggestions"):
            if s.get(k):
                resp[k] = s[k]
        # phần dành cho giao diện thân thiện: câu này có phải do bộ luật trả lời thay LLM không, chỉ số được tính theo
        # định nghĩa nào, và có thể hỏi tiếp điều gì (đều dựng bằng code từ kế hoạch vừa chạy)
        resp["fallback"] = s.get("llm_fallback")
        if s["status"] == "success":
            resp["definitions"] = _definitions(s)
            resp["follow_ups"] = _follow_ups(s)
        return resp


def _definitions(s) -> list[dict]:
    """Định nghĩa các chỉ số có trong câu trả lời (theo semantic layer), để người dùng biết con số được tính thế nào."""
    sem = get_semantic()
    names = list((s.get("plan") or {}).get("metrics") or []) or [m["metric"] for m in s.get("metrics_matched") or []]
    return [{"name": n, "label": sem.metrics[n]["label"], "description": sem.metrics[n].get("description") or "",
             "owner": sem.metrics[n].get("owner") or ""} for n in dict.fromkeys(names) if n in sem.metrics]


def _follow_ups(s, limit: int = 4) -> list[str]:
    """Gợi ý câu hỏi tiếp theo cho kết quả vừa trả. Chỉ gợi ý điều hệ thống chắc chắn làm được: chia theo một chiều mà chỉ số
    hỗ trợ, so với cùng kỳ, tìm nguyên nhân biến động. Câu gợi ý viết theo dạng bộ luật hiểu được khi nối tiếp hội thoại."""
    from .nlu import _period_label
    sem, plan = get_semantic(), s.get("plan") or {}
    metric = next((m for m in plan.get("metrics") or [] if m in sem.metrics), None)
    if not metric:
        return []
    mdef = sem.metrics[metric]
    label = mdef["label"].lower()
    cfg = insights.analysis_config(sem, metric)
    used = set(plan.get("dimensions") or []) | {f["dimension"] for f in plan.get("filters") or []}
    tr = plan.get("time_range") or {}
    period = None
    if tr.get("start") and tr.get("end"):
        p = _period_label(dt.date.fromisoformat(tr["start"]), dt.date.fromisoformat(tr["end"]))
        period = p if re.fullmatch(r"T\d{1,2}/\d{4}|Q[1-4]/\d{4}|năm \d{4}", p) else None
    out = []
    if s.get("variance"):
        best = s["variance"].get("best_dimension")
        if best in sem.dims and period:
            out.append(f"{mdef['label']} theo {sem.dims[best]['label'].lower()} {period}")
        out += [f"Chia theo {sem.dims[d]['label'].lower()}" for d in cfg["drivers"] if d not in used and d != best][:1]
        return out[:limit]
    out += [f"Chia theo {sem.dims[d]['label'].lower()}" for d in cfg["drivers"] if d not in used][:2]
    if tr and not plan.get("compare"):
        out.append("So với cùng kỳ năm trước")
    additive = mdef.get("additive", mdef.get("type") != "derived" and mdef.get("format") != "percent")
    if period and additive:
        out.append(f"Tìm nguyên nhân biến động {label} {period}")
    return out[:limit]


def _cost_public(c):
    if not c:
        return None
    return {k: c.get(k) for k in ("total_bytes", "human", "decision", "level", "reasons", "tables", "large_tables", "thresholds")}


def _render(template: str, facts: dict) -> str:
    return re.sub(r"\{([a-z0-9_]+)\}", lambda m: facts[m.group(1)]["text"] if m.group(1) in facts else m.group(0), template)


def infer_meta(columns, types, rows) -> list[dict]:
    sem = get_semantic()
    out = []
    for i, c in enumerate(columns):
        t = (types[i] if i < len(types) else "").upper()
        if c in sem.metrics:
            m = sem.metrics[c]
            out.append({"name": c, "label": m["label"], "kind": "metric", "format": m.get("format", "number"),
                        "additive": m.get("additive", m.get("format") != "percent")})
            continue
        if c in sem.dims:
            d = sem.dims[c]
            out.append({"name": c, "label": d["label"], "kind": "dimension", "time": d.get("kind") == "time", "format": "text"})
            continue
        numeric = any(x in t for x in ("INT", "DOUBLE", "DECIMAL", "FLOAT", "NUMERIC", "REAL"))
        is_id = c.startswith("ma_") or c in ("dong", "nam", "thang", "quy", "tuan", "thu")
        label = _catalog_label(c)
        if "DATE" in t or "TIMESTAMP" in t or c in ("thang", "nam", "quy", "tuan", "ngay"):
            out.append({"name": c, "label": label, "kind": "dimension", "time": True, "format": "text"})
        elif numeric and not is_id:
            nc = norm(c.replace("_", " "))
            fmt = "percent" if re.search(r"pct|ty le|ti le|bien|ty trong", nc) else \
                ("currency" if re.search(r"doanh thu|tien|^tt|gia|chi phi|loi nhuan|von|^ck|dtt|thanh tien", nc) else "number")
            out.append({"name": c, "label": label, "kind": "metric", "format": fmt, "additive": fmt != "percent"})
        else:
            out.append({"name": c, "label": label, "kind": "dimension", "time": False, "format": "text"})
    return out


def _catalog_label(col: str) -> str:
    """Nhãn thân thiện cho cột của SQL tự do: lấy phần đầu mô tả nghiệp vụ trong catalog (vd ma_kh -> Mã khách hàng)."""
    for t in get_catalog().tables.values():
        if t["schema"] == profile.serving_schema() and col in t["columns"] and t["columns"][col].get("description"):
            d = re.split(r"\s*(?:->|\(|—|:|,)", t["columns"][col]["description"])[0].strip()
            if d and len(d) <= 40:
                return d
    return col


from . import insights  # noqa: E402  (tránh vòng import)

_agent: Agent | None = None


def get_agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = Agent()
    return _agent

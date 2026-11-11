"""
Bộ đánh giá Execution Accuracy kiểu Spider/BIRD trên tập câu hỏi mẫu (eval/questions.json):
  - chạy câu hỏi qua agent (không cần duyệt HITL), chạy SQL chuẩn (gold), so khớp kết quả theo chuẩn Spider 2.0:
    mỗi cột của kết quả gold phải xuất hiện (cùng đa tập giá trị, làm tròn) trong kết quả dự đoán, số dòng bằng nhau
    (cho phép dự đoán có thêm cột phụ, không phụ thuộc thứ tự cột/dòng)
  - câu hỏi kỳ vọng hỏi lại (expect=clarify) được tính đúng nếu agent hỏi lại
  - câu hỏi phân tích biến động kiểm tra chiều giải thích & loại nguyên nhân gốc tìm ra
  - optimize(): tự tối ưu few-shot — thêm cặp (câu hỏi, SQL chuẩn) của các câu sai trên tập train vào kho few-shot,
    đo lại trên tập dev (không rò rỉ: dev không bao giờ được thêm vào kho)
"""
from __future__ import annotations

import json
import time
from collections import Counter, defaultdict

from . import config
from .governance import UserCtx, validate
from .store import get_store
from .warehouse import get_warehouse

EVAL_FILE = config.EVAL_DIR / "questions.json"


def load_questions() -> list[dict]:
    return json.loads(EVAL_FILE.read_text(encoding="utf-8"))


def _norm_val(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    return str(v).strip()


def compare(gold_cols, gold_rows, pred_cols, pred_rows) -> tuple[bool, str]:
    if pred_rows is None:
        return False, "Không có kết quả dự đoán"
    if len(gold_rows) != len(pred_rows):
        return False, f"Số dòng khác nhau (gold {len(gold_rows)} / dự đoán {len(pred_rows)})"
    pred_vecs = [Counter(_norm_val(r[j]) for r in pred_rows) for j in range(len(pred_cols))]
    used = set()
    for i, gc in enumerate(gold_cols):
        gv = Counter(_norm_val(r[i]) for r in gold_rows)
        hit = next((j for j, pv in enumerate(pred_vecs) if j not in used and pv == gv), None)
        if hit is None:
            return False, f"Không tìm thấy cột tương ứng với '{gc}'"
        used.add(hit)
    return True, "Khớp"


def run_eval(split: str = "all", mode: str | None = None, label: str | None = None, save: bool = True,
             items: list[dict] | None = None) -> dict:
    from .agent import get_agent
    agent = get_agent()
    store = get_store()
    wh = get_warehouse()
    admin = UserCtx("eval_bot", "admin", {})
    qs = items or [q for q in load_questions() if split == "all" or q.get("split") == split]
    details = []
    t_all = time.time()
    for q in qs:
        cid = store.create_conversation("eval_bot", "eval")
        t0 = time.time()
        resp = None
        try:
            for turn in q.get("turns", [])[:-1] if q.get("turns") else []:
                agent.ask(admin, cid, turn, eval_mode=True, mode=mode)
            question = q["turns"][-1] if q.get("turns") else q["question"]
            resp = agent.ask(admin, cid, question, eval_mode=True, mode=mode)
            if resp["type"] == "clarification" and q.get("clarify_answer") is not None:
                resp = agent.ask(admin, cid, "", clarification={"id": resp["clarification"]["id"], "option": q["clarify_answer"]},
                                 eval_mode=True, mode=mode)
        except Exception as e:
            resp = {"type": "error", "text": f"{type(e).__name__}: {e}"}
        latency = int((time.time() - t0) * 1000)
        store.delete_conversation(cid)
        d = {"id": q["id"], "question": q.get("question") or " → ".join(q.get("turns", [])), "difficulty": q.get("difficulty"),
             "split": q.get("split"), "tags": q.get("tags", []), "pred_type": resp.get("type"), "pred_sql": resp.get("sql"),
             "pred_source": resp.get("sql_source"), "gold_sql": q.get("gold_sql"), "latency_ms": latency,
             "retries": len(resp.get("repairs") or []), "bytes": (resp.get("cost") or {}).get("total_bytes"),
             "audit_ok": (resp.get("audit") or {}).get("ok"), "error": resp.get("error")}
        exp = q.get("expect", "result")
        if exp == "clarify":
            d["correct"] = resp.get("type") == "clarification"
            d["reason"] = "Agent hỏi lại đúng kỳ vọng" if d["correct"] else f"Kỳ vọng hỏi lại nhưng nhận {resp.get('type')}"
        elif exp == "blocked":
            d["correct"] = resp.get("type") in ("blocked", "error") and bool(resp.get("error"))
            d["reason"] = "Chặn đúng" if d["correct"] else "Kỳ vọng bị chặn"
        elif exp == "variance":
            v = resp.get("variance") or {}
            kinds = {c["kind"] for c in v.get("root_causes", [])}
            ok_dim = not q.get("best_dimension_in") or v.get("best_dimension") in q["best_dimension_in"]
            ok_cause = not q.get("root_cause") or q["root_cause"] in kinds
            d["correct"] = resp.get("type") == "variance" and ok_dim and ok_cause
            d["reason"] = f"best_dimension={v.get('best_dimension')}, nguyên nhân={sorted(kinds)}"
        else:
            gv = validate(q["gold_sql"], admin)
            if not gv.ok:
                d["correct"], d["reason"] = False, f"Gold SQL lỗi: {gv.errors[0]['message']}"
            else:
                g = wh.execute(gv.sql_exec, max_rows=20000)
                if resp.get("type") != "result":
                    d["correct"], d["reason"] = False, f"Agent trả về {resp.get('type')}: {(resp.get('text') or '')[:120]}"
                else:
                    d["correct"], d["reason"] = compare(g["columns"], g["rows"], resp.get("columns") or [], resp.get("rows"))
        details.append(d)
    n = len(details)

    def acc(rows):
        return round(100.0 * sum(r["correct"] for r in rows) / len(rows), 1) if rows else None

    by_diff, by_split, by_tag = defaultdict(list), defaultdict(list), defaultdict(list)
    for d in details:
        by_diff[d["difficulty"]].append(d)
        by_split[d["split"]].append(d)
        for t in d["tags"]:
            by_tag[t].append(d)
    result_items = [d for d in details if d["pred_type"] == "result"]
    summary = {
        "label": label or f"{mode or 'auto'} · {split}", "mode": mode or "auto", "split": split, "n": n,
        "execution_accuracy": acc(details),
        "by_difficulty": {k: {"n": len(v), "acc": acc(v)} for k, v in sorted(by_diff.items(), key=lambda x: str(x[0]))},
        "by_split": {k: {"n": len(v), "acc": acc(v)} for k, v in by_split.items()},
        "by_tag": {k: {"n": len(v), "acc": acc(v)} for k, v in sorted(by_tag.items())},
        "valid_sql_rate": round(100.0 * len(result_items) / max(1, sum(1 for d in details if d["gold_sql"])), 1),
        "audit_pass_rate": round(100.0 * sum(1 for d in result_items if d["audit_ok"]) / max(1, len(result_items)), 1),
        "avg_latency_ms": int(sum(d["latency_ms"] for d in details) / max(n, 1)),
        "avg_retries": round(sum(d["retries"] for d in details) / max(n, 1), 2),
        "total_bytes": sum(d["bytes"] or 0 for d in details),
        "fewshots_in_store": len(get_store().list_fewshots()),
        "elapsed_s": round(time.time() - t_all, 1),
    }
    eid = store.save_eval(summary["label"], summary, details) if save else None
    return {"id": eid, "summary": summary, "details": details}


def optimize(mode: str | None = None) -> dict:
    """Tự tối ưu few-shot theo phản hồi của bộ eval: học từ lỗi trên train, đo trên dev."""
    from .retriever import get_retriever
    store = get_store()
    before = run_eval("dev", mode, label=f"{mode or 'auto'} · dev · trước tối ưu")
    train = run_eval("train", mode, label=f"{mode or 'auto'} · train (thu thập lỗi)")
    added = []
    for d in train["details"]:
        if not d["correct"] and d.get("gold_sql"):
            store.add_fewshot(d["question"], d["gold_sql"].strip(), source="optimizer", verified=True)
            added.append(d["question"])
    get_retriever().reload_fewshots()
    after = run_eval("dev", mode, label=f"{mode or 'auto'} · dev · sau tối ưu")
    return {"added": added, "before": before["summary"], "after": after["summary"], "train": train["summary"],
            "before_id": before["id"], "after_id": after["id"]}

"""
Ước lượng chi phí quét dữ liệu (bytes scanned) XÁC ĐỊNH bằng code, mô phỏng mô hình tính phí của BigQuery:
  bytes = Σ_bảng  số_dòng_sau_cắt_phân_vùng × Σ độ_rộng_trung_bình(cột được tham chiếu)
Cắt phân vùng (partition pruning) chỉ áp dụng khi điều kiện lọc so sánh TRỰC TIẾP cột phân vùng với hằng ngày.
"""
from __future__ import annotations

import datetime as dt
import re

from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from . import config
from .catalog import get_catalog
from .textutil import human_bytes


def _literal_date(e) -> dt.date | None:
    if isinstance(e, exp.Cast) and e.to and e.to.this in (exp.DataType.Type.DATE, exp.DataType.Type.TIMESTAMP):
        e = e.this
    if isinstance(e, exp.Literal) and e.is_string:
        try:
            return dt.date.fromisoformat(e.this[:10])
        except ValueError:
            return None
    return None


def _flatten_and(e):
    if isinstance(e, exp.And):
        yield from _flatten_and(e.left)
        yield from _flatten_and(e.right)
    elif isinstance(e, exp.Paren):
        yield from _flatten_and(e.this)
    else:
        yield e


def _is_pcol(e, alias, pcol) -> bool:
    return isinstance(e, exp.Column) and e.name == pcol and (not e.table or e.table == alias)


def partition_range(where, alias: str, pcol: str):
    """Trả về (lo, hi) với hi là cận trên KHÔNG bao gồm; None nếu không cắt được."""
    if where is None:
        return None
    lo, hi = None, None
    cond = where.this if isinstance(where, exp.Where) else where
    for c in _flatten_and(cond):
        if isinstance(c, exp.Between) and _is_pcol(c.this, alias, pcol):
            a, b = _literal_date(c.args.get("low")), _literal_date(c.args.get("high"))
            if a:
                lo = max(lo, a) if lo else a
            if b:
                b2 = b + dt.timedelta(days=1)
                hi = min(hi, b2) if hi else b2
            continue
        if isinstance(c, (exp.GTE, exp.GT, exp.LT, exp.LTE, exp.EQ)):
            left, right = c.left, c.right
            flipped = False
            if not _is_pcol(left, alias, pcol) and _is_pcol(right, alias, pcol):
                left, right, flipped = right, left, True
            if not _is_pcol(left, alias, pcol):
                continue
            d = _literal_date(right)
            if not d:
                continue
            op = type(c)
            if flipped:
                op = {exp.GTE: exp.LTE, exp.GT: exp.LT, exp.LT: exp.GT, exp.LTE: exp.GTE, exp.EQ: exp.EQ}[op]
            if op in (exp.GTE, exp.GT):
                d2 = d if op is exp.GTE else d + dt.timedelta(days=1)
                lo = max(lo, d2) if lo else d2
            elif op in (exp.LT, exp.LTE):
                d2 = d if op is exp.LT else d + dt.timedelta(days=1)
                hi = min(hi, d2) if hi else d2
            else:
                lo = max(lo, d) if lo else d
                hi = min(hi, d + dt.timedelta(days=1)) if hi else d + dt.timedelta(days=1)
        if isinstance(c, exp.In) and _is_pcol(c.this, alias, pcol):
            ds = [_literal_date(x) for x in c.expressions]
            if ds and all(ds):
                lo = max(lo, min(ds)) if lo else min(ds)
                hi = min(hi, max(ds) + dt.timedelta(days=1)) if hi else max(ds) + dt.timedelta(days=1)
    if lo is None and hi is None:
        return None
    return lo, hi


def estimate(qualified, rls_tables: list[str] | None = None) -> dict:
    cat = get_catalog()
    hitl = config.policies().get("hitl", {})
    large_rows = hitl.get("large_table_rows", 100000)
    items = []
    if qualified is None:
        return {"total_bytes": 0, "human": "0 B", "tables": [], "large_tables": []}
    for scope in traverse_scope(qualified):
        for alias, src in scope.sources.items():
            if not isinstance(src, exp.Table) or not isinstance(src.this, exp.Identifier):
                continue
            fq = f"{src.db}.{src.name}".lower()
            t = cat.get(fq)
            if not t:
                continue
            cols = sorted({c.name for c in scope.columns if c.table == alias and c.name in t["columns"]})
            if not cols:  # COUNT(*) -> tối thiểu 1 cột hẹp nhất
                narrow = min(t["columns"].values(), key=lambda c: c["avg_width"])
                cols = [narrow["name"]]
            width = sum(t["columns"][c]["avg_width"] for c in cols)
            rows = t["rows"]
            pr = None
            part = t.get("partition")
            if part:
                pr = partition_range(scope.expression.args.get("where"), alias, part["column"])
                if pr:
                    lo, hi = pr
                    rows = sum(n for d, n in part["counts"].items()
                               if (lo is None or d >= lo.isoformat()) and (hi is None or d < hi.isoformat()))
            b = int(rows * width)
            items.append({"table": fq, "alias": alias, "columns": cols, "rows_total": t["rows"], "rows_scanned": rows,
                          "partition_column": part["column"] if part else None,
                          "partition_filter": [str(pr[0]) if pr[0] else None, str(pr[1]) if pr[1] else None] if pr else None,
                          "pruned_pct": round(100 * (1 - rows / t["rows"]), 1) if t["rows"] and part else 0.0,
                          "bytes": b, "large": t["rows"] >= large_rows})
    # chi phí phụ của RLS: vị từ RLS dạng "... IN (SELECT ... FROM <bảng tra cứu> ...)" phải quét thêm bảng tra cứu đó
    rls_rules = config.policies().get("rls") or {}
    for fq in rls_tables or []:
        lookups = {m.lower() for rule in rls_rules.values() for m in re.findall(r"\bFROM\s+([\w.]+)", rule.get(fq) or "", re.I)}
        for lookup in sorted(lookups):
            t = cat.get(lookup)
            if t:
                items.append({"table": lookup, "alias": f"rls:{fq}", "columns": [], "rows_total": t["rows"],
                              "rows_scanned": t["rows"], "partition_column": None, "partition_filter": None,
                              "pruned_pct": 0.0, "bytes": int(t["rows"] * 12), "large": False})
    total = sum(i["bytes"] for i in items)
    large = sorted({i["table"] for i in items if i["large"]})
    return {"total_bytes": total, "human": human_bytes(total), "tables": items, "large_tables": large}


def decide(est: dict, role_cfg: dict, always_review: bool = False) -> dict:
    """Quyết định theo chính sách: auto | approval (HITL) | blocked."""
    hitl = config.policies().get("hitl", {})
    c = role_cfg.get("cost", {})
    total = est["total_bytes"]
    reasons, level = [], "ok"
    decision = "auto"
    if total > c.get("max_bytes", 1e18):
        return {"decision": "blocked", "level": "blocked",
                "reasons": [f"Ước tính quét {est['human']} vượt hạn mức tối đa {human_bytes(c['max_bytes'])} của vai trò."],
                "thresholds": c}
    if total > c.get("warn_bytes", 1e18):
        level = "warn"
        reasons.append(f"Ước tính quét {est['human']} vượt ngưỡng cảnh báo {human_bytes(c['warn_bytes'])}.")
    if total > c.get("approval_bytes", 1e18):
        decision = "approval"
        reasons.append(f"Vượt ngưỡng phải duyệt {human_bytes(c['approval_bytes'])}.")
    if est["large_tables"] and total > hitl.get("hitl_min_bytes", 0):
        decision = "approval"
        reasons.append(f"Truy vấn quét bảng lớn ({', '.join(est['large_tables'])}) với {est['human']} — cần xác nhận SQL trước khi chạy.")
    if always_review:
        decision = "approval"
        reasons.append("Chế độ 'luôn duyệt SQL' đang bật.")
    return {"decision": decision, "level": level, "reasons": reasons, "thresholds": c}

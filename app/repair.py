"""
Agent sửa lỗi SQL (tự retry khi validate/execution fail) + tối ưu chi phí xác định:
  - rule-based: tên cột/bảng sai (gợi ý từ catalog + thông báo của DuckDB), thiếu GROUP BY, fan-out (SUM cột cấp
    đơn sau khi join bảng cấp dòng), chia cho 0
  - LLM-based (khi có Claude): gửi SQL + lỗi + schema liên quan để sinh bản sửa
  - optimize_partition_filters: viết lại YEAR(col)=N / STRFTIME(col,'%Y-%m')='YYYY-MM' / DATE_TRUNC(...) = ... thành
    điều kiện khoảng trên cột phân vùng để cắt phân vùng (giảm bytes scanned)
"""
from __future__ import annotations

import datetime as dt
import difflib
import re

import sqlglot
from sqlglot import exp

from .catalog import get_catalog
from .profile import serving_schema
from .textutil import norm

D = "duckdb"


def _rename_column(tree, old: str, new: str, table_alias: str | None = None):
    changed = False
    for c in tree.find_all(exp.Column):
        if c.name.lower() == old.lower():
            c.set("this", exp.to_identifier(new))
            if table_alias is not None:
                c.set("table", exp.to_identifier(table_alias) if table_alias else None)
            changed = True
    return changed


def _column_by_description(name: str, tables: list[str]) -> str | None:
    cat = get_catalog()
    key = norm(name.replace("_", " "))
    best = None
    for fq in tables:
        t = cat.get(fq)
        if not t:
            continue
        for c in t["columns"].values():
            desc = norm(c["description"] + " " + " ".join(c["synonyms"]))
            if key and key in desc:
                score = len(key) / (len(desc) + 1)
                if best is None or score > best[0]:
                    best = (score, c["name"])
    return best[1] if best else None


def rule_repair(sql: str, error: dict, tables: list[str]) -> tuple[str | None, str]:
    """Trả về (sql_mới, mô tả) hoặc (None, lý do không sửa được)."""
    code = error.get("code")
    try:
        tree = sqlglot.parse_one(sql, read=D)
    except Exception:
        return None, "Không parse được SQL để sửa tự động."
    cat = get_catalog()
    if code == "UNKNOWN_COLUMN":
        col = error.get("column")
        sug = error.get("suggestion") or _column_by_description(col or "", tables)
        if col and sug and _rename_column(tree, col, sug):
            return tree.sql(dialect=D, pretty=True), f"Đổi cột '{col}' → '{sug}' (theo catalog)."
        return None, "Không tìm được cột thay thế."
    if code == "UNKNOWN_TABLE":
        t, sug = error.get("table"), error.get("suggestion")
        sch = serving_schema()
        if t and not sug:
            name = t.split(".")[-1]
            cands = [x for x in cat.tables if x.startswith(sch + ".")]
            m = difflib.get_close_matches(f"{sch}.{name}", cands, n=1, cutoff=0.4)
            sug = m[0] if m else None
        if t and sug and sug.startswith(sch + "."):
            for tb in tree.find_all(exp.Table):
                if f"{tb.db or sch}.{tb.name}".lower() == t:
                    tb.set("this", exp.to_identifier(sug.split(".")[1]))
                    tb.set("db", exp.to_identifier(sch))
            return tree.sql(dialect=D, pretty=True), f"Đổi bảng {t} → {sug}."
        return None, f"Không tìm được bảng thay thế trong tầng {sch}."
    if code == "BINDER":
        raw = error.get("raw") or error.get("message", "")
        m = re.search(r'Referenced column "([^"]+)" not found.*?Candidate bindings: "([^"]+)"', raw, re.S)
        if m:
            old, cand = m.group(1), m.group(2)
            parts = cand.split(".")
            if _rename_column(tree, old.split(".")[-1], parts[-1], parts[0] if len(parts) > 1 else None):
                return tree.sql(dialect=D, pretty=True), f"Đổi cột '{old}' → '{cand}' (gợi ý của engine)."
        m = re.search(r'Table with name (\S+) does not exist.*?Did you mean "([^"]+)"', raw, re.S)
        if m:
            old, new = m.group(1).strip('"!'), m.group(2)
            for tb in tree.find_all(exp.Table):
                if tb.name.lower() == old.split(".")[-1].lower():
                    p = new.split(".")
                    tb.set("this", exp.to_identifier(p[-1]))
                    if len(p) > 1:
                        tb.set("db", exp.to_identifier(p[-2]))
            return tree.sql(dialect=D, pretty=True), f"Đổi bảng {old} → {new} (gợi ý của engine)."
        m = re.search(r'column "?([^"\s]+)"? must appear in the GROUP BY clause', raw)
        if m and isinstance(tree, exp.Select):
            col = m.group(1)
            target = next((s for s in tree.selects if s.alias_or_name == col.split(".")[-1] or s.sql() == col), None)
            expr = target.this if isinstance(target, exp.Alias) else (target or exp.column(col))
            grp = tree.args.get("group")
            exprs = list(grp.expressions) if grp else []
            exprs.append(expr.copy())
            tree.set("group", exp.Group(expressions=exprs))
            return tree.sql(dialect=D, pretty=True), f"Bổ sung '{col}' vào GROUP BY."
        if "Division by zero" in raw:
            for d in tree.find_all(exp.Div):
                if not isinstance(d.expression, exp.Nullif):
                    d.set("expression", exp.Nullif(this=d.expression.copy(), expression=exp.Literal.number(0)))
            return tree.sql(dialect=D, pretty=True), "Bọc mẫu số bằng NULLIF(…, 0)."
        return None, "Lỗi engine chưa có quy tắc sửa tự động."
    if code == "L004":
        # SUM(cột cấp đơn) sau khi join cấp dòng -> dùng biểu thức tương đương trên bảng cấp dòng (catalog.yml: lint.fanout)
        from .sqlcheck import lint_rules
        changed = False
        for fo in lint_rules()["fanout"]:
            line_alias = next((tb.alias_or_name for tb in tree.find_all(exp.Table)
                               if f"{tb.db or serving_schema()}.{tb.name}".lower() == fo["line"]), None)
            mapping = {k: v.replace("{line}", line_alias or "") for k, v in (fo.get("line_equivalents") or {}).items()}
            if not line_alias or not mapping:
                continue
            for agg in tree.find_all(exp.Sum):
                for c in agg.find_all(exp.Column):
                    if c.name in mapping:
                        c.replace(sqlglot.parse_one(f"({mapping[c.name]})", read=D))
                        changed = True
        if changed:
            return tree.sql(dialect=D, pretty=True), "Sửa fan-out: thay cột cấp đơn bằng biểu thức cấp dòng tương ứng."
        return None, "Không sửa được fan-out tự động."
    return None, "Chưa có quy tắc sửa cho lỗi này."


def _date_lit(d: dt.date):
    return exp.Cast(this=exp.Literal.string(d.isoformat()), to=exp.DataType.build("DATE"))


def optimize_partition_filters(sql: str) -> tuple[str, list[str]]:
    """Viết lại điều kiện thời gian không sargable trên cột phân vùng thành khoảng [lo, hi)."""
    cat = get_catalog()
    pcols = {t["partition"]["column"] for t in cat.tables.values() if t.get("partition")}
    try:
        tree = sqlglot.parse_one(sql, read=D)
    except Exception:
        return sql, []
    notes = []

    def rng(col, lo, hi):
        return exp.and_(exp.GTE(this=col.copy(), expression=_date_lit(lo)), exp.LT(this=col.copy(), expression=_date_lit(hi)))

    for eq in list(tree.find_all(exp.EQ)):
        left, right = eq.left, eq.right
        if isinstance(right, exp.Func) and not isinstance(left, exp.Func):
            left, right = right, left
        col = left.find(exp.Column) if isinstance(left, exp.Expression) else None
        if not col or col.name not in pcols or not isinstance(right, exp.Literal):
            continue
        val = right.this
        new = None
        if isinstance(left, exp.Year) and re.fullmatch(r"\d{4}", str(val)):
            y = int(val)
            new = rng(col, dt.date(y, 1, 1), dt.date(y + 1, 1, 1))
        elif isinstance(left, (exp.TimeToStr, exp.Anonymous)) or "strftime" in left.sql(dialect=D).lower():
            s = left.sql(dialect=D).lower()
            m = re.fullmatch(r"(\d{4})-(\d{2})", str(val))
            if "%y-%m" in s and m:
                y, mo = int(m.group(1)), int(m.group(2))
                lo = dt.date(y, mo, 1)
                hi = dt.date(y + (mo == 12), mo % 12 + 1, 1)
                new = rng(col, lo, hi)
        elif isinstance(left, exp.Extract) and "year" in left.sql(dialect=D).lower() and re.fullmatch(r"\d{4}", str(val)):
            y = int(val)
            new = rng(col, dt.date(y, 1, 1), dt.date(y + 1, 1, 1))
        if new is not None:
            notes.append(f"Viết lại `{eq.sql(dialect=D)}` → `{new.sql(dialect=D)}` để cắt phân vùng.")
            eq.replace(exp.Paren(this=new))
    if not notes:
        return sql, []
    return tree.sql(dialect=D, pretty=True), notes

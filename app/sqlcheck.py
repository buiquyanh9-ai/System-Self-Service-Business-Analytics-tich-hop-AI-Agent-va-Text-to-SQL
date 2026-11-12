"""
Phân tích tĩnh SQL (AST): lint các lỗi logic phổ biến trong kho dữ liệu, đối chiếu định nghĩa chỉ số với
semantic layer, cảnh báo dữ liệu có vấn đề chất lượng đã biết, và tính lineage cột (truy vấn -> dw -> raw).
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.lineage import lineage as sg_lineage
from sqlglot.optimizer.scope import traverse_scope

from . import config, profile
from .catalog import get_catalog

def lint_rules() -> dict:
    """Quy tắc lint riêng của kho dữ liệu đang nạp (catalog.yml: lint). Doanh nghiệp không khai báo thì bỏ qua nhóm đó.

      fanout:           cộng cột cấp cha sau khi JOIN bảng cấp con (nhân bản số liệu)
      required_filters: tính tổng các cột tiền/số lượng mà không lọc một cột trạng thái
      semi_additive:    cộng chỉ số ảnh chụp (tồn kho, số dư) qua nhiều ngày chốt
    """
    r = config.catalog_yaml().get("lint") or {}
    return {k: list(r.get(k) or []) for k in ("fanout", "required_filters", "semi_additive")}


def _src_fq(scope, alias):
    src = scope.sources.get(alias)
    if isinstance(src, exp.Table) and isinstance(src.this, exp.Identifier):
        return f"{src.db}.{src.name}".lower()
    return None


def _norm_expr(e: str) -> str:
    e = e.lower().replace('"', "")
    e = re.sub(r"\b[a-z_][a-z0-9_]*\.", "", e)
    return re.sub(r"\s+", "", e)


def lint(v, known_issues: list[dict] | None = None, date_range: tuple | None = None) -> list[dict]:
    """v: governance.Validation (đã có qualified tree). Trả về danh sách {code, severity, message, hint}."""
    out = []
    q, tree = v.qualified, v.tree
    if q is None:
        return out
    cat = get_catalog()
    rules = lint_rules()
    schema = profile.serving_schema()

    # L001 SELECT *
    for s in tree.find_all(exp.Select):
        if any(isinstance(e, exp.Star) for e in s.expressions):
            out.append({"code": "L001", "severity": "warning", "message": "Dùng SELECT * — quét toàn bộ cột, tăng chi phí.",
                        "hint": "Chỉ chọn các cột cần thiết."})
            break
    agg_types = (exp.Sum, exp.Avg)
    for scope in traverse_scope(q):
        sel = scope.expression
        if not isinstance(sel, exp.Select):
            continue
        tables = {a: _src_fq(scope, a) for a in scope.sources}
        fqs = {t for t in tables.values() if t}
        # L002 tích Descartes
        for j in sel.args.get("joins") or []:
            if not j.args.get("on") and not j.args.get("using") and (j.args.get("kind") or "").upper() != "CROSS" \
                    and not isinstance(j.this, exp.Unnest):
                out.append({"code": "L002", "severity": "warning",
                            "message": f"JOIN không có điều kiện ON/USING với {j.this.sql()[:60]} — nguy cơ tích Descartes.",
                            "hint": "Bổ sung điều kiện nối khóa."})
        # L003 điều kiện không sargable trên cột phân vùng
        where = sel.args.get("where")
        if where is not None:
            for alias, fq in tables.items():
                t = cat.get(fq) if fq else None
                part = (t or {}).get("partition")
                if not part:
                    continue
                for fn in where.find_all(exp.Func):
                    if isinstance(fn, (exp.Cast, exp.And, exp.Or)):
                        continue
                    cols = [c for c in fn.find_all(exp.Column) if c.name == part["column"] and c.table == alias]
                    if cols and not isinstance(fn, (exp.Between, exp.In)):
                        out.append({"code": "L003", "severity": "warning",
                                    "message": f"Điều kiện trên cột phân vùng {fq}.{part['column']} bị bọc trong hàm "
                                               f"{fn.sql()[:50]} — không cắt được phân vùng, phải quét toàn bảng.",
                                    "hint": f"Viết dạng khoảng: {part['column']} >= DATE 'YYYY-MM-DD' AND {part['column']} < DATE 'YYYY-MM-DD'."})
                        break
        # L004 fan-out: SUM cột cấp cha (vd cấp đơn) khi join với bảng cấp con (vd cấp dòng)
        for fo in rules["fanout"]:
            if fo["line"] not in fqs or fo["header"] not in fqs:
                continue
            for agg in sel.find_all(*agg_types):
                for c in agg.find_all(exp.Column):
                    if tables.get(c.table) == fo["header"] and c.name in fo["measures"]:
                        out.append({"code": "L004", "severity": "error",
                                    "message": f"Nhân bản số liệu (fan-out): {agg.sql()[:60]} cộng cột cấp ĐƠN ({fo['header']}.{c.name}) "
                                               f"sau khi JOIN với bảng cấp DÒNG — mỗi đơn bị cộng nhiều lần.",
                                    "hint": fo.get("hint") or f"Tổng hợp {fo['header']} riêng trước khi JOIN."})
                        break
        # L006 tính tổng tiền/số lượng mà thiếu lọc cột trạng thái bắt buộc
        for rf in rules["required_filters"]:
            hit = any(c.name in (rf["tables"].get(tables.get(c.table)) or [])
                      for agg in sel.find_all(*agg_types) for c in agg.find_all(exp.Column))
            if hit and not any(c.name == rf["column"] for c in sel.find_all(exp.Column)):
                out.append({"code": "L006", "severity": "warning", "message": rf["message"], "hint": rf.get("hint", "")})
        # L007 chỉ số semi-additive bị cộng qua thời gian
        for sa in rules["semi_additive"]:
            snap = sa["snapshot_column"]
            for agg in sel.find_all(exp.Sum):
                if not any(tables.get(c.table) == sa["table"] and c.name in sa["measures"] for c in agg.find_all(exp.Column)):
                    continue
                grp = sel.args.get("group")
                grouped = grp is not None and any(isinstance(g, exp.Column) and g.name == snap for g in grp.expressions)
                filtered = where is not None and any(
                    isinstance(x, exp.EQ) and any(isinstance(y, exp.Column) and y.name == snap for y in x.find_all(exp.Column))
                    or isinstance(x, exp.In) and isinstance(x.this, exp.Column) and x.this.name == snap
                    for x in where.find_all(exp.EQ, exp.In))
                if not grouped and not filtered:
                    out.append({"code": "L007", "severity": "warning",
                                "message": f"{agg.sql()[:50]}: {sa.get('label', 'chỉ số này')} là chỉ số semi-additive, "
                                           "cộng qua nhiều ngày chốt sẽ sai.",
                                "hint": f"Lọc 1 ngày chốt (vd {snap} = (SELECT MAX({snap}) ...)) hoặc GROUP BY {snap}."})
        # L005 chia không NULLIF
        for d in sel.find_all(exp.Div):
            r = d.expression
            if not isinstance(r, (exp.Literal, exp.Nullif)) and not r.find(exp.Nullif):
                out.append({"code": "L005", "severity": "info", "message": f"Phép chia {d.sql()[:50]} có thể chia cho 0.",
                            "hint": "Bọc mẫu số bằng NULLIF(x, 0)."})
                break
    # L008 tầng raw/stg
    lower = [t for t in v.tables if not t.startswith(schema + ".")]
    if lower:
        out.append({"code": "L008", "severity": "info",
                    "message": f"Truy vấn tầng chưa làm sạch: {', '.join(lower)}.",
                    "hint": f"Báo cáo nghiệp vụ nên dùng tầng {schema} để thống nhất định nghĩa."})
    # L010 vấn đề chất lượng dữ liệu đã biết
    for iss in known_issues or []:
        if iss["table"] in v.tables:
            cols_used = {c.name for c in q.find_all(exp.Column)}
            if iss.get("columns") and not set(iss["columns"]) & cols_used:
                continue
            if date_range and iss.get("since") and date_range[1] and str(date_range[1]) <= iss["since"]:
                continue
            if iss.get("requires_range") and not (date_range and date_range[1]):
                continue
            out.append({"code": "L010", "severity": "warning", "message": f"Dữ liệu có vấn đề chất lượng chưa xử lý: {iss['message']}",
                        "hint": iss.get("hint", "Xem trang Chất lượng dữ liệu.")})
    # bỏ trùng
    seen, uniq = set(), []
    for o in out:
        k = (o["code"], o["message"])
        if k not in seen:
            seen.add(k)
            uniq.append(o)
    return uniq


def matched_metrics(v) -> list[dict]:
    """Đối chiếu các biểu thức tổng hợp trong truy vấn với định nghĩa chỉ số của semantic layer."""
    if v.tree is None:
        return []
    sem = config.semantic_layer()
    norms = {}
    for name, m in sem["metrics"].items():
        if m.get("sql"):
            norms.setdefault(_norm_expr(m["sql"]), []).append((name, m))
    found = []
    filters_txt = _norm_expr(v.tree.sql(dialect="duckdb"))
    for agg in v.tree.find_all(exp.AggFunc):
        key = _norm_expr(agg.sql(dialect="duckdb"))
        for name, m in norms.get(key, []):
            ok_filter = True
            if m.get("filter"):
                ok_filter = _norm_expr(m["filter"]).replace("<>", "!=") in filters_txt.replace("<>", "!=") or \
                    _norm_expr(m["filter"]) in filters_txt
            found.append({"metric": name, "label": m["label"], "expression": agg.sql(dialect="duckdb"),
                          "filter_matches": ok_filter, "expected_filter": m.get("filter")})
    uniq = {f["metric"]: f for f in found}
    return list(uniq.values())


def query_lineage(v, max_cols: int = 10) -> list[dict]:
    """Lineage cột đầu ra -> cột nguồn trong dw (sqlglot.lineage) -> cột raw (lineage model)."""
    from .lineage import get_model_lineage

    if v.tree is None or not isinstance(v.tree, (exp.Select, exp.Query)):
        return []
    sql = v.tree.sql(dialect="duckdb")
    schema = get_catalog().sqlglot_schema()
    out = []
    try:
        names = [s.alias_or_name for s in v.tree.selects][:max_cols] if hasattr(v.tree, "selects") else []
    except Exception:
        names = []
    ml = get_model_lineage()
    for name in names:
        if not name or name == "*":
            continue
        try:
            node = sg_lineage(name, sql, schema=schema, dialect="duckdb")
        except Exception:
            continue
        sources = set()
        for n in node.walk():
            if isinstance(n.expression, exp.Table):
                fq = f"{n.expression.db}.{n.expression.name}".lower()
                sources.add(f"{fq}.{n.name.split('.')[-1]}")
        upstream = set()
        for s in sources:
            upstream |= ml.upstream_raw(s)
        out.append({"column": name, "sources": sorted(sources), "raw_sources": sorted(upstream)})
    return out

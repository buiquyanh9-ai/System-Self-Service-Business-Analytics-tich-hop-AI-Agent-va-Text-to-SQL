"""
Trình biên dịch semantic layer: KẾ HOẠCH TRUY VẤN (JSON) -> SQL DuckDB.
Đảm bảo mọi chỉ số đã đặt tên luôn được tính đúng 1 định nghĩa (config/semantic_layer.yml).

Plan:
{
  "metrics": ["doanh_thu_thuan"], "dimensions": ["thang", "vung"],
  "filters": [{"dimension": "vung", "op": "in", "values": ["Miền Bắc"]}],
  "time_range": {"start": "2026-01-01", "end": "2026-11-01", "label": "..."},    # end KHÔNG bao gồm
  "compare": {"start": "...", "end": "...", "label": "..."} | null,              # so sánh kỳ
  "having": [{"metric": "doanh_thu_thuan", "op": ">", "value": 1e10}],
  "order_by": [{"field": "doanh_thu_thuan", "direction": "desc"}], "limit": 10
}
"""
from __future__ import annotations

import datetime as dt
import re

from . import config
from .catalog import get_catalog
from .textutil import norm

VALUE_LIST_CAP = 400          # thống kê catalog chỉ lưu tối đa từng này giá trị phân biệt cho mỗi cột
TIME_GRAINS = ["day", "week", "month", "quarter", "year"]
GRAIN_OF_DIM = {"ngay": "day", "tuan": "week", "thang": "month", "quy": "quarter", "nam": "year"}
OPS = {"in": "IN", "not_in": "NOT IN", "eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
       "like": "ILIKE", "=": "=", ">": ">", "<": "<", ">=": ">=", "<=": "<=", "!=": "<>"}


class SemanticError(Exception):
    pass


def q(v) -> str:
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def time_expr(grain: str, col: str) -> str:
    return {
        "day": f"CAST({col} AS DATE)",
        "week": f"CAST(DATE_TRUNC('week', {col}) AS DATE)",
        "month": f"STRFTIME({col}, '%Y-%m')",
        "quarter": f"CAST(YEAR({col}) AS VARCHAR) || '-Q' || CAST(QUARTER({col}) AS VARCHAR)",
        "year": f"YEAR({col})",
    }[grain]


def _month_align(start: str | None, end: str | None):
    s = dt.date.fromisoformat(start).replace(day=1) if start else None
    e = None
    if end:
        e0 = dt.date.fromisoformat(end)
        e = e0 if e0.day == 1 else (e0.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    return (s.isoformat() if s else None), (e.isoformat() if e else None)


class Semantic:
    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or config.semantic_layer()
        for mdl in self.cfg["models"].values():          # YAML 1.1: khóa `on` bị đọc thành True
            for jd in mdl.get("joins", {}).values():
                if True in jd:
                    jd["on"] = jd.pop(True)
        self.models = self.cfg["models"]
        self.metrics = self.cfg["metrics"]
        self.dims = self.cfg["dimensions"]

    # ------------------------------------------------------------------ tra cứu
    def base_metrics(self, name: str) -> list[str]:
        m = self.metrics[name]
        if m.get("type") == "derived":
            out = []
            for ref in re.findall(r"\{(\w+)\}", m["expr"]):
                for b in self.base_metrics(ref):
                    if b not in out:
                        out.append(b)
            return out
        return [name]

    def metric_models(self, name: str) -> list[str]:
        return sorted({self.metrics[b]["model"] for b in self.base_metrics(name)})

    def _aliases(self, expr: str) -> set[str]:
        return set(re.findall(r"\b([a-z_][a-z0-9_]*)\.[a-z_]", expr))

    def dim_supported(self, model: str, dim: str) -> bool:
        d = self.dims.get(dim)
        if not d:
            return False
        mdl = self.models[model]
        if d.get("kind") == "time":
            return TIME_GRAINS.index(d["grain"]) >= TIME_GRAINS.index(mdl.get("time_grain_min", "day"))
        aliases = self._aliases(self.dim_expr(model, dim))
        return all(a == mdl["alias"] or a in mdl["joins"] for a in aliases)

    def dims_for_metric(self, metric: str) -> list[str]:
        models = self.metric_models(metric)
        return [d for d in self.dims if all(self.dim_supported(m, d) for m in models)]

    def dim_expr(self, model: str, dim: str) -> str:
        d = self.dims[dim]
        if d.get("kind") == "time":
            return time_expr(d["grain"], self.models[model]["time_column"])
        return (d.get("expr_by_model") or {}).get(model, d["expr"])

    def dim_column(self, dim: str) -> tuple[str, str] | None:
        """Bảng và cột vật lý đứng sau một chiều phân tích (None với chiều thời gian hoặc biểu thức không phải cột đơn)."""
        d = self.dims.get(dim, {})
        if d.get("kind") == "time" or not re.fullmatch(r"\w+\.\w+", d.get("expr") or ""):
            return None
        alias, col = d["expr"].split(".")
        for mdl in self.models.values():
            j = (mdl.get("joins") or {}).get(alias)
            table = j["table"] if j else (mdl["table"] if mdl["alias"] == alias else None)
            if table:
                t = get_catalog().get(table)
                if t and col in t["columns"]:
                    return table, col
        return None

    def dim_values(self, dim: str) -> list[str]:
        loc = self.dim_column(dim)
        if not loc:
            return []
        return get_catalog().get(loc[0])["columns"][loc[1]].get("values", []) or []

    # ------------------------------------------------------------------ biên dịch 1 model
    def _model_query(self, model: str, metric_names: list[str], dims: list[str], filters: list[dict],
                     time_range: dict | None, having: list[dict] | None = None, notes: list | None = None,
                     with_components: bool = True) -> tuple[str, list[str]]:
        mdl = self.models[model]
        a = mdl["alias"]
        notes = notes if notes is not None else []
        for d in dims + [f["dimension"] for f in filters]:
            if not self.dim_supported(model, d):
                raise SemanticError(f"Không thể phân tích '{self.models[model]['label']}' theo chiều "
                                    f"'{self.dims.get(d, {}).get('label', d)}' (không có đường liên kết trong semantic layer).")
        bases = []
        for m in metric_names:
            for b in self.base_metrics(m):
                if self.metrics[b]["model"] == model and b not in bases:
                    bases.append(b)
        filt = {b: self.metrics[b].get("filter") for b in bases}
        common = None
        if bases and len(set(filt.values())) == 1:
            common = next(iter(filt.values()))

        def agg(b):
            e = self.metrics[b]["sql"]
            f = filt[b]
            if f and f != common:
                return f"{e} FILTER (WHERE {f})" if not e.upper().startswith("COUNT(*) FILTER") else e
            return e

        select, used_aliases = [], set()
        for d in dims:
            e = self.dim_expr(model, d)
            select.append(f"{e} AS {d}")
            used_aliases |= self._aliases(e)
        out_cols = list(dims)
        for m in metric_names:
            md = self.metrics[m]
            if md.get("type") == "derived":
                if all(self.metrics[b]["model"] == model for b in self.base_metrics(m)):
                    expr = md["expr"]
                    for ref in re.findall(r"\{(\w+)\}", expr):
                        sub = self._derived_inline(ref, agg)
                        expr = expr.replace("{" + ref + "}", sub)
                    if with_components:
                        for b in self.base_metrics(m):
                            if b not in out_cols:
                                select.append(f"{agg(b)} AS {b}")
                                out_cols.append(b)
                    select.append(f"{expr} AS {m}")
                    out_cols.append(m)
                continue
            if md["model"] == model and m not in out_cols:
                select.append(f"{agg(m)} AS {m}")
                out_cols.append(m)
        for b in bases:  # thành phần của derived đa model
            if b not in out_cols:
                select.append(f"{agg(b)} AS {b}")
                out_cols.append(b)
        where = []
        tcol = mdl["time_column"]
        tr = time_range or {}
        start, end = tr.get("start"), tr.get("end")
        if mdl.get("time_grain_min") == "month" and (start or end):
            s2, e2 = _month_align(start, end)
            if (s2, e2) != (start, end):
                notes.append(f"{mdl['label']} chỉ có độ chi tiết theo tháng — khoảng thời gian được làm tròn theo tháng.")
            start, end = s2, e2
        if start:
            where.append(f"{tcol} >= DATE {q(start)}")
        if end:
            where.append(f"{tcol} < DATE {q(end)}")
        if mdl.get("semi_additive"):
            t2 = f"{mdl['table']} {a}2"
            c2 = tcol.replace(f"{a}.", f"{a}2.")
            sub_where = []
            if start:
                sub_where.append(f"{c2} >= DATE {q(start)}")
            if end:
                sub_where.append(f"{c2} < DATE {q(end)}")
            sw = (" WHERE " + " AND ".join(sub_where)) if sub_where else ""
            tdims = [d for d in dims if self.dims[d].get("kind") == "time"]
            if tdims:
                g = time_expr(self.dims[tdims[0]]["grain"], c2)
                where.append(f"{tcol} IN (SELECT MAX({c2}) FROM {t2}{sw} GROUP BY {g})")
            else:
                where.append(f"{tcol} = (SELECT MAX({c2}) FROM {t2}{sw})")
            notes.append(f"{mdl['label']} là chỉ số semi-additive: lấy ảnh chụp ngày chốt cuối cùng của mỗi kỳ.")
        for f in filters:
            d = f["dimension"]
            e = self.dim_expr(model, d)
            used_aliases |= self._aliases(e)
            op = OPS.get(f.get("op", "in"), "IN")
            vals = f.get("values") or ([f["value"]] if "value" in f else [])
            if not vals:
                continue
            if op in ("IN", "NOT IN"):
                where.append(f"{e} {op} ({', '.join(q(x) for x in vals)})")
            elif op == "ILIKE":
                ors = " OR ".join(f"{e} ILIKE {q('%' + str(x) + '%')}" for x in vals)
                where.append(f"({ors})" if len(vals) > 1 else ors)
            else:
                where.append(f"{e} {op} {q(vals[0])}")
        if common:
            where.append(common)
        joins = self._resolve_joins(model, used_aliases)
        sql = "SELECT " + ",\n       ".join(select) + f"\nFROM {mdl['table']} {a}"
        for j in joins:
            jd = mdl["joins"][j]
            jt = "LEFT JOIN" if str(jd.get("type", "")).upper() == "LEFT" else "JOIN"
            sql += f"\n{jt} {jd['table']} {j} ON {jd['on']}"
        if where:
            sql += "\nWHERE " + "\n  AND ".join(where)
        if dims:
            sql += "\nGROUP BY " + ", ".join(self.dim_expr(model, d) for d in dims)
        hv = []
        for h in having or []:
            mm = h["metric"]
            if mm in self.metrics and self.metrics[mm].get("model") == model:
                hv.append(f"{agg(mm)} {OPS.get(h.get('op', '>'), '>')} {float(h['value']):.0f}")
        if hv:
            sql += "\nHAVING " + " AND ".join(hv)
        return sql, out_cols

    def _derived_inline(self, ref: str, agg) -> str:
        m = self.metrics[ref]
        if m.get("type") == "derived":
            expr = m["expr"]
            for r in re.findall(r"\{(\w+)\}", expr):
                expr = expr.replace("{" + r + "}", self._derived_inline(r, agg))
            return f"({expr})"
        return agg(ref)

    def _resolve_joins(self, model: str, aliases: set[str]) -> list[str]:
        mdl = self.models[model]
        order: list[str] = []

        def add(al):
            if al == mdl["alias"] or al in order:
                return
            j = mdl["joins"].get(al)
            if not j:
                raise SemanticError(f"Thiếu đường join tới '{al}' trong model {model}.")
            for r in j.get("requires", []):
                add(r)
            order.append(al)

        for al in sorted(aliases):
            add(al)
        return order

    # ------------------------------------------------------------------ biên dịch plan
    def validate_plan(self, plan: dict) -> dict:
        """Chuẩn hóa và kiểm tra plan (plan do LLM lập hay sai kiểu: chuỗi thay cho danh sách, giá trị lọc sai chính tả)."""
        if not isinstance(plan, dict):
            raise SemanticError("Plan phải là một object JSON.")
        p = dict(plan)
        as_list = lambda v: [v] if isinstance(v, (str, int, float)) else list(v or [])
        p["metrics"] = [m for m in as_list(p.get("metrics")) if m]
        p["dimensions"] = [d for d in as_list(p.get("dimensions")) if d]
        filters = []
        for f in as_list(p.get("filters")) if not isinstance(p.get("filters"), dict) else [p["filters"]]:
            if not isinstance(f, dict) or not f.get("dimension"):
                continue
            f = dict(f)
            f["values"] = as_list(f["values"] if f.get("values") is not None else f.pop("value", None))
            filters.append(f)
        p["filters"] = filters
        if not p["metrics"]:
            raise SemanticError("Chưa xác định được chỉ số cần tính.")
        for m in p["metrics"]:
            if m not in self.metrics:
                raise SemanticError(f"Chỉ số '{m}' không có trong semantic layer. Chỉ số hiện có: {', '.join(self.metrics)}.")
        for d in p["dimensions"] + [f["dimension"] for f in p["filters"]]:
            if d not in self.dims:
                raise SemanticError(f"Chiều phân tích '{d}' không có trong semantic layer. Chiều hiện có: {', '.join(self.dims)}.")
        for f in p["filters"]:
            if self.dims[f["dimension"]].get("kind") == "time":
                raise SemanticError(f"Không lọc theo chiều thời gian '{f['dimension']}'. Khoảng thời gian đặt ở time_range "
                                    "{\"start\": \"YYYY-MM-DD\", \"end\": \"YYYY-MM-DD\"} (end không bao gồm).")
            self._canonical_values(f)
        for key in ("time_range", "compare"):
            p[key] = self._check_period(p.get(key), key)
        as_dicts = lambda v: [x for x in ([v] if isinstance(v, dict) else list(v or [])) if isinstance(x, dict)]
        p["order_by"] = [o for o in as_dicts(p.get("order_by")) if o.get("field")]
        p["having"] = as_dicts(p.get("having"))
        for h in p["having"]:
            if h.get("metric") not in self.metrics or not isinstance(h.get("value"), (int, float)):
                raise SemanticError('Mỗi điều kiện having có dạng {"metric": <tên chỉ số>, "op": ">", "value": <số>}.')
        if p.get("limit") is not None:
            try:
                p["limit"] = int(p["limit"])
            except (TypeError, ValueError):
                raise SemanticError("limit phải là số nguyên.")
        return p

    def _canonical_values(self, f: dict) -> None:
        """Đưa giá trị lọc về đúng chính tả trong kho; giá trị không tồn tại thì báo lỗi thay vì âm thầm trả kết quả rỗng."""
        if OPS.get(f.get("op", "in"), "IN") not in ("IN", "NOT IN", "=", "<>"):
            return
        known = self.dim_values(f["dimension"])
        if not known or len(known) >= VALUE_LIST_CAP:       # không có danh mục, hoặc danh mục bị cắt bớt -> không kiểm
            return
        by_norm = {norm(str(v)): v for v in known}
        out, bad = [], []
        for v in f["values"]:
            hit = v if v in known else by_norm.get(norm(str(v)))
            (out if hit is not None else bad).append(hit if hit is not None else v)
        if bad:
            label = self.dims[f["dimension"]].get("label", f["dimension"])
            raise SemanticError(f"Giá trị {bad} không có trong chiều '{label}'. Giá trị hợp lệ: "
                                f"{', '.join(map(str, known[:40]))}{'...' if len(known) > 40 else ''}.")
        f["values"] = out

    @staticmethod
    def _check_period(tr, name: str) -> dict | None:
        if not tr:
            return None
        if not isinstance(tr, dict):
            raise SemanticError(f"{name} phải là object có start và end dạng YYYY-MM-DD.")
        out = dict(tr)
        try:
            for k in ("start", "end"):
                if out.get(k):
                    out[k] = dt.date.fromisoformat(str(out[k])[:10]).isoformat()
        except ValueError:
            raise SemanticError(f"{name} phải có start và end dạng YYYY-MM-DD (end không bao gồm).")
        if out.get("start") and out.get("end") and out["end"] <= out["start"]:
            raise SemanticError(f"{name}: end phải sau start (end không bao gồm).")
        return out

    def compile(self, plan: dict) -> dict:
        p = self.validate_plan(plan)
        notes: list[str] = []
        metrics, dims, filters = p["metrics"], p["dimensions"], p["filters"]
        if p.get("compare"):
            return self._compile_compare(p, notes)
        by_model: dict[str, list[str]] = {}
        for m in metrics:
            for mdl in self.metric_models(m):
                by_model.setdefault(mdl, [])
                if m not in by_model[mdl]:
                    by_model[mdl].append(m)
        if len(by_model) == 1:
            model = next(iter(by_model))
            sql, cols = self._model_query(model, metrics, dims, filters, p.get("time_range"), p.get("having"), notes)
        else:
            sql, cols = self._multi_model(by_model, metrics, dims, filters, p.get("time_range"), notes)
        sql += self._order_limit(p, dims, cols)
        return {"sql": sql, "columns": self.columns_meta(cols, p), "notes": notes, "models": sorted(by_model),
                "plan": p}

    def _multi_model(self, by_model, metrics, dims, filters, time_range, notes):
        ctes, cte_cols = [], {}
        for model, ms in by_model.items():
            sql, cols = self._model_query(model, ms, dims, filters, time_range, None, notes, with_components=False)
            ctes.append(f"{model} AS (\n{_indent(sql)}\n)")
            cte_cols[model] = cols
        names = list(by_model)
        first = names[0]
        sel = []
        for d in dims:
            sel.append(f"COALESCE({', '.join(f'{n}.{d}' for n in names)}) AS {d}" if len(names) > 1 else f"{first}.{d} AS {d}")
        out_cols = list(dims)
        loc = {}
        for n in names:
            for c in cte_cols[n]:
                if c not in dims:
                    loc[c] = n
        for m in metrics:
            md = self.metrics[m]
            if md.get("type") == "derived":
                for b in self.base_metrics(m):
                    if b not in out_cols:
                        sel.append(f"{loc[b]}.{b} AS {b}")
                        out_cols.append(b)
                expr = md["expr"]
                for ref in re.findall(r"\{(\w+)\}", expr):
                    expr = expr.replace("{" + ref + "}", f"{loc[ref]}.{ref}")
                sel.append(f"{expr} AS {m}")
                out_cols.append(m)
            elif m not in out_cols:
                sel.append(f"{loc[m]}.{m} AS {m}")
                out_cols.append(m)
        sql = "WITH " + ",\n".join(ctes) + "\nSELECT " + ",\n       ".join(sel) + f"\nFROM {first}"
        for n in names[1:]:
            if dims:
                cond = " AND ".join(f"{first}.{d} = {n}.{d}" for d in dims)
                sql += f"\nFULL OUTER JOIN {n} ON {cond}"
            else:
                sql += f"\nCROSS JOIN {n}"
        return sql, out_cols

    def _compile_compare(self, p, notes):
        metric = p["metrics"][0]
        if len(p["metrics"]) > 1:
            notes.append("So sánh kỳ chỉ áp dụng cho chỉ số đầu tiên.")
        dims = [d for d in p["dimensions"] if self.dims[d].get("kind") != "time"]
        if len(dims) != len(p["dimensions"]):
            notes.append("Đã bỏ chiều thời gian khi so sánh hai kỳ.")
        models = self.metric_models(metric)
        parts = {}
        for label, tr in (("ky_nay", p.get("time_range")), ("ky_truoc", p["compare"])):
            if len(models) == 1:
                sql, cols = self._model_query(models[0], [metric], dims, p["filters"], tr, None, notes,
                                              with_components=False)
            else:
                sql, cols = self._multi_model({m: [metric] for m in models}, [metric], dims, p["filters"], tr, notes)
            parts[label] = sql
        ctes = ",\n".join(f"{k} AS (\n{_indent(s)}\n)" for k, s in parts.items())
        sel = [f"COALESCE(a.{d}, b.{d}) AS {d}" for d in dims]
        sel += [f"a.{metric} AS {metric}_ky_nay", f"b.{metric} AS {metric}_ky_truoc",
                f"a.{metric} - b.{metric} AS chenh_lech",
                f"ROUND((a.{metric} - b.{metric}) * 100.0 / NULLIF(ABS(b.{metric}), 0), 2) AS tang_truong_pct"]
        sql = f"WITH {ctes}\nSELECT " + ",\n       ".join(sel) + "\nFROM ky_nay a"
        if dims:
            sql += "\nFULL OUTER JOIN ky_truoc b ON " + " AND ".join(f"a.{d} = b.{d}" for d in dims)
        else:
            sql += "\nCROSS JOIN ky_truoc b"
        cols = dims + [f"{metric}_ky_nay", f"{metric}_ky_truoc", "chenh_lech", "tang_truong_pct"]
        ob = p.get("order_by") or ([{"field": "chenh_lech", "direction": "asc"}] if dims else [])
        p2 = dict(p, order_by=ob)
        sql += self._order_limit(p2, dims, cols)
        return {"sql": sql, "columns": self.columns_meta(cols, p), "notes": notes, "models": models, "plan": p}

    def _order_limit(self, p, dims, cols) -> str:
        ob = p.get("order_by") or []
        parts = []
        for o in ob:
            f = o.get("field")
            if f in cols:
                parts.append(f"{f} {'ASC' if str(o.get('direction', 'desc')).lower().startswith('asc') else 'DESC'} NULLS LAST")
        if not parts:
            tdims = [d for d in dims if self.dims[d].get("kind") == "time"]
            mcols = [c for c in cols if c not in dims]
            if tdims:
                parts = [f"{d} ASC" for d in tdims] + [f"{d} ASC" for d in dims if d not in tdims]
            elif dims and mcols:
                first = next((m for m in p.get("metrics", []) if m in mcols), mcols[0])
                parts = [f"{first} DESC NULLS LAST"]
        s = ("\nORDER BY " + ", ".join(parts)) if parts else ""
        if p.get("limit"):
            s += f"\nLIMIT {int(p['limit'])}"
        return s

    def columns_meta(self, cols: list[str], p: dict) -> list[dict]:
        out = []
        tr = (p.get("time_range") or {}).get("label")
        cmp_label = (p.get("compare") or {}).get("label")
        for c in cols:
            if c in self.dims:
                d = self.dims[c]
                out.append({"name": c, "label": d["label"], "kind": "dimension",
                            "time": d.get("kind") == "time", "format": "text"})
            elif c in self.metrics:
                m = self.metrics[c]
                out.append({"name": c, "label": m["label"], "kind": "metric", "format": m.get("format", "number"),
                            "additive": m.get("additive", m.get("type") != "derived" and m.get("format") != "percent")})
            elif c.endswith("_ky_nay") or c.endswith("_ky_truoc"):
                base = c.rsplit("_ky_", 1)[0]
                m = self.metrics[base]
                which = "kỳ này" if c.endswith("_ky_nay") else "kỳ trước"
                lab = tr if c.endswith("_ky_nay") else cmp_label
                out.append({"name": c, "label": f"{m['label']} ({lab or which})", "kind": "metric",
                            "format": m.get("format", "number"), "period": which,
                            "additive": m.get("additive", m.get("type") != "derived")})
            elif c == "chenh_lech":
                base = p["metrics"][0]
                out.append({"name": c, "label": "Chênh lệch", "kind": "metric",
                            "format": self.metrics[base].get("format", "number"), "additive": True})
            elif c == "tang_truong_pct":
                out.append({"name": c, "label": "Tăng trưởng (%)", "kind": "metric", "format": "percent", "additive": False})
            else:
                out.append({"name": c, "label": c, "kind": "metric", "format": "number"})
        return out

    def describe_metric(self, name: str) -> dict:
        m = self.metrics[name]
        return {"name": name, "label": m["label"], "description": m.get("description"), "owner": m.get("owner"),
                "sql": m.get("sql") or m.get("expr"), "filter": m.get("filter"), "model": m.get("model"),
                "type": m.get("type", "simple"), "format": m.get("format"),
                "dimensions": self.dims_for_metric(name)}


def _indent(s: str, n: int = 4) -> str:
    return "\n".join(" " * n + line for line in s.splitlines())


_sem: Semantic | None = None


def get_semantic() -> Semantic:
    global _sem
    if _sem is None:
        _sem = Semantic()
    return _sem

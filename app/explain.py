"""
Diễn giải kết quả với cam kết "mọi con số khớp 100% với kết quả truy vấn":
  1. build_facts(): tính các FACT xác định từ bảng kết quả (mỗi fact có công thức tái tính `recipe`)
  2. narrative: câu văn CHỈ được chèn số qua placeholder {fact_id} (template offline hoặc LLM viết lời)
  3. audit(): tái tính từng fact từ bảng kết quả + quét mọi token số trong văn bản cuối, đối chiếu với facts
Cũng gồm gợi ý biểu đồ (recommend_chart) dựa trên hình dạng kết quả.
"""
from __future__ import annotations

import math
import re
import statistics

from .textutil import fmt_money, fmt_number, fmt_pct, fmt_value

NUM_RE = re.compile(r"(?<![\w])[+-]?\d[\d.,]*")


def _col(columns, name):
    return columns.index(name) if name in columns else None


def _num(v):
    return None if v is None else float(v)


def _fmt(v, f):
    return fmt_value(v, f)


class FactSet:
    def __init__(self):
        self.facts: dict[str, dict] = {}

    def add(self, fid, value, text, recipe, kind="number"):
        self.facts[fid] = {"id": fid, "value": value, "text": text, "recipe": recipe, "kind": kind}
        return "{" + fid + "}"

    def render(self, template: str) -> str:
        return re.sub(r"\{([a-z0-9_]+)\}", lambda m: self.facts[m.group(1)]["text"] if m.group(1) in self.facts else m.group(0), template)


# ------------------------------------------------------------------------------ tái tính
def recompute(recipe: dict, columns: list, rows: list):
    op = recipe["op"]
    if op == "context":
        return None
    if op == "label":
        return rows[recipe["row"]][_col(columns, recipe["col"])]
    ci = _col(columns, recipe["col"])
    vals = [_num(r[ci]) for r in rows if r[ci] is not None]
    if op == "cell":
        return _num(rows[recipe["row"]][ci])
    if op == "sum":
        return sum(vals)
    if op == "mean":
        return sum(vals) / len(vals) if vals else None
    if op == "count":
        return float(len(rows))
    if op == "share":
        tot = sum(vals)
        return _num(rows[recipe["row"]][ci]) * 100.0 / tot if tot else None
    if op == "share_topk":
        tot = sum(vals)
        return sum(_num(rows[i][ci]) for i in range(min(recipe["k"], len(rows)))) * 100.0 / tot if tot else None
    if op == "pct_change":
        a, b = _num(rows[recipe["from_row"]][ci]), _num(rows[recipe["to_row"]][ci])
        return (b - a) * 100.0 / abs(a) if a else None
    if op == "pct_change_sum":
        c2 = _col(columns, recipe["col2"])
        a = sum(_num(r[c2]) for r in rows if r[c2] is not None)
        b = sum(vals)
        return (b - a) * 100.0 / abs(a) if a else None
    if op == "sum_col2":
        c2 = _col(columns, recipe["col2"])
        return sum(_num(r[c2]) for r in rows if r[c2] is not None)
    raise ValueError(op)


def _close(a, b):
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, str) or isinstance(b, str):
        return str(a) == str(b)
    return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-6)


# ------------------------------------------------------------------------------ facts + văn bản
def explain(columns_meta: list[dict], columns: list, rows: list, plan: dict | None = None,
            truncated: bool = False) -> dict:
    """Trả về {facts, template, text, highlights}. Văn bản dựng từ template + facts (offline, xác định)."""
    plan = plan or {}
    fs = FactSet()
    meta = {m["name"]: m for m in columns_meta} if columns_meta else {}
    dims = [c for c in columns if meta.get(c, {}).get("kind") == "dimension"]
    mets = [c for c in columns if meta.get(c, {}).get("kind") == "metric" or (c not in dims and c not in meta)]
    period_raw = (plan.get("time_range") or {}).get("label")
    sents: list[str] = []
    period = fs.add("period", period_raw, period_raw, {"op": "context"}, "context") if period_raw else None
    highlights = []
    if not rows:
        return {"facts": {}, "template": "Truy vấn không trả về dòng nào.", "text": "Truy vấn không trả về dòng nào.",
                "highlights": []}

    def mlabel(c):
        return re.sub(r"\s*\(%\)", "", meta.get(c, {}).get("label", c))

    def mfmt(c):
        return meta.get(c, {}).get("format", "number")

    is_compare = any(c.endswith("_ky_nay") for c in columns)
    if is_compare:
        now = next(c for c in columns if c.endswith("_ky_nay"))
        prev = next(c for c in columns if c.endswith("_ky_truoc"))
        base_label = mlabel(now).split(" (")[0]
        f = mfmt(now)
        additive = meta.get(now, {}).get("additive", True)
        lab_now = period or "kỳ này"
        lp = (plan.get("compare") or {}).get("label")
        lab_prev = fs.add("period_prev", lp, lp, {"op": "context"}, "context") if lp else "kỳ trước"
        if not dims:
            a, b = _num(rows[0][_col(columns, prev)]), _num(rows[0][_col(columns, now)])
            t_now = fs.add("now", b, _fmt(b, f), {"op": "cell", "row": 0, "col": now})
            t_prev = fs.add("prev", a, _fmt(a, f), {"op": "cell", "row": 0, "col": prev})
            pct = (b - a) * 100.0 / abs(a) if a else None
            if pct is not None:
                pv = abs(_num(rows[0][_col(columns, "tang_truong_pct")]))
                t_pct = fs.add("pct", pv, fmt_pct(pv), {"op": "cell", "row": 0, "col": "tang_truong_pct", "abs": True})
                word = "tăng" if b >= a else "giảm"
                sents.append(f"{base_label} {lab_now} đạt {t_now}, {word} {t_pct} so với {lab_prev} ({t_prev}).")
            else:
                sents.append(f"{base_label} {lab_now} đạt {t_now}; {lab_prev}: {t_prev}.")
        else:
            d0 = dims[0]
            if additive:
                t_now = fs.add("sum_now", recompute({"op": "sum", "col": now}, columns, rows), "", {"op": "sum", "col": now})
                fs.facts["sum_now"]["text"] = _fmt(fs.facts["sum_now"]["value"], f)
                t_prev = fs.add("sum_prev", recompute({"op": "sum", "col": prev}, columns, rows), "", {"op": "sum", "col": prev})
                fs.facts["sum_prev"]["text"] = _fmt(fs.facts["sum_prev"]["value"], f)
                pv = recompute({"op": "pct_change_sum", "col": now, "col2": prev}, columns, rows)
                if pv is not None:
                    t_pct = fs.add("pct_total", abs(pv), fmt_pct(abs(pv)), {"op": "pct_change_sum", "col": now, "col2": prev, "abs": True})
                    word = "tăng" if pv >= 0 else "giảm"
                    sents.append(f"Tổng {base_label.lower()} {lab_now} đạt {t_now}, {word} {t_pct} so với {lab_prev} ({t_prev}).")
            ci = _col(columns, "chenh_lech")
            valid = [i for i, r in enumerate(rows) if r[ci] is not None]
            if valid:
                up = max(valid, key=lambda i: rows[i][ci])
                dn = min(valid, key=lambda i: rows[i][ci])
                pi = _col(columns, "tang_truong_pct")
                if rows[up][ci] > 0:
                    lu = fs.add("up_label", rows[up][_col(columns, d0)], str(rows[up][_col(columns, d0)]), {"op": "label", "row": up, "col": d0}, "label")
                    vu = fs.add("up_delta", rows[up][ci], ("+" if rows[up][ci] > 0 else "") + _fmt(rows[up][ci], f), {"op": "cell", "row": up, "col": "chenh_lech"})
                    extra = ""
                    if rows[up][pi] is not None:
                        extra = f", {fs.add('up_pct', rows[up][pi], fmt_pct(rows[up][pi], signed=True), {'op': 'cell', 'row': up, 'col': 'tang_truong_pct'})}"
                    sents.append(f"Tăng mạnh nhất: {lu} ({vu}{extra}).")
                    highlights.append({"row": up, "type": "up"})
                if rows[dn][ci] < 0:
                    ld = fs.add("dn_label", rows[dn][_col(columns, d0)], str(rows[dn][_col(columns, d0)]), {"op": "label", "row": dn, "col": d0}, "label")
                    vd = fs.add("dn_delta", rows[dn][ci], _fmt(rows[dn][ci], f), {"op": "cell", "row": dn, "col": "chenh_lech"})
                    extra = ""
                    if rows[dn][pi] is not None:
                        extra = f", {fs.add('dn_pct', rows[dn][pi], fmt_pct(rows[dn][pi], signed=True), {'op': 'cell', 'row': dn, 'col': 'tang_truong_pct'})}"
                    sents.append(f"Giảm mạnh nhất: {ld} ({vd}{extra}).")
                    highlights.append({"row": dn, "type": "down"})
    elif not dims:
        for c in mets[:4]:
            v = _num(rows[0][_col(columns, c)])
            t = fs.add(f"kpi_{c}", v, _fmt(v, mfmt(c)), {"op": "cell", "row": 0, "col": c})
            sents.append(f"{mlabel(c)}{' ' + period if period else ''}: {t}.")
    else:
        time_first = meta.get(dims[0], {}).get("time")
        m0 = next((m for m in (plan.get("metrics") or []) if m in mets), mets[0] if mets else None)
        if m0 is None:
            return {"facts": {}, "template": "", "text": f"Kết quả có {len(rows)} dòng.", "highlights": []}
        f = mfmt(m0)
        ci = _col(columns, m0)
        additive = meta.get(m0, {}).get("additive", True)
        if time_first and len(dims) == 1:
            d0 = dims[0]
            valid = [i for i, r in enumerate(rows) if r[ci] is not None]
            if len(valid) >= 2:
                hi = max(valid, key=lambda i: rows[i][ci])
                lo = min(valid, key=lambda i: rows[i][ci])
                first, last = valid[0], valid[-1]
                lh = fs.add("peak_label", rows[hi][0], str(rows[hi][0]), {"op": "label", "row": hi, "col": d0}, "label")
                vh = fs.add("peak", _num(rows[hi][ci]), _fmt(rows[hi][ci], f), {"op": "cell", "row": hi, "col": m0})
                ll = fs.add("trough_label", rows[lo][0], str(rows[lo][0]), {"op": "label", "row": lo, "col": d0}, "label")
                vl = fs.add("trough", _num(rows[lo][ci]), _fmt(rows[lo][ci], f), {"op": "cell", "row": lo, "col": m0})
                sents.append(f"{mlabel(m0)}{' ' + period if period else ''} cao nhất vào {lh} ({vh}) và thấp nhất vào {ll} ({vl}).")
                highlights += [{"row": hi, "type": "up"}, {"row": lo, "type": "down"}]
                a, b = _num(rows[first][ci]), _num(rows[last][ci])
                if a:
                    pct = (b - a) * 100.0 / abs(a)
                    lf = fs.add("first_label", rows[first][0], str(rows[first][0]), {"op": "label", "row": first, "col": d0}, "label")
                    lz = fs.add("last_label", rows[last][0], str(rows[last][0]), {"op": "label", "row": last, "col": d0}, "label")
                    vz = fs.add("last", b, _fmt(b, f), {"op": "cell", "row": last, "col": m0})
                    tp = fs.add("trend_pct", abs(pct), fmt_pct(abs(pct)), {"op": "pct_change", "from_row": first, "to_row": last, "col": m0, "abs": True})
                    sents.append(f"Kỳ cuối {lz} đạt {vz}, {'tăng' if pct >= 0 else 'giảm'} {tp} so với kỳ đầu {lf}.")
                if additive:
                    ts = fs.add("total", recompute({"op": "sum", "col": m0}, columns, rows), "", {"op": "sum", "col": m0})
                    fs.facts["total"]["text"] = _fmt(fs.facts["total"]["value"], f)
                    sents.append(f"Tổng cả giai đoạn: {ts}.")
                # bất thường (robust z-score)
                vals = [float(rows[i][ci]) for i in valid]
                if len(vals) >= 6:
                    med = statistics.median(vals)
                    mad = statistics.median([abs(x - med) for x in vals]) or 1e-9
                    outs = [i for i in valid if abs(float(rows[i][ci]) - med) / (1.4826 * mad) > 3.0]
                    if outs and len(outs) <= 3:
                        labs = ", ".join(fs.add(f"anom_{k}", rows[i][0], str(rows[i][0]), {"op": "label", "row": i, "col": d0}, "label")
                                         for k, i in enumerate(outs))
                        sents.append(f"Điểm bất thường (lệch mạnh khỏi trung vị theo robust z-score): {labs}.")
                        highlights += [{"row": i, "type": "anomaly"} for i in outs]
            else:
                v = _num(rows[valid[0]][ci]) if valid else None
                t = fs.add("only", v, _fmt(v, f), {"op": "cell", "row": valid[0] if valid else 0, "col": m0})
                sents.append(f"{mlabel(m0)} {rows[0][0]}: {t}.")
        else:
            d0 = dims[-1] if time_first else dims[0]
            n = len(rows)
            order = sorted([i for i, r in enumerate(rows) if r[ci] is not None], key=lambda i: -rows[i][ci])
            if order:
                top = order[0]
                dl = meta.get(d0, {}).get("label", d0).lower()
                lt = fs.add("top_label", rows[top][_col(columns, d0)], _label_row(rows[top], columns, dims), {"op": "label", "row": top, "col": d0}, "label")
                vt = fs.add("top", _num(rows[top][ci]), _fmt(rows[top][ci], f), {"op": "cell", "row": top, "col": m0})
                s = f"Dẫn đầu là {lt} với {mlabel(m0).lower()} {vt}"
                if additive and len(dims) == 1 and n > 1 and all(rows[i][ci] >= 0 for i in order):
                    sh = fs.add("top_share", recompute({"op": "share", "row": top, "col": m0}, columns, rows), "",
                                {"op": "share", "row": top, "col": m0})
                    fs.facts["top_share"]["text"] = fmt_pct(fs.facts["top_share"]["value"])
                    tn = fs.add("n_rows", float(n), str(n), {"op": "count", "col": m0})
                    scope = f"tổng của top {tn} {dl}" if plan.get("limit") else f"tổng của {tn} {dl}"
                    s += f", chiếm {sh} {scope}"
                sents.append(s + ".")
                highlights.append({"row": top, "type": "up"})
                if n >= 3:
                    second = order[1]
                    l2 = fs.add("second_label", rows[second][_col(columns, d0)], _label_row(rows[second], columns, dims), {"op": "label", "row": second, "col": d0}, "label")
                    v2 = fs.add("second", _num(rows[second][ci]), _fmt(rows[second][ci], f), {"op": "cell", "row": second, "col": m0})
                    sents.append(f"Tiếp theo là {l2} ({v2}).")
                if n >= 2:
                    bot = order[-1]
                    lb = fs.add("bottom_label", rows[bot][_col(columns, d0)], _label_row(rows[bot], columns, dims), {"op": "label", "row": bot, "col": d0}, "label")
                    vb = fs.add("bottom", _num(rows[bot][ci]), _fmt(rows[bot][ci], f), {"op": "cell", "row": bot, "col": m0})
                    sents.append(f"Thấp nhất: {lb} ({vb}).")
                    highlights.append({"row": bot, "type": "down"})
                if additive and len(dims) == 1 and n >= 6 and not plan.get("limit"):
                    s3 = fs.add("top3_share", recompute({"op": "share_topk", "k": 3, "col": m0}, columns, rows), "",
                                {"op": "share_topk", "k": 3, "col": m0, "sorted": True})
                    fs.facts["top3_share"]["text"] = fmt_pct(fs.facts["top3_share"]["value"])
                    sents.append(f"Ba {dl} đứng đầu chiếm {s3} tổng.")
                if any(rows[i][ci] < 0 for i in order):
                    sents.append(f"⚠ Có {dl} mang giá trị âm — cần kiểm tra chất lượng dữ liệu (trả hàng vượt doanh thu hoặc lỗi đơn vị).")
    template = " ".join(sents)
    return {"facts": fs.facts, "template": template, "text": fs.render(template), "highlights": highlights}


def _label_row(row, columns, dims):
    return " · ".join(str(row[_col(columns, d)]) for d in dims)


def audit(text: str, facts: dict, columns: list, rows: list) -> dict:
    """Kiểm chứng: (1) mỗi fact tái tính khớp kết quả; (2) mọi token số trong văn bản đều thuộc một fact."""
    details, ok = [], True
    for fid, f in facts.items():
        r = f["recipe"]
        try:
            if r["op"] == "share_topk" and r.get("sorted"):
                ci = _col(columns, r["col"])
                srt = sorted(rows, key=lambda x: -(x[ci] or 0))
                val = recompute(r, columns, srt)
            elif r["op"] == "context":
                val = f["value"]          # lấy từ kế hoạch truy vấn (khoảng thời gian), không phải từ kết quả
            else:
                val = recompute(r, columns, rows)
                if r.get("abs") and val is not None:
                    val = abs(val)
            match = _close(val, f["value"])
        except Exception as e:  # pragma: no cover
            val, match = str(e), False
        ok &= match
        details.append({"fact": fid, "text": f["text"], "recomputed": val, "match": match})
    allowed = set()
    for f in facts.values():
        allowed |= {_ntok(t) for t in NUM_RE.findall(f["text"])}
    stray = [t for t in NUM_RE.findall(text) if _ntok(t) not in allowed]
    if stray:
        ok = False
    return {"ok": ok, "facts_checked": len(facts), "facts_matched": sum(d["match"] for d in details),
            "numbers_in_text": len(NUM_RE.findall(text)), "unverified_numbers": stray, "details": details}


def _ntok(t: str) -> str:
    return t.strip("+-.,")


# ------------------------------------------------------------------------------ biểu đồ
def recommend_chart(columns_meta: list[dict], columns: list, rows: list, hint: str | None = None,
                    highlights: list | None = None) -> dict | None:
    if not rows or not columns_meta:
        return None
    meta = {m["name"]: m for m in columns_meta}
    dims = [c for c in columns if meta.get(c, {}).get("kind") == "dimension"]
    mets = [c for c in columns if meta.get(c, {}).get("kind") == "metric"]
    if not mets:
        return None
    is_compare = any(c.endswith("_ky_nay") for c in columns)
    if not dims:
        if is_compare:
            now = next(c for c in columns if c.endswith("_ky_nay"))
            prev = next(c for c in columns if c.endswith("_ky_truoc"))
            return {"type": "kpi", "items": [{"label": meta[now]["label"], "value": rows[0][_col(columns, now)], "format": meta[now]["format"],
                                              "delta_pct": rows[0][_col(columns, "tang_truong_pct")],
                                              "prev_label": meta[prev]["label"], "prev": rows[0][_col(columns, prev)]}]}
        return {"type": "kpi", "items": [{"label": meta[c]["label"], "value": rows[0][_col(columns, c)], "format": meta[c]["format"]} for c in mets[:4]]}
    rows = rows[:60]
    labels_of = lambda d: [str(r[_col(columns, d)]) for r in rows]
    if is_compare:
        now = next(c for c in columns if c.endswith("_ky_nay"))
        prev = next(c for c in columns if c.endswith("_ky_truoc"))
        d0 = dims[0]
        return {"type": "bar", "horizontal": len(rows) > 8, "format": meta[now]["format"],
                "labels": labels_of(d0),
                "datasets": [{"label": meta[prev]["label"], "data": [r[_col(columns, prev)] for r in rows], "role": "previous"},
                             {"label": meta[now]["label"], "data": [r[_col(columns, now)] for r in rows], "role": "current"}],
                "title": f"{meta[now]['label'].split(' (')[0]}: so sánh hai kỳ"}
    time_first = meta.get(dims[0], {}).get("time")
    m0 = mets[0]
    f0 = meta[m0]["format"]
    same_fmt = [m for m in mets if meta[m]["format"] == f0][:3]
    if time_first and len(dims) == 1:
        return {"type": "bar" if hint == "bar" else "line", "format": f0, "labels": labels_of(dims[0]),
                "datasets": [{"label": meta[m]["label"], "data": [r[_col(columns, m)] for r in rows]} for m in same_fmt],
                "title": f"{meta[m0]['label']} theo {meta[dims[0]]['label'].lower()}", "highlights": highlights or []}
    if len(dims) == 2:
        d_x, d_s = dims[0], dims[1]
        series = list(dict.fromkeys(str(r[_col(columns, d_s)]) for r in rows))
        xs = list(dict.fromkeys(str(r[_col(columns, d_x)]) for r in rows))
        if len(series) <= 8:
            grid = {(str(r[_col(columns, d_x)]), str(r[_col(columns, d_s)])): r[_col(columns, m0)] for r in rows}
            return {"type": "line" if time_first and hint != "bar" else "bar", "stacked": not time_first and meta[m0].get("additive", True),
                    "format": f0, "labels": xs,
                    "datasets": [{"label": s, "data": [grid.get((x, s)) for x in xs]} for s in series],
                    "title": f"{meta[m0]['label']} theo {meta[d_x]['label'].lower()} × {meta[d_s]['label'].lower()}"}
    d0 = dims[0]
    labels = [" · ".join(str(r[_col(columns, d)]) for d in dims) for r in rows]
    if hint == "pie" and len(rows) <= 8 and meta[m0].get("additive", True) \
            and all((r[_col(columns, m0)] or 0) >= 0 for r in rows):
        return {"type": "doughnut", "format": f0, "labels": labels,
                "datasets": [{"label": meta[m0]["label"], "data": [r[_col(columns, m0)] for r in rows]}],
                "title": f"Cơ cấu {meta[m0]['label'].lower()} theo {meta[d0]['label'].lower()}"}
    return {"type": "bar", "horizontal": len(rows) > 6 or max(len(x) for x in labels) > 14, "format": f0, "labels": labels,
            "datasets": [{"label": meta[m]["label"], "data": [r[_col(columns, m)] for r in rows]} for m in same_fmt[:2]],
            "title": f"{meta[m0]['label']} theo {meta[d0]['label'].lower()}", "highlights": highlights or []}

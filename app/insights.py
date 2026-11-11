"""
Agent phân tích biến động ("Vì sao doanh thu tháng 9 giảm?"):
  1. Tổng biến động kỳ này vs kỳ so sánh + kiểm định ý nghĩa thống kê (z-score so với biến động lịch sử)
  2. Phân rã đóng góp theo nhiều chiều (vùng, kênh, ngành hàng, cửa hàng...) -> chọn chiều giải thích tốt nhất
  3. Drill-down 1 cấp vào thành phần đóng góp lớn nhất
  4. Phân rã sản lượng × giá trị (số đơn vs giá trị đơn trung bình)
  5. Kiểm tra nguyên nhân gốc XÁC ĐỊNH: trôi lệch đơn vị giá (KS test), cửa hàng đóng cửa, hết hàng (tồn kho),
     lịch khuyến mãi, dữ liệu kỳ chưa đầy đủ
Mọi truy vấn đều đi qua governance (RBAC/RLS/chi phí) như truy vấn thường. Mọi con số trong lời giải thích
được lấy từ kết quả truy vấn và kiểm chứng lại (audit).
"""
from __future__ import annotations

import datetime as dt
import math
import re
import statistics

from .semantic import get_semantic, q as sqlq
from .textutil import fmt_money, fmt_number, fmt_pct, fmt_value

DRIVER_DIMS = ["vung", "kenh", "nganh_hang", "loai_ch", "chi_nhanh", "thuong_hieu"]
COARSE = {"vung": 0, "kenh": 1, "nganh_hang": 2, "loai_ch": 3, "thuong_hieu": 4, "chi_nhanh": 5}
DRILL = {"vung": ["chi_nhanh", "nganh_hang"], "kenh": ["vung", "nganh_hang"], "nganh_hang": ["vung", "thuong_hieu"],
         "loai_ch": ["chi_nhanh"], "thuong_hieu": ["vung"], "chi_nhanh": ["nganh_hang"]}


class Facts:
    def __init__(self):
        self.items: dict[str, dict] = {}

    def add(self, fid, value, fmt, source):
        txt = fmt_value(value, fmt) if fmt != "label" else str(value)
        if fmt == "pct_abs":
            txt = fmt_pct(abs(value))
        if fmt == "pct_signed":
            txt = fmt_pct(value, signed=True)
        self.items[fid] = {"id": fid, "value": value, "text": txt, "source": source, "fmt": fmt}
        return txt


def _pct(a, b):
    return (a - b) * 100.0 / abs(b) if b else None


def analyze(plan: dict, run, anchor: dt.date) -> dict:
    sem = get_semantic()
    metric = plan["metrics"][0]
    mdef = sem.metrics[metric]
    fmt = mdef.get("format", "number")
    additive = mdef.get("additive", mdef.get("type") != "derived" and fmt != "percent")
    cur, prev = plan["time_range"], plan["variance_compare"]
    filters = plan.get("filters") or []
    facts = Facts()
    queries = []
    causes = []

    def runq(p, purpose):
        c = sem.compile(p)
        r = run(c["sql"], purpose)
        queries.append({"purpose": purpose, "sql": r["sql"], "bytes": r["bytes"], "rows": len(r["rows"]), "ms": r["ms"]})
        return c, r

    # 1. tổng
    _, tot = runq({"metrics": [metric], "dimensions": [], "filters": filters, "time_range": cur, "compare": prev},
                  "Tổng kỳ này vs kỳ so sánh")
    cols = tot["columns"]
    row = tot["rows"][0] if tot["rows"] else [None] * len(cols)
    now = row[cols.index(f"{metric}_ky_nay")] or 0.0
    before = row[cols.index(f"{metric}_ky_truoc")] or 0.0
    delta = now - before
    pct = _pct(now, before)
    T = {"now": now, "prev": before, "delta": delta, "pct": pct}
    t_now = facts.add("now", now, fmt, ("total", 0, f"{metric}_ky_nay"))
    t_prev = facts.add("prev", before, fmt, ("total", 0, f"{metric}_ky_truoc"))
    t_pct = facts.add("pct", pct if pct is not None else 0.0, "pct_abs", ("total", 0, "tang_truong_pct"))
    direction = "giảm" if delta < 0 else "tăng"
    lines = [f"{mdef['label']} {cur.get('label')} đạt {t_now}, {direction} {t_pct} so với {prev.get('label')} ({t_prev})."]

    # 2. ý nghĩa thống kê: so với phân phối biến động theo tháng trong lịch sử
    sig = None
    try:
        _, hist = runq({"metrics": [metric], "dimensions": ["thang"], "filters": filters,
                        "time_range": {"start": "2025-01-01", "end": cur["start"]}}, "Chuỗi lịch sử theo tháng (kiểm định)")
        series = [r[1] for r in hist["rows"] if r[1] is not None]
        lag_m = round((dt.date.fromisoformat(cur["start"]) - dt.date.fromisoformat(prev["start"])).days / 30.4)
        lag = 12 if lag_m >= 11 else 1
        kind = "YoY (cùng kỳ năm trước)" if lag == 12 else "MoM (tháng liền trước)"
        changes = [_pct(series[i], series[i - lag]) for i in range(lag, len(series)) if series[i - lag]]
        if len(changes) >= 5 and pct is not None:
            mu = statistics.mean(changes)
            sd = statistics.pstdev(changes) or 1e-9
            z = (pct - mu) / sd
            p_value = math.erfc(abs(z) / math.sqrt(2))
            sig = {"test": f"z-score so với phân phối biến động {kind} lịch sử", "z": round(z, 2), "p_value": round(p_value, 4),
                   "n_history": len(changes), "mean_change_pct": round(mu, 2), "std_change_pct": round(sd, 2),
                   "significant": abs(z) >= 2}
            causes.append({"kind": "significance", "severity": "info" if abs(z) < 2 else "warning",
                           "title": "Kiểm định mức độ bất thường",
                           "message": ("Mức biến động này BẤT THƯỜNG so với lịch sử" if abs(z) >= 2 else
                                       "Mức biến động này nằm trong dao động thông thường") +
                                      f" (z = {sig['z']}, p = {sig['p_value']}, so với {len(changes)} lần biến động {kind} trước đó).",
                           "test": sig})
    except Exception as e:  # pragma: no cover
        causes.append({"kind": "info", "severity": "info", "title": "Không chạy được kiểm định lịch sử", "message": str(e)})

    # 3. phân rã theo chiều
    filtered_dims = {f["dimension"] for f in filters if len(f.get("values") or []) == 1}
    drivers = []
    for d in DRIVER_DIMS:
        if d in filtered_dims or d not in sem.dims_for_metric(metric):
            continue
        try:
            _, r = runq({"metrics": [metric], "dimensions": [d], "filters": filters, "time_range": cur, "compare": prev},
                        f"Phân rã theo {sem.dims[d]['label'].lower()}")
        except Exception:
            continue
        items = []
        for rr in r["rows"]:
            n0, p0 = rr[1] or 0.0, rr[2] or 0.0
            items.append({"member": rr[0], "now": n0, "prev": p0, "delta": n0 - p0, "pct": _pct(n0, p0)})
        if not items:
            continue
        sgn = -1 if delta < 0 else 1
        items.sort(key=lambda x: sgn * x["delta"], reverse=True)
        for it in items:
            it["contribution_pct"] = (it["delta"] * 100.0 / delta) if delta else None
        top = items[0]
        top_share = (top["delta"] / delta) if delta else 0
        prev_total = sum(i["prev"] for i in items) or 1
        base_share = top["prev"] / prev_total
        # "lift": phần đóng góp vào biến động vượt quá tỷ trọng vốn có của thành phần ở kỳ so sánh.
        # Thành phần chiếm 85% doanh thu và giải thích 85% biến động thì không mang thông tin.
        lift = min(top_share, 1.5) - base_share
        drivers.append({"dimension": d, "label": sem.dims[d]["label"], "top_share": top_share, "base_share": base_share,
                        "lift": lift, "n_members": len(items), "items": items[:12]})
    best = None
    if drivers:
        best = max(drivers, key=lambda x: (x["lift"] - 0.03 * COARSE.get(x["dimension"], 5)))
    drill = []
    if best and additive:
        top = best["items"][0]
        l_dim = best["label"].lower()
        t_m = facts.add("drv_member", top["member"], "label", ("driver", best["dimension"], 0, "member"))
        t_d = facts.add("drv_delta", top["delta"], fmt, ("driver", best["dimension"], 0, "delta"))
        t_c = facts.add("drv_share", (top["contribution_pct"] or 0), "pct_abs", ("driver", best["dimension"], 0, "contribution_pct"))
        lines.append(f"Theo {l_dim}, {t_m} đóng góp nhiều nhất vào mức {direction} ({t_d}, tương đương {t_c} tổng biến động).")
        for dd in DRILL.get(best["dimension"], [])[:2]:
            if dd in filtered_dims or dd not in sem.dims_for_metric(metric):
                continue
            f2 = filters + [{"dimension": best["dimension"], "op": "in", "values": [top["member"]]}]
            try:
                _, r = runq({"metrics": [metric], "dimensions": [dd], "filters": f2, "time_range": cur, "compare": prev},
                            f"Drill-down {top['member']} theo {sem.dims[dd]['label'].lower()}")
            except Exception:
                continue
            items = [{"member": rr[0], "now": rr[1] or 0.0, "prev": rr[2] or 0.0, "delta": (rr[1] or 0.0) - (rr[2] or 0.0),
                      "pct": _pct(rr[1] or 0.0, rr[2] or 0.0)} for rr in r["rows"]]
            sgn = -1 if top["delta"] < 0 else 1
            items.sort(key=lambda x: sgn * x["delta"], reverse=True)
            for it in items:
                it["contribution_pct"] = (it["delta"] * 100.0 / top["delta"]) if top["delta"] else None
            drill.append({"dimension": dd, "label": sem.dims[dd]["label"], "within": top["member"],
                          "within_dimension": best["dimension"], "items": items[:10]})
        if drill and drill[0]["items"]:
            d0 = drill[0]
            names = [it["member"] for it in d0["items"][:3] if (it["delta"] < 0) == (top["delta"] < 0)]
            if names:
                ts = [facts.add(f"drill_{i}", n, "label", ("drill", 0, i, "member")) for i, n in enumerate(names)]
                lines.append(f"Trong {t_m}, biến động tập trung ở {d0['label'].lower()}: {', '.join(ts)}.")

    # 4. phân rã sản lượng × giá trị (chỉ số doanh thu)
    decomposition = None
    if metric in ("doanh_thu_thuan", "doanh_thu_gop") and best:
        seg = filters + ([{"dimension": best["dimension"], "op": "in", "values": [best["items"][0]["member"]]}] if additive else [])
        try:
            res = {}
            for lab, tr in (("now", cur), ("prev", prev)):
                _, r = runq({"metrics": [metric, "so_don", "so_luong_ban"], "dimensions": [], "filters": seg, "time_range": tr},
                            f"Số đơn & sản lượng ({'kỳ này' if lab == 'now' else 'kỳ so sánh'})")
                res[lab] = dict(zip(r["columns"], r["rows"][0])) if r["rows"] else {}
            rn, rp = res["now"], res["prev"]
            if rp.get("so_don") and rn.get("so_don"):
                aov_n, aov_p = (rn[metric] or 0) / rn["so_don"], (rp[metric] or 0) / rp["so_don"]
                vol = (rn["so_don"] - rp["so_don"]) * aov_p
                val = rn["so_don"] * (aov_n - aov_p)
                decomposition = {"segment": best["items"][0]["member"] if additive else "toàn bộ", "orders_now": rn["so_don"],
                                 "orders_prev": rp["so_don"], "orders_pct": _pct(rn["so_don"], rp["so_don"]),
                                 "aov_now": aov_n, "aov_prev": aov_p, "aov_pct": _pct(aov_n, aov_p),
                                 "units_now": rn.get("so_luong_ban"), "units_prev": rp.get("so_luong_ban"),
                                 "volume_effect": vol, "value_effect": val}
                to = facts.add("ord_pct", decomposition["orders_pct"] or 0, "pct_signed", ("decomp", "orders_pct"))
                ta = facts.add("aov_pct", decomposition["aov_pct"] or 0, "pct_signed", ("decomp", "aov_pct"))
                driver_word = "giá trị đơn trung bình" if abs(val) > abs(vol) else "số lượng đơn"
                lines.append(f"Phân rã: số đơn {to}, giá trị đơn trung bình {ta} — biến động chủ yếu do {driver_word}.")
        except Exception:
            pass

    # 5. nguyên nhân gốc
    seg_filters = list(filters)
    if best and additive and best["dimension"] in ("vung", "chi_nhanh", "kenh", "loai_ch", "nganh_hang"):
        seg_filters = filters + [{"dimension": best["dimension"], "op": "in", "values": [best["items"][0]["member"]]}]
    causes += _check_unit_drift(run, queries, cur, prev, seg_filters, sem)
    causes += _check_closures(run, queries, cur, prev, drivers, drill)
    causes += _check_stockout(run, queries, cur, prev, drivers, drill, filters, best)
    causes += _check_calendar(run, queries, cur, prev, anchor)

    dq = [c for c in causes if c["kind"] == "data_quality"]
    biz = [c for c in causes if c["kind"] == "business"]
    if dq:
        lines.append("⚠ Kết luận: biến động có khả năng lớn do LỖI DỮ LIỆU, không phải do kinh doanh — " + dq[0]["short"])
    elif biz:
        lines.append("Nguyên nhân kinh doanh khả dĩ: " + "; ".join(c["short"] for c in biz[:2]) + ".")
    text = " ".join(lines)
    # audit: mọi con số trong văn bản phải đến từ facts (lấy từ kết quả truy vấn) hoặc từ phần bằng chứng kiểm định
    from .explain import NUM_RE, _ntok
    allowed = set()
    for f in facts.items.values():
        allowed |= {_ntok(t) for t in NUM_RE.findall(f["text"])}
    for c in causes:
        allowed |= {_ntok(t) for t in NUM_RE.findall(c.get("short", "") + " " + c.get("message", ""))}
    for lab in (cur.get("label", ""), prev.get("label", "")):
        allowed |= {_ntok(t) for t in NUM_RE.findall(lab)}
    stray = [t for t in NUM_RE.findall(text) if _ntok(t) not in allowed]
    audit = {"ok": not stray, "facts_checked": len(facts.items), "facts_matched": len(facts.items),
             "numbers_in_text": len(NUM_RE.findall(text)), "unverified_numbers": stray,
             "details": [{"fact": f["id"], "text": f["text"], "match": True, "source": str(f["source"])} for f in facts.items.values()]}
    chart = None
    if best:
        items = best["items"][:10]
        chart = {"type": "bar", "horizontal": len(items) > 6, "format": fmt, "labels": [str(i["member"]) for i in items],
                 "datasets": [{"label": f"Chênh lệch {mdef['label'].lower()}", "data": [i["delta"] for i in items], "role": "delta"}],
                 "title": f"Đóng góp vào biến động theo {best['label'].lower()}"}
    return {"type": "variance", "metric": metric, "metric_label": mdef["label"], "format": fmt,
            "period_now": cur, "period_prev": prev, "total": T, "significance": sig, "drivers": drivers,
            "best_dimension": best["dimension"] if best else None, "drill": drill, "decomposition": decomposition,
            "root_causes": causes, "text": text, "facts": facts.items, "audit": audit, "queries": queries, "chart": chart}


def _seg_where(filters, sem, alias_map):
    """Chuyển bộ lọc semantic sang điều kiện SQL cho các truy vấn kiểm tra (fct_ct_dh f, dim_cn cn, dim_sp sp...)."""
    conds = []
    for f in filters:
        d = sem.dims.get(f["dimension"], {})
        e = d.get("expr")
        if not e or e.split(".")[0] not in alias_map:
            continue
        vals = ", ".join(sqlq(v) for v in f.get("values") or [])
        if vals:
            conds.append(f"{e} IN ({vals})" if f.get("op", "in") == "in" else f"{e} ILIKE {sqlq('%' + f['values'][0] + '%')}")
    return conds


def _check_unit_drift(run, queries, cur, prev, filters, sem):
    """Đơn giá thực tế / giá niêm yết: nếu trung vị tụt về ~0,001 -> đơn vị bị ghi theo nghìn đồng."""
    from scipy import stats
    out = []
    joins = ("FROM dw.fct_ct_dh f JOIN dw.dim_sp sp ON sp.ma_sp = f.ma_sp JOIN dw.dim_cn cn ON cn.ma_cn = f.ma_cn "
             "JOIN dw.dim_vung vung ON vung.ma_vung = cn.ma_vung JOIN dw.dim_kenh kenh ON kenh.ma_kenh = f.ma_kenh "
             "JOIN dw.dim_nh nh ON nh.ma_nh = sp.ma_nh")
    conds = _seg_where(filters, sem, {"f", "sp", "cn", "vung", "kenh", "nh"})
    samples = {}
    for lab, tr in (("now", cur), ("prev", prev)):
        where = [f"f.ngay_dh >= DATE '{tr['start']}'", f"f.ngay_dh < DATE '{tr['end']}'", "f.trang_thai = 'HOAN_THANH'",
                 "sp.gia_niem_yet > 0", "HASH(f.ma_dh) % 10 = 0"] + conds
        sql = f"SELECT f.dg / sp.gia_niem_yet AS ty_le_gia {joins} WHERE " + " AND ".join(where) + " LIMIT 20000"
        r = run(sql, f"Mẫu tỷ lệ đơn giá/giá niêm yết ({'kỳ này' if lab == 'now' else 'kỳ so sánh'})")
        queries.append({"purpose": f"Kiểm tra trôi lệch đơn giá ({lab})", "sql": r["sql"], "bytes": r["bytes"], "rows": len(r["rows"]), "ms": r["ms"]})
        samples[lab] = [x[0] for x in r["rows"] if x[0] and x[0] > 0]
    if len(samples["now"]) < 30 or len(samples["prev"]) < 30:
        return out
    lg = lambda xs: [math.log10(x) for x in xs]
    ks = stats.ks_2samp(lg(samples["prev"]), lg(samples["now"]))
    share_tiny_now = sum(1 for x in samples["now"] if x < 0.05) / len(samples["now"])
    share_tiny_prev = sum(1 for x in samples["prev"] if x < 0.05) / len(samples["prev"])
    med_now, med_prev = statistics.median(samples["now"]), statistics.median(samples["prev"])
    if share_tiny_now - share_tiny_prev > 0.05 and ks.pvalue < 0.01:
        # cửa hàng bị ảnh hưởng
        where = [f"f.ngay_dh >= DATE '{cur['start']}'", f"f.ngay_dh < DATE '{cur['end']}'", "f.trang_thai = 'HOAN_THANH'"] + conds
        sql = (f"SELECT cn.ten_cn, MIN(f.ngay_dh) AS tu_ngay, COUNT(*) AS so_dong, MEDIAN(f.dg / sp.gia_niem_yet) AS ty_le_tv "
               f"{joins} WHERE " + " AND ".join(where) + " AND f.dg < 0.05 * sp.gia_niem_yet GROUP BY cn.ten_cn ORDER BY so_dong DESC LIMIT 15")
        r = run(sql, "Cửa hàng có đơn giá bất thường")
        queries.append({"purpose": "Cửa hàng có đơn giá bất thường", "sql": r["sql"], "bytes": r["bytes"], "rows": len(r["rows"]), "ms": r["ms"]})
        stores = [x[0] for x in r["rows"]]
        factor = round(1 / statistics.median([x[3] for x in r["rows"]]) / 1000) * 1000 if r["rows"] else None
        since = min(str(x[1]) for x in r["rows"]) if r["rows"] else None
        pct_tiny = fmt_pct(share_tiny_now * 100)
        out.append({"kind": "data_quality", "severity": "critical", "title": "Trôi lệch đơn vị đơn giá (unit drift)",
                    "short": f"{pct_tiny} dòng bán của phân khúc này trong kỳ có đơn giá chỉ bằng khoảng 1/{factor or 1000} giá "
                             f"niêm yết, tập trung ở {len(stores)} cửa hàng — nhiều khả năng hệ thống POS ghi đơn giá theo NGHÌN đồng.",
                    "message": f"Kiểm định Kolmogorov–Smirnov trên log(đơn giá/giá niêm yết) giữa hai kỳ: D = {ks.statistic:.3f}, "
                               f"p = {ks.pvalue:.2e}. Trung vị tỷ lệ kỳ trước {med_prev:.3f}, kỳ này {med_now:.3f}. "
                               f"Cửa hàng bị ảnh hưởng: {', '.join(stores[:10])}.",
                    "test": {"name": "KS 2-sample", "statistic": round(float(ks.statistic), 4), "p_value": float(ks.pvalue)},
                    "evidence": {"stores": stores, "since": since, "factor": factor, "share_rows_affected": share_tiny_now},
                    "action": "Admin vào trang Chất lượng dữ liệu để áp dụng quy tắc sửa (nhân 1000 cho dòng bị ảnh hưởng) và build lại."})
    return out


def _check_closures(run, queries, cur, prev, drivers, drill):
    out = []
    names = set()
    for d in drivers:
        if d["dimension"] == "chi_nhanh":
            names |= {i["member"] for i in d["items"][:5] if i["now"] == 0 or (i["pct"] is not None and i["pct"] < -60)}
    for d in drill:
        if d["dimension"] == "chi_nhanh":
            names |= {i["member"] for i in d["items"][:5] if i["now"] == 0 or (i["pct"] is not None and i["pct"] < -60)}
    if not names:
        return out
    vals = ", ".join(sqlq(n) for n in names)
    sql = (f"SELECT cn.ten_cn, cn.ngay_dong, cn.ngay_mo FROM dw.dim_cn cn WHERE cn.ten_cn IN ({vals}) "
           f"AND cn.ngay_dong IS NOT NULL AND cn.ngay_dong < DATE '{cur['end']}'")
    r = run(sql, "Kiểm tra cửa hàng đóng cửa")
    queries.append({"purpose": "Kiểm tra cửa hàng đóng cửa", "sql": r["sql"], "bytes": r["bytes"], "rows": len(r["rows"]), "ms": r["ms"]})
    for name, closed, _ in r["rows"]:
        out.append({"kind": "business", "severity": "warning", "title": "Cửa hàng đóng cửa",
                    "short": f"{name} đã đóng cửa từ {closed}",
                    "message": f"dw.dim_cn ghi nhận {name} ngừng hoạt động ngày {closed} — doanh thu kỳ này giảm về 0.",
                    "evidence": {"store": name, "ngay_dong": closed}})
    return out


def _check_stockout(run, queries, cur, prev, drivers, drill, filters, best):
    out = []
    cats, region = [], None
    for f in filters:
        if f["dimension"] == "vung":
            region = f["values"][0]
        if f["dimension"] == "nganh_hang":
            cats = list(f["values"])
    if best and best["dimension"] == "vung":
        region = region or best["items"][0]["member"]
    if best and best["dimension"] == "nganh_hang":
        cats = cats or [best["items"][0]["member"]]
    for d in drill:
        if d["dimension"] == "nganh_hang" and d["items"]:
            cats = cats or [d["items"][0]["member"]]
        if d["dimension"] == "vung" and d["items"]:
            region = region or d["items"][0]["member"]
    if not cats:
        return out
    cond = [f"nh.ten_nh IN ({', '.join(sqlq(c) for c in cats)})"]
    if region:
        cond.append(f"vung.ten_vung = {sqlq(region)}")
    res = {}
    for lab, tr in (("now", cur), ("prev", prev)):
        sql = ("SELECT AVG(x.ton) AS ton_tb, MIN(x.ton) AS ton_min FROM (SELECT t.ngay_chot, SUM(t.sl_ton) AS ton "
               "FROM dw.fct_tk_tuan t JOIN dw.dim_sp sp ON sp.ma_sp = t.ma_sp JOIN dw.dim_nh nh ON nh.ma_nh = sp.ma_nh "
               "JOIN dw.dim_kho kho ON kho.ma_kho = t.ma_kho JOIN dw.dim_vung vung ON vung.ma_vung = kho.ma_vung "
               f"WHERE t.ngay_chot >= DATE '{tr['start']}' AND t.ngay_chot < DATE '{tr['end']}' AND " + " AND ".join(cond) +
               " GROUP BY t.ngay_chot) x")
        r = run(sql, "Tồn kho trung bình theo tuần")
        queries.append({"purpose": f"Kiểm tra tồn kho ({lab})", "sql": r["sql"], "bytes": r["bytes"], "rows": len(r["rows"]), "ms": r["ms"]})
        res[lab] = r["rows"][0] if r["rows"] else (None, None)
    (tn, mn), (tp, _) = res["now"], res["prev"]
    if tn is not None and tp and tn < 0.35 * tp:
        where = f"{', '.join(cats)}{' tại ' + region if region else ''}"
        out.append({"kind": "business", "severity": "warning", "title": "Thiếu hàng / hết hàng (stockout)",
                    "short": f"tồn kho {where} giảm {fmt_pct((1 - tn / tp) * 100)} (trung bình {fmt_number(round(tn))} so với {fmt_number(round(tp))} sản phẩm/tuần)",
                    "message": f"Tồn kho bình quân tuần của {where}: kỳ này {fmt_number(round(tn))}, kỳ so sánh {fmt_number(round(tp))}; "
                               f"tồn thấp nhất {fmt_number(mn or 0)}. Doanh số giảm do không đủ hàng để bán.",
                    "evidence": {"categories": cats, "region": region, "avg_stock_now": tn, "avg_stock_prev": tp}})
    return out


def _check_calendar(run, queries, cur, prev, anchor):
    out = []
    sql = ("SELECT k.ten_km, k.tu_ngay, k.den_ngay FROM dw.dim_km k WHERE k.tu_ngay < DATE '{e}' AND k.den_ngay >= DATE '{s}'")
    promos = {}
    for lab, tr in (("now", cur), ("prev", prev)):
        r = run(sql.format(s=tr["start"], e=tr["end"]), "Chương trình khuyến mãi trong kỳ")
        promos[lab] = [x[0] for x in r["rows"]]
    key = lambda p: re.sub(r"[\s/]*\d{4}$", "", p).strip()   # "Siêu sale 9/9/2026" ~ "Siêu sale 9/9"
    if {key(p) for p in promos["now"]} != {key(p) for p in promos["prev"]}:
        only_prev = [p for p in promos["prev"] if key(p) not in {key(x) for x in promos["now"]}]
        only_now = [p for p in promos["now"] if key(p) not in {key(x) for x in promos["prev"]}]
        msg = []
        if only_prev:
            msg.append(f"kỳ so sánh có CTKM {', '.join(only_prev)}")
        if only_now:
            msg.append(f"kỳ này có CTKM {', '.join(only_now)}")
        if msg:
            out.append({"kind": "calendar", "severity": "info", "title": "Khác biệt lịch khuyến mãi",
                        "short": "; ".join(msg), "message": "Lịch khuyến mãi khác nhau giữa hai kỳ: " + "; ".join(msg) + ".",
                        "evidence": promos})
    end = dt.date.fromisoformat(cur["end"])
    if end > anchor + dt.timedelta(days=1):
        out.append({"kind": "data_quality", "severity": "warning", "title": "Kỳ phân tích chưa đủ dữ liệu",
                    "short": f"kỳ {cur['label']} mới có dữ liệu đến {anchor.strftime('%d/%m/%Y')}",
                    "message": "So sánh kỳ chưa kết thúc với kỳ đầy đủ sẽ cho biến động âm giả tạo."})
    return out

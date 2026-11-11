"""
Kiểm định chất lượng & trôi lệch dữ liệu (deterministic, thống kê) + quy tắc sửa có kiểm duyệt.

Các kiểm tra: schema drift (so với data contract), mã trạng thái chưa chuẩn hóa, dòng trùng, số lượng âm,
khóa ngoại mồ côi (late-arriving dimension), khách hàng trùng SĐT, trôi lệch đơn vị đơn giá (change-point +
KS test), giá nhập nhầm (x10), bất thường sản lượng theo ngày (robust z / so với cùng thứ), trôi lệch phân phối
kênh/thanh toán (PSI + chi-square), độ tươi dữ liệu.
Quy tắc sửa = biểu thức SQL cài vào hook {{ rule() }} của model hoặc dòng mapping trong bảng ref; được
kiểm tra cú pháp/AST trước khi áp dụng, sau đó build lại toàn bộ model và chạy lại kiểm định.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import statistics
import time

import sqlglot
from sqlglot import exp

from . import config
from .catalog import get_catalog
from .textutil import fmt_money, fmt_number, fmt_pct, norm
from .warehouse import get_warehouse

REPORT = config.VAR / "dq_report.json"
STD_STATUS = {"hoanthanh": "HOAN_THANH", "completed": "HOAN_THANH", "complete": "HOAN_THANH", "done": "HOAN_THANH",
              "ht": "HOAN_THANH", "huy": "HUY", "cancelled": "HUY", "canceled": "HUY", "cancel": "HUY",
              "trahang": "TRA_HANG", "returned": "TRA_HANG", "return": "TRA_HANG", "danggiao": "DANG_GIAO",
              "shipping": "DANG_GIAO", "delivering": "DANG_GIAO"}


def _f(id, check, title, severity, message, **kw):
    d = {"id": id, "check": check, "title": title, "severity": severity, "message": message, "status": "open"}
    d.update(kw)
    return d


def _psi(p: dict, q: dict) -> float:
    keys = set(p) | set(q)
    sp, sq = sum(p.values()) or 1, sum(q.values()) or 1
    s = 0.0
    for k in keys:
        a = max(p.get(k, 0) / sp, 1e-6)
        b = max(q.get(k, 0) / sq, 1e-6)
        s += (a - b) * math.log(a / b)
    return s


def run_checks() -> dict:
    from scipy import stats
    wh = get_warehouse()
    cat = get_catalog()
    t0 = time.time()
    fs: list[dict] = []
    active = _load_rules()
    active_ids = {f["id"] for f in active.get("fixes", []) if f.get("enabled", True)}
    anchor = dt.date.fromisoformat(cat.anchor_date())

    # 1. schema drift
    for table, expected in config.contracts().items():
        t = cat.get(table)
        if not t:
            fs.append(_f(f"schema_missing_{table}", "schema_drift", f"Thiếu bảng nguồn {table}", "critical",
                         "Bảng có trong data contract nhưng không tồn tại."))
            continue
        actual = list(t["columns"])
        added = [c for c in actual if c not in expected]
        removed = [c for c in expected if c not in actual]
        if added or removed:
            det = []
            for c in added:
                det.append({"column": c, "type": t["columns"][c]["type"], "null_frac": t["columns"][c]["null_frac"]})
            msg = []
            if added:
                msg.append(f"cột mới ngoài contract: {', '.join(added)}")
            if removed:
                msg.append(f"cột bị mất: {', '.join(removed)}")
            fs.append(_f(f"schema_drift_{table}", "schema_drift", f"Schema drift ở {table}", "critical" if removed else "warning",
                         f"Nguồn {table} thay đổi cấu trúc so với data contract — " + "; ".join(msg) +
                         ". Cột mới chưa được đưa vào model nên không có trong kho dw.",
                         table=table, evidence=det,
                         recommendation="Cập nhật config/contracts.yml và model staging nếu cột mới có giá trị nghiệp vụ."))

    # 2. mã trạng thái chưa map
    rows = wh.fetch("SELECT nguon, trang_thai_goc, COUNT(*) FROM stg.stg_don_hang WHERE trang_thai = 'KHAC' GROUP BY 1, 2 ORDER BY 3 DESC")
    if rows:
        seed, unknown = [], []
        for src, code, n in rows:
            key = norm(code or "").replace(" ", "").replace("_", "")
            std = STD_STATUS.get(key)
            if std:
                seed.append({"table": "ref.status_map", "row": {"nguon": src, "ma_goc": (code or "").strip().lower(), "trang_thai_chuan": std}})
            else:
                unknown.append(code)
        total = sum(r[2] for r in rows)
        fix = None
        fid = "status_unmapped"
        if seed:
            fix = {"id": "fix_status_map", "kind": "seed_rows", "seed_rows": seed,
                   "description": "Bổ sung mapping: " + "; ".join(f"{s['row']['nguon']} '{s['row']['ma_goc']}' → {s['row']['trang_thai_chuan']}" for s in seed)}
        fs.append(_f(fid, "domain", "Mã trạng thái đơn chưa được chuẩn hóa", "warning",
                     f"{fmt_number(total)} đơn có trạng thái nguồn không nằm trong ref.status_map nên bị gán KHAC "
                     f"(bị loại khỏi doanh thu thuần). Mã chưa nhận diện được: {', '.join(map(str, unknown)) or 'không'}.",
                     table="stg.stg_don_hang", columns=["trang_thai"],
                     evidence=[{"nguon": s, "ma_goc": c, "so_don": n} for s, c, n in rows], fix=fix,
                     status="fixed" if "fix_status_map" in active_ids and not seed else "open"))

    # 3. dòng trùng & số lượng âm (đã xử lý ở staging)
    dup = wh.scalar("SELECT COUNT(*) - COUNT(DISTINCT (order_id, line_no)) FROM raw.ecom_order_items")
    if dup:
        fs.append(_f("dup_ecom_items", "duplicates", "Dòng trùng trong raw.ecom_order_items", "info",
                     f"{fmt_number(dup)} dòng bị nạp lặp (ingest replay). Model stg_ct_don_hang đã khử trùng bằng ROW_NUMBER().",
                     table="raw.ecom_order_items", status="handled"))
    neg = wh.scalar("SELECT COUNT(*) FROM raw.pos_ct_don_hang WHERE so_luong <= 0")
    if neg:
        fs.append(_f("neg_qty_pos", "range", "Số lượng âm trong chi tiết hóa đơn POS", "info",
                     f"{fmt_number(neg)} dòng có so_luong <= 0 (ghi nhầm hàng trả vào hóa đơn bán). Đã loại ở staging; hàng trả lấy từ ERP.",
                     table="raw.pos_ct_don_hang", status="handled"))
    nul = wh.scalar("SELECT AVG(CASE WHEN tien_ck IS NULL THEN 1.0 ELSE 0 END) FROM raw.pos_ct_don_hang")
    if nul:
        fs.append(_f("null_discount", "nulls", "Chiết khấu NULL", "info",
                     f"{fmt_pct(nul * 100)} dòng POS có tien_ck NULL — staging chuyển thành 0.", table="raw.pos_ct_don_hang", status="handled"))

    # 4. khóa ngoại mồ côi, khách trùng
    orphan = wh.scalar("SELECT COUNT(*) FROM dw.fct_dh d WHERE d.ma_kh IS NOT NULL AND NOT EXISTS (SELECT 1 FROM dw.dim_kh k WHERE k.ma_kh = d.ma_kh)")
    if orphan:
        fs.append(_f("orphan_customer", "referential", "Đơn tham chiếu khách hàng chưa có trong CRM", "warning",
                     f"{fmt_number(orphan)} đơn có ma_kh không tồn tại trong dw.dim_kh (late-arriving dimension) — các phân tích "
                     "theo thuộc tính khách hàng (hạng thẻ, tỉnh) sẽ bỏ sót các đơn này.", table="dw.fct_dh", columns=["ma_kh"],
                     recommendation="Tạo bản ghi khách hàng tạm 'Chưa xác định' hoặc chạy lại đồng bộ CRM."))
    dupc = wh.fetch("SELECT COUNT(*), SUM(n) FROM (SELECT sdt, COUNT(*) n FROM dw.dim_kh GROUP BY sdt HAVING COUNT(*) > 1)")
    if dupc and dupc[0][0]:
        fs.append(_f("dup_customer_phone", "duplicates", "Khách hàng trùng số điện thoại", "warning",
                     f"{fmt_number(dupc[0][0])} số điện thoại gắn với nhiều mã khách ({fmt_number(dupc[0][1])} bản ghi) — chỉ số "
                     "'số khách hàng' có thể bị đếm thừa.", table="dw.dim_kh", columns=["sdt"],
                     recommendation="Áp dụng quy tắc hợp nhất khách hàng (master data) theo SĐT."))
    unk = wh.scalar("SELECT COUNT(*) FROM dw.dim_kh WHERE tinh_tp = 'Không xác định'")
    if unk:
        fs.append(_f("unknown_province", "domain", "Khách hàng không xác định được tỉnh/thành", "info",
                     f"{fmt_number(unk)} khách hàng có dia_chi_tinh rỗng hoặc ngoài bảng alias.", table="dw.dim_kh", columns=["tinh_tp"]))

    # 5. trôi lệch đơn vị đơn giá theo cửa hàng (change-point + KS test)
    daily = wh.fetch("""
        SELECT h.ma_cn, cn.ten_cn, h.ngay_dh, MEDIAN(l.dg / sp.gia_ban_le) AS r, COUNT(*) AS n
        FROM stg.stg_ct_don_hang l JOIN stg.stg_don_hang h ON h.ma_dh = l.ma_dh
        JOIN raw.erp_san_pham sp ON sp.ma_hang = l.ma_sp JOIN dw.dim_cn cn ON cn.ma_cn = h.ma_cn
        WHERE l.nguon = 'POS' AND sp.gia_ban_le > 0
        GROUP BY 1, 2, 3 ORDER BY 1, 3""")
    by_store: dict = {}
    for ma, ten, d, r, n in daily:
        by_store.setdefault((ma, ten), []).append((str(d)[:10], r, n))
    drifted = []
    for (ma, ten), series in by_store.items():
        idx = next((i for i, (_, r, _) in enumerate(series) if r is not None and r < 0.05 and
                    sum(1 for _, r2, _ in series[i:] if r2 is not None and r2 < 0.05) >= 0.9 * len(series[i:])), None)
        if idx is None or idx < 14:
            continue
        before = [math.log10(r) for _, r, _ in series[max(0, idx - 60):idx] if r and r > 0]
        after = [math.log10(r) for _, r, _ in series[idx:] if r and r > 0]
        if len(before) < 10 or len(after) < 5:
            continue
        ks = stats.ks_2samp(before, after)
        factor = 10 ** round(statistics.median(before) - statistics.median(after))
        drifted.append({"ma_cn": ma, "ten_cn": ten, "since": series[idx][0], "days_affected": len(after),
                        "factor": int(factor), "ks_D": round(float(ks.statistic), 3), "p_value": float(ks.pvalue),
                        "median_ratio_before": round(10 ** statistics.median(before), 4),
                        "median_ratio_after": round(10 ** statistics.median(after), 6)})
    if drifted:
        stores = [d["ma_cn"] for d in drifted]
        since = min(d["since"] for d in drifted)
        factor = statistics.mode([d["factor"] for d in drifted])
        cond = " OR ".join(f"(h.ma_cn = '{d['ma_cn']}' AND h.ngay_dh >= DATE '{d['since']}')" for d in drifted)
        guard = f"u.nguon = 'POS' AND ({cond}) AND u.dg_goc * {factor} BETWEEN 0.3 * sp.gia_ban_le AND 1.5 * sp.gia_ban_le"
        impact = wh.fetch(f"""
            SELECT COUNT(*), SUM(f.tt), SUM(f.tt) * {factor} FROM dw.fct_ct_dh f JOIN dw.dim_sp sp ON sp.ma_sp = f.ma_sp
            WHERE f.ma_cn IN ({', '.join("'" + s + "'" for s in stores)}) AND f.ngay_dh >= DATE '{since}'
              AND f.dg < 0.05 * sp.gia_niem_yet AND f.trang_thai = 'HOAN_THANH'""")[0]
        n_rows, cur_rev, true_rev = impact
        fix = {"id": "fix_unit_drift_pos", "kind": "model_rule", "model": "stg.stg_ct_don_hang",
               "slots": {"dg": f"CASE WHEN {guard} THEN {{x}} * {factor} ELSE {{x}} END",
                         "ck": f"CASE WHEN {guard} THEN {{x}} * {factor} ELSE {{x}} END"},
               "description": f"Nhân đơn giá & chiết khấu với {factor} cho dòng POS của {len(stores)} cửa hàng từ ngày bắt đầu lệch "
                              f"(chỉ khi giá sau quy đổi nằm trong 30–150% giá niêm yết)."}
        fs.append(_f("unit_drift_pos", "distribution_drift", "Trôi lệch đơn vị đơn giá (POS)", "critical",
                     f"{len(stores)} cửa hàng ({', '.join(d['ten_cn'] for d in drifted[:6])}{'...' if len(drifted) > 6 else ''}) "
                     f"có đơn giá/giá niêm yết giảm đột ngột khoảng {factor} lần từ {since} — hệ thống POS ghi đơn giá theo nghìn đồng. "
                     f"{fmt_number(n_rows or 0)} dòng bị ảnh hưởng; doanh thu đang ghi nhận {fmt_money(cur_rev or 0)} thay vì khoảng "
                     f"{fmt_money(true_rev or 0)}.",
                     table="dw.fct_ct_dh", columns=["dg", "ck", "tt"], since=since,
                     test={"name": "Kolmogorov–Smirnov 2 mẫu trên log10(đơn giá/giá niêm yết) theo ngày, trước vs sau điểm thay đổi",
                           "max_D": max(d["ks_D"] for d in drifted), "max_p_value": max(d["p_value"] for d in drifted)},
                     evidence=drifted, fix=fix,
                     metrics={"rows_affected": n_rows, "revenue_recorded": cur_rev, "revenue_corrected": true_rev}))
    elif "fix_unit_drift_pos" in active_ids:
        fs.append(_f("unit_drift_pos", "distribution_drift", "Trôi lệch đơn vị đơn giá (POS)", "info",
                     "Đã áp dụng quy tắc sửa — không còn cửa hàng có đơn giá lệch đơn vị.", status="fixed", table="dw.fct_ct_dh"))

    # 6. giá nhập nhầm x10
    typo = wh.fetch("""SELECT l.ma_dh, l.ma_sp, l.dg, sp.gia_ban_le FROM stg.stg_ct_don_hang l
                       JOIN raw.erp_san_pham sp ON sp.ma_hang = l.ma_sp WHERE l.dg > 5 * sp.gia_ban_le ORDER BY l.dg DESC LIMIT 50""")
    if typo:
        n_typo = wh.scalar("SELECT COUNT(*) FROM stg.stg_ct_don_hang l JOIN raw.erp_san_pham sp ON sp.ma_hang = l.ma_sp WHERE l.dg > 5 * sp.gia_ban_le")
        excess = wh.scalar("SELECT SUM(l.sl * l.dg * 0.9) FROM stg.stg_ct_don_hang l JOIN raw.erp_san_pham sp ON sp.ma_hang = l.ma_sp WHERE l.dg > 5 * sp.gia_ban_le")
        fix = {"id": "fix_price_typo", "kind": "model_rule", "model": "stg.stg_ct_don_hang",
               "slots": {"dg": "CASE WHEN u.dg_goc > 5 * sp.gia_ban_le AND u.dg_goc / 10 BETWEEN 0.5 * sp.gia_ban_le AND 1.5 * sp.gia_ban_le THEN {x} / 10 ELSE {x} END"},
               "description": "Chia 10 cho đơn giá > 5 lần giá niêm yết nếu sau khi chia nằm trong 50–150% giá niêm yết (lỗi gõ thừa số 0)."}
        fs.append(_f("price_outlier", "outliers", "Đơn giá bất thường (gõ thừa số 0)", "warning",
                     f"{fmt_number(n_typo)} dòng có đơn giá > 5 lần giá niêm yết, làm doanh thu bị thổi phồng khoảng {fmt_money(excess or 0)}.",
                     table="dw.fct_ct_dh", columns=["dg"],
                     evidence=[{"ma_dh": a, "ma_sp": b, "dg": c, "gia_niem_yet": d} for a, b, c, d in typo[:10]], fix=fix))

    # 7. bất thường sản lượng theo ngày (so với trung vị cùng thứ 4 tuần trước)
    cnt = wh.fetch(f"""SELECT d.ngay_dh, COUNT(*), BOOL_OR(n.ngay_le) FROM dw.fct_dh d JOIN dw.dim_ngay n ON n.ngay = d.ngay_dh
                       WHERE d.ngay_dh >= DATE '{anchor - dt.timedelta(days=90)}' GROUP BY 1 ORDER BY 1""")
    series = {str(d)[:10]: (n, h) for d, n, h in cnt}
    anomalies = []
    for d, (n, holiday) in series.items():
        day = dt.date.fromisoformat(d)
        if day < anchor - dt.timedelta(days=30):
            continue
        ref = [series.get(str(day - dt.timedelta(weeks=k)), (None, None))[0] for k in range(1, 5)]
        ref = [x for x in ref if x]
        if len(ref) < 3:
            continue
        med = statistics.median(ref)
        ratio = n / med if med else None
        if ratio and (ratio < 0.6 or ratio > 1.8):
            anomalies.append({"ngay": d, "so_don": n, "trung_vi_cung_thu": med, "ty_le": round(ratio, 2), "ngay_le": bool(holiday)})
    if anomalies:
        last = anomalies[-1]
        partial = last["ngay"] == anchor.isoformat() and last["ty_le"] < 0.6
        fs.append(_f("volume_anomaly", "volume", "Số đơn theo ngày bất thường", "warning",
                     ("Ngày dữ liệu mới nhất " + last["ngay"] + f" chỉ có {fmt_number(last['so_don'])} đơn, bằng {fmt_pct(last['ty_le'] * 100)} "
                      "mức thường lệ của cùng thứ — nhiều khả năng dữ liệu chưa nạp đủ (partial load)." if partial else
                      f"{len(anomalies)} ngày có số đơn lệch mạnh so với cùng thứ 4 tuần trước.") +
                     (" Các ngày khác: " + ", ".join(a["ngay"] for a in anomalies[:-1]) if len(anomalies) > 1 else ""),
                     table="dw.fct_dh", evidence=anomalies,
                     test={"name": "Tỷ lệ so với trung vị cùng thứ 4 tuần trước", "threshold": "<0.6 hoặc >1.8"},
                     recommendation="Chặn báo cáo ngày chưa đủ dữ liệu hoặc chờ job nạp hoàn tất."))

    # 8. trôi lệch phân phối (PSI + chi-square): kênh, phương thức thanh toán
    for col, title in (("ma_kenh", "Cơ cấu kênh bán"), ("pttt", "Cơ cấu phương thức thanh toán")):
        a = dict(wh.fetch(f"SELECT {col}, COUNT(*) FROM dw.fct_dh WHERE ngay_dh >= DATE '{anchor - dt.timedelta(days=30)}' GROUP BY 1"))
        b = dict(wh.fetch(f"SELECT {col}, COUNT(*) FROM dw.fct_dh WHERE ngay_dh >= DATE '{anchor - dt.timedelta(days=120)}' "
                          f"AND ngay_dh < DATE '{anchor - dt.timedelta(days=30)}' GROUP BY 1"))
        if not a or not b:
            continue
        psi = _psi(a, b)
        keys = sorted(set(a) | set(b), key=str)
        chi = stats.chi2_contingency([[a.get(k, 0) for k in keys], [b.get(k, 0) for k in keys]])
        sev = "warning" if psi > 0.25 else ("info" if psi > 0.1 else "ok")
        sa, sb = sum(a.values()), sum(b.values())
        fs.append(_f(f"psi_{col}", "distribution_drift", f"{title}: 30 ngày gần nhất so với 90 ngày trước", sev,
                     f"PSI = {psi:.3f} ({'trôi lệch đáng kể' if psi > 0.25 else 'thay đổi vừa phải' if psi > 0.1 else 'ổn định'}); "
                     f"chi-square p = {chi.pvalue:.2e}.",
                     table="dw.fct_dh", columns=[col],
                     test={"name": "Population Stability Index + chi-square", "psi": round(psi, 4), "chi2": round(float(chi.statistic), 2),
                           "p_value": float(chi.pvalue)},
                     evidence=[{"gia_tri": k, "ty_trong_gan_day": round(a.get(k, 0) * 100 / sa, 2),
                                "ty_trong_truoc_do": round(b.get(k, 0) * 100 / sb, 2)} for k in keys],
                     status="ok" if sev == "ok" else "open"))

    # 9. độ tươi
    fresh = []
    for table, col in (("dw.fct_dh", "ngay_dh"), ("dw.fct_tk_tuan", "ngay_chot"), ("dw.fct_th", "ngay_tra"), ("dw.fct_nk", "ngay_nk")):
        mx = wh.scalar(f"SELECT MAX({col}) FROM {table}")
        lag = (anchor - dt.date.fromisoformat(str(mx)[:10])).days if mx else None
        fresh.append({"table": table, "max_date": mx, "lag_days": lag})
    stale = [f for f in fresh if f["lag_days"] is not None and f["lag_days"] > 7]
    fs.append(_f("freshness", "freshness", "Độ tươi dữ liệu", "warning" if stale else "ok",
                 ("Bảng chậm cập nhật: " + ", ".join(f"{s['table']} (trễ {s['lag_days']} ngày)" for s in stale)) if stale
                 else "Các bảng sự kiện đều cập nhật trong vòng 7 ngày.", evidence=fresh, status="open" if stale else "ok"))

    order = {"critical": 0, "warning": 1, "info": 2, "ok": 3}
    fs.sort(key=lambda f: (f["status"] in ("handled", "fixed", "ok"), order.get(f["severity"], 9)))
    rep = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "elapsed_s": round(time.time() - t0, 2), "anchor": anchor.isoformat(),
           "summary": {s: sum(1 for f in fs if f["severity"] == s and f["status"] == "open") for s in ("critical", "warning", "info")},
           "active_fixes": [f for f in active.get("fixes", [])], "findings": fs}
    REPORT.write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return rep


def latest_report(run_if_missing: bool = True) -> dict | None:
    if REPORT.exists():
        return json.loads(REPORT.read_text(encoding="utf-8"))
    return run_checks() if run_if_missing else None


def known_issues() -> list[dict]:
    """Vấn đề chất lượng còn mở -> cảnh báo trong lint của truy vấn (L010)."""
    rep = latest_report(run_if_missing=False)
    if not rep:
        return []
    out = []
    for f in rep["findings"]:
        if f["status"] != "open" or f["severity"] not in ("critical", "warning") or not f.get("table", "").startswith("dw."):
            continue
        if f["check"] in ("distribution_drift", "outliers") and f.get("columns"):
            out.append({"table": f["table"], "columns": f.get("columns"), "since": f.get("since"),
                        "message": f["title"] + (f" (từ {f['since']})" if f.get("since") else ""),
                        "hint": "Kết quả có thể sai lệch — xem trang Chất lượng dữ liệu." + (" Admin có quy tắc sửa đề xuất." if f.get("fix") else "")})
        if f["check"] == "volume":
            out.append({"table": "dw.fct_ct_dh", "columns": ["ngay_dh"], "since": rep.get("anchor"), "requires_range": True,
                        "message": "Ngày dữ liệu mới nhất có thể chưa nạp đủ", "hint": "Tránh so sánh ngày/tháng hiện tại với kỳ đầy đủ."})
    return out


# ------------------------------------------------------------------------------ áp dụng / hoàn tác quy tắc sửa
ALLOWED_RULE_ALIASES = {"u", "h", "sp"}


def _load_rules() -> dict:
    if config.RULES_FILE.exists():
        return json.loads(config.RULES_FILE.read_text(encoding="utf-8"))
    return {"fixes": []}


def validate_fix(fix: dict) -> list[str]:
    errs = []
    for slot, tpl in (fix.get("slots") or {}).items():
        if "{x}" not in tpl:
            errs.append(f"Slot {slot}: thiếu placeholder {{x}}.")
        try:
            e = sqlglot.parse_one(f"SELECT {tpl.replace('{x}', 'u.dg_goc')} AS v FROM t", read="duckdb")
        except Exception as ex:
            errs.append(f"Slot {slot}: lỗi cú pháp — {ex}")
            continue
        if e.find(exp.Subquery) or len(list(e.find_all(exp.Select))) > 1:
            errs.append(f"Slot {slot}: không được chứa truy vấn con.")
        for c in e.find_all(exp.Column):
            if c.table and c.table not in ALLOWED_RULE_ALIASES:
                errs.append(f"Slot {slot}: bí danh '{c.table}' không hợp lệ (chỉ u, h, sp).")
        bad = [f.sql_name() for f in e.find_all(exp.Func) if f.sql_name().lower() in {"read_csv", "read_parquet", "getenv", "glob"}]
        if bad:
            errs.append(f"Slot {slot}: hàm bị cấm {bad}.")
    cat = get_catalog()
    for s in fix.get("seed_rows") or []:
        t = cat.get(s["table"])
        if not s["table"].startswith("ref.") or not t:
            errs.append(f"Seed row: chỉ được ghi bảng ref.* tồn tại ({s['table']}).")
            continue
        for k in s["row"]:
            if k not in t["columns"]:
                errs.append(f"Seed row: cột {k} không có trong {s['table']}.")
    return errs


def _rebuild() -> dict:
    import sys
    sys.path.insert(0, str(config.ROOT))
    from warehouse.build import run_build
    wh = get_warehouse()
    with wh.writable() as con:
        res = run_build(con=con, verbose=False)
    get_catalog().reload()
    from .nlu import get_nlu
    get_nlu(reload=True)
    return res


def apply_fix(finding_id: str, username: str) -> dict:
    rep = latest_report()
    f = next((x for x in rep["findings"] if x["id"] == finding_id), None)
    if not f or not f.get("fix"):
        raise ValueError("Không tìm thấy quy tắc sửa cho phát hiện này.")
    fix = dict(f["fix"])
    errs = validate_fix(fix)
    if errs:
        return {"ok": False, "errors": errs}
    before = _impact_probe(f)
    rules = _load_rules()
    rules["fixes"] = [x for x in rules["fixes"] if x["id"] != fix["id"]]
    fix.update({"enabled": True, "applied_by": username, "applied_at": time.strftime("%Y-%m-%d %H:%M:%S"), "finding": finding_id,
                "title": f["title"]})
    rules["fixes"].append(fix)
    config.RULES_FILE.write_text(json.dumps(rules, ensure_ascii=False, indent=1), encoding="utf-8")
    build = _rebuild()
    after = _impact_probe(f)
    report = run_checks()
    return {"ok": True, "fix": fix, "build": build, "impact": {"before": before, "after": after}, "report": report}


def revert_fix(fix_id: str, username: str) -> dict:
    rules = _load_rules()
    found = False
    for x in rules["fixes"]:
        if x["id"] == fix_id:
            x["enabled"] = False
            x["reverted_by"] = username
            found = True
    if not found:
        raise ValueError("Không tìm thấy quy tắc.")
    config.RULES_FILE.write_text(json.dumps(rules, ensure_ascii=False, indent=1), encoding="utf-8")
    build = _rebuild()
    rules["fixes"] = [x for x in rules["fixes"] if x.get("enabled", True)]
    config.RULES_FILE.write_text(json.dumps(rules, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "build": build, "report": run_checks()}


def _impact_probe(f: dict) -> dict | None:
    """Doanh thu thuần theo tháng 3 tháng gần nhất — để thấy tác động trước/sau khi sửa."""
    wh = get_warehouse()
    anchor = dt.date.fromisoformat(get_catalog().anchor_date())
    start = (anchor.replace(day=1) - dt.timedelta(days=75)).replace(day=1)
    rows = wh.fetch(f"""SELECT STRFTIME(ngay_dh, '%Y-%m') AS thang, SUM(tt - tien_tra) FROM dw.fct_ct_dh
                       WHERE trang_thai = 'HOAN_THANH' AND ngay_dh >= DATE '{start}' GROUP BY 1 ORDER BY 1""")
    return {"metric": "Doanh thu thuần", "rows": [{"thang": a, "value": b, "text": fmt_money(b)} for a, b in rows]}

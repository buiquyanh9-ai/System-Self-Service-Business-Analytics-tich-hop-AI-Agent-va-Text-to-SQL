"""Kiểm thử các thành phần xác định: governance, chi phí, semantic, NLU, diễn giải & audit, so khớp eval."""
import pytest

from app import cost, explain, sqlcheck
from app.evals import compare
from app.governance import UserCtx, validate
from app.nlu import get_nlu
from app.semantic import SemanticError, get_semantic
from app.warehouse import get_warehouse

ADMIN = UserCtx("admin", "admin", {})
ANALYST = UserCtx("analyst", "analyst", {})
BAC = UserCtx("analyst_bac", "analyst", {"vung": ["B"]})


# ------------------------------------------------------------------ governance
@pytest.mark.parametrize("sql,code", [
    ("DROP TABLE dw.fct_dh", "FORBIDDEN_STATEMENT"),
    ("DELETE FROM dw.fct_dh", "FORBIDDEN_STATEMENT"),
    ("SELECT 1; SELECT 2", "MULTI_STATEMENT"),
    ("SELECT * FROM read_csv('C:/x.csv')", "FORBIDDEN_FUNCTION"),
    ("SELECT * FROM raw.pos_don_hang", "FORBIDDEN_SCHEMA"),
    ("SELECT * FROM information_schema.tables", "FORBIDDEN_SCHEMA"),
    ("SELECT ma_kh, sdt FROM dw.dim_kh", "FORBIDDEN_COLUMN"),
    ("SELECT * FROM dw.dim_kh", "FORBIDDEN_COLUMN"),
    ("SELECT luong_cb FROM dw.dim_nv", "FORBIDDEN_COLUMN"),
    ("SELECT SUM(thanh_tien) FROM dw.fct_ct_dh", "UNKNOWN_COLUMN"),
    ("SELECT * FROM dw.khong_ton_tai", "UNKNOWN_TABLE"),
    ("SELEC 1", "SYNTAX"),
])
def test_analyst_blocked(sql, code):
    v = validate(sql, ANALYST)
    assert not v.ok and v.errors[0]["code"] == code


def test_admin_can_read_raw_and_pii():
    assert validate("SELECT ma_kh, sdt FROM dw.dim_kh LIMIT 3", ADMIN).ok
    assert validate("SELECT COUNT(*) FROM raw.pos_don_hang", ADMIN).ok


def test_unknown_column_suggestion_uses_catalog_description():
    v = validate("SELECT SUM(f.thanh_tien) FROM dw.fct_ct_dh f", ANALYST)
    assert v.errors[0]["suggestion"] == "tt"


def test_rls_injected_and_effective():
    sql = "SELECT v.ten_vung, COUNT(*) FROM dw.fct_dh d JOIN dw.dim_cn c ON c.ma_cn = d.ma_cn JOIN dw.dim_vung v ON v.ma_vung = c.ma_vung GROUP BY 1"
    v = validate(sql, BAC)
    assert v.ok and "dw.fct_dh" in v.rls_applied and "dw.dim_cn" in v.rls_applied
    rows = get_warehouse().execute(v.sql_exec)["rows"]
    assert [r[0] for r in rows] == ["Miền Bắc"]


def test_limit_added():
    v = validate("SELECT ma_sp FROM dw.dim_sp", ANALYST)
    assert v.limit_added and "LIMIT" in v.sql_exec


# ------------------------------------------------------------------ chi phí
def test_partition_pruning_reduces_bytes():
    full = validate("SELECT SUM(f.tt) FROM dw.fct_ct_dh f", ADMIN)
    month = validate("SELECT SUM(f.tt) FROM dw.fct_ct_dh f WHERE f.ngay_dh >= DATE '2026-09-01' AND f.ngay_dh < DATE '2026-10-01'", ADMIN)
    b_full = cost.estimate(full.qualified)["total_bytes"]
    e_month = cost.estimate(month.qualified)
    assert e_month["tables"][0]["partition_filter"] == ["2026-09-01", "2026-10-01"]
    assert e_month["total_bytes"] < b_full / 10


def test_non_sargable_filter_not_pruned_and_linted():
    v = validate("SELECT SUM(f.tt) FROM dw.fct_ct_dh f WHERE YEAR(f.ngay_dh) = 2026 AND f.trang_thai = 'HOAN_THANH'", ADMIN)
    assert cost.estimate(v.qualified)["tables"][0]["partition_filter"] is None
    assert "L003" in {l["code"] for l in sqlcheck.lint(v)}


def test_hitl_decision_for_large_scan():
    v = validate("SELECT c.ten_cn, SUM(f.tt) FROM dw.fct_ct_dh f JOIN dw.dim_cn c ON c.ma_cn = f.ma_cn GROUP BY 1", ANALYST)
    d = cost.decide(cost.estimate(v.qualified), ANALYST.role_cfg)
    assert d["decision"] == "approval"


def test_lint_fanout_and_semi_additive():
    v = validate("SELECT SUM(d.tong_tt) FROM dw.fct_dh d JOIN dw.fct_ct_dh f ON f.ma_dh = d.ma_dh", ADMIN)
    assert "L004" in {l["code"] for l in sqlcheck.lint(v)}
    v = validate("SELECT SUM(sl_ton) FROM dw.fct_tk_tuan", ADMIN)
    assert "L007" in {l["code"] for l in sqlcheck.lint(v)}


# ------------------------------------------------------------------ semantic layer
def test_semantic_metric_definition_consistency():
    c = get_semantic().compile({"metrics": ["doanh_thu_thuan"], "dimensions": [], "time_range": {"start": "2026-09-01", "end": "2026-10-01"}})
    assert "SUM(f.tt - f.tien_tra)" in c["sql"] and "f.trang_thai = 'HOAN_THANH'" in c["sql"]
    v = validate(c["sql"], ADMIN)
    assert sqlcheck.matched_metrics(v)[0]["metric"] == "doanh_thu_thuan"


def test_semantic_rejects_unsupported_dimension():
    with pytest.raises(SemanticError):
        get_semantic().compile({"metrics": ["ke_hoach_dt"], "dimensions": ["nganh_hang"]})


def test_multi_fact_derived_metric_runs():
    c = get_semantic().compile({"metrics": ["ty_le_dat_kh"], "dimensions": ["vung"], "time_range": {"start": "2026-10-01", "end": "2026-11-01"}})
    r = get_warehouse().execute(validate(c["sql"], ADMIN).sql_exec)
    assert r["columns"][-1] == "ty_le_dat_kh" and r["row_count"] == 3


# ------------------------------------------------------------------ NLU tiếng Việt
def test_nlu_time_and_dims():
    r = get_nlu().parse("Top 5 cửa hàng có doanh thu thuần cao nhất tháng 10/2026")
    p = r.plan
    assert p["metrics"] == ["doanh_thu_thuan"] and p["dimensions"] == ["chi_nhanh"] and p["limit"] == 5
    assert p["time_range"]["start"] == "2026-10-01" and p["time_range"]["end"] == "2026-11-01"


def test_nlu_asks_clarification_for_ambiguous_revenue():
    r = get_nlu().parse("Doanh thu tháng trước?")
    assert r.clarification and r.clarification["id"] == "revenue_definition"


def test_nlu_followup_uses_memory():
    n = get_nlu()
    prev = n.parse("Doanh thu thuần theo miền tháng 10/2026").plan
    r = n.parse("so với cùng kỳ năm ngoái", {"last_plan": prev})
    assert r.follow_up and r.plan["compare"]["start"] == "2025-10-01"


def test_nlu_brand_not_confused_with_tier():
    p = get_nlu().parse("Top 5 thương hiệu có lợi nhuận gộp cao nhất tháng 10/2026").plan
    assert p["dimensions"] == ["thuong_hieu"] and not p["filters"]


# ------------------------------------------------------------------ diễn giải & audit
def test_explanation_numbers_all_verified():
    c = get_semantic().compile({"metrics": ["doanh_thu_thuan"], "dimensions": ["vung"], "time_range": {"start": "2026-10-01", "end": "2026-11-01", "label": "T10/2026"}})
    r = get_warehouse().execute(validate(c["sql"], ADMIN).sql_exec)
    e = explain.explain(c["columns"], r["columns"], r["rows"], {"metrics": ["doanh_thu_thuan"], "time_range": {"label": "T10/2026"}})
    a = explain.audit(e["text"], e["facts"], r["columns"], r["rows"])
    assert a["ok"] and a["facts_matched"] == a["facts_checked"]
    tampered = e["text"].replace(e["facts"]["top"]["text"], "999,99 tỷ ₫")
    assert not explain.audit(tampered, e["facts"], r["columns"], r["rows"])["ok"]


# ------------------------------------------------------------------ eval compare
def test_eval_compare_spider2_style():
    assert compare(["a", "b"], [[1, 2.0], [2, 3.0]], ["x", "b2", "a2"], [[9, 3.0, 2], [9, 2.0, 1]])[0]
    assert not compare(["a"], [[1], [2]], ["a"], [[1]])[0]

"""Đa doanh nghiệp: chọn workspace, doanh nghiệp thứ hai trả lời theo cấu hình riêng, công cụ nhận kho dữ liệu mới.

Mỗi tiến trình phục vụ một doanh nghiệp, nên phần kiểm thử doanh nghiệp thứ hai chạy trong tiến trình con với
biến môi trường WORKSPACE. Kho của workspace mẫu nằm trong var/ (không commit); thiếu thì bỏ qua và nêu lệnh tạo.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

import duckdb
import pytest
import yaml

from app import config, insights, onboard
from app.semantic import get_semantic

ROOT = pathlib.Path(__file__).resolve().parents[1]
XYZ = ROOT / "workspaces" / "giaovan_xyz"

PROBE = r"""
import json, sys
from app import dq, profile
from app.agent import get_agent
from app.governance import UserCtx, validate
from app.store import get_store
from app.warehouse import get_warehouse

agent, store = get_agent(), get_store()
admin = UserCtx("admin", "admin", {})
out = {"company": profile.get_profile()["company"]["name"], "schema": profile.serving_schema(), "intro": profile.intro_text()}
for key, q in (("by_region", "Số vận đơn theo miền tháng 9/2026"), ("revenue", "Doanh thu tháng trước là bao nhiêu?"),
               ("volume", "Sản lượng theo miền tháng 10/2026"), ("dest", "Số vận đơn giao tới Miền Trung tháng 6/2026"),
               ("why", "Vì sao số vận đơn tháng 9/2026 giảm so với tháng 8/2026?"), ("who", "bạn là ai?")):
    cid = store.create_conversation("admin", "t")
    r = agent.ask(admin, cid, q, eval_mode=True, mode="offline")
    store.delete_conversation(cid)
    out[key] = {"type": r["type"], "sql": r.get("sql"), "rows": r.get("rows"), "text": r.get("text"),
                "options": (r.get("clarification") or {}).get("options"),
                "best_dimension": (r.get("variance") or {}).get("best_dimension")}
analyst = UserCtx("analyst", "analyst", {})
trung = UserCtx("analyst_trung", "analyst", {"mien": ["Miền Trung"]})
out["pii"] = validate("SELECT phone FROM mart.dim_customer", analyst).errors[0]["code"]
out["other_business_table"] = validate("SELECT COUNT(*) FROM dw.fct_ct_dh", admin).errors[0]["code"]
out["system_schema"] = validate("SELECT * FROM information_schema.tables", admin).errors[0]["code"]
out["default_schema"] = validate("SELECT COUNT(*) FROM fact_shipment", analyst).tables
v = validate("SELECT h.region, COUNT(*) FROM mart.fact_shipment s JOIN mart.dim_hub h ON h.hub_id = s.origin_hub_id GROUP BY 1", trung)
out["rls_rows"] = get_warehouse().execute(v.sql_exec)["rows"]
out["lint"] = [l["code"] for l in __import__("app.sqlcheck", fromlist=["lint"]).lint(validate("SELECT SUM(shipping_fee) FROM mart.fact_shipment", admin))]
out["dq"] = sorted({f["check"] for f in dq.run_checks()["findings"]})
from app.lineage import get_model_lineage
out["lineage_nodes"] = [n["id"] for n in get_model_lineage().graph()["nodes"]]
summary = profile.public_summary()
out["console_example"], out["console_tries"] = summary["console_example"], summary["console_tries"]
out["console_example_ok"] = validate(summary["console_example"], admin).ok
out["starters"] = {}
for g in summary["starters"]:
    for q in g["questions"]:
        cid = store.create_conversation("admin", "t")
        out["starters"][q] = agent.ask(admin, cid, q, eval_mode=True, mode="offline")["type"]
        store.delete_conversation(cid)
print("PROBE=" + json.dumps(out, ensure_ascii=False, default=str))
"""


def _run(workspace: str, code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, WORKSPACE=workspace, PYTHONIOENCODING="utf-8",
               APP_DB=str(pathlib.Path(tempfile.gettempdir()) / f"data01_test_{workspace}.sqlite"))
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8")


@pytest.fixture(scope="module")
def xyz():
    if not (XYZ / "var" / "warehouse.duckdb").exists() or not (XYZ / "var" / "catalog_stats.json").exists():
        pytest.skip("Chưa có kho của workspace mẫu. Chạy: python workspaces/giaovan_xyz/warehouse/generate.py "
                    "&& python -m app.onboard giaovan_xyz --stats")
    p = _run("giaovan_xyz", PROBE)
    line = next((l for l in p.stdout.splitlines() if l.startswith("PROBE=")), None)
    assert line, p.stderr[-2000:]
    return json.loads(line[len("PROBE="):])


# ------------------------------------------------------------------ chọn workspace
def test_default_workspace_is_the_sample_business_at_project_root():
    assert config.WORKSPACE == "" and config.CONFIG == ROOT / "config" and config.VAR == ROOT / "var"
    assert "" in config.list_workspaces() and "giaovan_xyz" in config.list_workspaces()


def test_unknown_workspace_stops_with_a_helpful_message():
    p = _run("khong_ton_tai", "import app.config")
    assert p.returncode != 0 and "khong_ton_tai" in p.stderr and "app.onboard" in p.stderr


# ------------------------------------------------------------------ doanh nghiệp thứ hai chạy bằng cấu hình riêng
def test_second_business_introduces_itself_from_its_own_profile(xyz):
    assert xyz["company"] == "Công ty CP Giao vận XYZ" and xyz["schema"] == "mart"
    assert "Trợ lý dữ liệu XYZ" in xyz["who"]["text"] and "chuyển phát" in xyz["who"]["text"]
    assert "ABC" not in xyz["intro"] and "doanh thu thuần" not in xyz["intro"].lower()


def test_second_business_answers_with_its_own_metric_definition(xyz):
    r = xyz["by_region"]
    assert r["type"] == "result" and "s.status <> 'CANCELLED'" in r["sql"] and "mart.dim_hub" in r["sql"]
    assert sorted(x[0] for x in r["rows"]) == ["Miền Bắc", "Miền Nam", "Miền Trung"]


def test_second_business_has_its_own_ambiguous_terms(xyz):
    """"Doanh thu" ở XYZ là cước ghi nhận hay cước phát sinh; "sản lượng" là số đơn hay số kg — khác hẳn ABC."""
    assert xyz["revenue"]["type"] == "clarification"
    assert any("Cước phát sinh" in o for o in xyz["revenue"]["options"]) and not any("thuần" in o for o in xyz["revenue"]["options"])
    assert xyz["volume"]["type"] == "clarification" and any("kg" in o for o in xyz["volume"]["options"])


def test_second_business_context_changes_which_dimension_a_value_filters(xyz):
    assert "dest_hub.region IN ('Miền Trung')" in xyz["dest"]["sql"]          # "giao tới" = miền phát, không phải miền gửi


def test_second_business_variance_analysis_runs_without_the_retail_pack(xyz):
    assert xyz["why"]["type"] == "variance" and xyz["why"]["best_dimension"] in ("mien", "tinh", "buu_cuc", "phan_khuc")


def test_second_business_governance_follows_its_own_policies(xyz):
    assert xyz["pii"] == "FORBIDDEN_COLUMN"
    assert xyz["other_business_table"] == "FORBIDDEN_SCHEMA"                   # schema dw không tồn tại ở doanh nghiệp này
    assert xyz["system_schema"] == "FORBIDDEN_SCHEMA"
    assert xyz["default_schema"] == ["mart.fact_shipment"]
    assert [r[0] for r in xyz["rls_rows"]] == ["Miền Trung"]
    assert "L006" in xyz["lint"]                                                # tổng cước mà không lọc trạng thái


def test_second_business_gets_generic_data_quality_checks(xyz):
    assert "freshness" in xyz["dq"] and "distribution_drift" in xyz["dq"]


def test_second_business_never_shows_the_other_business_lineage(xyz):
    """Workspace nhận kho dựng sẵn không có models.sql: sơ đồ nguồn gốc phải rỗng, không được lấy của doanh nghiệp mẫu ở gốc."""
    assert xyz["lineage_nodes"] == []
    assert not (XYZ / "var" / "model_lineage.json").exists()


def test_second_business_sql_page_uses_its_own_example(xyz):
    assert "mart.fact_shipment" in xyz["console_example"] and "dw." not in xyz["console_example"] and xyz["console_example_ok"]
    assert xyz["console_tries"][0] == "DROP TABLE mart.fact_shipment"


def test_second_business_welcome_questions_all_get_answers(xyz):
    assert len(xyz["starters"]) >= 4
    assert all(t in ("result", "variance", "info", "clarification") for t in xyz["starters"].values()), xyz["starters"]


# ------------------------------------------------------------------ phân tích biến động không cần khai báo
def test_variance_drivers_are_chosen_automatically_when_not_declared(monkeypatch):
    sem = get_semantic()
    declared = insights.analysis_config(sem, "doanh_thu_thuan")
    assert declared["drivers"][0] == "vung" and declared["decomposition"]["count"] == "so_don"
    monkeypatch.setitem(sem.cfg, "analysis", {})
    auto = insights.analysis_config(sem, "doanh_thu_thuan")
    assert auto["drivers"] and len(auto["drivers"]) <= insights.MAX_AUTO_DRIVERS and auto["decomposition"] is None
    assert all(sem.dims[d].get("kind") != "time" and 2 <= len(sem.dim_values(d)) <= insights.MAX_DRIVER_VALUES for d in auto["drivers"])


# ------------------------------------------------------------------ nhận một kho dữ liệu mới
@pytest.fixture
def small_db(tmp_path):
    path = tmp_path / "nguon.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA sales")
    con.execute("CREATE TABLE sales.dim_store AS SELECT 'S' || i AS store_id, 'Cửa hàng ' || i AS store_name, "
                "CASE WHEN i % 2 = 0 THEN 'Hà Nội' ELSE 'Đà Nẵng' END AS city, '09' || i AS phone FROM range(1, 9) t(i)")
    con.execute("CREATE TABLE sales.fact_orders AS SELECT 'O' || i AS order_id, DATE '2026-01-01' + CAST(i % 90 AS INTEGER) AS order_date, "
                "'S' || (1 + i % 8) AS store_id, CASE WHEN i % 3 = 0 THEN 'Online' ELSE 'Tại quầy' END AS channel, "
                "100000.0 + i AS amount, 1 + i % 4 AS qty FROM range(1, 601) t(i)")
    con.execute("CREATE TABLE sales.fact_budget AS SELECT DATE '2026-01-01' + INTERVAL (i % 3) MONTH AS month, 'S' || (1 + i % 8) AS store_id, "
                "5000000.0 AS budget_amount FROM range(0, 24) t(i)")
    con.close()
    return path


def test_onboard_profiles_the_warehouse_and_drafts_a_working_config(small_db, tmp_path):
    res = onboard.onboard("thu_nghiem", db=str(small_db), company="Công ty Thử", ws_root=tmp_path / "ws", log=lambda *_: None)
    ws = pathlib.Path(res["workspace"])
    assert res["schema"] == "sales" and set(res["written"]) >= {"profile.yml", "catalog.yml", "semantic_layer.yml", "policies.yml"}
    stats = json.loads((ws / "var" / "catalog_stats.json").read_text(encoding="utf-8"))
    assert stats["tables"]["sales.fact_orders"]["rows"] == 600
    assert sorted(stats["tables"]["sales.fact_orders"]["columns"]["channel"]["values"]) == ["Online", "Tại quầy"]
    assert "values" not in stats["tables"]["sales.dim_store"]["columns"]["phone"]            # cột nhạy cảm không lưu danh mục giá trị
    sem = yaml.safe_load((ws / "config" / "semantic_layer.yml").read_text(encoding="utf-8"))
    orders = sem["models"]["orders"]
    assert orders["table"] == "sales.fact_orders" and orders["time_column"].endswith(".order_date") and orders["time_grain_min"] == "day"
    assert orders["joins"]["store"]["on"].endswith("store_id = store.store_id")
    assert sem["models"]["budget"]["time_grain_min"] == "month"                              # mọi ngày đều là mùng 1 -> độ chi tiết tháng
    assert sem["dimensions"]["city"]["expr"] == "store.city" and sem["dimensions"]["channel"]["expr"].endswith(".channel")
    assert sem["metrics"]["tong_amount"]["format"] == "currency" and sem["metrics"]["so_dong_orders"]["sql"] == "COUNT(*)"
    profile = yaml.safe_load((ws / "config" / "profile.yml").read_text(encoding="utf-8"))
    assert profile["company"]["name"] == "Công ty Thử" and profile["warehouse"]["anchor"] == {"table": "sales.fact_orders", "column": "order_date"}
    catalog = yaml.safe_load((ws / "config" / "catalog.yml").read_text(encoding="utf-8"))
    assert catalog["tables"]["sales.dim_store"]["columns"]["phone"]["sensitivity"] == "pii"
    assert json.loads((ws / "eval" / "questions.json").read_text(encoding="utf-8")) == []


def test_onboard_never_overwrites_reviewed_config_unless_forced(small_db, tmp_path):
    kw = dict(db=str(small_db), ws_root=tmp_path / "ws", log=lambda *_: None)
    ws = pathlib.Path(onboard.onboard("thu_nghiem", **kw)["workspace"])
    (ws / "config" / "profile.yml").write_text("company: {name: Đã rà soát}\nwarehouse: {schema: sales}\n", encoding="utf-8")
    again = onboard.onboard("thu_nghiem", **kw)
    assert "profile.yml" in again["kept"] and "Đã rà soát" in (ws / "config" / "profile.yml").read_text(encoding="utf-8")
    stats_only = onboard.onboard("thu_nghiem", stats_only=True, ws_root=tmp_path / "ws", log=lambda *_: None)
    assert stats_only["written"] == [] and stats_only["tables"] == 3
    forced = onboard.onboard("thu_nghiem", force=True, **kw)
    assert "profile.yml" in forced["written"]


def test_onboard_tells_dimension_from_fact_without_naming_convention(tmp_path):
    con = duckdb.connect()
    con.execute("CREATE SCHEMA app")
    con.execute("CREATE TABLE app.stores AS SELECT i AS store_id, 'CH ' || i AS name, CASE WHEN i % 2 = 0 THEN 'A' ELSE 'B' END AS tier FROM range(1, 11) t(i)")
    con.execute("CREATE TABLE app.orders AS SELECT i AS order_id, DATE '2026-01-01' + CAST(i % 60 AS INTEGER) AS created_at, "
                "1 + i % 10 AS store_id, 50.0 * i AS total_amount FROM range(1, 401) t(i)")
    kinds = {fq: t["kind"] for fq, t in onboard.classify(onboard.compute_stats(con, ["app"]), "app").items()}
    assert kinds == {"app.stores": "dimension", "app.orders": "fact"}


def test_onboard_rejects_bad_workspace_name(tmp_path):
    with pytest.raises(SystemExit):
        onboard.onboard("Tên Có Dấu", ws_root=tmp_path)

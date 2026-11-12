"""
Đưa một kho DuckDB bất kỳ vào hệ thống thành một workspace (một doanh nghiệp).

    python -m app.onboard <ten> --db duong/dan/kho.duckdb [--schema mart] [--name "Công ty ..."]
    python -m app.onboard <ten> --stats          # dữ liệu đổi: chỉ tính lại thống kê catalog

Bước 1 tính thống kê catalog (số dòng, độ rộng cột, tỷ lệ NULL, giá trị phân biệt, phân bố theo ngày) vào
workspaces/<ten>/var/catalog_stats.json. Thống kê này nuôi bộ ước lượng chi phí, bộ tìm schema và danh mục giá trị lọc.

Bước 2 sinh NHÁP bộ cấu hình trong workspaces/<ten>/config/ (profile, catalog, semantic_layer, policies...). Bản nháp
chỉ suy ra từ tên cột, kiểu dữ liệu và thống kê: bảng nào là sự kiện, bảng nào là chiều, nối với nhau qua khóa nào,
cột nào đếm/cộng được. Nó KHÔNG biết nghiệp vụ, nên người am hiểu phải rà lại trước khi dùng: nhãn tiếng Việt và từ
đồng nghĩa, công thức và điều kiện lọc của từng chỉ số, cột nhạy cảm, thuật ngữ mơ hồ, phân quyền theo dòng.
File cấu hình đã có thì không bị ghi đè (trừ khi thêm --force).

Sau đó chạy:  WORKSPACE=<ten> python -m uvicorn app.main:app
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import sys
import time

import duckdb
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "main", "temp"}
NUMERIC = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "DOUBLE", "FLOAT", "DECIMAL", "REAL", "UTINYINT",
           "USMALLINT", "UINTEGER", "UBIGINT")
TYPE_WIDTH = {"BOOLEAN": 1, "TINYINT": 1, "SMALLINT": 2, "INTEGER": 4, "BIGINT": 8, "HUGEINT": 16, "FLOAT": 4, "DOUBLE": 8,
              "DATE": 4, "TIMESTAMP": 8, "TIME": 8, "UTINYINT": 1, "USMALLINT": 2, "UINTEGER": 4, "UBIGINT": 8,
              "TIMESTAMP WITH TIME ZONE": 8, "INTERVAL": 16}
MAX_VALUES = 400              # cột văn bản có tối đa từng này giá trị phân biệt thì lưu danh mục giá trị
MIN_PARTITION_ROWS = 5000     # bảng từ cỡ này trở lên mới coi cột ngày là cột phân vùng
PII_RE = re.compile(r"(phone|mobile|sdt|so_dt|dien_thoai|e_?mail|ho_ten|full_?name|cmnd|cccd|passport|address|dia_chi|"
                    r"salary|luong|birth|ngay_sinh)", re.I)
MONEY_RE = re.compile(r"(amount|fee|price|cost|revenue|sales|tien|gia_|_gia|phi_|_phi|doanh_thu|thanh_tien|cod)", re.I)
KEY_RE = re.compile(r"(^id$|_id$|_code$|_key$|^ma_|^id_)", re.I)
DENIED_FUNCTIONS = ["read_csv", "read_csv_auto", "read_parquet", "parquet_scan", "read_json", "read_json_auto",
                    "read_json_objects", "read_text", "read_blob", "read_ndjson", "glob", "sniff_csv", "getenv",
                    "current_setting", "duckdb_secrets", "duckdb_settings", "query", "query_table", "iceberg_scan",
                    "delta_scan", "sqlite_scan", "postgres_scan", "mysql_scan"]


def _is_num(t: str) -> bool:
    return t.startswith(NUMERIC)


def _is_date(t: str) -> bool:
    return t.startswith(("DATE", "TIMESTAMP"))


# ====================================================================== bước 1: thống kê catalog
def compute_stats(con, schemas: list[str], partitions: dict | None = None, pii: set | None = None) -> dict:
    """Thống kê mọi bảng trong các schema đã cho. partitions: {bảng: cột ngày} khai báo tay; thiếu thì tự chọn."""
    partitions, pii = dict(partitions or {}), set(pii or ())
    marks = ", ".join("?" for _ in schemas)
    tables = con.execute(f"SELECT schema_name, table_name FROM duckdb_tables() WHERE schema_name IN ({marks}) "
                         "ORDER BY schema_name, table_name", schemas).fetchall()
    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "tables": {}, "meta": {}}
    for schema, table in tables:
        fq = f"{schema}.{table}"
        cols = con.execute("SELECT column_name, data_type FROM duckdb_columns() WHERE schema_name = ? AND table_name = ? "
                           "ORDER BY column_index", [schema, table]).fetchall()
        n = con.execute(f"SELECT COUNT(*) FROM {fq}").fetchone()[0]
        agg = []
        for c, t in cols:
            q = f'"{c}"'
            agg += [f"AVG(CASE WHEN {q} IS NULL THEN 1.0 ELSE 0.0 END)", f"APPROX_COUNT_DISTINCT({q})",
                    f"AVG(STRLEN({q}))" if t.startswith("VARCHAR") else "NULL",
                    f"CAST(MIN({q}) AS VARCHAR)" if _is_num(t) or _is_date(t) else "NULL",
                    f"CAST(MAX({q}) AS VARCHAR)" if _is_num(t) or _is_date(t) else "NULL"]
        row = con.execute(f"SELECT {', '.join(agg)} FROM {fq}").fetchone() if cols and n else [None] * (5 * len(cols))
        cstats = {}
        for i, (c, t) in enumerate(cols):
            nullf, nd, avgw, mn, mx = row[5 * i: 5 * i + 5]
            width = float(avgw or 0) + 4.0 if t.startswith("VARCHAR") else 8.0 if t.startswith("DECIMAL") else float(TYPE_WIDTH.get(t, 8))
            cs = {"type": t, "null_frac": round(float(nullf or 0), 4), "distinct": int(nd or 0),
                  "avg_width": round(width, 2), "min": mn, "max": mx}
            if t.startswith("VARCHAR") and 0 < (nd or 0) <= MAX_VALUES and c not in pii and not PII_RE.search(c):
                vals = con.execute(f'SELECT "{c}", COUNT(*) FROM {fq} WHERE "{c}" IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1 '
                                   f"LIMIT {MAX_VALUES}").fetchall()
                cs["values"] = [v[0] for v in vals]
            cstats[c] = cs
        ts = {"schema": schema, "table": table, "rows": n, "columns": cstats,
              "row_width": round(sum(c["avg_width"] for c in cstats.values()), 2)}
        ts["bytes"] = int(ts["row_width"] * n)
        pcol = partitions.get(fq) or _guess_partition(cstats, n)
        if pcol and pcol in cstats and n:
            parts = con.execute(f'SELECT CAST(CAST("{pcol}" AS DATE) AS VARCHAR), COUNT(*) FROM {fq} GROUP BY 1 ORDER BY 1').fetchall()
            ts["partition"] = {"column": pcol, "granularity": "day", "counts": {d: c for d, c in parts if d}}
        out["tables"][fq] = ts
    return out


def _guess_partition(cols: dict, rows: int) -> str | None:
    """Cột ngày đại diện cho thời điểm phát sinh của một bảng sự kiện: ít NULL nhất, rồi tới nhiều ngày nhất."""
    dates = [(c["null_frac"], -c["distinct"], name) for name, c in cols.items() if _is_date(c["type"]) and c["distinct"] >= 30]
    return min(dates)[2] if dates and rows >= MIN_PARTITION_ROWS else None


# ====================================================================== bước 2: nháp cấu hình
def _short(table: str) -> str:
    return re.sub(r"^(dim|fact|fct|d|f)_", "", table)


def _label(name: str) -> str:
    return name.replace("_", " ").strip().capitalize()


def classify(stats: dict, schema: str) -> dict:
    """Phân loại bảng (chiều / sự kiện) và cột (khóa / ngày / số đo / phân loại) từ thống kê."""
    tables = {}
    for fq, t in stats["tables"].items():
        if t["schema"] != schema:
            continue
        cols = {"key": [], "date": [], "measure": [], "category": [], "pii": []}
        for c, s in t["columns"].items():
            if PII_RE.search(c):
                cols["pii"].append(c)
            elif KEY_RE.search(c):
                cols["key"].append(c)
            elif _is_date(s["type"]):
                cols["date"].append(c)
            elif _is_num(s["type"]):
                cols["measure"].append(c)
            elif s.get("values") and len(s["values"]) >= 2:
                cols["category"].append(c)
        pk = next((c for c in cols["key"] if t["rows"] and t["columns"][c]["distinct"] >= 0.9 * t["rows"]), None)
        tables[fq] = {"name": t["table"], "rows": t["rows"], "cols": cols, "pk": pk, "kind": None}
    for fq, t in tables.items():
        # 1) theo quy ước đặt tên kho dữ liệu (dim_*, fact_*/fct_*) nếu có
        if re.match(r"(dim|d)_", t["name"]):
            t["kind"] = "dimension" if t["pk"] else None
        elif re.match(r"(fact|fct|f)_", t["name"]):
            t["kind"] = "fact" if t["cols"]["date"] else None
        if t["kind"] or re.match(r"(dim|d|fact|fct|f)_", t["name"]):
            continue
        # 2) không theo quy ước: bảng chiều là bảng có khóa chính được một bảng lớn hơn hẳn tham chiếu tới
        refs = [o for ofq, o in tables.items() if ofq != fq and t["pk"] and o["pk"] != t["pk"]
                and any(k == t["pk"] or k.endswith("_" + t["pk"]) for k in o["cols"]["key"])]
        if any(o["rows"] >= 2 * t["rows"] for o in refs):
            t["kind"] = "dimension"
        elif t["cols"]["date"]:
            t["kind"] = "fact"
    return tables


def draft_semantic(con, stats: dict, schema: str) -> dict:
    """Nháp semantic layer: mỗi bảng sự kiện một model, mỗi cột phân loại một chiều, mỗi cột số một chỉ số tổng."""
    tables = classify(stats, schema)
    dims_tbl = {fq: t for fq, t in tables.items() if t["kind"] == "dimension"}
    models, dimensions, metrics, used_alias = {}, {}, {}, set()
    for n, lab, g in (("ngay", "Ngày", "day"), ("tuan", "Tuần", "week"), ("thang", "Tháng", "month"), ("quy", "Quý", "quarter"),
                      ("nam", "Năm", "year")):
        dimensions[n] = {"label": lab, "kind": "time", "grain": g, "synonyms": [lab.lower(), f"theo {lab.lower()}"]}
    join_alias: dict = {}       # (model, bảng chiều) -> bí danh đầu tiên dùng cho bảng chiều đó trong model
    for fq, t in sorted(tables.items(), key=lambda x: -x[1]["rows"]):
        if t["kind"] != "fact":
            continue
        name = _short(t["name"])
        alias = next(a for a in (name[0], name[:2], name[:3], name) if a not in used_alias)
        used_alias.add(alias)
        st = stats["tables"][fq]["columns"]
        tcol = min(t["cols"]["date"], key=lambda c: (st[c]["null_frac"], -st[c]["distinct"]))
        monthly = con.execute(f'SELECT COUNT(*) FROM {fq} WHERE DAY("{tcol}") <> 1').fetchone()[0] == 0
        joins = {}
        for k in t["cols"]["key"]:
            for dfq, d in dims_tbl.items():
                if k == d["pk"] or k.endswith("_" + d["pk"]):
                    base = _short(d["name"])
                    ja = base if k == d["pk"] else f"{k[: -len(d['pk']) - 1]}_{base}"
                    joins[ja] = {"table": dfq, "on": f"{alias}.{k} = {ja}.{d['pk']}"}
                    join_alias.setdefault((name, dfq), ja)
        models[name] = {"label": _label(name), "table": fq, "alias": alias, "time_column": f"{alias}.{tcol}",
                        "time_grain_min": "month" if monthly else "day", "grain": "", "joins": joins}
        metrics[f"so_dong_{name}"] = {"label": f"Số dòng {_label(name).lower()}", "model": name, "sql": "COUNT(*)",
                                      "format": "number", "description": "", "synonyms": []}
        for c in t["cols"]["measure"]:
            metrics[f"tong_{c}"] = {"label": f"Tổng {_label(c).lower()}", "model": name, "sql": f"SUM({alias}.{c})",
                                    "format": "currency" if MONEY_RE.search(c) else "number", "description": "", "synonyms": []}
        for c in t["cols"]["category"]:
            dn = c if c not in dimensions else f"{name}_{c}"
            dimensions[dn] = {"label": _label(c), "expr": f"{alias}.{c}", "synonyms": []}
    for dfq, d in dims_tbl.items():
        base = _short(d["name"])
        per_model = {m: a for (m, x), a in join_alias.items() if x == dfq}
        if not per_model:
            continue
        canon = base if base in per_model.values() else next(iter(per_model.values()))
        for c in d["cols"]["category"]:
            dn = c if c not in dimensions else f"{base}_{c}"
            dim = {"label": _label(c), "expr": f"{canon}.{c}", "synonyms": []}
            other = {m: f"{a}.{c}" for m, a in per_model.items() if a != canon}
            if other:
                dim["expr_by_model"] = other
            dimensions[dn] = dim
    return {"version": 1, "models": models, "dimensions": dimensions, "metrics": metrics, "ambiguities": []}


def draft_configs(con, stats: dict, schema: str, company: str) -> dict[str, str]:
    """Trả {tên file: nội dung} của bộ cấu hình nháp."""
    tables = classify(stats, schema)
    sem = draft_semantic(con, stats, schema)
    facts = sorted((t for t in tables.values() if t["kind"] == "fact"), key=lambda t: -t["rows"])
    main = next((m for m in sem["models"].values()), None)
    profile = {
        "version": 1,
        "company": {"name": company, "short_name": "", "industry": "", "description": ""},
        "assistant": {"name": "Trợ lý dữ liệu", "language": "Vietnamese"},
        "warehouse": {"engine": "DuckDB", "schema": schema, "schema_note": "",
                      "anchor": {"table": main["table"], "column": main["time_column"].split(".")[1]} if main else None,
                      "core_tables": list(dict.fromkeys([main["table"]] + [j["table"] for j in main["joins"].values()]))[:6] if main else [],
                      "notes": []},
        "out_of_scope": ["dự báo tương lai hay khuyến nghị đầu tư", "sửa, xóa dữ liệu (chỉ đọc)",
                         "trả dữ liệu cá nhân hoặc cột bị hạn chế theo vai trò của bạn", "câu hỏi ngoài dữ liệu của doanh nghiệp"],
        "sample_questions": [],
    }
    catalog = {"lint": {}, "tables": {}}
    for fq, t in stats["tables"].items():
        if t["schema"] != schema:
            continue
        cols = {}
        for c in t["columns"]:
            cols[c] = {"description": ""}
            if PII_RE.search(c):
                cols[c]["sensitivity"] = "pii"
        catalog["tables"][fq] = {"description": "", "synonyms": [], "grain": "", "columns": cols}
    role = lambda label, deny, admin, mult: {
        "label": label, "statements": ["SELECT"], "schemas": [schema], "deny_sensitivity": deny,
        "can_manage_schema": admin, "can_view_all_logs": admin,
        "cost": {"warn_bytes": 20_000_000 * mult, "approval_bytes": 60_000_000 * mult, "max_bytes": 400_000_000 * mult},
        "max_rows": 5000 * mult}
    policies = {
        "roles": {"analyst": role("Analyst (chỉ đọc)", ["pii", "confidential"], False, 1),
                  "admin": role("Admin (quản trị schema)", [], True, 4)},
        "hitl": {"large_table_rows": 100000, "hitl_min_bytes": 15_000_000},
        "users": {"admin": {"password": "admin123", "role": "admin", "full_name": "Quản trị dữ liệu"},
                  "analyst": {"password": "analyst123", "role": "analyst", "full_name": "Chuyên viên phân tích"}},
        "rls": {}, "denied_functions": DENIED_FUNCTIONS,
    }
    head = ("# NHÁP sinh tự động bởi `python -m app.onboard` từ tên cột, kiểu dữ liệu và thống kê của kho.\n"
            "# Công cụ không biết nghiệp vụ: hãy rà lại {what} trước khi dùng.\n")
    dump = lambda d: yaml.safe_dump(d, allow_unicode=True, sort_keys=False, width=140)
    n_fact, n_dim = len(facts), sum(1 for t in tables.values() if t["kind"] == "dimension")
    return {
        "profile.yml": head.format(what="tên doanh nghiệp, ngành, mô tả, câu hỏi mẫu") + dump(profile),
        "catalog.yml": head.format(what="mô tả bảng/cột, từ đồng nghĩa, cột nhạy cảm (sensitivity: pii | confidential)") + dump(catalog),
        "semantic_layer.yml": head.format(what=f"nhãn tiếng Việt, từ đồng nghĩa, công thức và bộ lọc của chỉ số "
                                               f"({n_fact} bảng sự kiện, {n_dim} bảng chiều nhận diện được)") + dump(sem),
        "policies.yml": head.format(what="MẬT KHẨU mặc định, vai trò, phân quyền theo dòng (rls)") + dump(policies),
        "contracts.yml": "# Data contract: {bảng nguồn: [danh sách cột mong đợi]} để phát hiện schema drift. Để trống nếu chưa dùng.\n{}\n",
        "fewshots.yml": "# Ví dụ câu hỏi -> SQL đã xác nhận: [{question: ..., sql: ...}]. Để trống nếu chưa có.\n[]\n",
    }


# ====================================================================== lệnh
def onboard(name: str, db: str | None = None, schema: str | None = None, company: str | None = None,
            stats_only: bool = False, force: bool = False, ws_root: pathlib.Path | None = None, log=print) -> dict:
    if not re.fullmatch(r"[a-z0-9_]+", name):
        raise SystemExit("Tên workspace chỉ gồm chữ thường không dấu, số và gạch dưới.")
    ws = (ws_root or ROOT / "workspaces") / name
    target = ws / "var" / "warehouse.duckdb"
    for sub in ("config", "var", "eval"):
        (ws / sub).mkdir(parents=True, exist_ok=True)
    if db and pathlib.Path(db).resolve() != target.resolve():
        shutil.copyfile(db, target)
    if not target.exists():
        raise SystemExit(f"Chưa có kho dữ liệu {target}. Truyền --db <file.duckdb>.")
    existing = {}
    if (ws / "config" / "profile.yml").exists():
        existing = yaml.safe_load((ws / "config" / "profile.yml").read_text(encoding="utf-8")) or {}
    con = duckdb.connect(str(target), read_only=True)
    try:
        found = [r[0] for r in con.execute("SELECT schema_name, COUNT(*) FROM duckdb_tables() GROUP BY 1 ORDER BY 2 DESC").fetchall()
                 if r[0] not in SYSTEM_SCHEMAS] or ["main"]
        schema = schema or (existing.get("warehouse") or {}).get("schema") or found[0]
        if schema not in found:
            raise SystemExit(f"Không thấy schema '{schema}' trong kho. Schema có bảng: {', '.join(found)}.")
        cat_yaml = {}
        if (ws / "config" / "catalog.yml").exists():
            cat_yaml = yaml.safe_load((ws / "config" / "catalog.yml").read_text(encoding="utf-8")) or {}
        pii = {c for t in (cat_yaml.get("tables") or {}).values() for c, d in (t.get("columns") or {}).items()
               if (d or {}).get("sensitivity")}
        stats = compute_stats(con, found, (existing.get("warehouse") or {}).get("partitions"), pii)
        (ws / "var" / "catalog_stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        log(f"Thống kê: {len(stats['tables'])} bảng -> {ws / 'var' / 'catalog_stats.json'}")
        written, kept = [], []
        if not stats_only:
            files = draft_configs(con, stats, schema, company or name)
            for fname, text in files.items():
                p = ws / "config" / fname
                if p.exists() and not force:
                    kept.append(fname)
                    continue
                p.write_text(text, encoding="utf-8")
                written.append(fname)
            q = ws / "eval" / "questions.json"
            if not q.exists():
                q.write_text("[]\n", encoding="utf-8")
            log(f"Nháp cấu hình đã ghi: {', '.join(written) or '(không có)'}" + (f"; giữ nguyên file đã có: {', '.join(kept)}" if kept else ""))
            log(f"Rà lại các file trong {ws / 'config'} rồi chạy: WORKSPACE={name} python -m uvicorn app.main:app")
    finally:
        con.close()
    return {"workspace": str(ws), "schema": schema, "tables": len(stats["tables"]), "written": written, "kept": kept}


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Tạo workspace cho một doanh nghiệp mới từ một kho DuckDB.")
    ap.add_argument("name", help="tên workspace (chữ thường không dấu, số, gạch dưới)")
    ap.add_argument("--db", help="file DuckDB nguồn (được chép vào workspaces/<tên>/var/warehouse.duckdb)")
    ap.add_argument("--schema", help="schema phục vụ phân tích (mặc định: schema có nhiều bảng nhất)")
    ap.add_argument("--name", dest="company", help="tên doanh nghiệp")
    ap.add_argument("--stats", action="store_true", help="chỉ tính lại thống kê catalog, không đụng tới cấu hình")
    ap.add_argument("--force", action="store_true", help="ghi đè file cấu hình đã có bằng bản nháp mới")
    a = ap.parse_args()
    onboard(a.name, a.db, a.schema, a.company, a.stats, a.force)


if __name__ == "__main__":
    main()

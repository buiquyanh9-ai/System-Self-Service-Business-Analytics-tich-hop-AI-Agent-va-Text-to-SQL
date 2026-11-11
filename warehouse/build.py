"""
Chạy các model biến đổi (warehouse/models.sql) theo thứ tự: raw/ref -> stg -> dw, rồi tính thống kê catalog
(số dòng, độ rộng cột, tỷ lệ NULL, phân vùng theo ngày) phục vụ ước lượng chi phí quét và semantic search.

    python warehouse/build.py            # build toàn bộ
    python warehouse/build.py --stats    # chỉ tính lại thống kê
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time

import duckdb

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "var" / "warehouse.duckdb"
MODELS_FILE = ROOT / "warehouse" / "models.sql"
RULES_FILE = ROOT / "var" / "active_rules.json"
STATS_FILE = ROOT / "var" / "catalog_stats.json"

# Cột phân vùng (mô phỏng partitioning theo ngày như BigQuery) -> dùng để ước lượng partition pruning
PARTITIONS = {
    "dw.fct_ct_dh": "ngay_dh", "dw.fct_dh": "ngay_dh", "dw.fct_th": "ngay_tra", "dw.fct_tk_tuan": "ngay_chot",
    "dw.fct_nk": "ngay_nk", "stg.stg_don_hang": "ngay_dh", "raw.pos_don_hang": "ngay_gio",
    "raw.erp_ton_kho": "ngay_chot", "raw.erp_nhap_kho": "ngay_nhap", "raw.erp_tra_hang": "ngay_tra",
}
PII_COLUMNS = {"ho_ten", "sdt", "email", "luong_cb", "luong_co_ban", "customer_id_raw"}

RULE_RE = re.compile(r"\{\{\s*rule\(\s*'([a-zA-Z0-9_]+)'\s*,\s*'([^']*)'\s*\)\s*\}\}")


def parse_models(text: str | None = None) -> list[dict]:
    """Tách models.sql thành danh sách model {name, desc, sql_template}."""
    text = text if text is not None else MODELS_FILE.read_text(encoding="utf-8")
    models, cur = [], None
    for line in text.splitlines():
        m = re.match(r"--\s*@model\s+(\S+)", line)
        if m:
            if cur:
                models.append(cur)
            cur = {"name": m.group(1), "desc": "", "lines": []}
            continue
        if cur is None:
            continue
        d = re.match(r"--\s*@desc\s+(.*)", line)
        if d:
            cur["desc"] = d.group(1).strip()
            continue
        cur["lines"].append(line)
    if cur:
        models.append(cur)
    for m in models:
        m["sql_template"] = "\n".join(m.pop("lines")).strip().rstrip(";")
    return models


def load_rules() -> dict:
    if RULES_FILE.exists():
        return json.loads(RULES_FILE.read_text(encoding="utf-8"))
    return {"fixes": []}


def render(sql_template: str, rules: dict | None = None) -> str:
    """Thay {{ rule('slot','default') }} bằng biểu thức đã áp các fix đang bật (theo thứ tự)."""
    rules = rules if rules is not None else load_rules()
    fixes = [f for f in rules.get("fixes", []) if f.get("enabled", True)]

    def _sub(m):
        slot, default = m.group(1), m.group(2)
        expr = default
        for f in fixes:
            tpl = f.get("slots", {}).get(slot)
            if tpl:
                expr = tpl.replace("{x}", f"({expr})")
        return expr

    return RULE_RE.sub(_sub, sql_template)


def apply_seed_rows(con, rules: dict):
    for f in rules.get("fixes", []):
        for s in f.get("seed_rows", []):
            table, row = s["table"], s["row"]
            cols = ", ".join(row.keys())
            cond = " AND ".join(f"{k} = ?" for k in row)
            vals = list(row.values())
            con.execute(f"DELETE FROM {table} WHERE {cond}", vals)
            if f.get("enabled", True):
                ph = ", ".join("?" for _ in row)
                con.execute(f"INSERT INTO {table} ({cols}) VALUES ({ph})", vals)


def run_build(db_path: pathlib.Path = DEFAULT_DB, verbose: bool = True, con=None) -> dict:
    log = print if verbose else (lambda *a, **k: None)
    own = con is None
    con = con or duckdb.connect(str(db_path))
    t0 = time.time()
    rules = load_rules()
    con.execute("CREATE SCHEMA IF NOT EXISTS stg; CREATE SCHEMA IF NOT EXISTS dw;")
    apply_seed_rows(con, rules)
    timings = []
    for m in parse_models():
        sql = render(m["sql_template"], rules)
        t = time.time()
        con.execute(sql)
        n = con.execute(f"SELECT COUNT(*) FROM {m['name']}").fetchone()[0]
        timings.append({"model": m["name"], "rows": n, "seconds": round(time.time() - t, 2)})
        log(f"  {m['name']:<24} {n:>10,} dòng  {time.time() - t:5.2f}s")
    stats = compute_stats(con)
    STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    if own:
        con.close()
    log(f"Build xong trong {time.time() - t0:.1f}s, thống kê -> {STATS_FILE}")
    return {"models": timings, "seconds": round(time.time() - t0, 2), "active_fixes": [f["id"] for f in rules["fixes"] if f.get("enabled", True)]}


TYPE_WIDTH = {"BOOLEAN": 1, "TINYINT": 1, "SMALLINT": 2, "INTEGER": 4, "BIGINT": 8, "HUGEINT": 16, "FLOAT": 4,
              "DOUBLE": 8, "DATE": 4, "TIMESTAMP": 8, "TIME": 8, "UTINYINT": 1, "USMALLINT": 2, "UINTEGER": 4,
              "UBIGINT": 8, "TIMESTAMP WITH TIME ZONE": 8, "INTERVAL": 16}


def compute_stats(con) -> dict:
    tables = con.execute("""
        SELECT schema_name, table_name, estimated_size FROM duckdb_tables()
        WHERE schema_name IN ('raw', 'ref', 'stg', 'dw') ORDER BY schema_name, table_name""").fetchall()
    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "tables": {}}
    meta = con.execute("SELECT * FROM ref.meta").fetchdf().to_dict("records")
    out["meta"] = meta[0] if meta else {}
    for schema, table, _ in tables:
        fq = f"{schema}.{table}"
        cols = con.execute("""SELECT column_name, data_type FROM duckdb_columns()
                              WHERE schema_name = ? AND table_name = ? ORDER BY column_index""",
                           [schema, table]).fetchall()
        n = con.execute(f"SELECT COUNT(*) FROM {fq}").fetchone()[0]
        agg = []
        for c, t in cols:
            q = f'"{c}"'
            agg.append(f"AVG(CASE WHEN {q} IS NULL THEN 1.0 ELSE 0.0 END)")
            agg.append(f"APPROX_COUNT_DISTINCT({q})")
            if t.startswith("VARCHAR"):
                agg.append(f"AVG(STRLEN({q}))")
                agg.append("NULL")
                agg.append("NULL")
            else:
                agg.append("NULL")
                if any(t.startswith(x) for x in ("INT", "BIGINT", "DOUBLE", "DECIMAL", "DATE", "TIMESTAMP", "FLOAT", "SMALLINT", "HUGEINT", "TINYINT")):
                    agg.append(f"CAST(MIN({q}) AS VARCHAR)")
                    agg.append(f"CAST(MAX({q}) AS VARCHAR)")
                else:
                    agg.append("NULL")
                    agg.append("NULL")
        row = con.execute(f"SELECT {', '.join(agg)} FROM {fq}").fetchone() if cols and n else [None] * (5 * len(cols))
        cstats = {}
        for i, (c, t) in enumerate(cols):
            nullf, nd, avgw, mn, mx = row[5 * i: 5 * i + 5]
            if t.startswith("VARCHAR"):
                width = float(avgw or 0) + 4.0        # độ dài trung bình + overhead
            elif t.startswith("DECIMAL"):
                width = 8.0
            else:
                width = float(TYPE_WIDTH.get(t, 8))
            cs = {"type": t, "null_frac": round(float(nullf or 0), 4), "distinct": int(nd or 0),
                  "avg_width": round(width, 2), "min": mn, "max": mx}
            if t.startswith("VARCHAR") and 0 < (nd or 0) <= 400 and c not in PII_COLUMNS:
                vals = con.execute(f'SELECT "{c}", COUNT(*) FROM {fq} WHERE "{c}" IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 400').fetchall()
                cs["values"] = [v[0] for v in vals]
            cstats[c] = cs
        tstat = {"schema": schema, "table": table, "rows": n, "columns": cstats,
                 "row_width": round(sum(c["avg_width"] for c in cstats.values()), 2)}
        tstat["bytes"] = int(tstat["row_width"] * n)
        pcol = PARTITIONS.get(fq)
        if pcol and pcol in cstats and n:
            parts = con.execute(f'SELECT CAST(CAST("{pcol}" AS DATE) AS VARCHAR), COUNT(*) FROM {fq} GROUP BY 1 ORDER BY 1').fetchall()
            tstat["partition"] = {"column": pcol, "granularity": "day", "counts": {d: c for d, c in parts if d}}
        out["tables"][fq] = tstat
    return out


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--stats", action="store_true", help="chỉ tính lại thống kê")
    a = ap.parse_args()
    if a.stats:
        con = duckdb.connect(a.db)
        STATS_FILE.write_text(json.dumps(compute_stats(con), ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        con.close()
        print("OK")
        return
    run_build(pathlib.Path(a.db))


if __name__ == "__main__":
    main()

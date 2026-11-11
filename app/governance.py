"""
Kiểm duyệt SQL xác định bằng CODE (không dùng LLM) trước khi thực thi:
  1. Cú pháp (sqlglot, dialect DuckDB), đúng 1 câu lệnh
  2. Chỉ cho phép truy vấn đọc (SELECT/WITH/UNION...)
  3. Chặn hàm truy cập file/mạng/cấu hình, chặn table function lạ
  4. RBAC: schema được phép theo vai trò, bảng phải tồn tại trong catalog
  5. Phân quyền cột: chặn cột PII/mật theo vai trò (qualify + mở rộng SELECT *)
  6. RLS: tiêm vị từ lọc dòng vào từng tham chiếu bảng (AST rewrite)
  7. Giới hạn số dòng trả về (LIMIT)
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify

from . import config
from .catalog import get_catalog

DIALECT = "duckdb"
SAFE_TABLE_FUNCS = {"generate_series", "range", "unnest", "explode"}
EXTRA_DENIED_FUNCS = {"columns", "pragma_table_info", "load", "install", "duckdb_tables", "duckdb_columns",
                      "duckdb_extensions", "duckdb_databases", "system", "read_csv", "read_parquet"}
FORBIDDEN_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter, exp.Command,
                   exp.Merge, exp.Copy, exp.Set, exp.Use, exp.Pragma, exp.Transaction, exp.Commit, exp.Rollback,
                   exp.Attach, exp.Detach, exp.Install)
FORBIDDEN_NODES = tuple(n for n in FORBIDDEN_NODES if isinstance(n, type))


@dataclass
class UserCtx:
    username: str
    role: str
    rls: dict = field(default_factory=dict)
    full_name: str = ""

    @property
    def role_cfg(self) -> dict:
        return config.policies()["roles"][self.role]


@dataclass
class Validation:
    ok: bool = True
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    sql_input: str = ""
    sql_exec: str = ""             # SQL thực thi (đã tiêm RLS + LIMIT)
    tables: list = field(default_factory=list)
    rls_applied: list = field(default_factory=list)
    limit_added: bool = False
    tree: object = None            # AST gốc (đã chuẩn hóa schema)
    qualified: object = None       # AST đã qualify (phục vụ cost/lint/lineage)
    checks: list = field(default_factory=list)

    def error(self, code, message, hint=None, **kw):
        self.ok = False
        self.errors.append({"code": code, "message": message, "hint": hint, **kw})

    def check(self, name, passed, detail=""):
        self.checks.append({"name": name, "passed": passed, "detail": detail})

    def to_dict(self):
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings, "sql_input": self.sql_input,
                "sql_exec": self.sql_exec, "tables": self.tables, "rls_applied": self.rls_applied,
                "limit_added": self.limit_added, "checks": self.checks}


def _func_name(f: exp.Expression) -> str:
    if isinstance(f, exp.Anonymous):
        return str(f.name).lower()
    try:
        return f.sql_name().lower()
    except Exception:
        return type(f).__name__.lower()


def _cte_names(tree) -> set[str]:
    return {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}


def _suggest(name: str, options: list[str]) -> str | None:
    m = difflib.get_close_matches(name, options, n=1, cutoff=0.5)
    return m[0] if m else None


def _suggest_by_description(name: str, tables: list[str]) -> str | None:
    """'thanh_tien' -> 'tt' vì mô tả cột tt là 'Thành tiền ...' (tên cột viết tắt kiểu kho dữ liệu thật)."""
    from .textutil import norm
    cat = get_catalog()
    key = norm(name.replace("_", " "))
    best = None
    for fq in tables:
        for c in cat.tables[fq]["columns"].values():
            text = norm((c.get("description") or "") + " | " + " | ".join(c.get("synonyms") or []))
            if key and re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", text):
                score = len(key) / (len(text) + 1)
                if best is None or score > best[0]:
                    best = (score, c["name"])
    return best[1] if best else None


def strip_sql(sql: str) -> str:
    s = sql.strip()
    s = re.sub(r"^```(?:sql)?\s*|\s*```$", "", s, flags=re.I).strip()
    while s.endswith(";"):
        s = s[:-1].rstrip()
    return s


def validate(sql: str, user: UserCtx, max_rows: int | None = None, dry_run: bool = True) -> Validation:
    from .warehouse import get_warehouse

    cat = get_catalog()
    pol = config.policies()
    role = user.role_cfg
    v = Validation(sql_input=sql)
    sql = strip_sql(sql)
    if not sql:
        v.error("EMPTY", "SQL rỗng.")
        return v

    # 1. cú pháp
    try:
        stmts = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except ParseError as e:
        err = e.errors[0] if getattr(e, "errors", None) else {}
        v.error("SYNTAX", f"Lỗi cú pháp SQL: {err.get('description') or str(e).splitlines()[0]}",
                f"Dòng {err.get('line')}, cột {err.get('col')}" if err else None)
        v.check("Cú pháp", False)
        return v
    if len(stmts) != 1:
        v.error("MULTI_STATEMENT", "Chỉ cho phép đúng 1 câu lệnh mỗi lần chạy.")
        v.check("Cú pháp", False)
        return v
    tree = stmts[0]
    v.check("Cú pháp", True, "Phân tích AST thành công (sqlglot / DuckDB)")

    # 2. loại câu lệnh
    if not isinstance(tree, exp.Query) or any(isinstance(n, FORBIDDEN_NODES) for n in tree.walk()):
        kind = type(tree).__name__.upper()
        v.error("FORBIDDEN_STATEMENT", f"Câu lệnh {kind} không được phép. Vai trò {user.role} chỉ được chạy truy vấn đọc (SELECT).",
                "Thay đổi schema/dữ liệu phải thực hiện qua chức năng quản trị có kiểm duyệt.")
        v.check("Chỉ đọc (SELECT)", False)
        return v
    v.check("Chỉ đọc (SELECT)", True)

    # 3. hàm bị cấm
    denied = {f.lower() for f in pol.get("denied_functions", [])} | EXTRA_DENIED_FUNCS
    bad = sorted({_func_name(f) for f in tree.find_all(exp.Func) if _func_name(f) in denied})
    if bad:
        v.error("FORBIDDEN_FUNCTION", f"Hàm không được phép: {', '.join(bad)} (truy cập file/hệ thống).")
        v.check("Hàm an toàn", False)
        return v

    # 4. bảng & schema
    ctes = _cte_names(tree)
    allowed_schemas = role.get("schemas", [])
    all_tables = list(cat.tables)
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            fname = _func_name(t.this) if isinstance(t.this, exp.Func) else type(t.this).__name__.lower()
            if fname not in SAFE_TABLE_FUNCS:
                v.error("FORBIDDEN_FUNCTION", f"Table function '{fname}' không được phép.")
            continue
        name = t.name.lower()
        db = (t.db or "").lower()
        if t.catalog:
            v.error("FORBIDDEN_SCHEMA", f"Không được tham chiếu catalog/database khác: {t.catalog}.")
            continue
        if not db and name in ctes:
            continue
        if not db:
            db = "dw"
            t.set("db", exp.to_identifier("dw"))
        fq = f"{db}.{name}"
        if db not in {"dw", "stg", "raw", "ref"}:
            v.error("FORBIDDEN_SCHEMA", f"Schema hệ thống '{db}' không được phép truy cập.")
            continue
        if fq not in cat.tables:
            sug = _suggest(fq, all_tables) or _suggest(name, [x.split(".")[1] for x in all_tables])
            v.error("UNKNOWN_TABLE", f"Bảng {fq} không tồn tại trong kho dữ liệu.",
                    f"Có phải ý bạn là {sug}?" if sug else None, table=fq, suggestion=sug)
            continue
        if db not in allowed_schemas:
            v.error("FORBIDDEN_SCHEMA", f"Vai trò {user.role} không được truy cập schema '{db}' (bảng {fq}).",
                    "Analyst chỉ truy vấn tầng dw (dữ liệu đã làm sạch).")
            continue
        if fq not in v.tables:
            v.tables.append(fq)
    if not v.ok:
        v.check("Phân quyền bảng/schema (RBAC)", False)
        return v
    v.check("Phân quyền bảng/schema (RBAC)", True, ", ".join(v.tables))

    # 5. qualify -> kiểm tra cột tồn tại + cột nhạy cảm
    schema = cat.sqlglot_schema()
    try:
        q = qualify(tree.copy(), schema=schema, dialect=DIALECT, validate_qualify_columns=True,
                    quote_identifiers=False, identify=False)
    except OptimizeError as e:
        msg = str(e)
        m = re.search(r"Column '([^']+)' could not be resolved", msg) or re.search(r"Unknown column: ([\w.\"]+)", msg)
        if m:
            col = m.group(1).split(".")[-1].strip('"')
            cands = sorted({c for fq in v.tables for c in cat.tables[fq]["columns"]})
            sug = _suggest_by_description(col, v.tables) or \
                (difflib.get_close_matches(col, cands, n=1, cutoff=0.72) or [None])[0]
            v.error("UNKNOWN_COLUMN", f"Cột '{m.group(1)}' không tồn tại trong các bảng được tham chiếu.",
                    f"Có phải ý bạn là '{sug}'?" if sug else None, column=col, suggestion=sug)
        else:
            v.error("ANALYSIS", f"Không phân tích tĩnh được truy vấn: {msg.splitlines()[0]}",
                    "Hãy viết tường minh bí danh bảng cho các cột.")
        v.check("Cột tồn tại", False)
        return v
    except Exception as e:  # pragma: no cover - lỗi lạ của optimizer
        v.error("ANALYSIS", f"Không phân tích tĩnh được truy vấn: {e}")
        return v
    v.qualified = q
    v.check("Cột tồn tại", True)

    deny = set(role.get("deny_sensitivity", []))
    if deny:
        from sqlglot.optimizer.scope import traverse_scope
        hits = set()
        for scope in traverse_scope(q):
            for col in scope.columns:
                src = scope.sources.get(col.table)
                if isinstance(src, exp.Table):
                    fq = f"{src.db}.{src.name}".lower()
                    sens = cat.sensitivity(fq, col.name)
                    if sens in deny:
                        hits.add((fq, col.name, sens))
            for star in scope.expression.find_all(exp.Star):
                if not isinstance(star.parent, exp.Count):
                    for fq in v.tables:
                        if any(c.get("sensitivity") in deny for c in cat.tables[fq]["columns"].values()):
                            hits.add((fq, "*", "pii"))
        if hits:
            lst = ", ".join(f"{fq}.{c} ({'dữ liệu cá nhân' if s == 'pii' else 'dữ liệu mật'})" for fq, c, s in sorted(hits))
            v.error("FORBIDDEN_COLUMN", f"Vai trò {user.role} không được truy cập cột: {lst}.",
                    "Hãy bỏ các cột này hoặc chọn cột cụ thể thay vì SELECT *. Có thể dùng mã khách hàng (ma_kh) để phân tích.")
            v.check("Phân quyền cột (PII/mật)", False)
            return v
    v.check("Phân quyền cột (PII/mật)", True, "Không truy cập cột bị hạn chế")

    # 6. RLS
    rewritten = tree.copy()
    rls_rules = pol.get("rls", {})
    originals = [t for t in rewritten.find_all(exp.Table) if isinstance(t.this, exp.Identifier) and t.db]
    for t in originals:
        fq = f"{t.db}.{t.name}".lower()
        preds = []
        for attr, values in (user.rls or {}).items():
            tpl = rls_rules.get(attr, {}).get(fq)
            if tpl:
                vals = ", ".join("'" + str(x).replace("'", "''") + "'" for x in values)
                preds.append(tpl.format(**{attr: vals}))
        if not preds:
            continue
        alias = t.alias or t.name
        sub = exp.select("*").from_(exp.table_(t.name, db=t.db)).where(" AND ".join(preds), dialect=DIALECT)
        t.replace(sub.subquery(alias))
        if fq not in v.rls_applied:
            v.rls_applied.append(fq)
    v.check("Row-level security (RLS)", True,
            f"Đã tiêm bộ lọc {', '.join(f'{k}={vv}' for k, vv in user.rls.items())} vào {', '.join(v.rls_applied)}"
            if v.rls_applied else "Không áp dụng (người dùng không bị giới hạn dòng)")

    # 7. LIMIT
    limit = max_rows or role.get("max_rows", 5000)
    if isinstance(rewritten, exp.Select) and not rewritten.args.get("limit"):
        rewritten = rewritten.limit(limit)
        v.limit_added = True
    v.tree = tree
    v.sql_exec = rewritten.sql(dialect=DIALECT, pretty=True)

    # 8. dry-run trên engine (binder / kiểu dữ liệu)
    if dry_run:
        dr = get_warehouse().dry_run(v.sql_exec)
        if not dr["ok"]:
            v.error("BINDER", "Engine từ chối truy vấn: " + dr["error"].split("\n")[0][:400],
                    _binder_hint(dr["error"]), raw=dr["error"])
            v.check("Dry-run trên engine", False)
            return v
        v.check("Dry-run trên engine", True, "EXPLAIN thành công")
    return v


def _binder_hint(msg: str) -> str | None:
    m = re.search(r'Candidate bindings: (.+)', msg)
    if m:
        return "Ứng viên: " + m.group(1)[:200]
    m = re.search(r'Did you mean "([^"]+)"', msg)
    if m:
        return f"Có phải ý bạn là {m.group(1)}?"
    return None


def pretty(sql: str) -> str:
    try:
        return sqlglot.transpile(strip_sql(sql), read=DIALECT, write=DIALECT, pretty=True)[0]
    except Exception:
        return sql

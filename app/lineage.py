"""Lineage cấp bảng & cột của các model biến đổi (raw/ref -> stg -> dw), tính bằng sqlglot trên SQL của model."""
from __future__ import annotations

import hashlib
import json
import threading

import sqlglot
from sqlglot import exp
from sqlglot.lineage import lineage as sg_lineage

from . import config
from .catalog import get_catalog

CACHE = config.VAR / "model_lineage.json"


class ModelLineage:
    def __init__(self, data: dict):
        self.models = data["models"]              # name -> {desc, sources, columns: {col: [src cols]}}
        self.col_edges = {}
        for m, info in self.models.items():
            for c, srcs in info["columns"].items():
                self.col_edges[f"{m}.{c}"] = set(srcs)

    def upstream_raw(self, col: str, _seen=None) -> set[str]:
        _seen = _seen or set()
        if col in _seen:
            return set()
        _seen.add(col)
        srcs = self.col_edges.get(col)
        if not srcs:
            return {col} if col.split(".")[0] in ("raw", "ref") else set()
        out = set()
        for s in srcs:
            out |= self.upstream_raw(s, _seen) if s.split(".")[0] not in ("raw", "ref") else {s}
        return out

    def upstream_path(self, col: str, depth=0) -> dict:
        srcs = sorted(self.col_edges.get(col, []))
        return {"column": col, "sources": [self.upstream_path(s, depth + 1) for s in srcs] if depth < 6 else []}

    def downstream(self, col: str) -> list[str]:
        return sorted(k for k, v in self.col_edges.items() if col in v)

    def graph(self) -> dict:
        cat = get_catalog()
        nodes, edges = {}, []
        for m, info in self.models.items():
            nodes[m] = {"id": m, "layer": m.split(".")[0], "desc": info.get("desc", ""), "rows": (cat.get(m) or {}).get("rows")}
            for s in info["sources"]:
                nodes.setdefault(s, {"id": s, "layer": s.split(".")[0], "desc": (cat.get(s) or {}).get("description", ""),
                                     "rows": (cat.get(s) or {}).get("rows")})
                edges.append({"from": s, "to": m})
        return {"nodes": list(nodes.values()), "edges": edges}


def _compute() -> dict:
    import sys
    sys.path.insert(0, str(config.ROOT))
    from warehouse.build import load_rules, parse_models, render

    schema = get_catalog().sqlglot_schema()
    rules = load_rules()
    out = {}
    for m in parse_models():
        sql = render(m["sql_template"], rules)
        try:
            stmt = sqlglot.parse_one(sql, read="duckdb")
        except Exception:
            continue
        q = stmt.expression if isinstance(stmt, exp.Create) else stmt
        ctes = {c.alias_or_name.lower() for c in q.find_all(exp.CTE)}
        sources = sorted({f"{t.db}.{t.name}".lower() for t in q.find_all(exp.Table)
                          if t.db and t.name.lower() not in ctes})
        target_schema, target_table = m["name"].split(".")
        cols = {}
        tcols = (get_catalog().get(m["name"]) or {}).get("columns", {})
        q_sql = q.sql(dialect="duckdb")
        for c in tcols:
            try:
                node = sg_lineage(c, q_sql, schema=schema, dialect="duckdb")
            except Exception:
                cols[c] = []
                continue
            srcs = set()
            for n in node.walk():
                if isinstance(n.expression, exp.Table):
                    srcs.add(f"{n.expression.db}.{n.expression.name}.{n.name.split('.')[-1]}".lower())
            cols[c] = sorted(srcs)
        out[m["name"]] = {"desc": m["desc"], "sources": sources, "columns": cols}
    return {"models": out}


_lock = threading.Lock()
_ml: ModelLineage | None = None
_key: str | None = None


def _cache_key() -> str:
    h = hashlib.sha1()
    h.update((config.ROOT / "warehouse" / "models.sql").read_bytes())
    if config.RULES_FILE.exists():
        h.update(config.RULES_FILE.read_bytes())
    return h.hexdigest()


def get_model_lineage() -> ModelLineage:
    global _ml, _key
    with _lock:
        key = _cache_key()
        if _ml is not None and _key == key:
            return _ml
        if CACHE.exists():
            data = json.loads(CACHE.read_text(encoding="utf-8"))
            if data.get("key") == key:
                _ml, _key = ModelLineage(data), key
                return _ml
        data = _compute()
        data["key"] = key
        CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        _ml, _key = ModelLineage(data), key
        return _ml

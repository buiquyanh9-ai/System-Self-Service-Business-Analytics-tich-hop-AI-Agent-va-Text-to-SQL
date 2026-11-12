"""Data catalog hợp nhất: thống kê kỹ thuật (từ build) + mô tả nghiệp vụ (catalog.yml) + chỉnh sửa của Admin."""
from __future__ import annotations

import datetime as dt
import json
import threading

from . import config
from .store import get_store


class Catalog:
    def __init__(self):
        self._lock = threading.Lock()
        self.tables: dict[str, dict] = {}
        self.meta: dict = {}
        self.reload()

    def reload(self):
        with self._lock:
            stats = json.loads(config.STATS_FILE.read_text(encoding="utf-8")) if config.STATS_FILE.exists() else {"tables": {}}
            cy = config.catalog_yaml().get("tables", {})
            overrides = get_store().get_overrides()
            tables = {}
            for fq, st in stats.get("tables", {}).items():
                doc = cy.get(fq, {})
                t = {
                    "name": fq, "schema": st["schema"], "table": st["table"], "rows": st["rows"],
                    "bytes": st["bytes"], "row_width": st["row_width"],
                    "description": doc.get("description", ""), "synonyms": list(doc.get("synonyms", [])),
                    "grain": doc.get("grain"), "partition": st.get("partition"), "columns": {},
                }
                if fq in overrides:
                    o = overrides[fq]
                    t["description"] = o["description"] or t["description"]
                    t["synonyms"] = o["synonyms"] or t["synonyms"]
                cdoc = doc.get("columns", {}) or {}
                for c, cs in st["columns"].items():
                    d = cdoc.get(c, {}) or {}
                    col = dict(cs)
                    col["name"] = c
                    col["description"] = d.get("description", "")
                    col["synonyms"] = list(d.get("synonyms", []))
                    col["sensitivity"] = d.get("sensitivity")
                    key = f"{fq}.{c}"
                    if key in overrides:
                        o = overrides[key]
                        col["description"] = o["description"] or col["description"]
                        col["synonyms"] = o["synonyms"] or col["synonyms"]
                    t["columns"][c] = col
                tables[fq] = t
            self.tables = tables
            self.meta = stats.get("meta", {})

    # ------------------------------------------------------------------ truy vấn catalog
    def get(self, fq: str) -> dict | None:
        return self.tables.get(fq.lower())

    def resolve(self, name: str) -> str | None:
        """Tên bảng (có/không schema) -> fq. Bảng không có schema mặc định thuộc schema phục vụ của doanh nghiệp."""
        from .profile import serving_schema
        name = name.lower().strip('"')
        if name in self.tables:
            return name
        if "." not in name and f"{serving_schema()}.{name}" in self.tables:
            return f"{serving_schema()}.{name}"
        return None

    def sqlglot_schema(self, schemas: list[str] | None = None) -> dict:
        out: dict = {}
        for fq, t in self.tables.items():
            if schemas and t["schema"] not in schemas:
                continue
            out.setdefault(t["schema"], {})[t["table"]] = {c: (v["type"] or "VARCHAR") for c, v in t["columns"].items()}
        return out

    def sensitivity(self, fq: str, col: str) -> str | None:
        t = self.get(fq)
        if not t:
            return None
        c = t["columns"].get(col)
        return c.get("sensitivity") if c else None

    def visible_tables(self, role_cfg: dict) -> list[dict]:
        return [t for t in self.tables.values() if t["schema"] in role_cfg.get("schemas", [])]

    def anchor_date(self) -> str:
        s = get_store().get_settings().get("anchor_date")
        if s:
            return s
        from .profile import anchor, serving_schema      # cột mốc thời gian khai báo trong hồ sơ doanh nghiệp
        tab, col = anchor() or (None, None)
        t = self.get(tab) if tab else None
        if t and t["columns"].get(col, {}).get("max"):
            return str(t["columns"][col]["max"])[:10]
        if self.meta.get("ngay_ket_thuc"):
            return str(self.meta["ngay_ket_thuc"])[:10]
        # hồ sơ chưa khai báo mốc: lấy ngày mới nhất trên cột phân vùng của các bảng trong schema phục vụ
        latest = [str(t["columns"][t["partition"]["column"]]["max"])[:10] for t in self.tables.values()
                  if t["schema"] == serving_schema() and t.get("partition") and t["columns"][t["partition"]["column"]].get("max")]
        return max(latest) if latest else dt.date.today().isoformat()

    def data_start(self) -> str:
        """Ngày dữ liệu sớm nhất (theo cột mốc trong hồ sơ doanh nghiệp)."""
        from .profile import anchor
        tab, col = anchor() or (None, None)
        t = self.get(tab) if tab else None
        if t and t["columns"].get(col, {}).get("min"):
            return str(t["columns"][col]["min"])[:10]
        return str(self.meta.get("ngay_bat_dau", "2000-01-01"))[:10]

    def describe(self, fq: str, role_cfg: dict | None = None, max_values: int = 12) -> dict:
        t = self.get(fq)
        if not t:
            return {}
        deny = set((role_cfg or {}).get("deny_sensitivity", []))
        cols = []
        for c in t["columns"].values():
            item = {"name": c["name"], "type": c["type"], "description": c["description"]}
            if c.get("sensitivity"):
                item["sensitivity"] = c["sensitivity"]
                if c["sensitivity"] in deny:
                    item["restricted"] = True
            if c.get("values") and not item.get("restricted"):
                item["sample_values"] = c["values"][:max_values]
            if c.get("min") is not None and c["type"] in ("DATE", "TIMESTAMP"):
                item["range"] = [str(c["min"])[:10], str(c["max"])[:10]]
            cols.append(item)
        return {"table": fq, "description": t["description"], "grain": t.get("grain"), "rows": t["rows"],
                "partition_column": (t.get("partition") or {}).get("column"), "columns": cols}

    def schema_text(self, tables: list[str], role_cfg: dict | None = None) -> str:
        """Mô tả schema dạng văn bản gọn cho prompt LLM."""
        lines = []
        for fq in tables:
            d = self.describe(fq, role_cfg, max_values=8)
            if not d:
                continue
            head = f"TABLE {fq} ({d['rows']:,} dòng"
            if d.get("partition_column"):
                head += f", phân vùng theo {d['partition_column']}"
            head += f") — {d['description']}"
            lines.append(head)
            for c in d["columns"]:
                s = f"  - {c['name']} {c['type']}"
                if c.get("description"):
                    s += f": {c['description']}"
                if c.get("restricted"):
                    s += " [BỊ HẠN CHẾ với vai trò hiện tại]"
                if c.get("sample_values"):
                    s += f" | giá trị: {', '.join(map(str, c['sample_values'][:8]))}"
                if c.get("range"):
                    s += f" | từ {c['range'][0]} đến {c['range'][1]}"
                lines.append(s)
        return "\n".join(lines)


_cat: Catalog | None = None


def get_catalog() -> Catalog:
    global _cat
    if _cat is None:
        _cat = Catalog()
    return _cat

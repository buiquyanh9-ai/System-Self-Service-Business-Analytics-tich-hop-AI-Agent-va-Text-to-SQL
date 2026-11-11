"""
Truy hồi ngữ cảnh (RAG) cho agent: BM25 trên tài liệu schema (bảng/cột/mô tả/từ đồng nghĩa), định nghĩa chỉ số
và kho few-shot (câu hỏi -> SQL đã xác nhận). Chạy hoàn toàn local; có thể thay bằng vector DB (Qdrant/pgvector)
qua cùng interface search().
"""
from __future__ import annotations

import math
import re
from collections import Counter

from . import config
from .catalog import get_catalog
from .semantic import get_semantic
from .store import get_store
from .textutil import bigrams, norm, tokens

STOP = {"cua", "la", "co", "nhung", "cac", "bao", "nhieu", "nao", "the", "va", "voi", "trong", "cho", "theo", "duoc",
        "mot", "hay", "gi", "o", "tai", "tu", "den", "khong", "nhu", "thi", "sao", "da", "dang", "se", "nhat", "ve"}


def _terms(text: str) -> list[str]:
    toks = [t for t in tokens(text) if t not in STOP]
    return toks + bigrams(toks)


class BM25:
    def __init__(self, docs: list[dict], k1=1.4, b=0.75):
        self.docs = docs
        self.k1, self.b = k1, b
        self.tf = [Counter(_terms(d["text"])) for d in docs]
        self.len = [sum(c.values()) for c in self.tf]
        self.avg = sum(self.len) / max(len(self.len), 1)
        df = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, k: int = 5) -> list[tuple[float, dict]]:
        qt = _terms(query)
        scores = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            for t in qt:
                if t in tf:
                    f = tf[t]
                    s += self.idf.get(t, 0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
            if s > 0:
                scores.append((s, self.docs[i]))
        scores.sort(key=lambda x: -x[0])
        return scores[:k]


class Retriever:
    def __init__(self):
        self.reload()

    def reload(self):
        cat, sem = get_catalog(), get_semantic()
        docs = []
        for fq, t in cat.tables.items():
            if t["schema"] not in ("dw",):
                continue
            parts = [fq, t["table"].replace("_", " "), t["description"], " ".join(t["synonyms"])]
            for c in t["columns"].values():
                parts += [c["name"].replace("_", " "), c["description"], " ".join(c["synonyms"])]
            docs.append({"kind": "table", "id": fq, "text": " ".join(parts)})
        for name, m in sem.metrics.items():
            docs.append({"kind": "metric", "id": name,
                         "text": " ".join([name.replace("_", " "), m["label"], m.get("description", ""), " ".join(m.get("synonyms", []))])})
        self.schema_index = BM25(docs)
        self.reload_fewshots()

    def reload_fewshots(self):
        self.fewshots = get_store().list_fewshots()
        self.fs_index = BM25([{"kind": "fewshot", "id": f["id"], "text": f["question"], "item": f} for f in self.fewshots]) \
            if self.fewshots else None

    def search_schema(self, query: str, k: int = 6) -> dict:
        hits = self.schema_index.search(query, k=k * 2)
        tables = [h[1]["id"] for h in hits if h[1]["kind"] == "table"][:k]
        metrics = [h[1]["id"] for h in hits if h[1]["kind"] == "metric"][:k]
        # luôn kèm các dimension liên quan đến bảng sự kiện được chọn
        core = ["dw.fct_ct_dh", "dw.dim_sp", "dw.dim_cn", "dw.dim_vung", "dw.dim_kenh", "dw.dim_nh"]
        for c in core:
            if len(tables) < k + 3 and c not in tables:
                tables.append(c)
        return {"tables": tables, "metrics": metrics, "scores": [(round(s, 2), d["kind"], d["id"]) for s, d in hits]}

    def search_fewshots(self, query: str, k: int = 4) -> list[dict]:
        if not self.fs_index:
            return []
        return [dict(d["item"], score=round(s, 3)) for s, d in self.fs_index.search(query, k=k)]

    def reusable(self, question: str, threshold: float) -> dict | None:
        """Câu hỏi gần như trùng (Jaccard token + cùng 'slot' số/giá trị) với ví dụ đã xác nhận -> tái sử dụng SQL."""
        if not self.fewshots:
            return None
        qa = set(t for t in tokens(question) if t not in STOP)
        sa = _slots(question)
        best = None
        for f in self.fewshots:
            if not f.get("verified") or not f.get("sql"):
                continue
            qb = set(t for t in tokens(f["question"]) if t not in STOP)
            if not qa or not qb:
                continue
            j = len(qa & qb) / len(qa | qb)
            if j >= threshold and _slots(f["question"]) == sa and (best is None or j > best[0]):
                best = (j, f)
        return dict(best[1], similarity=round(best[0], 3)) if best else None


def _slots(q: str) -> tuple:
    n = norm(q)
    nums = tuple(sorted(re.findall(r"\d+", n)))
    rel = tuple(sorted(w for w in ("thang nay", "thang truoc", "nam nay", "nam ngoai", "quy nay", "quy truoc", "hom nay",
                                    "hom qua", "tuan nay", "tuan truoc", "mien bac", "mien nam", "mien trung") if w in n))
    return nums + rel


_r: Retriever | None = None


def get_retriever() -> Retriever:
    global _r
    if _r is None:
        _r = Retriever()
    return _r

"""Kho lưu trữ metadata ứng dụng (SQLite): hội thoại, log truy vấn, phản hồi, few-shot, cài đặt, override catalog."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY, username TEXT, title TEXT, created_at REAL, updated_at REAL, memory TEXT);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT, role TEXT, content TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS ix_msg_conv ON messages(conversation_id);
CREATE TABLE IF NOT EXISTS query_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts REAL, username TEXT, role TEXT, conversation_id TEXT,
    question TEXT, sql TEXT, status TEXT, error TEXT, est_bytes INTEGER, rows INTEGER, latency_ms INTEGER,
    retries INTEGER, llm_mode TEXT, source TEXT, plan TEXT, lint TEXT, approved_by TEXT);
CREATE INDEX IF NOT EXISTS ix_log_user ON query_log(username);
CREATE TABLE IF NOT EXISTS pending (run_id TEXT PRIMARY KEY, conversation_id TEXT, username TEXT, state TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, username TEXT, rating INTEGER, comment TEXT,
    corrected_sql TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS fewshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT, sql TEXT, plan TEXT, source TEXT, verified INTEGER,
    created_at REAL, uses INTEGER DEFAULT 0, UNIQUE(question, source));
CREATE TABLE IF NOT EXISTS catalog_overrides (
    key TEXT PRIMARY KEY, description TEXT, synonyms TEXT, updated_by TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS eval_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, label TEXT, summary TEXT, details TEXT);
"""


class Store:
    def __init__(self, path=None):
        self.path = str(path or config.APP_DB)
        config.VAR.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ helpers
    def _exec(self, sql, args=()):
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur

    def _all(self, sql, args=()):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def _one(self, sql, args=()):
        with self._lock:
            r = self._conn.execute(sql, args).fetchone()
            return dict(r) if r else None

    # ------------------------------------------------------------------ settings
    def get_settings(self) -> dict:
        s = dict(config.DEFAULT_SETTINGS)
        for r in self._all("SELECT key, value FROM settings"):
            s[r["key"]] = json.loads(r["value"])
        return s

    def set_settings(self, values: dict):
        for k, v in values.items():
            if k in config.DEFAULT_SETTINGS:
                self._exec("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (k, json.dumps(v)))

    # ------------------------------------------------------------------ conversations
    def create_conversation(self, username: str, title: str = "Hội thoại mới") -> str:
        cid = uuid.uuid4().hex[:12]
        now = time.time()
        self._exec("INSERT INTO conversations VALUES (?, ?, ?, ?, ?, ?)", (cid, username, title, now, now, json.dumps({})))
        return cid

    def get_conversation(self, cid: str) -> dict | None:
        c = self._one("SELECT * FROM conversations WHERE id = ?", (cid,))
        if c:
            c["memory"] = json.loads(c["memory"] or "{}")
        return c

    def list_conversations(self, username: str) -> list[dict]:
        return self._all("SELECT id, title, created_at, updated_at FROM conversations WHERE username = ? "
                         "ORDER BY updated_at DESC LIMIT 50", (username,))

    def update_conversation(self, cid: str, memory: dict | None = None, title: str | None = None):
        if memory is not None:
            self._exec("UPDATE conversations SET memory = ?, updated_at = ? WHERE id = ?",
                       (json.dumps(memory, ensure_ascii=False, default=str), time.time(), cid))
        if title is not None:
            self._exec("UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?", (title, time.time(), cid))

    def delete_conversation(self, cid: str):
        self._exec("DELETE FROM messages WHERE conversation_id = ?", (cid,))
        self._exec("DELETE FROM conversations WHERE id = ?", (cid,))

    def add_message(self, cid: str, role: str, content: dict):
        self._exec("INSERT INTO messages(conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                   (cid, role, json.dumps(content, ensure_ascii=False, default=str), time.time()))

    def get_messages(self, cid: str) -> list[dict]:
        rows = self._all("SELECT role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY id", (cid,))
        for r in rows:
            r["content"] = json.loads(r["content"])
        return rows

    # ------------------------------------------------------------------ logs
    def log_query(self, **kw):
        cols = ["run_id", "ts", "username", "role", "conversation_id", "question", "sql", "status", "error",
                "est_bytes", "rows", "latency_ms", "retries", "llm_mode", "source", "plan", "lint", "approved_by"]
        kw.setdefault("ts", time.time())
        for k in ("plan", "lint"):
            if k in kw and not isinstance(kw[k], str):
                kw[k] = json.dumps(kw[k], ensure_ascii=False, default=str)
        vals = [kw.get(c) for c in cols]
        self._exec(f"INSERT INTO query_log({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", vals)

    def get_logs(self, username: str | None = None, limit: int = 300) -> list[dict]:
        if username:
            return self._all("SELECT * FROM query_log WHERE username = ? ORDER BY id DESC LIMIT ?", (username, limit))
        return self._all("SELECT * FROM query_log ORDER BY id DESC LIMIT ?", (limit,))

    def log_stats(self) -> dict:
        r = self._one("""SELECT COUNT(*) n, SUM(status='success') ok, SUM(status='error') err,
                         SUM(status='blocked') blocked, SUM(status='pending_approval') pending,
                         SUM(status='rejected') rejected, AVG(latency_ms) lat, SUM(retries) retries,
                         SUM(est_bytes) bytes FROM query_log""")
        return r or {}

    # ------------------------------------------------------------------ pending HITL
    def save_pending(self, run_id: str, cid: str, username: str, state: dict):
        self._exec("INSERT OR REPLACE INTO pending VALUES (?, ?, ?, ?, ?)",
                   (run_id, cid, username, json.dumps(state, ensure_ascii=False, default=str), time.time()))

    def pop_pending(self, run_id: str) -> dict | None:
        r = self._one("SELECT * FROM pending WHERE run_id = ?", (run_id,))
        if r:
            self._exec("DELETE FROM pending WHERE run_id = ?", (run_id,))
            r["state"] = json.loads(r["state"])
        return r

    # ------------------------------------------------------------------ feedback & few-shot
    def add_feedback(self, run_id, username, rating, comment=None, corrected_sql=None):
        self._exec("INSERT INTO feedback(run_id, username, rating, comment, corrected_sql, ts) VALUES (?,?,?,?,?,?)",
                   (run_id, username, rating, comment, corrected_sql, time.time()))

    def feedback_stats(self) -> dict:
        return self._one("SELECT COUNT(*) n, SUM(rating > 0) up, SUM(rating < 0) down FROM feedback") or {}

    def add_fewshot(self, question: str, sql: str, plan=None, source="feedback", verified=True):
        self._exec("INSERT OR REPLACE INTO fewshots(question, sql, plan, source, verified, created_at) VALUES (?,?,?,?,?,?)",
                   (question, sql, json.dumps(plan, ensure_ascii=False) if plan else None, source, int(verified),
                    time.time()))

    def list_fewshots(self) -> list[dict]:
        rows = self._all("SELECT * FROM fewshots ORDER BY id")
        for r in rows:
            r["plan"] = json.loads(r["plan"]) if r["plan"] else None
        return rows

    def delete_fewshots(self, source: str | None = None, fid: int | None = None):
        if fid is not None:
            self._exec("DELETE FROM fewshots WHERE id = ?", (fid,))
        elif source:
            self._exec("DELETE FROM fewshots WHERE source = ?", (source,))

    def touch_fewshot(self, fid: int):
        self._exec("UPDATE fewshots SET uses = uses + 1 WHERE id = ?", (fid,))

    # ------------------------------------------------------------------ catalog overrides
    def get_overrides(self) -> dict:
        out = {}
        for r in self._all("SELECT * FROM catalog_overrides"):
            out[r["key"]] = {"description": r["description"], "synonyms": json.loads(r["synonyms"] or "[]"),
                             "updated_by": r["updated_by"], "ts": r["ts"]}
        return out

    def set_override(self, key: str, description: str | None, synonyms: list[str] | None, user: str):
        self._exec("INSERT OR REPLACE INTO catalog_overrides VALUES (?, ?, ?, ?, ?)",
                   (key, description, json.dumps(synonyms or [], ensure_ascii=False), user, time.time()))

    # ------------------------------------------------------------------ eval
    def save_eval(self, label: str, summary: dict, details: list) -> int:
        cur = self._exec("INSERT INTO eval_runs(ts, label, summary, details) VALUES (?, ?, ?, ?)",
                         (time.time(), label, json.dumps(summary, ensure_ascii=False, default=str),
                          json.dumps(details, ensure_ascii=False, default=str)))
        return cur.lastrowid

    def list_evals(self, limit=20) -> list[dict]:
        rows = self._all("SELECT id, ts, label, summary FROM eval_runs ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["summary"] = json.loads(r["summary"])
        return rows

    def get_eval(self, eid: int) -> dict | None:
        r = self._one("SELECT * FROM eval_runs WHERE id = ?", (eid,))
        if r:
            r["summary"] = json.loads(r["summary"])
            r["details"] = json.loads(r["details"])
        return r


_store: Store | None = None


def get_store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store

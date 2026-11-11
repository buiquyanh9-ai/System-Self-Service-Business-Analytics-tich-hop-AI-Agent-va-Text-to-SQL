"""Kết nối kho dữ liệu DuckDB (read-only, chặn truy cập file/mạng), thực thi có timeout, dry-run (EXPLAIN), rebuild."""
from __future__ import annotations

import datetime as dt
import decimal
import math
import threading
import time
from contextlib import contextmanager

import duckdb

from . import config


class RWLock:
    def __init__(self):
        self._cond = threading.Condition()
        self._readers = 0
        self._writer = False

    @contextmanager
    def read(self):
        with self._cond:
            while self._writer:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                self._cond.notify_all()

    @contextmanager
    def write(self):
        with self._cond:
            while self._writer or self._readers:
                self._cond.wait()
            self._writer = True
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()


def _py(v):
    if v is None:
        return None
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else v
    if isinstance(v, (dt.datetime,)):
        return v.isoformat(sep=" ")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, (int, str, bool)):
        return v
    if hasattr(v, "item"):
        return _py(v.item())
    return str(v)


class QueryError(Exception):
    def __init__(self, message: str, kind: str = "execution"):
        super().__init__(message)
        self.kind = kind


class Warehouse:
    def __init__(self, path=None):
        self.path = str(path or config.WAREHOUSE_DB)
        self.lock = RWLock()
        self._con = None
        self.version = 0     # tăng sau mỗi lần rebuild

    def _open(self):
        self._con = duckdb.connect(self.path, read_only=True,
                                   config={"enable_external_access": False, "threads": 4})
        self._con.execute("SET lock_configuration = true")

    def con(self):
        if self._con is None:
            self._open()
        return self._con

    # ------------------------------------------------------------------ thực thi
    def execute(self, sql: str, timeout_s: float = 20, max_rows: int = 5000) -> dict:
        with self.lock.read():
            cur = self.con().cursor()
            timer = threading.Timer(timeout_s, cur.interrupt)
            t0 = time.time()
            timer.start()
            try:
                cur.execute(sql)
                cols = [d[0] for d in cur.description] if cur.description else []
                types = [str(d[1]) for d in cur.description] if cur.description else []
                rows = cur.fetchmany(max_rows + 1)
            except duckdb.InterruptException:
                raise QueryError(f"Truy vấn vượt quá thời gian cho phép ({timeout_s}s) và đã bị hủy.", "timeout")
            except duckdb.Error as e:
                msg = str(e)
                if "interrupt" in msg.lower():
                    raise QueryError(f"Truy vấn vượt quá thời gian cho phép ({timeout_s}s) và đã bị hủy.", "timeout")
                raise QueryError(msg, "execution")
            finally:
                timer.cancel()
                try:
                    cur.close()
                except Exception:
                    pass
            truncated = len(rows) > max_rows
            rows = rows[:max_rows]
            return {"columns": cols, "types": types, "rows": [[_py(v) for v in r] for r in rows],
                    "row_count": len(rows), "truncated": truncated, "elapsed_ms": int((time.time() - t0) * 1000)}

    def dry_run(self, sql: str) -> dict:
        """Kiểm tra binder/kiểu dữ liệu mà không chạy truy vấn (giống BigQuery dry-run)."""
        with self.lock.read():
            cur = self.con().cursor()
            try:
                cur.execute("EXPLAIN " + sql)
                plan = "\n".join(r[1] for r in cur.fetchall())
                return {"ok": True, "plan": plan}
            except duckdb.Error as e:
                return {"ok": False, "error": str(e)}
            finally:
                cur.close()

    def scalar(self, sql: str, params=None):
        with self.lock.read():
            cur = self.con().cursor()
            try:
                r = cur.execute(sql, params or []).fetchone()
                return _py(r[0]) if r else None
            finally:
                cur.close()

    def fetch(self, sql: str, params=None) -> list[tuple]:
        with self.lock.read():
            cur = self.con().cursor()
            try:
                return [tuple(_py(v) for v in r) for r in cur.execute(sql, params or []).fetchall()]
            finally:
                cur.close()

    def fetch_df(self, sql: str, params=None):
        with self.lock.read():
            cur = self.con().cursor()
            try:
                return cur.execute(sql, params or []).fetchdf()
            finally:
                cur.close()

    # ------------------------------------------------------------------ quản trị
    @contextmanager
    def writable(self):
        """Đóng kết nối read-only, mở kết nối ghi (dùng khi rebuild model / áp quy tắc sửa dữ liệu)."""
        with self.lock.write():
            if self._con is not None:
                self._con.close()
                self._con = None
            con = duckdb.connect(self.path)
            try:
                yield con
            finally:
                con.close()
                self.version += 1


_wh: Warehouse | None = None


def get_warehouse() -> Warehouse:
    global _wh
    if _wh is None:
        _wh = Warehouse()
    return _wh

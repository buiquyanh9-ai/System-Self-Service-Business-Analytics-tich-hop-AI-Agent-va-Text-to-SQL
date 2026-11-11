"""Đường dẫn, nạp file cấu hình YAML và thiết lập runtime (có thể chỉnh trong trang Admin)."""
from __future__ import annotations

import os
import pathlib
import threading
from functools import lru_cache

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
VAR = ROOT / "var"
CONFIG = ROOT / "config"
WEB = ROOT / "web"
EVAL_DIR = ROOT / "eval"
WAREHOUSE_DB = pathlib.Path(os.environ.get("WAREHOUSE_DB", VAR / "warehouse.duckdb"))
APP_DB = pathlib.Path(os.environ.get("APP_DB", VAR / "app.sqlite"))
STATS_FILE = VAR / "catalog_stats.json"
RULES_FILE = VAR / "active_rules.json"
SECRET = os.environ.get("APP_SECRET", "dev-secret-change-me-" + str(ROOT))

_lock = threading.Lock()


def _load_yaml(name: str) -> dict:
    with open(CONFIG / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=None)
def semantic_layer() -> dict:
    return _load_yaml("semantic_layer.yml")


@lru_cache(maxsize=None)
def catalog_yaml() -> dict:
    return _load_yaml("catalog.yml")


@lru_cache(maxsize=None)
def policies() -> dict:
    return _load_yaml("policies.yml")


@lru_cache(maxsize=None)
def contracts() -> dict:
    return _load_yaml("contracts.yml")


DEFAULT_SETTINGS = {
    # chế độ LLM: auto (Claude nếu có credentials, ngược lại offline), claude, offline
    "llm_mode": os.environ.get("LLM_MODE", "auto"),
    "llm_model": os.environ.get("LLM_MODEL", "claude-opus-5-5"),
    "llm_effort": os.environ.get("LLM_EFFORT", "medium"),
    "max_repair_attempts": 3,
    "query_timeout_s": 20,
    "always_review": False,            # luôn yêu cầu duyệt SQL trước khi chạy
    "fewshot_k": 4,
    "fewshot_reuse_threshold": 0.8,    # Jaccard tối thiểu để tái sử dụng SQL đã xác nhận (kèm điều kiện trùng slot số/thời gian)
    "anchor_date": None,               # None = ngày dữ liệu mới nhất trong kho
}


def reload_configs():
    semantic_layer.cache_clear()
    catalog_yaml.cache_clear()
    policies.cache_clear()
    contracts.cache_clear()

#!/usr/bin/env bash
# Khởi chạy DATA-01 (Linux/macOS/Git Bash):  ./run.sh   |  REBUILD=1 SCALE=0.5 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
PORT="${PORT:-8000}"
if [[ "${REBUILD:-0}" == "1" || ! -f var/warehouse.duckdb ]]; then
  echo "== Sinh dữ liệu giả (scale=${SCALE:-1.0}) và build kho dữ liệu..."
  rm -f var/app.sqlite var/dq_report.json var/model_lineage.json var/active_rules.json
  python warehouse/generate.py --scale "${SCALE:-1.0}"
  python warehouse/build.py
fi
echo "== Mở http://127.0.0.1:${PORT}  (admin/admin123 · analyst/analyst123 · analyst_bac/analyst123 · analyst_nam/analyst123)"
exec python -m uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT}"

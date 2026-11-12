#!/usr/bin/env bash
# Khởi chạy DATA-01 (Linux/macOS/Git Bash):  ./run.sh   |  REBUILD=1 SCALE=0.5 ./run.sh
# Doanh nghiệp khác:  WORKSPACE=giaovan_xyz PORT=8001 ./run.sh      (bỏ WORKSPACE = doanh nghiệp mẫu ABC ở gốc dự án)
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
PORT="${PORT:-8000}"
if [[ -n "${WORKSPACE:-}" ]]; then
  WS="workspaces/${WORKSPACE}"
  if [[ "${REBUILD:-0}" == "1" || ! -f "${WS}/var/warehouse.duckdb" ]]; then
    [[ -f "${WS}/warehouse/generate.py" ]] || { echo "Chưa có kho dữ liệu cho workspace '${WORKSPACE}'. Tạo bằng: python -m app.onboard ${WORKSPACE} --db <file.duckdb>"; exit 1; }
    echo "== Sinh dữ liệu giả cho workspace ${WORKSPACE}..."
    rm -f "${WS}/var/app.sqlite" "${WS}/var/dq_report.json"
    python "${WS}/warehouse/generate.py" --scale "${SCALE:-1.0}"
  fi
  [[ -f "${WS}/var/catalog_stats.json" && "${REBUILD:-0}" != "1" ]] || python -m app.onboard "${WORKSPACE}" --stats
  export WORKSPACE
  echo "== Workspace ${WORKSPACE}: mở http://127.0.0.1:${PORT}  (tài khoản: xem ${WS}/config/policies.yml)"
else
  if [[ "${REBUILD:-0}" == "1" || ! -f var/warehouse.duckdb ]]; then
    echo "== Sinh dữ liệu giả (scale=${SCALE:-1.0}) và build kho dữ liệu..."
    rm -f var/app.sqlite var/dq_report.json var/model_lineage.json var/active_rules.json
    python warehouse/generate.py --scale "${SCALE:-1.0}"
    python warehouse/build.py
  fi
  echo "== Mở http://127.0.0.1:${PORT}  (admin/admin123 · analyst/analyst123 · analyst_bac/analyst123 · analyst_nam/analyst123)"
fi
exec python -m uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT}"

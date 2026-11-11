import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# dùng kho metadata tạm để test không làm bẩn var/app.sqlite
os.environ.setdefault("APP_DB", str(pathlib.Path(tempfile.gettempdir()) / "data01_test_app.sqlite"))
if not (ROOT / "var" / "warehouse.duckdb").exists():
    raise SystemExit("Chưa có kho dữ liệu. Chạy: python warehouse/generate.py && python warehouse/build.py")

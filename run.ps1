# Khởi chạy DATA-01 trên Windows (PowerShell):  .\run.ps1        (thêm -Rebuild để sinh lại dữ liệu)
param([switch]$Rebuild, [double]$Scale = 1.0, [int]$Port = 8000)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"
# Hiển thị tiếng Việt đúng trên console Windows PowerShell 5.1
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
if ($Rebuild -or -not (Test-Path "var/warehouse.duckdb")) {
    Write-Host "== Sinh dữ liệu giả (scale=$Scale) và build kho dữ liệu..."
    if (Test-Path "var") { Remove-Item -Recurse -Force "var/app.sqlite", "var/dq_report.json", "var/model_lineage.json", "var/active_rules.json" -ErrorAction SilentlyContinue }
    python warehouse/generate.py --scale $Scale
    python warehouse/build.py
}
Write-Host "== Mở http://127.0.0.1:$Port  (admin/admin123 · analyst/analyst123 · analyst_bac/analyst123 · analyst_nam/analyst123)"
python -m uvicorn app.main:app --host 127.0.0.1 --port $Port

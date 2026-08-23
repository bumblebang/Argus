@echo off
chcp 65001 >nul
REM Value scan - undervalued-stock research batch (Toss-free: Naver/Yahoo/LLM).
REM Scheduled: ArgusValueScanAM/PM (weekdays 07:40/15:45) + ArgusValueScanSat (Sat 10:00).
REM Exits quietly on LLM limit (next cycle retries).
REM Re-register: powershell -ExecutionPolicy Bypass -File scripts\register_value_scan.ps1
setlocal
cd /d "%~dp0\.."
if not exist logs mkdir logs
set "PYTHONIOENCODING=utf-8"

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [run_value_scan] venv python not found: %PY%>> logs\value_scan.run.log
    exit /b 1
)

echo.>> logs\value_scan.run.log
echo ===== run %date% %time% =====>> logs\value_scan.run.log
"%PY%" scripts\value_scan.py >> logs\value_scan.run.log 2>&1

endlocal

@echo off
chcp 65001 >nul
REM Value scan batch. Prefer: argus value-scan
setlocal
cd /d "%~dp0\.."
if not exist logs mkdir logs
set "PYTHONIOENCODING=utf-8"

set "ARGUS=%~dp0..\.venv\Scripts\argus.exe"
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%ARGUS%" if not exist "%PY%" (
    echo [run_value_scan] venv missing>> logs\value_scan.run.log
    exit /b 1
)

echo.>> logs\value_scan.run.log
echo ===== run %date% %time% =====>> logs\value_scan.run.log
if exist "%ARGUS%" (
  "%ARGUS%" value-scan >> logs\value_scan.run.log 2>&1
) else (
  "%PY%" scripts\value_scan.py >> logs\value_scan.run.log 2>&1
)

endlocal

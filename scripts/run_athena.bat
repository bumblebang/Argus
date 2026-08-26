@echo off
chcp 65001 >nul
REM Athena deep-research window - generates dossiers for the market about to open.
REM Scheduled: ArgusAthenaKR (weekdays 05:35 KST), ArgusAthenaUS (weekdays 15:45 KST).
REM Prefer: argus athena  (pip install -e .). Fallback: python scripts\athena.py
setlocal
cd /d "%~dp0\.."
if not exist logs mkdir logs
set "PYTHONIOENCODING=utf-8"

set "ARGUS=%~dp0..\.venv\Scripts\argus.exe"
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%ARGUS%" if not exist "%PY%" (
    echo [run_athena] venv missing>> logs\athena.run.log
    exit /b 1
)

echo.>> logs\athena.run.log
echo ===== run %date% %time% =====>> logs\athena.run.log
if exist "%ARGUS%" (
  "%ARGUS%" athena >> logs\athena.run.log 2>&1
) else (
  "%PY%" scripts\athena.py >> logs\athena.run.log 2>&1
)

endlocal

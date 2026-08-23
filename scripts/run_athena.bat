@echo off
chcp 65001 >nul
REM Athena deep-research window - generates dossiers for the market about to open.
REM Scheduled: ArgusAthenaKR (weekdays 05:35 KST), ArgusAthenaUS (weekdays 15:45 KST).
REM Market/hard-stop is auto-detected from config athena.windows.
setlocal
cd /d "%~dp0\.."
if not exist logs mkdir logs
set "PYTHONIOENCODING=utf-8"

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [run_athena] venv python not found: %PY%>> logs\athena.run.log
    exit /b 1
)

echo.>> logs\athena.run.log
echo ===== run %date% %time% =====>> logs\athena.run.log
"%PY%" scripts\athena.py >> logs\athena.run.log 2>&1

endlocal

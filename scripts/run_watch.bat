@echo off
chcp 65001 >nul
REM Argus-Watch continuous monitor loop (Task Scheduler, logon trigger).
REM Paper/monitor only in M1 (no orders). Runs forever; Task Scheduler restarts on crash.
REM Pure ASCII only (Korean comments break under cmd CP949).
setlocal
cd /d "%~dp0\.."
if not exist logs mkdir logs
set "PYTHONIOENCODING=utf-8"

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [run_watch] venv python not found: %PY%>> logs\watch.run.log
    exit /b 1
)

echo.>> logs\watch.run.log
echo ===== watch start %date% %time% =====>> logs\watch.run.log
"%PY%" scripts\watch.py >> logs\watch.run.log 2>&1
echo ===== watch exit %errorlevel% %date% %time% =====>> logs\watch.run.log

endlocal

@echo off
chcp 65001 >nul
REM Shadow ledger score. Re-register: register_shadow_score.ps1
setlocal
cd /d "%~dp0\.."
if not exist logs mkdir logs
set "PYTHONIOENCODING=utf-8"

set "ARGUS=%~dp0..\.venv\Scripts\argus.exe"
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%ARGUS%" if not exist "%PY%" (
    echo [run_shadow_score] venv missing>> logs\shadow_score.run.log
    exit /b 1
)

echo.>> logs\shadow_score.run.log
echo ===== run %date% %time% =====>> logs\shadow_score.run.log
if exist "%ARGUS%" (
  "%ARGUS%" shadow-score >> logs\shadow_score.run.log 2>&1
) else (
  "%PY%" scripts\score_shadow_ledger.py >> logs\shadow_score.run.log 2>&1
)

endlocal

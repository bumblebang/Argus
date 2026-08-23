@echo off
chcp 65001 >nul
REM Public share page — brief(TTL) + HTML + GitHub Pages publish.
REM Scheduled: ArgusPublic (daily 08:00 KST). single_commit=true → amend+force-push, history 1.
setlocal
cd /d "%~dp0\.."
if not exist logs mkdir logs
set "PYTHONIOENCODING=utf-8"

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [run_public] venv python not found: %PY%>> logs\public_page.run.log
    exit /b 1
)

echo.>> logs\public_page.run.log
echo ===== run %date% %time% =====>> logs\public_page.run.log
"%PY%" scripts\publish_public.py >> logs\public_page.run.log 2>&1

endlocal

@echo off
chcp 65001 >nul
REM Public share page — brief + HTML + GitHub Pages publish.
setlocal
cd /d "%~dp0\.."
if not exist logs mkdir logs
set "PYTHONIOENCODING=utf-8"

set "ARGUS=%~dp0..\.venv\Scripts\argus.exe"
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%ARGUS%" if not exist "%PY%" (
    echo [run_public] venv missing>> logs\public_page.run.log
    exit /b 1
)

echo.>> logs\public_page.run.log
echo ===== run %date% %time% =====>> logs\public_page.run.log
if exist "%ARGUS%" (
  "%ARGUS%" public >> logs\public_page.run.log 2>&1
) else (
  "%PY%" scripts\publish_public.py >> logs\public_page.run.log 2>&1
)

endlocal

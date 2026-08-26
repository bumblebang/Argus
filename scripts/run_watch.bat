@echo off
chcp 65001 >nul
REM Argus-Watch continuous monitor (Task Scheduler). Prefer argus watch.
REM Pure ASCII (Korean comments break under cmd CP949).
REM Note: register_watch.ps1 uses pythonw scripts\watch.py for no-console;
REM this .bat is the console/logon-friendly path.
setlocal
cd /d "%~dp0\.."
if not exist logs mkdir logs
set "PYTHONIOENCODING=utf-8"

set "ARGUS=%~dp0..\.venv\Scripts\argus.exe"
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%ARGUS%" if not exist "%PY%" (
    echo [run_watch] venv missing>> logs\watch.run.log
    exit /b 1
)

echo.>> logs\watch.run.log
echo ===== watch start %date% %time% =====>> logs\watch.run.log
if exist "%ARGUS%" (
  "%ARGUS%" watch >> logs\watch.run.log 2>&1
) else (
  "%PY%" scripts\watch.py >> logs\watch.run.log 2>&1
)
echo ===== watch exit %errorlevel% %date% %time% =====>> logs\watch.run.log

endlocal

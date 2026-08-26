@echo off
chcp 65001 >nul
REM Pre/post session batch: market_state + agent_cycle dry.
REM Not the always-on daemon (that is ArgusWatch / argus watch).
setlocal
cd /d "%~dp0\.."
if not exist logs mkdir logs
set "PYTHONIOENCODING=utf-8"

set "ARGUS=%~dp0..\.venv\Scripts\argus.exe"
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%ARGUS%" if not exist "%PY%" (
    echo [run_bot] venv missing>> logs\bot.run.log
    exit /b 1
)

echo.>> logs\bot.run.log
echo ===== run %date% %time% =====>> logs\bot.run.log

if exist "%ARGUS%" (
  "%ARGUS%" market-state >> logs\bot.run.log 2>&1
  "%ARGUS%" agent-cycle --cli >> logs\bot.run.log 2>&1
) else (
  "%PY%" scripts\build_market_state.py >> logs\bot.run.log 2>&1
  "%PY%" scripts\agent_cycle.py --cli >> logs\bot.run.log 2>&1
)

endlocal

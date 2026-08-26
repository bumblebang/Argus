@echo off
chcp 65001 >nul
REM Argus data refresh - builds market_state.json only (NO paper cycle).
REM Prefer argus CLI (pip install -e .); fallback python scripts\*.py
setlocal
cd /d "%~dp0\.."
if not exist logs mkdir logs
set "PYTHONIOENCODING=utf-8"

set "ARGUS=%~dp0..\.venv\Scripts\argus.exe"
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%ARGUS%" if not exist "%PY%" (
    echo [run_market_state] venv missing>> logs\market_state.run.log
    exit /b 1
)

echo.>> logs\market_state.run.log
echo ===== run %date% %time% =====>> logs\market_state.run.log

REM screen.py is NOT called here — ArgusWatch owns the trading universe.
if exist "%ARGUS%" (
  "%ARGUS%" market-state >> logs\market_state.run.log 2>&1
  "%ARGUS%" baserate >> logs\market_state.run.log 2>&1
  "%ARGUS%" earnings-cal >> logs\market_state.run.log 2>&1
  "%ARGUS%" macro-cal >> logs\market_state.run.log 2>&1
) else (
  "%PY%" scripts\build_market_state.py >> logs\market_state.run.log 2>&1
  "%PY%" scripts\baserate.py >> logs\market_state.run.log 2>&1
  "%PY%" scripts\earnings_cal.py >> logs\market_state.run.log 2>&1
  "%PY%" scripts\macro_cal.py >> logs\market_state.run.log 2>&1
)

endlocal

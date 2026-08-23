@echo off
chcp 65001 >nul
REM Argus data refresh - builds market_state.json only (NO paper cycle).
REM Unification: the always-on daemon (ArgusWatch) is the sole paper trader.
REM This batch just refreshes the slow macro/regime/news data the daemon brain reads.
setlocal
cd /d "%~dp0\.."
if not exist logs mkdir logs
set "PYTHONIOENCODING=utf-8"

REM Call venv python by absolute path (activate.bat breaks on spaced/non-ASCII paths).
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [run_market_state] venv python not found: %PY%>> logs\market_state.run.log
    exit /b 1
)

echo.>> logs\market_state.run.log
echo ===== run %date% %time% =====>> logs\market_state.run.log

REM NOTE: screen.py is intentionally NOT called here anymore.
REM The always-on daemon (ArgusWatch / engine.universe_refresher) now OWNS the trading
REM universe: it runs the core screen pre-open (~07:30 KST KR / ET04:00 US, DST-auto) and
REM rolls in intraday movers (add-only union). A scheduled screen at 08:30/16:00 would
REM overwrite that live 07:30-core + intraday-mover state -> universe churn/data loss.
REM Run scripts/screen.py manually only for ad-hoc inspection (--dry is safe).
REM
REM 1) Refresh macro/regime/fundamentals for the current universe (Toss + EDGAR/DART/...).
"%PY%" scripts\build_market_state.py >> logs\market_state.run.log 2>&1
REM 2) Base rates: per-symbol setup win-rate stats for the brain. -> data/base_rates.json
"%PY%" scripts\baserate.py >> logs\market_state.run.log 2>&1
REM 3) Earnings calendar + consensus (KR Naver / US Finnhub). -> data/earnings_calendar.json
"%PY%" scripts\earnings_cal.py >> logs\market_state.run.log 2>&1
REM 4) Macro events (FOMC/금통위 curated YAML + optional Finnhub). -> data/macro_calendar.json
"%PY%" scripts\macro_cal.py >> logs\market_state.run.log 2>&1

endlocal

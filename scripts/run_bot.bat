@echo off
chcp 65001 >nul
REM 장전/장후 배치 — 국면 파일 + 에이전트 1사이클 (페이퍼).
REM 상주 데몬이 아니다. 상주는 scripts\watch.py (작업명 ArgusWatch).
setlocal
cd /d "%~dp0\.."
if not exist logs mkdir logs
set "PYTHONIOENCODING=utf-8"

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [run_bot] venv python 없음: %PY%>> logs\bot.run.log
    exit /b 1
)

echo.>> logs\bot.run.log
echo ===== run %date% %time% =====>> logs\bot.run.log

"%PY%" scripts\build_market_state.py >> logs\bot.run.log 2>&1
"%PY%" scripts\agent_cycle.py --cli >> logs\bot.run.log 2>&1

endlocal

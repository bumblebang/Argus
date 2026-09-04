@echo off
chcp 65001 >nul
REM Tier0 measurement reports (dossier / wiring / baseline).
REM Re-register: register_measurement_reports.ps1
setlocal
cd /d "%~dp0\.."
if not exist logs mkdir logs
if not exist data mkdir data
set "PYTHONIOENCODING=utf-8"

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [run_measurement_reports] venv missing>> logs\measurement_reports.run.log
    exit /b 1
)

echo.>> logs\measurement_reports.run.log
echo ===== run %date% %time% =====>> logs\measurement_reports.run.log

"%PY%" scripts\dossier_report.py --out data\dossier_quality.json >> logs\measurement_reports.run.log 2>&1
if errorlevel 1 (
  echo [dossier_report] FAIL>> logs\measurement_reports.run.log
  set ERR=1
)

"%PY%" scripts\wiring_mismatch_report.py --days 14 --threshold 3 --out data\wiring_mismatch.json >> logs\measurement_reports.run.log 2>&1
if errorlevel 1 (
  echo [wiring_mismatch] FAIL>> logs\measurement_reports.run.log
  set ERR=1
)

"%PY%" scripts\measurement_baseline.py --out data\measurement_baseline_latest.json >> logs\measurement_reports.run.log 2>&1
if errorlevel 1 (
  echo [measurement_baseline] FAIL>> logs\measurement_reports.run.log
  set ERR=1
)

if defined ERR (
  echo [run_measurement_reports] done with errors>> logs\measurement_reports.run.log
  endlocal & exit /b 1
)
echo [run_measurement_reports] OK>> logs\measurement_reports.run.log
endlocal
exit /b 0

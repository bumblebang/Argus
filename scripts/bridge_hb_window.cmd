@echo off
cd /d "%~dp0.."
title Argus bridge serve (hb+judge)
echo Argus bridge --serve 60  (heartbeat + judge, one window)
echo Optional headless: set CURSOR_API_KEY + pip install cursor-sdk
echo ROOT=%CD%
set "ARGUS=%CD%\.venv\Scripts\argus.exe"
set "PY=%CD%\.venv\Scripts\python.exe"
if exist "%ARGUS%" (
  "%ARGUS%" bridge --serve 60
) else (
  "%PY%" "scripts\bridge_tick.py" --serve 60
)
echo serve exited=%ERRORLEVEL%
pause

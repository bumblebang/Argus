@echo off
cd /d "%~dp0.."
title Argus bridge serve (hb+auto)
echo Argus bridge --serve 60  (heartbeat + auto, one window)
echo ROOT=%CD%
".venv\Scripts\python.exe" "scripts\bridge_tick.py" --serve 60
echo serve exited=%ERRORLEVEL%
pause

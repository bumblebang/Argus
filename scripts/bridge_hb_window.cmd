@echo off
cd /d "%~dp0..\.."
title Argus bridge hb-loop
echo Argus bridge hb-loop - keep this window open
echo ROOT=%CD%
".\argus\.venv\Scripts\python.exe" ".\argus\scripts\bridge_tick.py" --auto
echo auto exit=%ERRORLEVEL%
".\argus\.venv\Scripts\python.exe" ".\argus\scripts\bridge_tick.py" --heartbeat-loop 60
echo hb-loop exited=%ERRORLEVEL%
pause

@echo off
cd /d "%~dp0.."
schtasks /End /TN ArgusWatch
timeout /t 3 /nobreak >nul
schtasks /Run /TN ArgusWatch
timeout /t 6 /nobreak >nul
".venv\Scripts\python.exe" -c "from src.engine.wake_request import request_brain_wake; print(request_brain_wake(reason='manual_bridge_retry'))"
echo done
pause

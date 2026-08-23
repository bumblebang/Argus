# Windows 상주

상주는 **ArgusWatch** (`scripts/watch.py`) 다. 매매 유니버스도 이 프로세스가 굴린다.

1. `.venv\Scripts\python.exe` 로 `scripts/watch.py --dry --ticks 1` 이 되는지 확인.
2. Claude Code가 PATH에 있으면 `claude_command: "claude"` 그대로. 작업 스케줄러 데몬에서만 안 보이면 MSIX 패키지 경로를 `python scripts/which_claude.py` 로 확인. 로그인은 `scripts\claude_login.bat` (봇이 쓰는 그 exe).
3. 상주 등록:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_watch.ps1
schtasks /Run /TN ArgusWatch
```

watch 프로세스가 절전을 막는다 (`SetThreadExecutionState`). 대시보드 `http://127.0.0.1:8787`.
무인 상주면 `.env` 에 `NTFY_TOPIC`.

중지: `schtasks /End /TN ArgusWatch`

장전 배치(Athena / market_state / value_scan)는 `scripts/run_*.bat` 를 작업 스케줄러에 시각을 나눠 등록한다. 유니버스 스크린은 데몬이 하므로 `run` 에 `screen.py` 를 넣을 필요는 없다. 예전 `register_task.ps1` 은 배치용이다.


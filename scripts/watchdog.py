"""Argus-Watch 워치독 — 하트비트 신선도 검사 후 stale 면 상주 루프 재기동.

OS 스케줄러가 N분마다 호출한다. 2단 방어:
  - 크래시(프로세스 종료): 스케줄러 KeepAlive / restart-on-failure 가 1차로 잡음.
  - 멈춤(hang, heartbeat 갱신 중단): 이 워치독이 잡아 작업을 재기동.

heartbeat 는 paths.resolve("watch_hb") — 컷오버 후 data/state/watch.heartbeat
(WatchLoop._beat 가 매 틱 epoch 기록).
장중 5초/휴장 60초 주기로 갱신되므로 STALE_SEC=300(5분) 이면 오탐 없이 hang 감지.

Windows: 작업 스케줄러 작업명 ArgusWatch (schtasks).
macOS: launchd 라벨 local.argus.watch (ARGUS_LAUNCHD_LABEL 로 덮어쓰기).
Linux: systemd --user 유닛 argus-watch.service (ARGUS_SYSTEMD_UNIT 로 덮어쓰기).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEARTBEAT = None  # main 에서 paths.resolve 로 설정
LOG = ROOT / "logs" / "watchdog.log"
TASK = "ArgusWatch"
LAUNCHD_LABEL = "local.argus.watch"
STALE_SEC = 300


def log(msg: str) -> None:
    # pythonw(무콘솔)에서는 sys.stdout 이 None 이라 print 가 못 쓰인다 → 파일로.
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass
    if sys.stdout is not None:
        print(msg)


def _heartbeat_path() -> Path:
    sys.path.insert(0, str(ROOT))
    from src import paths as _paths
    return _paths.resolve("watch_hb", configured="data/watch.heartbeat")


def heartbeat_age() -> float | None:
    try:
        d = json.loads(_heartbeat_path().read_text(encoding="utf-8"))
        return time.time() - float(d.get("ts", 0))
    except (OSError, ValueError):
        return None


def restart_argv(platform: str, *, uid: int | None = None) -> list[list[str]] | None:
    """재기동에 쓸 argv 목록. 이 OS 에서 모르면 None."""
    if platform.startswith("win"):
        return [
            ["schtasks", "/End", "/TN", TASK],
            ["schtasks", "/Run", "/TN", TASK],
        ]
    if platform == "darwin":
        if uid is None:
            uid = os.getuid()
        label = os.environ.get("ARGUS_LAUNCHD_LABEL", LAUNCHD_LABEL)
        return [["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"]]
    if platform.startswith("linux"):
        unit = os.environ.get("ARGUS_SYSTEMD_UNIT", "argus-watch.service")
        return [["systemctl", "--user", "restart", unit]]
    return None


def restart() -> None:
    cmds = restart_argv(sys.platform)
    if not cmds:
        log(f"[watchdog] restart not wired for {sys.platform} — start watch.py yourself")
        return
    for argv in cmds:
        subprocess.run(argv, capture_output=True)


def main() -> int:
    age = heartbeat_age()
    if age is None:
        log("[watchdog] heartbeat missing -> (re)start"); restart(); return 0
    if age > STALE_SEC:
        log(f"[watchdog] heartbeat stale {age:.0f}s > {STALE_SEC}s -> restart")
        restart(); return 0
    log(f"[watchdog] heartbeat fresh {age:.0f}s -> ok")
    return 0


if __name__ == "__main__":
    from src.cli.legacy import warn_legacy_script
    warn_legacy_script("argus watchdog")
    sys.exit(main())

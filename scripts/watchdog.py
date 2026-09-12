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
    if sys.stdout is None:
        return
    # 출력 실패가 재기동을 막으면 안 된다 — log() 는 restart() 바로 앞에서 불린다.
    # (cp949 콘솔에 em dash 를 찍다 UnicodeEncodeError 로 죽은 사례: post-merge 훅)
    try:
        print(msg)
    except (UnicodeEncodeError, OSError, ValueError):
        pass


def _heartbeat_path() -> Path:
    sys.path.insert(0, str(ROOT))
    from src import paths as _paths
    return _paths.resolve("watch_hb", configured="data/watch.heartbeat")


def heartbeat_payload() -> dict | None:
    try:
        return json.loads(_heartbeat_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def heartbeat_age() -> float | None:
    d = heartbeat_payload()
    if not d:
        return None
    try:
        return time.time() - float(d.get("ts", 0))
    except (TypeError, ValueError):
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
    kw: dict = {"capture_output": True}
    if sys.platform.startswith("win") and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    for argv in cmds:
        subprocess.run(argv, **kw)


REV_STATE = ROOT / "data" / "state" / "watchdog_rev.json"
DIRTY_STABLE_RUNS = 2      # 미커밋 변경은 연속 N회 같은 지문일 때만 재기동


def _rev_state() -> dict:
    try:
        d = json.loads(REV_STATE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_rev_state(d: dict) -> None:
    try:
        REV_STATE.parent.mkdir(parents=True, exist_ok=True)
        REV_STATE.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass


def _dirty_settled(live: str) -> bool:
    """같은 dirty 지문을 연속 DIRTY_STABLE_RUNS 회 봤는가.

    편집 도중(반쯤 저장된 파일)에 라이브 데몬을 재기동하지 않기 위한 안정화.
    워치독이 15분 주기이므로 실질적으로 '15분 이상 변화가 멈춘 뒤' 재기동한다.
    """
    st = _rev_state()
    seen = int(st.get("seen") or 0) + 1 if st.get("rev") == live else 1
    _save_rev_state({"rev": live, "seen": seen, "ts": time.time()})
    return seen >= DIRTY_STABLE_RUNS


def code_rev_stale(hb: dict | None) -> tuple[bool, str, str]:
    """디스크 코드와 프로세스 기동 rev 불일치. (stale, proc_rev, live_rev).

    HEAD 불일치(머지/풀)는 즉시 재기동. 미커밋 변경(dirty 지문)만 다르면 편집이
    끝난 뒤 재기동하도록 연속 관측을 요구한다.
    """
    sys.path.insert(0, str(ROOT))
    from src.code_rev import current_code_rev, split_rev

    live = current_code_rev(ROOT)
    proc = str((hb or {}).get("code_rev") or "").strip()
    if live == "unknown" or not proc:
        return False, proc, live
    if live == proc:
        if REV_STATE.exists():
            _save_rev_state({})          # 동기 상태 — 관측 카운터 초기화
        return False, proc, live
    live_head, live_dirty = split_rev(live)
    proc_head, _proc_dirty = split_rev(proc)
    if live_head != proc_head:
        return True, proc, live          # 커밋이 다르다 — 즉시
    if not live_dirty:
        # 디스크는 깨끗한데 프로세스는 미커밋본 — 커밋/되돌림 직후. 즉시 재기동.
        return True, proc, live
    return _dirty_settled(live), proc, live


def main() -> int:
    age = heartbeat_age()
    hb = heartbeat_payload() or {}
    if age is None:
        log("[watchdog] heartbeat missing -> (re)start"); restart(); return 0
    if age > STALE_SEC:
        log(f"[watchdog] heartbeat stale {age:.0f}s > {STALE_SEC}s -> restart")
        restart(); return 0
    stale_rev, proc_rev, live_rev = code_rev_stale(hb)
    if stale_rev:
        log(f"[watchdog] code_rev stale proc={proc_rev} live={live_rev} -> restart")
        _save_rev_state({})              # 재기동 직후 다시 세지 않도록 카운터 비움
        restart(); return 0
    # age 는 신선해도 장중 polled=0 이면 가짜 초록 — 재기동은 안 하고 경보만(alert_check).
    should = list(hb.get("should_be_open") or [])
    mkts = list(hb.get("markets_open") or [])
    polled = int(hb.get("polled") or 0)
    ok = hb.get("ok")
    poll_unhealthy = (
        ok is False
        or (should and polled <= 0)
        or (should and not mkts)
        or (mkts and polled <= 0)
    )
    if poll_unhealthy:
        log(f"[watchdog] heartbeat fresh {age:.0f}s but poll unhealthy "
            f"(ok={ok}, should={should}, polled={polled}, markets={mkts}) "
            f"— no restart, alert-check 담당")
        return 0
    log(f"[watchdog] heartbeat fresh {age:.0f}s -> ok")
    return 0

if __name__ == "__main__":
    from src.cli.legacy import warn_legacy_script
    warn_legacy_script("argus watchdog")
    sys.exit(main())

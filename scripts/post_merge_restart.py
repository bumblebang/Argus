"""git post-merge/post-rewrite: main 코드 변경 시 ArgusWatch 재기동.

githooks/post-merge 가 호출. watch 경로(scripts/src 등)가 바뀐 경우만 재기동.

출력은 재기동을 막을 권한이 없다: 2026-09-11 PR #51 머지 때 안내 문구의 em dash
(U+2014)가 cp949 콘솔에 안 찍혀 UnicodeEncodeError 로 훅이 죽었고, 그 print 가
restart_watch() 앞이라 자동 재기동이 통째로 유실됐다(구 코드로 계속 운행).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
WATCH_PATH_PREFIXES = (
    "scripts/",
    "src/",
    "tests/golden/",
    "config.example.yaml",
    "pyproject.toml",
)


def say(msg: str, *, err: bool = False) -> None:
    """안내 출력. 실패해도(콘솔 인코딩·파이프 끊김) 호출측 흐름을 끊지 않는다."""
    stream = sys.stderr if err else sys.stdout
    if stream is None:
        return
    try:
        print(msg, file=stream)
    except (UnicodeEncodeError, OSError, ValueError):
        try:  # 최소한 무슨 일이 있었는지는 남긴다(ASCII 로 강등)
            print(msg.encode("ascii", "backslashreplace").decode("ascii"), file=stream)
        except Exception:
            pass


def paths_changed(prev_head: str) -> bool:
    if not prev_head:
        return True
    r = subprocess.run(
        ["git", "diff", "--name-only", prev_head, "HEAD", "--"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return True
    for line in (r.stdout or "").splitlines():
        p = line.strip().replace("\\", "/")
        if not p:
            continue
        if any(p == pref.rstrip("/") or p.startswith(pref) for pref in WATCH_PATH_PREFIXES):
            return True
    return False


def restart_watch() -> bool:
    if sys.platform.startswith("win"):
        ps1 = ROOT / "scripts" / "restart_watch.ps1"
        # Hidden + CREATE_NO_WINDOW: git 훅/IDE에서 돌릴 때 검은 콘솔이 깜빡이지 않게.
        # (claude CLI 무창과 별개 — 여기는 재기동용 powershell)
        kw: dict = {
            "cwd": ROOT,
            "capture_output": True,
            "text": True,
            "check": False,
        }
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        r = subprocess.run(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden",
             "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-Quiet"],
            **kw,
        )
        if r.returncode != 0:
            say(f"post-merge: restart_watch 실패 — {r.stderr or r.stdout}", err=True)
            return False
        return True
    sys.path.insert(0, str(ROOT / "scripts"))
    import watchdog as wd  # noqa: WPS433

    wd.restart()
    return True


def main() -> int:
    prev = sys.argv[1] if len(sys.argv) > 1 else ""
    if not paths_changed(prev):
        say("post-merge: watch 코드 경로 변경 없음 — 재기동 스킵")
        return 0
    say("post-merge: watch 코드 변경 감지 — ArgusWatch 재기동")
    if not restart_watch():
        say("post-merge: 수동 재기동 — scripts/restart_watch.ps1", err=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""git post-merge/post-rewrite: main 코드 변경 시 ArgusWatch 재기동.

githooks/post-merge 가 호출. watch 경로(scripts/src 등)가 바뀐 경우만 재기동.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WATCH_PATH_PREFIXES = (
    "scripts/",
    "src/",
    "tests/golden/",
    "config.example.yaml",
    "pyproject.toml",
)


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
        r = subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-Quiet"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            print(f"post-merge: restart_watch 실패 — {r.stderr or r.stdout}", file=sys.stderr)
            return False
        return True
    sys.path.insert(0, str(ROOT / "scripts"))
    import watchdog as wd  # noqa: WPS433

    wd.restart()
    return True


def main() -> int:
    prev = sys.argv[1] if len(sys.argv) > 1 else ""
    if not paths_changed(prev):
        print("post-merge: watch 코드 경로 변경 없음 — 재기동 스킵")
        return 0
    print("post-merge: watch 코드 변경 감지 — ArgusWatch 재기동")
    if not restart_watch():
        print("post-merge: 수동 재기동 — scripts/restart_watch.ps1", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

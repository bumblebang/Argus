"""실행 중인 watch 와 디스크 코드 버전 대조용 git HEAD 식별자.

watchdog 가 heartbeat.code_rev ≠ current_code_rev() 이면 재기동한다.
머지/풀 후 프로세스가 구 코드를 들고 도는 상황(PR #26 미반영 등)을 막는다.
"""
from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

from .config import ROOT


@lru_cache(maxsize=1)
def current_code_rev(root: Path | None = None) -> str:
    """짧은 git HEAD(12자). .git 없음·실패 시 'unknown' — watchdog 는 비교 스킵."""
    base = (root or ROOT).resolve()
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=base,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        rev = (r.stdout or "").strip()
        if r.returncode == 0 and rev:
            return rev
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "unknown"

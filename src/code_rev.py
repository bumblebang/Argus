"""실행 중인 watch 와 디스크 코드 버전 대조용 식별자.

watchdog 가 heartbeat.code_rev ≠ current_code_rev() 이면 재기동한다.
머지/풀 후 프로세스가 구 코드를 들고 도는 상황(PR #26 미반영 등)을 막는다.

**git HEAD 만으로는 부족하다.** 커밋하지 않은 워킹트리 수정본을 들고 도는 프로세스는
HEAD 가 같아 "동기" 로 보이고, 로그·heartbeat 의 code_rev 도 실제 돌던 코드와 다른
커밋을 가리켜 사후분석을 오도한다. 그래서 런타임 코드(`src/`·`scripts/`·`main.py`)의
워킹트리 변경분을 해시해 `<head12>+<dirty8>` 로 붙인다.

지문 범위를 런타임 코드로 한정하는 이유: `data/` 는 봇이 매 틱 쓰는 산출물이라 지문에
넣으면 계속 흔들려 재기동이 폭주한다.
"""
from __future__ import annotations

import hashlib
import subprocess
from functools import lru_cache
from pathlib import Path

from .config import ROOT

# 워킹트리 지문 대상 — 프로세스가 실제로 import·실행하는 코드만.
CODE_PATHS: tuple[str, ...] = ("src", "scripts", "main.py")


def _git(base: Path, *args: str) -> str | None:
    """git 호출. 실패·타임아웃이면 None(호출부가 스킵 판단)."""
    try:
        r = subprocess.run(["git", *args], cwd=base, capture_output=True,
                           timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "replace")


def worktree_fingerprint(root: Path | None = None) -> str:
    """런타임 코드의 미커밋 변경 지문(8자). 깨끗하거나 판정 불가면 빈 문자열.

    추적 파일은 `git diff HEAD` 내용을, 미추적 파일은 경로+내용 해시를 함께 넣는다
    (미추적 새 모듈을 편집해도 지문이 움직이도록).
    """
    base = (root or ROOT).resolve()
    diff = _git(base, "diff", "HEAD", "--", *CODE_PATHS)
    status = _git(base, "status", "--porcelain", "-uall", "--", *CODE_PATHS)
    if diff is None or status is None:
        return ""
    h = hashlib.sha1(diff.encode("utf-8", "replace"))
    for line in status.splitlines():
        if not line.startswith("?? "):
            continue
        rel = line[3:].strip().strip('"')
        h.update(rel.encode("utf-8", "replace"))
        try:
            h.update((base / rel).read_bytes())
        except OSError:
            pass                       # 읽기 실패해도 경로만으로 변화는 잡힌다
    if not diff and not any(l.startswith("?? ") for l in status.splitlines()):
        return ""
    return h.hexdigest()[:8]


@lru_cache(maxsize=2)
def current_code_rev(root: Path | None = None) -> str:
    """`<git HEAD 12자>` — 미커밋 런타임 코드가 있으면 `<head12>+<dirty8>`.

    .git 없음·git 실패 시 'unknown' — watchdog 는 비교를 스킵한다.
    프로세스 안에서는 캐시된다(기동 시점의 코드를 계속 가리켜야 하므로).
    """
    base = (root or ROOT).resolve()
    head = (_git(base, "rev-parse", "--short=12", "HEAD") or "").strip()
    if not head:
        return "unknown"
    dirty = worktree_fingerprint(base)
    return f"{head}+{dirty}" if dirty else head


def split_rev(rev: str) -> tuple[str, str]:
    """`<head>+<dirty>` → (head, dirty). dirty 없으면 ('head', '')."""
    head, _, dirty = str(rev or "").partition("+")
    return head, dirty

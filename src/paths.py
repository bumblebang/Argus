"""운영 경로 계약 — 논리 키 → 레거시/신 레이아웃 (G0 매니페스트와 동기).

Phase 0: 물리 레이아웃은 그대로. I/O 호출을 이 API로 모은다.
Phase 2: resolve 가 data/state/… 와 레거시 data/* 둘 다 찾음.
  - 존재 우선: LAYOUT(신) → configured → LEGACY(구)
  - 둘 다 없으면 LAYOUT 을 쓰기 대상으로 반환 (컷오버 후 기본)
  - inbox: 신=data/inbox, 구=data/llm_inbox (PokeTokenBarWin 호환 유지)

절대 Path 가 ROOT 아래 레거시/LAYOUT 상대경로와 같으면 dual-search 한다
(ROOT/'data'/'bot.db' 가 state/bot.db 를 놓치지 않게).
"""
from __future__ import annotations

from pathlib import Path

from .config import ROOT

# tests/golden/ops_path_manifest.json 의 paths 와 동일해야 한다 (레거시 계약).
CANONICAL: dict[str, str] = {
    "inbox": "data/llm_inbox",
    "bridge_hb": "data/llm_inbox/bridge.heartbeat",
    "bridge_mode": "data/llm_inbox/bridge.mode",
    "bridge_request": "data/llm_inbox/request.json",
    "bridge_response": "data/llm_inbox/response.json",
    "watch_hb": "data/watch.heartbeat",
    "watch_pid": "data/watch.pid",
    "halt": "data/HALT",
    "wake_request": "data/brain_wake_request.json",
    "paper": "data/paper_account.json",
    "db": "data/bot.db",
    "decisions": "data/decisions.jsonl",
    "brain_mode": "data/brain_mode.json",
    "bridge_script": "scripts/bridge_tick.py",
}

# Phase 2 목표 상대경로. 레거시와 같으면 이동 없음(bridge_script 등).
LAYOUT: dict[str, str] = {
    "inbox": "data/inbox",
    "bridge_hb": "data/inbox/bridge.heartbeat",
    "bridge_mode": "data/inbox/bridge.mode",
    "bridge_request": "data/inbox/request.json",
    "bridge_response": "data/inbox/response.json",
    "watch_hb": "data/state/watch.heartbeat",
    "watch_pid": "data/state/watch.pid",
    "halt": "data/state/HALT",
    "wake_request": "data/state/brain_wake_request.json",
    "paper": "data/state/paper_account.json",
    "db": "data/state/bot.db",
    "decisions": "data/ledgers/decisions.jsonl",
    "brain_mode": "data/state/brain_mode.json",
    "bridge_script": "scripts/bridge_tick.py",
}

# 컷오버 시 디렉터리/파일 이동 계획 (src → dst). inbox 는 특수(junction).
MIGRATE_MOVES: list[tuple[str, str]] = [
    ("data/bot.db", "data/state/bot.db"),
    ("data/paper_account.json", "data/state/paper_account.json"),
    ("data/watch.heartbeat", "data/state/watch.heartbeat"),
    ("data/watch.pid", "data/state/watch.pid"),
    ("data/HALT", "data/state/HALT"),
    ("data/brain_wake_request.json", "data/state/brain_wake_request.json"),
    ("data/brain_mode.json", "data/state/brain_mode.json"),
    ("data/decisions.jsonl", "data/ledgers/decisions.jsonl"),
]


def rel(key: str) -> str:
    """논리 키의 레거시 상대경로 문자열 (posix). G0·기본값 계약용."""
    try:
        return CANONICAL[key]
    except KeyError as e:
        known = ", ".join(sorted(CANONICAL))
        raise KeyError(f"unknown path key {key!r}; known: {known}") from e


def layout_rel(key: str) -> str:
    """신 레이아웃 상대경로. 없으면 레거시와 동일."""
    if key not in CANONICAL:
        raise KeyError(f"unknown path key {key!r}")
    return LAYOUT.get(key, CANONICAL[key])


def _as_abs(base: Path, maybe: str | Path) -> Path:
    p = Path(maybe)
    if p.is_absolute():
        return p
    return (base / p)


def _posix(s: str) -> str:
    return str(s).replace("\\", "/")


def _under_repo_layout(base: Path, configured: Path, legacy_rel: str,
                       new_rel: str) -> bool:
    """configured 가 repo 아래 레거시/LAYOUT 경로면 dual-search 대상."""
    try:
        conf = configured.resolve()
        base_r = base.resolve()
        rel_s = conf.relative_to(base_r).as_posix()
    except (ValueError, OSError):
        return False
    return rel_s in (legacy_rel, new_rel)


def _usable_existing(path: Path) -> bool:
    """존재하고 '쓸 만한' 후보인가.

    빈 파일(st_size==0)은 LAYOUT 이 레거시보다 먼저 있어도 채택하지 않는다 —
    부분 컷오버 중 touch 된 빈 state/bot.db 가 찬 레거시를 가리는 사고 방지.
    빈 디렉터리도 동일(빈 data/inbox 가 찬 llm_inbox 를 가리지 않게).
    존재하지 않으면 False.
    """
    try:
        if not path.exists():
            return False
        if path.is_dir():
            try:
                next(path.iterdir())
                return True
            except StopIteration:
                return False
        return path.stat().st_size > 0
    except OSError:
        return False


def resolve(key: str, *, root: Path | None = None,
            configured: str | Path | None = None) -> Path:
    """repo root 기준 절대경로.

    존재 우선: LAYOUT → configured → LEGACY (단, 빈 파일은 건너뜀).
    쓸 만한 후보가 없으면 LAYOUT(컷오버 후 쓰기 기본).

    configured 가 repo 밖·다른 상대경로면(테스트 tmp 등) dual-search 없이 그대로.
    ROOT/'data'/'bot.db' 처럼 절대 Path 여도 레거시/LAYOUT 이면 dual-search.
    """
    if key not in CANONICAL:
        raise KeyError(f"unknown path key {key!r}")
    base = ROOT if root is None else Path(root)
    legacy_rel = rel(key)
    new_rel = layout_rel(key)

    configured_path: Path | None = None
    dual = True
    if configured is not None and str(configured).strip():
        conf_s = _posix(configured)
        configured_path = _as_abs(base, configured)
        if conf_s not in (legacy_rel, new_rel):
            # 절대경로·이상 상대경로 — repo 레이아웃이면 dual, 아니면 오버라이드
            if not _under_repo_layout(base, configured_path, legacy_rel, new_rel):
                return configured_path.resolve()
            dual = True

    candidates: list[Path] = []
    if dual and new_rel != legacy_rel:
        candidates.append(base / new_rel)
    if configured_path is not None:
        candidates.append(configured_path)
    candidates.append(base / legacy_rel)

    seen: set[str] = set()
    uniq: list[Path] = []
    for c in candidates:
        k = str(c)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)

    # 1순위: 비어 있지 않은 존재 파일/디렉터리 (LAYOUT→configured→LEGACY 순서 유지)
    for c in uniq:
        if _usable_existing(c):
            return c.resolve()
    # 2순위: 빈 파일만 있을 때 — 그래도 있는 쪽(쓰기/HALT touch 등)
    for c in uniq:
        try:
            if c.exists():
                return c.resolve()
        except OSError:
            continue
    # 쓰기 기본: LAYOUT (컷오버 후)
    return (base / new_rel).resolve()


def ensure_parent(key: str, *, root: Path | None = None,
                  configured: str | Path | None = None) -> Path:
    """resolve 후 부모 디렉터리 생성."""
    path = resolve(key, root=root, configured=configured)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

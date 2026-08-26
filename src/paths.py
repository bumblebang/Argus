"""운영 경로 계약 — 논리 키 → 레거시/신 레이아웃 (G0 매니페스트와 동기).

Phase 0: 물리 레이아웃은 그대로. I/O 호출을 이 API로 모은다.
Phase 2: resolve 가 data/state/… 와 레거시 data/* 둘 다 찾음.
  - 존재 우선: configured → LAYOUT(신) → LEGACY(구)
  - 둘 다 없으면 LEGACY 를 쓰기 대상으로 반환 (컷오버 전 기본)
  - inbox: 신=data/inbox, 구=data/llm_inbox (PokeTokenBarWin 호환 유지)
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


def resolve(key: str, *, root: Path | None = None,
            configured: str | Path | None = None) -> Path:
    """repo root 기준 절대경로.

    존재 우선 (config 가 레거시/LAYOUT 상대경로일 때):
      configured(존재) → LAYOUT → LEGACY → LEGACY(쓰기 기본)

    configured 가 레거시·LAYOUT 상대경로가 **아니면** (테스트 tmp·절대 오버라이드)
    dual-search 없이 그 경로를 그대로 쓴다 — 운영 data/* 로 새지 않음.
    """
    if key not in CANONICAL:
        raise KeyError(f"unknown path key {key!r}")
    base = ROOT if root is None else Path(root)
    legacy_rel = rel(key)
    new_rel = layout_rel(key)

    if configured is not None and str(configured).strip():
        conf_s = str(configured).replace("\\", "/")
        if conf_s not in (legacy_rel, new_rel):
            return _as_abs(base, configured).resolve()
        configured_path = _as_abs(base, configured)
    else:
        configured_path = None

    candidates: list[Path] = []
    if configured_path is not None:
        candidates.append(configured_path)
    if new_rel != legacy_rel:
        candidates.append(base / new_rel)
    candidates.append(base / legacy_rel)

    seen: set[str] = set()
    uniq: list[Path] = []
    for c in candidates:
        k = str(c)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)

    for c in uniq:
        if c.exists():
            return c.resolve()
    if configured_path is not None:
        return configured_path.resolve()
    return (base / legacy_rel).resolve()


def ensure_parent(key: str, *, root: Path | None = None,
                  configured: str | Path | None = None) -> Path:
    """resolve 후 부모 디렉터리 생성."""
    path = resolve(key, root=root, configured=configured)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

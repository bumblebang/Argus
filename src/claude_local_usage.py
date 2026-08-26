"""Claude Code 로컬 JSONL 사용량 — 토케포케 local_usage 와 동일 계약(얇은 이식).

~/.claude/projects/**/*.jsonl 의 assistant.usage 를 합산.
구독 잔량 API 가 아니라 호출당 토큰 합(추정 % 분자).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class UsageEntry:
    id: str
    when_ts: float
    total: int


def _home() -> Path:
    return Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or ".")


def _int(v: Any) -> int:
    if v is None or isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, dict):
        for k in ("value", "tokens", "count", "input_tokens", "output_tokens"):
            if k in v:
                return _int(v[k])
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _parse_ts(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        ms = float(raw)
        if ms < 1e12:
            ms *= 1000.0
        return ms / 1000.0
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def claude_project_roots() -> list[Path]:
    home = _home()
    roots = [
        home / ".claude" / "projects",
        home / ".config" / "claude" / "projects",
    ]
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        roots.insert(0, Path(cfg) / "projects")
    out: list[Path] = []
    seen: set[Path] = set()
    for r in roots:
        try:
            r = r.resolve()
        except OSError:
            continue
        if r in seen or not r.is_dir():
            continue
        seen.add(r)
        out.append(r)
    return out


def parse_claude_line(line: str) -> UsageEntry | None:
    if '"usage"' not in line or '"assistant"' not in line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if obj.get("type") != "assistant":
        return None
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    when = _parse_ts(obj.get("timestamp"))
    if when is None:
        return None
    mid = str(msg.get("id") or "")
    rid = str(obj.get("requestId") or "")
    eid = f"{mid}|{rid}"
    if eid == "|":
        eid = str(obj.get("uuid") or obj.get("id") or "")
    if not eid:
        return None
    total = (
        _int(usage.get("input_tokens"))
        + _int(usage.get("output_tokens"))
        + _int(usage.get("cache_creation_input_tokens"))
        + _int(usage.get("cache_read_input_tokens"))
    )
    if total <= 0:
        return None
    return UsageEntry(id=eid, when_ts=when, total=total)


def _dedup_keep_max(entries: Iterable[UsageEntry]) -> list[UsageEntry]:
    best: dict[str, UsageEntry] = {}
    for e in entries:
        prev = best.get(e.id)
        if prev is None or e.total >= prev.total:
            best[e.id] = e
    return list(best.values())


def _jsonl_candidates(roots: list[Path], *, since_ts: float) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for path in root.rglob("*.jsonl"):
                if not path.is_file():
                    continue
                try:
                    if path.stat().st_mtime >= since_ts - 3600:
                        out.append(path)
                except OSError:
                    continue
        except OSError:
            continue
    return out


def tokens_since(*, since_ts: float, now: float | None = None,
                 roots: list[Path] | None = None) -> dict[str, Any]:
    """since_ts 이후(포함) assistant usage 합. 반환: used, n_events, roots_scanned."""
    del now  # 예약(테스트 주입용 자리)
    roots = list(roots) if roots is not None else claude_project_roots()
    entries: list[UsageEntry] = []
    for path in _jsonl_candidates(roots, since_ts=since_ts):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            ent = parse_claude_line(line)
            if ent is None or ent.when_ts < since_ts:
                continue
            entries.append(ent)
    kept = _dedup_keep_max(entries)
    used = sum(e.total for e in kept)
    return {
        "used": used,
        "n_events": len(kept),
        "roots": [str(r) for r in roots],
        "since_ts": since_ts,
    }


def tokens_in_window(*, window_sec: float = 5 * 3600,
                     now: float | None = None,
                     roots: list[Path] | None = None) -> dict[str, Any]:
    ts = time.time() if now is None else float(now)
    since = ts - float(window_sec)
    out = tokens_since(since_ts=since, now=ts, roots=roots)
    out["window_sec"] = float(window_sec)
    out["now"] = ts
    return out

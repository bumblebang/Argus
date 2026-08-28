"""NXT 지원 여부 — 갭반등 15:20 vs 19:50 유니버스 split.

토스 StockInfo 의 nxtSupported(있으면) + stock_info_cache.json.
필드 없으면 None — 15:20(정규-only)에는 포함, 19:50(NXT)에는 제외(보수적).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from ..logging_setup import get_logger
from .stock_info import INFO_CACHE_PATH

log = get_logger("src.nxt_universe")


def _load_cache(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def nxt_supported_for(symbol: str, cache: dict | None = None) -> bool | None:
    """True=NXT 지원, False=미지원, None=캐시·필드 없음."""
    sym = str(symbol).strip()
    if not sym:
        return None
    raw = cache if cache is not None else _load_cache(INFO_CACHE_PATH)
    info = (raw.get(sym) or {}).get("info") or {}
    val = info.get("nxtSupported")
    if val is None:
        return None
    return bool(val)


def nxt_supported_map(symbols: Iterable[str],
                        cache_path: Path | None = None) -> dict[str, bool | None]:
    cache = _load_cache(cache_path or INFO_CACHE_PATH)
    return {str(s): nxt_supported_for(s, cache) for s in symbols if s}


def filter_items_for_gap_scan(items: list[dict], wake_reason: str,
                              nxt: dict[str, bool | None] | None = None) -> list[dict]:
    """gap_rebound_scan / nxt_gap_scan 시 후보 종목 split."""
    lookup = nxt or {}
    if wake_reason == "gap_rebound_scan":
        return [i for i in items
                if lookup.get(str(i.get("symbol") or ""), None) is not True]
    if wake_reason == "nxt_gap_scan":
        return [i for i in items
                if lookup.get(str(i.get("symbol") or ""), None) is True]
    return items

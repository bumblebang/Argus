"""갭반등 급하락 풀 — 스윙·day_pool 과 분리, 15:15 전용 refresh.

거래대금 상위 liquidity_top → 당일 등락률 하락 순 decline_top. security_filter 통과분만
yaml 저장. 뇌 15:20(gap_rebound_scan) 직전에 풀을 맞춘다(주문 여유 ~5분).
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

import yaml

from .config import ROOT
from .day_pool import merge_swing_and_day
from .datasources.nxt_universe import nxt_supported_for
from .datasources.stock_info import load_info_cache
from .logging_setup import get_logger
from .security_filter import filter_candidates

_GAP_SOURCE = "gap_rebound"

log = get_logger("src.gap_decline_pool")

OUT = ROOT / "data" / "gap_decline_pool.yaml"
_DEFAULT_LIQ_TOP = 300
_DEFAULT_DECLINE_TOP = 100
_DEFAULT_STRATEGY = "rsi_reversion"


def gap_decline_pool_cfg(cfg) -> dict:
    raw = cfg.raw if hasattr(cfg, "raw") else (cfg or {})
    return (raw.get("gap_decline_pool") or {}) if isinstance(raw, dict) else {}


def load_gap_decline_pool(path: Path | None = None) -> dict:
    """{market: [item,...]}. 없거나 깨지면 {}."""
    p = path or OUT
    try:
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as e:
        log.warning("gap_decline_pool 로드 실패(빈 dict): %s", e)
    return {}


def merge_all_pools(swing: dict | None, day: dict | None,
                    gap_decline: dict | None) -> dict:
    """swing ∪ day ∪ gap_decline. 앞 풀에 있으면 중복 생략."""
    out = merge_swing_and_day(swing, day)
    for market, lst in (gap_decline or {}).items():
        m = str(market).upper()
        bucket = out.setdefault(m, [])
        have = {it.get("symbol") for it in bucket}
        for it in (lst or []):
            if not isinstance(it, dict) or not it.get("symbol"):
                continue
            sym = it["symbol"]
            if sym in have:
                continue
            row = dict(it)
            row["pool"] = "gap_decline"
            row.setdefault("source", _GAP_SOURCE)
            bucket.append(row)
            have.add(sym)
    return out


def _atomic_write(mapping: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(mapping, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def _rows_from_discovery(fetch_top: Callable[..., list], market: str, *,
                         liq_top: int) -> list[dict]:
    """discovery.top_by_trading_value → fluctuation 내림(하락 우선) 정렬."""
    rows = fetch_top(market, count=liq_top, pool=liq_top)
    for r in rows:
        r["fluctuation"] = float(r.get("fluctuation") or 0)
    rows.sort(key=lambda x: x["fluctuation"])
    return rows


def refresh_gap_decline_pool(fetch_top: Callable[..., list], cfg, market: str = "KR",
                             *, now_fn=time.time, path: Path | None = None) -> dict | None:
    """거래대금 top → 하락률 정렬 top N. 실패·0건이면 기존 파일 유지(None)."""
    gcfg = gap_decline_pool_cfg(cfg)
    if not gcfg.get("enabled", True):
        return None
    markets = [str(m).upper() for m in (gcfg.get("markets") or ["KR"])]
    if market not in markets:
        return None
    liq_top = max(50, min(500, int(gcfg.get("liquidity_top", _DEFAULT_LIQ_TOP))))
    decline_top = max(10, min(200, int(gcfg.get("decline_top", _DEFAULT_DECLINE_TOP))))
    strategy = str(gcfg.get("strategy", _DEFAULT_STRATEGY))

    try:
        ranked = _rows_from_discovery(fetch_top, market, liq_top=liq_top)
    except Exception as e:
        log.warning("[%s] gap_decline_pool 발굴 실패: %s", market, e)
        return None
    ranked = ranked[:decline_top]
    if not ranked:
        log.warning("[%s] gap_decline_pool 랭킹 0 — 기존 파일 유지.", market)
        return None

    cands = [(market, r["symbol"], r.get("name") or r["symbol"]) for r in ranked]
    cands = filter_candidates(cands)
    keep = {sym for _m, sym, _n in cands}
    info_cache = load_info_cache()
    now = now_fn()
    items = []
    for i, r in enumerate(ranked):
        if r["symbol"] not in keep:
            continue
        fl = r.get("fluctuation")
        sym = r["symbol"]
        items.append({
            "symbol": sym,
            "name": r.get("name") or sym,
            "strategy": strategy,
            "layer": "gap_decline",
            "pool": "gap_decline",
            "source": _GAP_SOURCE,
            "rank": i + 1,
            "fluctuation": fl,
            "decline_pct": fl,
            "trading_amount": r.get("trading_value"),
            "nxt_supported": nxt_supported_for(sym, info_cache),
            "added_at": now,
        })
    if not items:
        log.warning("[%s] gap_decline_pool 필터 후 0 — 기존 유지.", market)
        return None

    out_path = path or OUT
    existing = load_gap_decline_pool(out_path)
    existing[market] = items
    _atomic_write(existing, out_path)
    log.info("[%s] gap_decline_pool %d종목 (liq=%d decline=%d) -> %s",
             market, len(items), liq_top, decline_top, out_path)
    return existing


def refresh_all_gap_decline_pools(fetch_top: Callable[..., list], cfg, *,
                                  now_fn=time.time, path: Path | None = None) -> dict | None:
    gcfg = gap_decline_pool_cfg(cfg)
    markets = [str(m).upper() for m in (gcfg.get("markets") or ["KR"])]
    last = None
    for m in markets:
        got = refresh_gap_decline_pool(fetch_top, cfg, m, now_fn=now_fn, path=path)
        if got is not None:
            last = got
    return last

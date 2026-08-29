"""갭반등 급하락 풀 — 스윙·day_pool 과 분리, 15:15 전용 refresh.

거래대금 상위 liquidity_top → 당일 등락률 하락 순 decline_top. security_filter 통과분만
yaml 저장. 뇌 15:20(gap_rebound_scan) 직전에 풀을 맞춘다(주문 여유 ~5분).
pool_date(거래일) 가 오늘이 아니면 merge·close_scan 판정에서 제외(전일 풀 잔존 방지).
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
from .market_hours import trading_date
from .security_filter import filter_candidates

_GAP_SOURCE = "gap_rebound"
_META_KEY = "_meta"

log = get_logger("src.gap_decline_pool")

OUT = ROOT / "data" / "gap_decline_pool.yaml"
_DEFAULT_LIQ_TOP = 300
_DEFAULT_DECLINE_TOP = 100
_DEFAULT_STRATEGY = "rsi_reversion"


def gap_decline_pool_cfg(cfg) -> dict:
    raw = cfg.raw if hasattr(cfg, "raw") else (cfg or {})
    return (raw.get("gap_decline_pool") or {}) if isinstance(raw, dict) else {}


def load_gap_decline_pool(path: Path | None = None) -> dict:
    """{market: [item,...], _meta?: {...}}. 없거나 깨지면 {}."""
    p = path or OUT
    try:
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as e:
        log.warning("gap_decline_pool 로드 실패(빈 dict): %s", e)
    return {}


def gap_pool_date(data: dict | None, market: str = "KR") -> str | None:
    """저장된 갭 풀 거래일(ISO). _meta·항목 pool_date 순."""
    if not isinstance(data, dict):
        return None
    meta = data.get(_META_KEY) or {}
    if isinstance(meta, dict) and meta.get("pool_date"):
        return str(meta["pool_date"])
    lst = data.get(market) or data.get(str(market).upper()) or []
    if lst and isinstance(lst[0], dict) and lst[0].get("pool_date"):
        return str(lst[0]["pool_date"])
    return None


def is_gap_pool_fresh(data: dict | None, market: str = "KR", *,
                      now_fn=time.time) -> bool:
    """pool_date 가 해당 시장 오늘 거래일과 같을 때만 True."""
    pd = gap_pool_date(data, market)
    if not pd:
        return False
    try:
        today = trading_date(market, now_fn())
    except Exception:
        return False
    return pd == today


def fresh_gap_symbols(data: dict | None, market: str = "KR", *,
                      now_fn=time.time) -> dict[str, str]:
    """오늘 pool_date 갭풀 symbol→pool_date. 신선하지 않으면 {}."""
    if not is_gap_pool_fresh(data, market, now_fn=now_fn):
        return {}
    pd = gap_pool_date(data, market)
    if not pd:
        return {}
    m = str(market).upper()
    lst = (data or {}).get(m) or []
    out: dict[str, str] = {}
    for it in lst:
        if isinstance(it, dict) and it.get("symbol"):
            out[str(it["symbol"])] = pd
    return out


def merge_all_pools(swing: dict | None, day: dict | None,
                    gap_decline: dict | None, *,
                    gap_market: str = "KR",
                    now_fn=time.time) -> dict:
    """swing ∪ day ∪ gap_decline(오늘 pool_date 만). 앞 풀에 있으면 중복 생략."""
    out = merge_swing_and_day(swing, day)
    gap = gap_decline if is_gap_pool_fresh(gap_decline, gap_market, now_fn=now_fn) else {}
    for market, lst in (gap or {}).items():
        if str(market).startswith("_"):
            continue
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
    """거래대금 top → 하락률 정렬 top N. 실패·0건·라이브 부족이면 기존 파일 유지(None)."""
    gcfg = gap_decline_pool_cfg(cfg)
    if not gcfg.get("enabled", True):
        return None
    markets = [str(m).upper() for m in (gcfg.get("markets") or ["KR"])]
    if market not in markets:
        return None
    liq_top = max(50, min(500, int(gcfg.get("liquidity_top", _DEFAULT_LIQ_TOP))))
    decline_top = max(10, min(200, int(gcfg.get("decline_top", _DEFAULT_DECLINE_TOP))))
    strategy = str(gcfg.get("strategy", _DEFAULT_STRATEGY))
    pool_date = trading_date(market, now_fn())

    try:
        ranked = _rows_from_discovery(fetch_top, market, liq_top=liq_top)
    except Exception as e:
        log.warning("[%s] gap_decline_pool 발굴 실패: %s", market, e)
        return None
    live_n = len([r for r in ranked if float(r.get("trading_value") or 0) > 0])
    if live_n < decline_top:
        log.warning("[%s] gap_decline_pool 라이브 %d < decline_top=%d — 기존 파일 유지",
                    market, live_n, decline_top)
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
            "pool_date": pool_date,
            "added_at": now,
        })
    if not items:
        log.warning("[%s] gap_decline_pool 필터 후 0 — 기존 유지.", market)
        return None

    out_path = path or OUT
    existing = load_gap_decline_pool(out_path)
    existing[market] = items
    existing[_META_KEY] = {
        "pool_date": pool_date,
        "refreshed_at": now,
        "market": market,
    }
    _atomic_write(existing, out_path)
    log.info("[%s] gap_decline_pool %d종목 pool_date=%s (liq=%d decline=%d) -> %s",
             market, len(items), pool_date, liq_top, decline_top, out_path)
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

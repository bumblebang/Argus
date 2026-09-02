"""데이트레 풀 — 스윙 유니버스(universe.yaml)와 분리된 토스 거래대금 랭킹.

뇌는 이 풀의 종목에만 horizon=day BUY 를 내고, 진입/청산 타이밍은 기존 armed 코드가
잡는다(백테스트 없는 새 필터로 코드 단독 arm 하지 않음). 각성 직전에 리프레시.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

import yaml

from .config import ROOT
from .logging_setup import get_logger
from .security_filter import filter_candidates

log = get_logger("src.day_pool")

OUT = ROOT / "data" / "day_pool.yaml"
_DEFAULT_TYPE = "MARKET_TRADING_AMOUNT"
_DEFAULT_LIVE = "realtime"
_DEFAULT_FALLBACK = "1d"
_DEFAULT_COUNT = 50
_DEFAULT_STRATEGY = "volatility_breakout"


def day_pool_cfg(cfg) -> dict:
    raw = cfg.raw if hasattr(cfg, "raw") else (cfg or {})
    return (raw.get("day_pool") or {}) if isinstance(raw, dict) else {}


def load_day_pool(path: Path | None = None) -> dict:
    """{market: [item,...]}. 없거나 깨지면 {}."""
    p = path or OUT
    try:
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError, yaml.YAMLError) as e:
        log.warning("day_pool 로드 실패(빈 dict): %s", e)
    return {}


def merge_swing_and_day(swing: dict | None, day: dict | None) -> dict:
    """감시·뇌 후보용 합집합. 겹치면 swing 을 남기고 day 로 중복 넣지 않는다."""
    out: dict[str, list] = {}
    for market, lst in (swing or {}).items():
        tagged = []
        for it in (lst or []):
            if not isinstance(it, dict) or not it.get("symbol"):
                continue
            row = dict(it)
            row.setdefault("pool", "swing")
            tagged.append(row)
        out[str(market).upper()] = tagged
    for market, lst in (day or {}).items():
        m = str(market).upper()
        bucket = out.setdefault(m, [])
        have = {it.get("symbol") for it in bucket}
        for it in (lst or []):
            if not isinstance(it, dict) or not it.get("symbol"):
                continue
            if it["symbol"] in have:
                continue
            row = dict(it)
            row["pool"] = "day"
            bucket.append(row)
            have.add(it["symbol"])
    return out


def _atomic_write(mapping: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(mapping, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def _rows_from_rankings(payload: dict | None) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    rows = payload.get("rankings")
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("symbol"):
            continue
        amt = r.get("tradingAmount")
        try:
            amt_f = float(amt) if amt is not None else 0.0
        except (TypeError, ValueError):
            amt_f = 0.0
        out.append({
            "symbol": str(r["symbol"]),
            "rank": r.get("rank"),
            "trading_amount": amt_f,
            "name": r.get("name") or r["symbol"],
        })
    return out


def refresh_day_pool(fetch_rankings: Callable[..., dict], cfg, market: str = "KR",
                     *, now_fn=time.time, path: Path | None = None) -> dict | None:
    """토스 거래대금 랭킹으로 day_pool.yaml 의 그 시장을 교체.

    fetch_rankings(rank_type, market_country, duration, count) -> API payload.
    라이브(realtime)가 비면 duration 폴백(1d, 전일 대금). 실패·0건이면 기존 파일 유지(None).
    """
    dcfg = day_pool_cfg(cfg)
    if not dcfg.get("enabled", True):
        return None
    markets = [str(m).upper() for m in (dcfg.get("markets") or ["KR"])]
    if market not in markets:
        return None
    count = max(1, min(100, int(dcfg.get("count", _DEFAULT_COUNT))))
    rank_type = str(dcfg.get("type", _DEFAULT_TYPE))
    live_dur = str(dcfg.get("duration_live", _DEFAULT_LIVE))
    fb_dur = str(dcfg.get("duration_fallback", _DEFAULT_FALLBACK))
    strategy = str(dcfg.get("strategy", _DEFAULT_STRATEGY))

    try:
        live = _rows_from_rankings(
            fetch_rankings(rank_type, market, live_dur, count))
    except Exception as e:
        log.warning("[%s] day_pool 라이브 랭킹 실패: %s", market, e)
        live = []
    rows = list(live)
    if not rows or all(r.get("trading_amount", 0) <= 0 for r in rows):
        try:
            fb = _rows_from_rankings(
                fetch_rankings(rank_type, market, fb_dur, count))
        except Exception as e:
            log.warning("[%s] day_pool 폴백 랭킹 실패: %s", market, e)
            fb = []
        seen = {r["symbol"] for r in rows}
        for r in fb:
            if r["symbol"] not in seen:
                rows.append(r)
                seen.add(r["symbol"])
        if fb:
            log.info("[%s] day_pool 라이브 %d건 — %s 폴백으로 %d건",
                     market, len(live), fb_dur, len(rows))
    rows = rows[:count]
    if not rows:
        log.warning("[%s] day_pool 랭킹 0 — 기존 파일 유지.", market)
        return None

    cands = [(market, r["symbol"], r.get("name") or r["symbol"]) for r in rows]
    cands = filter_candidates(cands)
    keep = {sym for _m, sym, _n in cands}
    now = now_fn()
    items = []
    for r in rows:
        if r["symbol"] not in keep:
            continue
        items.append({
            "symbol": r["symbol"],
            "name": r.get("name") or r["symbol"],
            "strategy": strategy,
            "layer": "day",
            "pool": "day",
            "rank": r.get("rank"),
            "trading_amount": r.get("trading_amount"),
            "added_at": now,
        })
    if not items:
        log.warning("[%s] day_pool 필터 후 0 — 기존 유지.", market)
        return None

    out_path = path or OUT
    existing = load_day_pool(out_path)
    existing[market] = items
    _atomic_write(existing, out_path)
    log.info("[%s] day_pool %d종목 (type=%s) -> %s",
             market, len(items), rank_type, out_path)
    return existing


def refresh_all_day_pools(fetch_rankings: Callable[..., dict], cfg, *,
                          now_fn=time.time, path: Path | None = None) -> dict | None:
    """config day_pool.markets 전 시장 갱신. 하나라도 성공하면 그 시점 파일 dict."""
    dcfg = day_pool_cfg(cfg)
    markets = [str(m).upper() for m in (dcfg.get("markets") or ["KR"])]
    last = None
    for m in markets:
        got = refresh_day_pool(fetch_rankings, cfg, m, now_fn=now_fn, path=path)
        if got is not None:
            last = got
    return last

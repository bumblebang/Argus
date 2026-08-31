"""룰 기반 종목 스크리너.

베이스 유니버스(후보군) -> 하드 필터(가격/거래대금/관리종목 제외) ->
전략별 랭킹(변동성/모멘텀/유동성) -> 전략별 상위 N 종목 선정.

순수 함수 중심이라 API 없이도 테스트 가능: fetch_candles 콜백을 주입한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pandas as pd

from .indicators import sma
from .logging_setup import get_logger

log = get_logger("screener")

# 랭킹 기준 -> Metrics 속성명 (모두 내림차순 정렬)
RANK_KEYS = {
    "volatility": "volatility",
    "momentum": "momentum",
    "trend": "trend_ratio",
    "liquidity": "avg_turnover",
}


@dataclass
class Metrics:
    symbol: str
    market: str
    name: str
    last_price: float
    avg_turnover: float   # 평균 거래대금 (close*volume)
    volatility: float     # 일간수익률 표준편차
    momentum: float       # lookback 구간 수익률
    trend_ratio: float    # 현재가 / SMA20


def compute_metrics(df: pd.DataFrame, symbol: str, market: str, name: str) -> Metrics | None:
    if df is None or len(df) < 20 or "close" not in df:
        return None
    close = df["close"].astype(float)
    vol = df["volume"].astype(float) if "volume" in df else pd.Series([0.0] * len(df))
    rets = close.pct_change().dropna()
    if rets.empty or close.iloc[-1] <= 0:
        return None
    sma20 = sma(close, 20).iloc[-1]
    return Metrics(
        symbol=symbol,
        market=market,
        name=name,
        last_price=float(close.iloc[-1]),
        avg_turnover=float((close * vol).tail(20).mean()),
        volatility=float(rets.tail(20).std()),
        momentum=float(close.iloc[-1] / close.iloc[0] - 1.0),
        trend_ratio=float(close.iloc[-1] / sma20) if sma20 else 0.0,
    )


def passes_filters(m: Metrics, criteria: dict) -> bool:
    market = m.market
    def _get(key, default):
        v = criteria.get(key, default)
        return v.get(market, default) if isinstance(v, dict) else v

    if m.last_price < _get("min_price", 0):
        return False
    if m.last_price > _get("max_price", float("inf")):
        return False
    if m.avg_turnover < _get("min_avg_turnover", 0):
        return False
    return True


def screen(
    candidates: list[tuple[str, str, str]],          # (market, symbol, name)
    fetch_candles: Callable[[str, str], pd.DataFrame],
    criteria: dict,
    exclude_fn: Callable[[str, str], bool] | None = None,
) -> dict[str, list[dict]]:
    """전략별로 선정된 종목을 시장별로 묶어 반환.

    반환: {"KR": [{symbol,name,strategy}, ...], "US": [...]}
    """
    # 1) 후보별 지표 계산 + 하드 필터
    passed: list[Metrics] = []
    for market, symbol, name in candidates:
        if exclude_fn and exclude_fn(symbol, market):
            log.info("[%s] 제외(관리/투자유의)", symbol)
            continue
        try:
            df = fetch_candles(symbol, market)
        except Exception as e:
            log.warning("[%s] 캔들 조회 실패: %s", symbol, e)
            continue
        m = compute_metrics(df, symbol, market, name)
        if m and passes_filters(m, criteria):
            passed.append(m)
    log.info("필터 통과: %d / %d", len(passed), len(candidates))

    # 2) 전략별 랭킹 -> 상위 N. 한 종목은 한 전략에만 배정(전략 순서 우선).
    picks_cfg: dict = criteria.get("picks", {})
    result: dict[str, list[dict]] = {}
    assigned: set[tuple[str, str]] = set()  # (market, symbol)

    for strat_name, cfg in picks_cfg.items():
        count = int(cfg.get("count", 5))
        rank_by = cfg.get("rank_by", "liquidity")
        attr = RANK_KEYS.get(rank_by, "avg_turnover")
        ranked = sorted(
            (m for m in passed if (m.market, m.symbol) not in assigned),
            key=lambda m: getattr(m, attr), reverse=True,
        )
        for m in ranked[:count]:
            assigned.add((m.market, m.symbol))
            result.setdefault(m.market, []).append(
                {"symbol": m.symbol, "name": m.name, "strategy": strat_name}
            )
            log.info("선정 [%s] %s (%s, %s=%.4g)", strat_name, m.symbol, rank_by,
                     rank_by, getattr(m, attr))
    return result


def liquidity_core_pick(
    candidates: list[tuple[str, str, str]],
    fetch_candles: Callable[[str, str], pd.DataFrame],
    criteria: dict,
    *,
    limit: int = 100,
    default_strategy: str = "ma_crossover",
    rank_by: str = "avg_turnover",
    discovery_tv: dict[str, float] | None = None,
    exclude_fn: Callable[[str, str], bool] | None = None,
) -> dict[str, list[dict]]:
    """거래대금 발굴 풀 → 하드필터 → 유동성 순 상위 limit (관심권 코어).

    rank_by: avg_turnover(20일 평균 거래대금, 기본) | discovery_tv(발굴 시점 거래대금).
    """
    passed: list[Metrics] = []
    for market, symbol, name in candidates:
        if exclude_fn and exclude_fn(symbol, market):
            log.info("[%s] 제외(관리/투자유의)", symbol)
            continue
        try:
            df = fetch_candles(symbol, market)
        except Exception as e:
            log.warning("[%s] 캔들 조회 실패: %s", symbol, e)
            continue
        m = compute_metrics(df, symbol, market, name)
        if m and passes_filters(m, criteria):
            passed.append(m)
    log.info("liquidity_core 필터 통과: %d / %d", len(passed), len(candidates))

    tv = discovery_tv or {}

    def _rank_key(m: Metrics) -> float:
        if rank_by == "discovery_tv":
            return float(tv.get(m.symbol, 0.0))
        return m.avg_turnover

    by_market: dict[str, list[Metrics]] = {}
    for m in passed:
        by_market.setdefault(m.market, []).append(m)

    result: dict[str, list[dict]] = {}
    for market, rows in by_market.items():
        ranked = sorted(rows, key=_rank_key, reverse=True)
        picked = []
        for m in ranked[: max(0, limit)]:
            picked.append({
                "symbol": m.symbol,
                "name": m.name,
                "strategy": default_strategy,
            })
            log.info("liquidity_core [%s] %s (%s=%.4g)", market, m.symbol,
                     rank_by, _rank_key(m))
        if picked:
            result[market] = picked
    return result


def _price_bounds(criteria: dict, market: str) -> tuple[float, float]:
    def _get(key, default):
        v = criteria.get(key, default)
        return v.get(market, default) if isinstance(v, dict) else v

    lo = float(_get("min_price", 0) or 0)
    hi = float(_get("max_price", float("inf")) or float("inf"))
    return lo, hi


def liquidity_core_pick_light(
    candidates: list[tuple[str, str, str]],
    criteria: dict,
    *,
    limit: int = 100,
    default_strategy: str = "ma_crossover",
    discovery_tv: dict[str, float] | None = None,
    discovery_prices: dict[str, float] | None = None,
    day_tag_top: int = 0,
    day_strategy: str = "volatility_breakout",
) -> dict[str, list[dict]]:
    """거래대금 발굴 풀 → 가격 스칼ar 필터 → TV 순 top N (캔들 fetch 없음).

    day_tag_top > 0 이면 TV 순 상위 N종에 pool=day·day_strategy 태그.
    """
    tv = discovery_tv or {}
    prices = discovery_prices or {}
    by_market: dict[str, list[tuple[str, str, str, float]]] = {}

    for market, symbol, name in candidates:
        lo, hi = _price_bounds(criteria, market)
        px = float(prices.get(symbol) or 0)
        if px <= 0:
            log.info("[%s] light 제외 %s (price missing)", market, symbol)
            continue
        if px < lo or px > hi:
            log.info("[%s] light 제외 %s (price=%.4g)", market, symbol, px)
            continue
        score = float(tv.get(symbol) or 0)
        by_market.setdefault(market, []).append((market, symbol, name, score))

    result: dict[str, list[dict]] = {}
    for market, rows in by_market.items():
        ranked = sorted(rows, key=lambda r: r[3], reverse=True)
        picked: list[dict] = []
        for i, (_m, symbol, name, score) in enumerate(ranked[: max(0, limit)]):
            row: dict = {
                "symbol": symbol,
                "name": name,
                "strategy": default_strategy,
            }
            if day_tag_top > 0 and i < day_tag_top:
                row["pool"] = "day"
                row["strategy"] = day_strategy
                row["layer"] = "day"
            picked.append(row)
            log.info("liquidity_core_light [%s] %s (discovery_tv=%.4g)", market, symbol, score)
        if picked:
            result[market] = picked
    log.info("liquidity_core_light 선정: %s",
             {m: len(v) for m, v in result.items()})
    return result


def load_candidates(data_dir: str | Path, markets: list[str]) -> list[tuple[str, str, str]]:
    """data/base_universe_{MARKET}.txt 에서 후보군 로드."""
    out: list[tuple[str, str, str]] = []
    for market in markets:
        path = Path(data_dir) / f"base_universe_{market}.txt"
        if not path.exists():
            log.warning("후보군 파일 없음: %s", path)
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",", 1)]
            symbol = parts[0]
            name = parts[1] if len(parts) > 1 else symbol
            out.append((market, symbol, name))
    return out

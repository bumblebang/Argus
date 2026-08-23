"""gem 스크린 — 역추세·저평가 발굴 축(코어 유니버스에 gem 버킷 추가).

메인 발굴(거래대금 상위)은 추세 추격 종목만 올려 회귀 전략이 쓸 종목(박스권·낙폭
과대)이 유니버스에 안 들어온다. gem 스크린은 이런 종목을 별도 깔때기로 골라
코어에 `source: "gem"` 태그로 붙인다 → Athena 도시에 → 강세면 스윙 진입.

깔때기 순서는 비용 순이다(싼 필터 먼저, 비싼 백테스트는 마지막 소수에만):
  1) 풀 발굴(discover_fn) — 기존 유니버스 심볼 제외
  2) 유동성 플로어(compute_metrics: avg_turnover·min_price)
  3) 프로파일 필터(A 박스형 또는 B 낙폭과대 반등형)
  4) edge 백테스트(rsi_reversion·bollinger_reversion vs buy_and_hold) — 통과분에만

심볼 단위 실패·예외는 스킵한다(스크린 전체가 죽지 않게).
"""
from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

from .logging_setup import get_logger
from .screener import compute_metrics, passes_filters
from .backtest import backtest, buy_and_hold
from .strategies import build_strategy

log = get_logger("src.gem_screen")

# edge 백테스트에 돌릴 회귀 전략들(리서치 매트릭스 실증: 하락·박스 국면에 강함).
_GEM_STRATEGIES = ("rsi_reversion", "bollinger_reversion")


def _gems_cfg(cfg, market: str) -> dict:
    """screener.gems 블록에서 이 시장에 쓸 파라미터를 뽑아 기본값과 합친다."""
    g = ((cfg.raw.get("screener", {}) or {}).get("gems", {}) or {})

    def _m(key, default):
        v = g.get(key, default)
        return v.get(market, default) if isinstance(v, dict) else v

    return {
        "enabled": bool(g.get("enabled", True)),
        "count": int(_m("count_per_market", {"KR": 5, "US": 3}.get(market, 3))),
        "pool": int(g.get("pool", 150)),
        "min_avg_turnover": float(_m("min_avg_turnover",
                                     {"KR": 1e9, "US": 2e7}.get(market, 2e7))),
        "drawdown_range": list(g.get("drawdown_range", [-0.60, -0.25])),
        "knife_20d_min": float(g.get("knife_20d_min", -0.10)),
        "box_momentum_max": float(g.get("box_momentum_max", 0.10)),
        "min_trades": int(g.get("min_trades", 3)),
    }


def _profile(df: pd.DataFrame, gcfg: dict) -> str | None:
    """캔들이 A(박스 수확형)/B(낙폭과대 반등형) 프로파일 중 하나면 그 이름, 아니면 None.

      A 박스형: 60일 모멘텀 절대값 ≤ box_momentum_max (방향성 없는 횡보).
      B 반등형: 52주 고점 대비 낙폭이 drawdown_range 안 그리고 자유낙하 아님
               (최근 20일 수익률 > knife_20d_min 그리고
                (20일 변동성 < 60일 변동성 또는 종가 > SMA20)).
    """
    close = df["close"].astype(float)
    if len(close) < 60:
        return None
    last = float(close.iloc[-1])
    if last <= 0:
        return None

    # A 박스형: 60일 모멘텀(60봉 전 대비 수익률) 절대값이 작다 = 횡보.
    mom_60 = last / float(close.iloc[-60]) - 1.0
    if abs(mom_60) <= gcfg["box_momentum_max"]:
        return "box"

    # B 반등형: 52주 고점 대비 낙폭이 허용 구간이고 자유낙하가 아님.
    peak = float(close.max())
    drawdown = last / peak - 1.0 if peak > 0 else 0.0
    lo, hi = gcfg["drawdown_range"]
    if lo <= drawdown <= hi:
        ret_20 = last / float(close.iloc[-20]) - 1.0
        if ret_20 > gcfg["knife_20d_min"]:
            rets = close.pct_change().dropna()
            vol_20 = float(rets.tail(20).std())
            vol_60 = float(rets.tail(60).std())
            sma_20 = float(close.tail(20).mean())
            if vol_20 < vol_60 or last > sma_20:
                return "rebound"
    return None


def _edge(df: pd.DataFrame, min_trades: int) -> tuple[str, float] | None:
    """회귀 전략들을 백테스트해 edge(전략수익률 − buy_and_hold)가 가장 큰 것을 고른다.

    edge > 0 이고 거래수 ≥ min_trades 인 전략만 후보. 전부 탈락이면 None.
    반환: (전략명, edge). LLM 호출 0(순수 코드 백테스트).
    """
    bh = buy_and_hold(df)
    best: tuple[str, float] | None = None
    for name in _GEM_STRATEGIES:
        try:
            res = backtest(build_strategy(name, {}), df)
        except Exception as e:
            log.debug("[gem] %s 백테스트 실패(스킵): %s", name, e)
            continue
        if res.n_trades < min_trades:
            continue
        edge = res.return_pct - bh
        if edge > 0 and (best is None or edge > best[1]):
            best = (name, edge)
    return best


def gem_candidates(cfg, market: str, fetch: Callable[[str, str], pd.DataFrame], *,
                   discover_fn: Callable[[object, int, bool], list] | None = None,
                   exclude: set[str] | None = None,
                   dry: bool = False) -> list[dict]:
    """gem 스크린 실행 → [{symbol, name, strategy, source: "gem"}] (gem_score 내림차순 상위 count).

    discover_fn(cfg, pool, dry) 주입(기본: universe_roll.discover_kr/us). exclude 에 든
    심볼(현재 유니버스)은 발굴 단계에서 제외한다. sector 는 여기서 채우지 않는다
    (universe_roll._annotate_sectors 가 나중에 채운다).

    실패·예외는 심볼 단위로 스킵한다(스크린 전체가 죽지 않게).
    """
    gcfg = _gems_cfg(cfg, market)
    if discover_fn is None:
        from . import universe_roll
        discover_fn = universe_roll._DISCOVER.get(market)
        if discover_fn is None:
            log.warning("[gem][%s] 미지원 시장 — 스킵", market)
            return []
    exclude = exclude or set()

    # 1) 풀 발굴 — 기존 유니버스 심볼 제외.
    try:
        raw = discover_fn(cfg, gcfg["pool"], dry)
    except Exception as e:
        log.warning("[gem][%s] 풀 발굴 실패(스킵): %s", market, e)
        return []
    cands = [(m, s, n) for (m, s, n) in raw if s not in exclude]
    if not cands:
        return []

    criteria = cfg.raw.get("screener", {}) or {}
    scored: list[tuple[float, dict]] = []
    for m, sym, name in cands:
        try:
            df = fetch(sym, m)
        except Exception as e:
            log.debug("[gem][%s] 히스토리 실패(스킵): %s", sym, e)
            continue
        if df is None or len(df) < 60:
            continue

        # 2) 유동성 플로어 — avg_turnover(gems 값으로 오버라이드) + min_price(기존 screener).
        metric = compute_metrics(df, sym, m, name)
        if metric is None:
            continue
        gem_criteria = dict(criteria)
        gem_criteria["min_avg_turnover"] = gcfg["min_avg_turnover"]
        if not passes_filters(metric, gem_criteria):
            continue

        # 3) 프로파일 필터(A 박스형 또는 B 반등형).
        if _profile(df, gcfg) is None:
            continue

        # 4) edge 백테스트(통과분에만) — edge > 0 이고 거래수 ≥ min_trades.
        picked = _edge(df, gcfg["min_trades"])
        if picked is None:
            continue
        strategy, edge = picked
        scored.append((edge, {"symbol": sym, "name": name,
                              "strategy": strategy, "source": "gem"}))

    # gem_score(=최대 edge) 내림차순 상위 count.
    scored.sort(key=lambda t: t[0], reverse=True)
    out = [item for _edge_val, item in scored[:gcfg["count"]]]
    log.info("[gem][%s] 후보 %d → 프로파일·edge 통과 %d → 상위 %d 선정",
             market, len(cands), len(scored), len(out))
    return out

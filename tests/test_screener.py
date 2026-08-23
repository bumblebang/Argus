import numpy as np
import pandas as pd

from src.screener import compute_metrics, passes_filters, screen


def _candles(trend=0.001, vol=0.02, base=100, volume=1_000_000, n=60, seed=1):
    rng = np.random.default_rng(seed)
    rets = rng.normal(trend, vol, n)
    close = base * np.cumprod(1 + rets)
    return pd.DataFrame({
        "open": np.concatenate([[close[0]], close[:-1]]),
        "high": close * 1.01, "low": close * 0.99, "close": close,
        "volume": [volume] * n,
    })


def test_compute_metrics_basic():
    m = compute_metrics(_candles(), "AAA", "KR", "테스트")
    assert m is not None
    assert m.last_price > 0
    assert m.avg_turnover > 0
    assert m.volatility > 0


def test_filters_reject_low_turnover():
    m = compute_metrics(_candles(volume=1), "AAA", "KR", "저거래")
    criteria = {"min_avg_turnover": {"KR": 1_000_000}, "min_price": 0, "max_price": 9e9}
    assert not passes_filters(m, criteria)


def test_screen_assigns_unique_strategy():
    # 변동성이 서로 다른 종목들 -> 전략별 상위 선정, 중복 배정 없음
    cands = [("KR", f"S{i}", f"name{i}") for i in range(6)]
    data = {
        "S0": _candles(vol=0.05, seed=10), "S1": _candles(vol=0.04, seed=11),
        "S2": _candles(vol=0.03, seed=12), "S3": _candles(vol=0.02, seed=13),
        "S4": _candles(vol=0.015, seed=14), "S5": _candles(vol=0.01, seed=15),
    }
    criteria = {
        "min_price": 0, "max_price": 9e9, "min_avg_turnover": 0,
        "picks": {
            "volatility_breakout": {"count": 2, "rank_by": "volatility"},
            "rsi_reversion": {"count": 2, "rank_by": "liquidity"},
        },
    }
    picks = screen(cands, lambda s, m: data[s], criteria)
    flat = [(p["symbol"], p["strategy"]) for p in picks.get("KR", [])]
    syms = [s for s, _ in flat]
    assert len(syms) == len(set(syms))          # 중복 배정 없음
    assert len(flat) == 4                         # 2 + 2

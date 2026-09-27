"""도시에 목표가 도달 가능성(ATR 배수) — 측정 헬퍼."""
import pandas as pd

from src.agents.dossier_reach import (atr_pct_from_bars, atr_pct_from_df, level_reach,
                                      reach_bucket)
from src.indicators import atr


def _flat_df(n=40, px=100.0, rng_pct=0.02):
    close = pd.Series([px] * n)
    return pd.DataFrame({"high": close * (1 + rng_pct / 2), "low": close * (1 - rng_pct / 2),
                         "close": close})


def test_atr_constant_range():
    df = _flat_df()
    a = atr(df["high"], df["low"], df["close"], 14)
    assert abs(float(a.iloc[-1]) - 2.0) < 1e-9
    assert abs(atr_pct_from_df(df) - 0.02) < 1e-9
    assert atr_pct_from_df(_flat_df(n=10)) is None       # 봉 부족


def test_atr_pct_from_bars_matches_df():
    bars = [(None, 101.0, 99.0, 100.0)] * 40
    assert abs(atr_pct_from_bars(bars) - 0.02) < 1e-9


def test_level_reach_multiples():
    r = level_reach(entry_low=98, entry_high=102, invalidation=96, target=110, atr_pct=0.02)
    assert r == {"atr_pct": 0.02, "tgt_atr": 5.0, "stop_atr": 2.0}
    assert level_reach(entry_low=98, entry_high=102, invalidation=96, target=110,
                       atr_pct=None) is None


def test_reach_bucket_edges():
    assert [reach_bucket(x) for x in (None, 2.9, 3.0, 4.0, 6.0)] == [
        "unknown", "<3", "3-4", "4-6", "6+"]

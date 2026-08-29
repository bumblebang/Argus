"""agents.features — 후보 피처 조립."""
import numpy as np
import pandas as pd

from src.agents.features import assemble, technical_summary


def _candles(n=300, base=100.0) -> list[dict]:
    rng = np.random.default_rng(7)
    close = base * np.cumprod(1 + rng.normal(0.0005, 0.015, n))
    df = pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=n),
        "open": close, "high": close * 1.01, "low": close * 0.99,
        "close": close, "volume": rng.integers(1e5, 1e6, n),
    })
    return df.to_dict(orient="records")


def test_technical_summary_long_horizon():
    t = technical_summary(pd.DataFrame(_candles()))
    assert {"pct_from_52w_high", "pct_from_52w_low", "ret_60d_pct",
            "vs_sma60_pct", "vs_sma120_pct"} <= set(t)
    assert technical_summary(pd.DataFrame(_candles(30))) == {}


def test_assemble_includes_long_horizon_fields():
    items = [{"symbol": "005930", "name": "삼성", "market": "KR"}]
    cands, _ = assemble(items, {}, lambda s, m: _candles(), enrich_strategy=False)
    assert len(cands) == 1
    c = cands[0]
    assert "pct_from_52w_high" in c and "ret_60d_pct" in c
    assert "vs_sma60_pct" in c and c.get("drawdown_lookback") == 60

"""갭반등 당일 형태 피처 — 백테스트·assemble·baserate 공용.

장중 15:20 대신 일봉/라이브가로 근사. 임계값은 gap_rebound_backtest prior 와 동기화.
"""
from __future__ import annotations

import pandas as pd

GAP_INTRADAY_FLOOR = -5.0
GAP_DOWN_DEEP_PCT = -2.0
CLOSE_LOC_LOW_MAX = 0.25
VOL_RATIO_SPIKE = 2.0


def day_close_loc(high: float, low: float, close: float) -> float | None:
    span = high - low
    if span <= 0:
        return None
    return (close - low) / span


def gap_shape_fields(df: pd.DataFrame | None, *, price: float | None = None) -> dict:
    """마지막 봉 기준 갭반등 형태 — gap_shape 서브객체 반환."""
    if df is None or len(df) < 2:
        return {}
    if not {"open", "close", "high", "low"}.issubset(df.columns):
        return {}
    row = df.iloc[-1]
    try:
        o = float(row["open"])
        h = float(row["high"])
        lo = float(row["low"])
        prev = float(df["close"].iloc[-2])
        px = float(price) if price is not None and price > 0 else float(row["close"])
    except (TypeError, ValueError):
        return {}
    if not o or not prev or not px:
        return {}

    gap_pct = (o / prev - 1) * 100
    intraday = (px / o - 1) * 100
    loc = day_close_loc(h, lo, px)
    vol_ratio = None
    if "volume" in df.columns and len(df) >= 20:
        vol = df["volume"].astype(float)
        ma = float(vol.tail(20).mean())
        last_v = float(vol.iloc[-1])
        if ma > 0:
            vol_ratio = round(last_v / ma, 2)

    shape: dict = {
        "gap_pct": round(gap_pct, 2),
        "intraday_after_open_pct": round(intraday - gap_pct, 2),
        "close_loc": round(loc, 2) if loc is not None else None,
        "gap_down_deep": bool(gap_pct <= GAP_DOWN_DEEP_PCT),
        "close_near_day_low": bool(loc is not None and loc <= CLOSE_LOC_LOW_MAX),
        "vol_ratio_20d": vol_ratio,
        "vol_spike": bool(vol_ratio is not None and vol_ratio >= VOL_RATIO_SPIKE),
    }
    return {"gap_shape": shape}


def gap_close_scan_mask(df: pd.DataFrame) -> pd.Series:
    """baserate 셋업: intraday<=-5% & 종가가 당일 레인지 하단."""
    if df is None or len(df) < 2:
        return pd.Series(dtype=bool)
    open_ = df["open"].astype(float)
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    intraday = close / open_ - 1
    span = (high - low).replace(0, pd.NA)
    close_loc = (close - low) / span
    fired = (intraday <= GAP_INTRADAY_FLOOR / 100) & (close_loc <= CLOSE_LOC_LOW_MAX)
    return fired.fillna(False)

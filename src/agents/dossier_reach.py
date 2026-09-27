"""도시에 목표가 도달 가능성 — 목표 거리를 ATR 배수로 잰다(측정 전용).

근거(2026-09-27, 강세 도시에 418건·90일, 스윙 20일 창, Wilder ATR14):
  목표 < 3 ATR : 기간 내 목표 도달 28% (독립 102종목)
  목표 3–4 ATR : 13% · 4–6 ATR : 2.4% · 6+ ATR : 0%
손절 거리(ATR)는 손익비가 ~2.3 으로 고정돼 목표 거리와 같이 움직일 뿐, 따로 쓸 신호가
아니었다(좁은 손절이 오히려 적중이 좋음) — 그래서 기준은 목표 하나만 둔다.

집행 판정(강세→중립 강등)은 PROTECTED(athena) 변경이라 eval 실험 등록 후 별도 PR.
여기는 측정 전용 — dossier_quality.reach_outcomes 가 일봉으로 소급 계산한다.
"""
from __future__ import annotations

import pandas as pd

from ..indicators import atr

DEFAULT_ATR_PERIOD = 14


def atr_pct_from_df(df: pd.DataFrame | None, period: int = DEFAULT_ATR_PERIOD) -> float | None:
    """일봉 df(high/low/close) → 마지막 ATR / 종가. 봉 부족·컬럼 없음이면 None."""
    if df is None or len(df) < period + 1:
        return None
    try:
        close = df["close"].astype(float)
        high = df["high"].astype(float) if "high" in df else close
        low = df["low"].astype(float) if "low" in df else close
        a = float(atr(high, low, close, period).iloc[-1])
        last = float(close.iloc[-1])
    except (KeyError, TypeError, ValueError):
        return None
    if not (a > 0 and last > 0):
        return None
    return a / last


def atr_pct_from_bars(bars: list[tuple], period: int = DEFAULT_ATR_PERIOD) -> float | None:
    """labels._load_ohlc 형식 [(dt, high, low, close), ...] → ATR%."""
    if len(bars) < period + 1:
        return None
    df = pd.DataFrame([{"high": h, "low": l, "close": c} for _, h, l, c in bars])
    return atr_pct_from_df(df, period)


def level_reach(*, entry_low, entry_high, invalidation, target,
                atr_pct: float | None) -> dict[str, float] | None:
    """진입존 중간값 기준 목표·무효화 거리를 ATR 배수로. 계산 불가면 None."""
    try:
        lo, hi = float(entry_low), float(entry_high)
        tgt, inv = float(target), float(invalidation)
    except (TypeError, ValueError):
        return None
    if not atr_pct or atr_pct <= 0 or lo <= 0 or hi <= 0:
        return None
    mid = (lo + hi) / 2
    return {
        "atr_pct": round(atr_pct, 5),
        "tgt_atr": round((tgt / mid - 1) / atr_pct, 2),
        "stop_atr": round((1 - inv / mid) / atr_pct, 2),
    }


def reach_bucket(tgt_atr: float | None) -> str:
    if tgt_atr is None:
        return "unknown"
    if tgt_atr < 3:
        return "<3"
    if tgt_atr < 4:
        return "3-4"
    if tgt_atr < 6:
        return "4-6"
    return "6+"

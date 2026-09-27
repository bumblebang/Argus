"""도시에 목표가 도달 가능성 — 목표 거리를 ATR 배수로 잰다(관찰 우선).

근거(2026-09-27, 강세 도시에 418건·90일, 스윙 20일 창):
  목표 < 4 ATR : 목표 먼저 71 / 무효화 먼저 75 / 미도달 158 → 기간 내 목표 도달 23%
  목표 ≥ 4 ATR : 목표 먼저  4 / 무효화 먼저 20 / 미도달  90 → 4%
손절 거리(ATR)는 손익비가 ~2.3 으로 고정돼 목표 거리와 같이 움직일 뿐, 따로 쓸 신호가
아니었다(좁은 손절이 오히려 적중이 좋음) — 그래서 기준은 목표 하나만 둔다.

mode:
  observe(기본) — evidence.reach 에 배수만 기록. 판정은 바꾸지 않는다.
  enforce       — 강세인데 tgt_atr > max_target_atr 면 중립 강등(sanitize 와 같은 방식).
  off           — 계산 안 함.
같은 종목 도시에가 48h 마다 갱신돼 라벨이 겹치므로 enforce 전환은 관찰 표본을 본 뒤에.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from ..indicators import atr

DEFAULT_MAX_TARGET_ATR = 4.0
DEFAULT_ATR_PERIOD = 14
_MODES = ("observe", "enforce", "off")


def reach_cfg(cfg) -> dict[str, Any]:
    """athena.reach 설정 + 기본값."""
    raw = getattr(cfg, "raw", cfg) if not isinstance(cfg, dict) else cfg
    if not isinstance(raw, dict):
        raw = {}
    block = (raw.get("athena") or {}).get("reach") or {}
    mode = str(block.get("mode", "observe")).lower()
    return {
        "mode": mode if mode in _MODES else "observe",
        "max_target_atr": float(block.get("max_target_atr", DEFAULT_MAX_TARGET_ATR)),
        "atr_period": int(block.get("atr_period", DEFAULT_ATR_PERIOD)),
    }


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


def apply_reach(d, reach: dict | None, rc: dict) -> tuple[Any, list[str]]:
    """강세 도시에에 도달 가능성 판정. (도시에, notes). observe 면 도시에 불변."""
    if reach is None or getattr(d, "stance", None) != "bullish":
        return d, []
    tgt_atr = reach.get("tgt_atr")
    if tgt_atr is None or tgt_atr <= rc["max_target_atr"]:
        return d, []
    msg = (f"목표가 {tgt_atr:.1f} ATR — 기간 내 도달 어려움"
           f"(> {rc['max_target_atr']:.1f} ATR)")
    if rc["mode"] == "enforce":
        return d.model_copy(update={"stance": "neutral"}), [msg + " → neutral 강등"]
    return d, [msg + " (관찰 모드: 판정 유지)"]

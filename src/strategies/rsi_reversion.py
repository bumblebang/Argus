"""전략 3: RSI 평균회귀.

  RSI < oversold  -> 과매도, 매수 (미보유 시)
  RSI > overbought -> 과매수, 매도 (보유 시)
"""
from __future__ import annotations

import pandas as pd

from .base import Strategy, Signal, Action, Position, ParamSpec
from ..indicators import rsi


class RSIReversion(Strategy):
    """RSI 평균회귀: RSI 과매도 시 매수, 과매수 시 매도. 횡보·박스권에 유리."""
    name = "rsi_reversion"
    horizon = "swing"
    PARAMS = (
        ParamSpec("period", 14, 2, 50, "int", "RSI 계산 기간"),
        ParamSpec("oversold", 30, 5, 49, "float", "과매도 임계(이하 매수)"),
        ParamSpec("overbought", 70, 51, 95, "float", "과매수 임계(이상 매도)"),
    )

    @property
    def min_candles(self) -> int:
        return int(self.params.get("period", 14)) + 2

    def decide(self, candles: pd.DataFrame, position: Position) -> Signal:
        period = int(self.params.get("period", 14))
        oversold = float(self.params.get("oversold", 30))
        overbought = float(self.params.get("overbought", 70))
        if len(candles) < period + 2:
            return self.hold("캔들 부족")

        close = candles["close"].astype(float)
        value = float(rsi(close, period).iloc[-1])

        if not position.is_open and value < oversold:
            return Signal(Action.BUY, f"과매도 매수 (RSI {value:.1f} < {oversold})")
        if position.is_open and value > overbought:
            return Signal(Action.SELL, f"과매수 매도 (RSI {value:.1f} > {overbought})")
        return self.hold(f"중립 (RSI {value:.1f})")

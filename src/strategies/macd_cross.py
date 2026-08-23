"""전략 6: MACD 크로스.

  MACD선이 시그널선 상향 돌파 -> 매수
  MACD선이 시그널선 하향 돌파 -> 매도
추세/모멘텀 전환을 EMA 로 부드럽게 포착. 단기~스윙(일/시간봉).
"""
from __future__ import annotations

import pandas as pd

from .base import Strategy, Signal, Action, Position, ParamSpec
from ..indicators import macd as macd_ind, crossed_up, crossed_down


class MACDCross(Strategy):
    """MACD 크로스: MACD선이 시그널선 상향 돌파 매수, 하향 매도. 모멘텀 전환. 단기 스윙."""
    name = "macd"
    horizon = "swing"
    PARAMS = (
        ParamSpec("fast", 12, 3, 40, "int", "단기 EMA 기간"),
        ParamSpec("slow", 26, 10, 100, "int", "장기 EMA 기간"),
        ParamSpec("signal", 9, 3, 30, "int", "시그널 EMA 기간"),
    )

    @classmethod
    def cross_check(cls, params: dict) -> tuple[dict, list[str]]:
        if params.get("fast", 0) >= params.get("slow", 0):
            fixed = max(3, int(params["slow"]) - 1)
            return {**params, "fast": fixed}, [
                f"fast {params['fast']} >= slow {params['slow']} → fast={fixed}"]
        return params, []

    @property
    def min_candles(self) -> int:
        return int(self.params.get("slow", 26)) + int(self.params.get("signal", 9)) + 2

    def decide(self, candles: pd.DataFrame, position: Position) -> Signal:
        fast = int(self.params.get("fast", 12))
        slow = int(self.params.get("slow", 26))
        sig = int(self.params.get("signal", 9))
        if len(candles) < slow + sig + 2:
            return self.hold("캔들 부족")
        close = candles["close"].astype(float)
        macd_line, signal_line, _ = macd_ind(close, fast, slow, sig)
        if not position.is_open and crossed_up(macd_line, signal_line):
            return Signal(Action.BUY, f"MACD 골든크로스 ({fast}/{slow}/{sig})")
        if position.is_open and crossed_down(macd_line, signal_line):
            return Signal(Action.SELL, f"MACD 데드크로스 ({fast}/{slow}/{sig})")
        return self.hold("크로스 없음")

"""전략 4: 돈치안 채널 돌파 (터틀 추세추종).

  종가 > 직전 entry_period 봉 최고가  -> 매수 (미보유)
  종가 < 직전 exit_period 봉 최저가    -> 매도 (보유)
강한 추세를 길게 타는 중장기 추세추종(주/일봉).
"""
from __future__ import annotations

import pandas as pd

from .base import Strategy, Signal, Action, Position, ParamSpec
from ..indicators import donchian_high, donchian_low


class DonchianBreakout(Strategy):
    """돈치안 채널 돌파(터틀): N봉 최고가 돌파 매수, M봉 최저가 이탈 매도. 중장기 추세추종."""
    name = "donchian_breakout"
    horizon = "position"
    PARAMS = (
        ParamSpec("entry_period", 20, 5, 120, "int", "돌파 기준 최고가 기간(진입)"),
        ParamSpec("exit_period", 10, 3, 60, "int", "이탈 기준 최저가 기간(청산)"),
    )

    @property
    def min_candles(self) -> int:
        return max(int(self.params.get("entry_period", 20)),
                   int(self.params.get("exit_period", 10))) + 2

    def decide(self, candles: pd.DataFrame, position: Position) -> Signal:
        ep = int(self.params.get("entry_period", 20))
        xp = int(self.params.get("exit_period", 10))
        if len(candles) < max(ep, xp) + 2:
            return self.hold("캔들 부족")
        high = candles["high"].astype(float)
        low = candles["low"].astype(float)
        price = float(candles["close"].astype(float).iloc[-1])
        # 당봉 제외(shift(1)) — 직전 N봉 채널을 당봉 종가가 돌파/이탈하는지.
        upper = donchian_high(high, ep).shift(1).iloc[-1]
        lower = donchian_low(low, xp).shift(1).iloc[-1]
        if not position.is_open and pd.notna(upper) and price > float(upper):
            return Signal(Action.BUY, f"돌파 매수 (종가 {price:,.2f} > {ep}봉 고점 {float(upper):,.2f})")
        if position.is_open and pd.notna(lower) and price < float(lower):
            return Signal(Action.SELL, f"이탈 매도 (종가 {price:,.2f} < {xp}봉 저점 {float(lower):,.2f})")
        return self.hold("채널 내")

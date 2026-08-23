"""전략 1: 변동성 돌파 (래리 윌리엄스).

  목표가 = 당일 시가 + (전일 고가 - 전일 저가) * k
  현재가 >= 목표가  -> 매수 (미보유 시)
  보유 중 익절/손절 도달 -> 매도
  (종가 청산은 runner 가 장 마감 직전에 처리)
"""
from __future__ import annotations

import pandas as pd

from .base import Strategy, Signal, Action, Position, ParamSpec


class VolatilityBreakout(Strategy):
    """변동성 돌파(래리 윌리엄스): 당일 시가 + 전일변동폭*k 돌파 시 매수. 추세·모멘텀 장."""
    name = "volatility_breakout"
    horizon = "day"
    PARAMS = (
        ParamSpec("k", 0.5, 0.0, 2.0, "float", "돌파 계수(전일 변동폭 * k)"),
        ParamSpec("target_profit_pct", 0.03, 0.005, 0.30, "float", "익절 비율"),
        ParamSpec("stop_loss_pct", 0.02, 0.005, 0.20, "float", "손절 비율"),
    )

    @property
    def min_candles(self) -> int:
        return 2

    def decide(self, candles: pd.DataFrame, position: Position) -> Signal:
        if len(candles) < 2:
            return self.hold("캔들 부족")

        prev = candles.iloc[-2]
        today = candles.iloc[-1]
        price = float(today["close"])  # 폴링 시점의 현재가(당일 캔들의 종가 필드)

        k = float(self.params.get("k", 0.5))
        rng = float(prev["high"]) - float(prev["low"])
        target = float(today["open"]) + rng * k

        if position.is_open:
            tp = float(self.params.get("target_profit_pct", 0.03))
            sl = float(self.params.get("stop_loss_pct", 0.02))
            change = (price - position.avg_price) / position.avg_price
            if change >= tp:
                return Signal(Action.SELL, f"익절 +{change:.2%}")
            if change <= -sl:
                return Signal(Action.SELL, f"손절 {change:.2%}")
            return self.hold(f"보유 중 ({change:+.2%})")

        if price >= target:
            return Signal(Action.BUY, f"돌파 매수 (목표 {target:,.2f} <= 현재 {price:,.2f})")
        return self.hold(f"목표가 미도달 ({price:,.2f} < {target:,.2f})")

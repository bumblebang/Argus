"""전략 8: 볼린저 밴드 돌파 (변동성 확장).

  종가 > 상단밴드  -> 변동성 확장 돌파 매수 (미보유)
  보유 중: 익절/손절 도달 또는 종가 < 중심선 -> 매도
분봉 데이트레에서 변동성 분출에 편승하고 빠르게 빠진다.
"""
from __future__ import annotations

import pandas as pd

from .base import Strategy, Signal, Action, Position, ParamSpec
from ..indicators import bollinger


class BollingerBreakout(Strategy):
    """볼린저 돌파: 종가가 상단밴드 돌파 시 매수, 손익절/중심선 이탈 시 매도. 데이트레 변동성."""
    name = "bollinger_breakout"
    horizon = "day"
    PARAMS = (
        ParamSpec("period", 20, 5, 60, "int", "기간"),
        ParamSpec("num_std", 2.0, 1.0, 3.5, "float", "밴드 폭(표준편차 배수)"),
        ParamSpec("target_profit_pct", 0.03, 0.005, 0.20, "float", "익절 비율"),
        ParamSpec("stop_loss_pct", 0.02, 0.005, 0.15, "float", "손절 비율"),
    )

    @property
    def min_candles(self) -> int:
        return int(self.params.get("period", 20)) + 2

    def decide(self, candles: pd.DataFrame, position: Position) -> Signal:
        period = int(self.params.get("period", 20))
        ns = float(self.params.get("num_std", 2.0))
        if len(candles) < period + 2:
            return self.hold("캔들 부족")
        close = candles["close"].astype(float)
        mid, upper, _lower = bollinger(close, period, ns)
        price = float(close.iloc[-1])
        up, m = float(upper.iloc[-1]), float(mid.iloc[-1])

        if position.is_open:
            tp = float(self.params.get("target_profit_pct", 0.03))
            sl = float(self.params.get("stop_loss_pct", 0.02))
            change = (price - position.avg_price) / position.avg_price
            if change >= tp:
                return Signal(Action.SELL, f"익절 +{change:.2%}")
            if change <= -sl:
                return Signal(Action.SELL, f"손절 {change:.2%}")
            if price < m:
                return Signal(Action.SELL, f"중심선 이탈 ({price:,.2f} < {m:,.2f})")
            return self.hold(f"보유 ({change:+.2%})")

        if price > up:
            return Signal(Action.BUY, f"상단밴드 돌파 매수 ({price:,.2f} > {up:,.2f})")
        return self.hold("밴드 내")

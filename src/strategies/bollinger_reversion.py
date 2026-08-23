"""전략 7: 볼린저 밴드 평균회귀.

  종가 < 하단밴드   -> 과매도 매수 (미보유)
  종가 >= 중심선     -> 평균회귀 완료 매도 (보유)
박스권·횡보에서 밴드 양끝을 오가는 되돌림. 단기 스윙(일/시간봉).
"""
from __future__ import annotations

import pandas as pd

from .base import Strategy, Signal, Action, Position, ParamSpec
from ..indicators import bollinger


class BollingerReversion(Strategy):
    """볼린저 평균회귀: 종가가 하단밴드 이탈 시 매수, 중심선 복귀 시 매도. 박스권. 단기 스윙."""
    name = "bollinger_reversion"
    horizon = "swing"
    PARAMS = (
        ParamSpec("period", 20, 5, 80, "int", "이동평균/표준편차 기간"),
        ParamSpec("num_std", 2.0, 1.0, 3.5, "float", "밴드 폭(표준편차 배수)"),
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
        mid, _upper, lower = bollinger(close, period, ns)
        price = float(close.iloc[-1])
        m, lo = float(mid.iloc[-1]), float(lower.iloc[-1])
        if not position.is_open and price < lo:
            return Signal(Action.BUY, f"하단밴드 이탈 매수 ({price:,.2f} < {lo:,.2f})")
        if position.is_open and price >= m:
            return Signal(Action.SELL, f"중심선 복귀 매도 ({price:,.2f} >= {m:,.2f})")
        return self.hold("밴드 내")

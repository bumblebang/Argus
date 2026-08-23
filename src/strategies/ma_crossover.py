"""전략 2: 이동평균 골든/데드 크로스.

  단기 MA 가 장기 MA 를 상향 돌파(골든크로스) -> 매수
  단기 MA 가 장기 MA 를 하향 돌파(데드크로스) -> 매도
"""
from __future__ import annotations

import pandas as pd

from .base import Strategy, Signal, Action, Position, ParamSpec
from ..indicators import sma, crossed_up, crossed_down


class MACrossover(Strategy):
    """이동평균 크로스: 단기MA가 장기MA 상향(골든) 시 매수, 하향(데드) 시 매도. 추세 추종."""
    name = "ma_crossover"
    horizon = "position"
    PARAMS = (
        ParamSpec("short", 5, 2, 60, "int", "단기 이동평균 기간"),
        ParamSpec("long", 20, 3, 240, "int", "장기 이동평균 기간"),
    )

    @classmethod
    def cross_check(cls, params: dict) -> tuple[dict, list[str]]:
        # 단기 < 장기 강제(아니면 골든/데드크로스 의미 없음).
        if params.get("short", 0) >= params.get("long", 0):
            fixed = max(2, int(params["long"]) - 1)
            msg = f"short {params['short']} >= long {params['long']} → short={fixed}"
            params = {**params, "short": fixed}
            return params, [msg]
        return params, []

    @property
    def min_candles(self) -> int:
        return int(self.params.get("long", 20)) + 2

    def decide(self, candles: pd.DataFrame, position: Position) -> Signal:
        short = int(self.params.get("short", 5))
        long = int(self.params.get("long", 20))
        if len(candles) < long + 2:
            return self.hold("캔들 부족")

        close = candles["close"].astype(float)
        fast = sma(close, short)
        slow = sma(close, long)

        if not position.is_open and crossed_up(fast, slow):
            return Signal(Action.BUY, f"골든크로스 (MA{short}>MA{long})")
        if position.is_open and crossed_down(fast, slow):
            return Signal(Action.SELL, f"데드크로스 (MA{short}<MA{long})")
        return self.hold("크로스 없음")

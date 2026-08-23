"""전략 5: 시계열 모멘텀 (절대 모멘텀).

  최근 lookback 봉 수익률(ROC) >= entry_pct  -> 매수
  수익률이 exit_pct 이하로 식음               -> 매도
'추세는 지속된다'는 모멘텀 효과에 베팅. 중장기(주/일봉).
"""
from __future__ import annotations

import pandas as pd

from .base import Strategy, Signal, Action, Position, ParamSpec


class Momentum(Strategy):
    """시계열 모멘텀: lookback 수익률이 임계 이상이면 매수, 식으면 매도. 추세 지속. 중장기."""
    name = "momentum"
    horizon = "position"
    PARAMS = (
        ParamSpec("lookback", 90, 10, 252, "int", "모멘텀 측정 기간(봉)"),
        ParamSpec("entry_pct", 0.05, 0.0, 0.50, "float", "진입 임계 수익률"),
        ParamSpec("exit_pct", 0.0, -0.30, 0.20, "float", "청산 임계 수익률"),
    )

    @classmethod
    def cross_check(cls, params: dict) -> tuple[dict, list[str]]:
        # 진입 임계는 청산 임계보다 높아야(아니면 사자마자 팖).
        if params.get("entry_pct", 0.0) <= params.get("exit_pct", 0.0):
            fixed = round(float(params.get("exit_pct", 0.0)) + 0.01, 4)
            return {**params, "entry_pct": fixed}, [
                f"entry_pct {params['entry_pct']} <= exit_pct {params['exit_pct']} → entry={fixed}"]
        return params, []

    @property
    def min_candles(self) -> int:
        return int(self.params.get("lookback", 90)) + 2

    def decide(self, candles: pd.DataFrame, position: Position) -> Signal:
        lb = int(self.params.get("lookback", 90))
        entry = float(self.params.get("entry_pct", 0.05))
        exit_ = float(self.params.get("exit_pct", 0.0))
        if len(candles) < lb + 2:
            return self.hold("캔들 부족")
        close = candles["close"].astype(float)
        m = float(close.iloc[-1] / close.iloc[-1 - lb] - 1)
        if not position.is_open and m >= entry:
            return Signal(Action.BUY, f"모멘텀 매수 ({lb}봉 {m:+.2%} >= {entry:.2%})")
        if position.is_open and m <= exit_:
            return Signal(Action.SELL, f"모멘텀 소멸 매도 ({lb}봉 {m:+.2%} <= {exit_:.2%})")
        return self.hold(f"모멘텀 {m:+.2%}")

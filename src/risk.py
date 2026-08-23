"""포지션 사이징 및 리스크 한도."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class RiskManager:
    capital: dict          # {"KR": 1000000, "US": 1000}
    max_position_pct: float = 0.20
    max_positions: int = 5
    daily_loss_limit_pct: float = 0.05
    allow_fractional: bool = False

    def size_buy(self, market: str, price: float, weight: float | None = None,
                 *, min_qty: float = 0.0) -> float:
        """매수 수량 계산. weight 지정 시 max_position_pct 대신 사용.

        min_qty>0 이면(고단가·작은 목표비중으로 floor=0 되는 경우) 자본으로
        그 수량만큼 살 수 있을 때 하한을 강제한다. 현금·종목비중 한도는 RiskGate 가
        최종 판정한다(allow_min_lot 와 짝).
        """
        if price <= 0:
            return 0.0
        cap = float(self.capital.get(market, 0))
        pct = weight if weight is not None else self.max_position_pct
        budget = cap * pct
        qty = budget / price
        if not self.allow_fractional:
            qty = math.floor(qty)
        qty = max(qty, 0.0)
        if min_qty > 0 and qty < min_qty and price * min_qty <= cap:
            qty = float(min_qty) if self.allow_fractional else float(math.floor(min_qty))
        return qty

    def can_open_new(self, open_positions: int) -> bool:
        return open_positions < self.max_positions

    def daily_loss_exceeded(self, market: str, realized_pnl: float) -> bool:
        cap = float(self.capital.get(market, 0))
        if cap <= 0:
            return False
        return realized_pnl <= -cap * self.daily_loss_limit_pct

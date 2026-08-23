"""주문 집행 결과 — bool 대신 실체결 스냅샷을 executor/store mirror 가 읽는다."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecuteResult:
    ok: bool
    filled_qty: float = 0.0
    avg_price: float | None = None
    fee: float = 0.0
    order_qty: float = 0.0
    limit_price: float | None = None
    status: str = ""
    order_id: str | None = None
    reject_reason: str = ""

    def __bool__(self) -> bool:
        return self.ok

    @property
    def partial(self) -> bool:
        return self.ok and self.filled_qty > 0 and self.filled_qty + 1e-9 < self.order_qty

    @classmethod
    def rejected(cls, reason: str, *, order_qty: float = 0.0,
                 limit_price: float | None = None) -> ExecuteResult:
        return cls(ok=False, order_qty=order_qty, limit_price=limit_price,
                   reject_reason=reason or "")

    @classmethod
    def from_fill(cls, *, fill_qty: float, fill_price: float, fee: float,
                  order_qty: float, limit_price: float | None,
                  status: str = "FILLED", order_id: str | None = None) -> ExecuteResult:
        return cls(
            ok=fill_qty > 0,
            filled_qty=float(fill_qty),
            avg_price=float(fill_price) if fill_qty > 0 else None,
            fee=float(fee),
            order_qty=float(order_qty),
            limit_price=limit_price,
            status=status,
            order_id=order_id,
        )

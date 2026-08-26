"""포지션 사이징 및 리스크 한도.

사이징 분모는 기본 실자산(equity). capital 은 손실예산 폴백·US 차단·min_lot 한도용.
(일손실·DD 분모는 게이트가 당일 시가 SoD equity 를 쓰고, 실패 시 capital 폴백.)
종목 목표비중(base_position_pct)·상한(max_position_pct)·확신 배율 밴드는
config risk.* 로 시점/운용자마다 바꾸면 된다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def risk_manager_from_cfg(risk_cfg: dict | None) -> "RiskManager":
    """config.risk 블록 → RiskManager. 키 빠져도 기본값으로 안전 기동."""
    rc = risk_cfg or {}
    return RiskManager(
        capital=dict(rc.get("capital") or {}),
        max_position_pct=float(rc.get("max_position_pct", 0.25)),
        max_positions=int(rc.get("max_positions", 5)),
        daily_loss_limit_pct=float(rc.get("daily_loss_limit_pct", 0.05)),
        allow_fractional=bool(rc.get("allow_fractional", False)),
        base_position_pct=float(rc.get("base_position_pct", 0.20)),
        sizing_base=str(rc.get("sizing_base", "equity")).lower(),
        conviction_size_floor=float(rc.get("conviction_size_floor", 0.75)),
        conviction_size_span=float(rc.get("conviction_size_span", 0.25)),
    )


@dataclass
class RiskManager:
    capital: dict          # {"KR": 1000000, "US": 1000} — 손실예산 폴백·US 차단
    max_position_pct: float = 0.25
    max_positions: int = 5
    daily_loss_limit_pct: float = 0.05
    allow_fractional: bool = False
    # 사이징 정책(config 로 조정) — 기본 총자산 20%, 확신도 75~100% 배율
    base_position_pct: float = 0.20
    sizing_base: str = "equity"          # "equity" | "capital"
    conviction_size_floor: float = 0.75
    conviction_size_span: float = 0.25

    def capital_of(self, market: str) -> float:
        return float(self.capital.get(market, 0) or 0)

    def sizing_base_amount(self, broker, market: str) -> float:
        """사이징 분모. sizing_base=equity 면 게이트와 같은 실자산, 실패 시 capital."""
        if self.sizing_base != "equity":
            return self.capital_of(market)
        gate = getattr(broker, "gate", None)
        acct = getattr(broker, "account", None)
        if gate is not None and acct is not None and hasattr(gate, "exposure_base_amount"):
            try:
                eq = float(gate.exposure_base_amount(acct, market))
                if eq > 0:
                    return eq
            except Exception:
                pass
        return self.capital_of(market)

    def size_buy(self, market: str, price: float, weight: float | None = None,
                 *, min_qty: float = 0.0,
                 base_equity: float | None = None,
                 notional_cap: float | None = None) -> float:
        """매수 수량. weight 미지정 시 base_position_pct.

        base_equity 가 양수면 그 값을 분모로 쓰고, 없으면 capital[market].
        notional_cap 이 있으면 예산을 그 금액 이하로 클립(슬리브 room·종목 잔여한도).
        min_qty>0 이면 floor=0 구멍일 때 하한(자본/분모로 살 수 있을 때만).
        """
        if price <= 0:
            return 0.0
        base = (float(base_equity) if base_equity is not None and float(base_equity) > 0
                else self.capital_of(market))
        pct = float(weight) if weight is not None else float(self.base_position_pct)
        budget = base * pct
        if notional_cap is not None:
            try:
                cap_n = float(notional_cap)
            except (TypeError, ValueError):
                cap_n = -1.0
            if cap_n >= 0:
                budget = min(budget, cap_n)
        qty = budget / price if price else 0.0
        if not self.allow_fractional:
            qty = math.floor(qty)
        qty = max(qty, 0.0)
        if min_qty > 0 and qty < min_qty and price * min_qty <= base:
            qty = float(min_qty) if self.allow_fractional else float(math.floor(min_qty))
        return qty

    def can_open_new(self, open_positions: int) -> bool:
        return open_positions < self.max_positions

    def daily_loss_exceeded(self, market: str, realized_pnl: float,
                            *, budget_base: float | None = None) -> bool:
        """일손실 한도. budget_base(SoD 등) 우선, 없으면 capital."""
        base = (float(budget_base) if budget_base is not None and float(budget_base) > 0
                else self.capital_of(market))
        if base <= 0:
            return False
        return realized_pnl <= -base * self.daily_loss_limit_pct

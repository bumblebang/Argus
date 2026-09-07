"""포지션 사이징 및 리스크 한도.

사이징 분모는 기본 실자산(equity). capital 은 손실예산 폴백·US 차단·min_lot 한도용.
(일손실·DD 분모는 게이트가 당일 시가 SoD equity 를 쓰고, 실패 시 capital 폴백.)
종목 목표비중(base_position_pct)·상한(max_position_pct)·확신 배율 밴드는
config risk.* 로 시점/운용자마다 바꾸면 된다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def _normalize_fractional(raw) -> dict | bool:
    """bool | {KR: bool, US: bool} → 시장별 dict(대문자 키). bool 이면 그대로."""
    if isinstance(raw, dict):
        return {str(k).upper(): bool(v) for k, v in raw.items()}
    return bool(raw)


def risk_manager_from_cfg(risk_cfg: dict | None) -> "RiskManager":
    """config.risk 블록 → RiskManager. 키 빠져도 기본값으로 안전 기동."""
    from .risk_gate import _normalize_max_positions
    rc = risk_cfg or {}
    return RiskManager(
        capital=dict(rc.get("capital") or {}),
        max_position_pct=float(rc.get("max_position_pct", 0.25)),
        max_positions=_normalize_max_positions(rc.get("max_positions", 5)),
        daily_loss_limit_pct=float(rc.get("daily_loss_limit_pct", 0.05)),
        allow_fractional=rc.get("allow_fractional", False),
        fractional_decimals=int(rc.get("fractional_decimals", 4)),
        base_position_pct=float(rc.get("base_position_pct", 0.20)),
        sizing_base=str(rc.get("sizing_base", "equity")).lower(),
        conviction_size_floor=float(rc.get("conviction_size_floor", 0.75)),
        conviction_size_span=float(rc.get("conviction_size_span", 0.25)),
    )


@dataclass
class RiskManager:
    capital: dict          # {"KR": 1000000, "US": 1000} — 손실예산 폴백·US 차단
    max_position_pct: float = 0.25
    max_positions: dict | int = None  # type: ignore[assignment]
    daily_loss_limit_pct: float = 0.05
    # bool 또는 시장별 dict({"KR": false, "US": true}) — 미장은 정규장 소수점 매수가 되므로
    # 고단가주도 '되는 만큼' 정상 비중으로 살 수 있다. KR 은 소수점 거래가 없어 false.
    allow_fractional: bool | dict = False
    fractional_decimals: int = 4
    # 사이징 정책(config 로 조정) — 기본 총자산 20%, 확신도 75~100% 배율
    base_position_pct: float = 0.20
    sizing_base: str = "equity"          # "equity" | "capital"
    conviction_size_floor: float = 0.75
    conviction_size_span: float = 0.25

    def __post_init__(self):
        from .risk_gate import _normalize_max_positions
        self.allow_fractional = _normalize_fractional(self.allow_fractional)
        if self.max_positions is None:
            self.max_positions = {"KR": 5, "US": 5}
        else:
            self.max_positions = _normalize_max_positions(self.max_positions)

    def fractional_for(self, market: str | None = None) -> bool:
        """이 시장에서 소수점 수량이 되는가. dict 미설정 시장은 False(정수)."""
        af = self.allow_fractional
        if isinstance(af, dict):
            return bool(af.get(str(market).upper(), False)) if market else False
        return bool(af)

    def max_positions_for(self, market: str | None = None) -> int:
        mp = self.max_positions if isinstance(self.max_positions, dict) else {"KR": 5, "US": 5}
        if market is None:
            return int(min(mp.values()) if mp else 5)
        m = str(market).upper()
        if m in mp:
            return int(mp[m])
        return int(min(mp.values()) if mp else 5)

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
                 notional_cap: float | None = None,
                 allow_fractional: bool | None = None) -> float:
        """매수 수량. weight 미지정 시 base_position_pct.

        base_equity 가 양수면 그 값을 분모로 쓰고, 없으면 capital[market].
        notional_cap 이 있으면 예산을 그 금액 이하로 클립(슬리브 room·종목 잔여한도).
        allow_fractional 을 주면 config 시장 설정을 덮어쓴다 — 소수점 매수가 **정규장
        한정**이라, 정규장에만 도는 트랙(밸류)만 켜고 프리/애프터도 도는 뇌는 끄기 위함.
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
        frac = (self.fractional_for(market) if allow_fractional is None
                else bool(allow_fractional))
        qty = budget / price if price else 0.0
        if not frac:
            qty = math.floor(qty)
        else:
            # 예산을 넘지 않도록 **내림** 반올림(round 는 예산 초과 가능).
            f = 10 ** int(self.fractional_decimals)
            qty = math.floor(qty * f) / f
        qty = max(qty, 0.0)
        # 최소 1주 부활은 **예산 안에서만**. 분모(base)만 보면 notional_cap(종목 잔여
        # 한도·슬리브 room)이 0 이어도 1주가 되살아나 한도를 우회한다 — 이미 목표비중을
        # 채운 고단가 종목에 1주씩 계속 얹히는 경로가 여기였다.
        if min_qty > 0 and qty < min_qty and price * min_qty <= min(base, budget):
            qty = float(min_qty) if frac else float(math.floor(min_qty))
        return qty

    def can_open_new(self, open_positions: int, market: str | None = None) -> bool:
        return open_positions < self.max_positions_for(market)

    def daily_loss_exceeded(self, market: str, realized_pnl: float,
                            *, budget_base: float | None = None) -> bool:
        """일손실 한도. budget_base(SoD 등) 우선, 없으면 capital."""
        base = (float(budget_base) if budget_base is not None and float(budget_base) > 0
                else self.capital_of(market))
        if base <= 0:
            return False
        return realized_pnl <= -base * self.daily_loss_limit_pct


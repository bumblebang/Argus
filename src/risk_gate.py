"""하드 리스크 게이트 — 모든 주문이 반드시 통과하는 단일 관문.

설계 원칙: 돈을 만지는 한도는 코드로 강제하며, 상위 판단 계층(LLM 에이전트)이
어떤 논리를 펴도 이 게이트를 우회할 수 없다. 주문은 항상 check() 로 검증한 뒤
승인된 것만 집행한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .logging_setup import get_logger

log = get_logger("risk.gate")


@dataclass
class Order:
    symbol: str
    market: str
    side: str          # "BUY" | "SELL"
    qty: float
    price: float

    @property
    def notional(self) -> float:
        return self.qty * self.price


@dataclass
class GateDecision:
    approved: bool
    reason: str


class RiskGate:
    """계좌 스냅샷 + 제안 주문 -> 승인/거부.

    limits 예:
      capital: {KR: 1e7, US: 1e4}
      max_position_pct: 0.2
      max_positions: 5
      daily_loss_limit_pct: 0.05
      max_order_notional: {KR: 5e6, US: 5e3}
      kill_switch_file: "data/HALT"
    """

    def __init__(self, limits: dict):
        self.capital = limits.get("capital", {})
        self.max_position_pct = float(limits.get("max_position_pct", 0.20))
        self.max_positions = int(limits.get("max_positions", 5))
        self.daily_loss_limit_pct = float(limits.get("daily_loss_limit_pct", 0.05))
        self.max_order_notional = limits.get("max_order_notional", {})
        self.kill_switch_file = limits.get("kill_switch_file", "data/HALT")
        # 포트폴리오 수준 감독관 한도(선택). None 이면 비활성 — 종목단위 한도만 적용.
        gross = limits.get("max_gross_exposure")
        self.max_gross_exposure = float(gross) if gross is not None else None
        sector = limits.get("max_sector_pct")
        self.max_sector_pct = float(sector) if sector is not None else None
        self.sector_map = limits.get("sector_map", {}) or {}   # {symbol: sector}
        if self.max_sector_pct is not None and not self.sector_map:
            log.warning("max_sector_pct=%s 설정됐지만 sector_map 이 비어 있음 — "
                        "섹터 집중도 검사가 사실상 비활성입니다(유니버스에 sector 누락?).",
                        self.max_sector_pct)
        # 드로다운 브레이커(선택): 실현누적+미실현 손실이 자본×한도 초과 시 신규 매수 차단.
        dd = limits.get("max_drawdown_pct")
        self.max_drawdown_pct = float(dd) if dd is not None else None
        # 노출 한도(종목비중·총익스포저·섹터)의 기준: "capital"(고정 자본) | "equity"(실자산).
        # 손실 예산(일손실·드로다운)은 배정 자본 기준이 맞으므로 항상 capital 을 쓴다.
        self.exposure_base = str(limits.get("exposure_base", "capital")).lower()
        # 최소 1주 시범매수: qty==min_lot_qty BUY 는 주문상한·종목비중 면제(현금·총익스포저·
        # 보유수·킬스위치는 유지). 고단가 종목이 목표비중 floor=0 으로 영구 탈락하는 구멍 보완.
        self.allow_min_lot = bool(limits.get("allow_min_lot", False))
        self.min_lot_qty = float(limits.get("min_lot_qty", 1.0))
        # KRX 경보·관리 등 하드스킵 심볼(BUY만). 파일/리스트로 주입.
        blocked = limits.get("blocked_symbols") or []
        self.blocked_symbols = {str(s) for s in blocked if s}
        blocked_file = limits.get("blocked_symbols_file")
        if blocked_file:
            try:
                from .datasources.krx_alerts import load_blocked_symbols
                self.blocked_symbols |= load_blocked_symbols(Path(blocked_file))
            except Exception as e:
                log.warning("blocked_symbols_file 로드 실패: %s", e)

    def _cap(self, market: str) -> float:
        return float(self.capital.get(market, 0.0))

    def _exposure_base(self, account, market: str) -> float:
        """노출 한도의 기준 금액. exposure_base='equity' 면 현재 실자산(현금+보유평가).

        고정 capital 은 자산이 불어나도 한도가 그대로라 실효 한도가 헐거워지고, 자산이
        줄면 반대로 과대 노출을 허용한다. equity 는 이를 추종한다. 산출 실패/0 이하면
        capital 로 폴백한다(한도가 조용히 사라지지 않게).
        """
        if self.exposure_base != "equity":
            return self._cap(market)
        try:
            eq = float(account.equity(market, getattr(account, "marks", None) or None))
        except Exception as e:
            log.warning("실자산 산출 실패 — capital 기준으로 폴백(%s): %s", market, e)
            return self._cap(market)
        return eq if eq > 0 else self._cap(market)

    def exposure_base_amount(self, account, market: str) -> float:
        """노출 한도 기준 금액(공개 API) — 밸류 슬리브가 게이트와 동일한 기준을 쓰도록."""
        return self._exposure_base(account, market)

    def _invested(self, account, market: str, sector: str | None = None) -> float:
        """시장(+섹터) 내 보유 익스포저 합(원가기준 qty×avg_price). 결정적·가격무관."""
        total = 0.0
        for sym, p in getattr(account, "positions", {}).items():
            if not getattr(p, "is_open", False):
                continue
            if account.symbol_market.get(sym) != market:
                continue
            if sector is not None and self.sector_map.get(sym) != sector:
                continue
            total += p.qty * p.avg_price
        return total

    def check(self, order: Order, account) -> GateDecision:
        """account: PaperAccount 호환 객체.
        필요한 속성: buying_power(market), position(symbol), open_count, realized_pnl(dict)."""
        m = order.market

        # 0) 킬스위치 — 이 파일이 있으면 모든 신규 주문 차단
        if Path(self.kill_switch_file).exists():
            return GateDecision(False, "킬스위치 활성(HALT 파일 존재)")

        # 1) 수량/가격 정합성
        if order.qty <= 0 or order.price <= 0:
            return GateDecision(False, f"비정상 수량/가격(qty={order.qty}, px={order.price})")

        pos = account.position(order.symbol)
        # 최소 1주 시범매수면 주문상한·종목비중만 면제(아래 BUY 분기에서 재사용).
        min_lot = (self.allow_min_lot and order.side == "BUY"
                   and order.qty == self.min_lot_qty)

        # 2) 주문당 최대 금액
        cap_notional = self.max_order_notional.get(m)
        if (not min_lot and cap_notional is not None
                and order.notional > float(cap_notional)):
            return GateDecision(False,
                f"주문금액 초과 ({order.notional:,.0f} > {float(cap_notional):,.0f})")

        if order.side == "BUY":
            if order.symbol in self.blocked_symbols:
                return GateDecision(False, f"KRX 경보·관리 차단({order.symbol})")

            # 3) 일 손실 한도 도달 시 신규 매수 차단 — '오늘' 실현손익 기준(시장 타임존
            #    날짜로 리셋). 누적 realized_pnl 을 쓰면 영구 차단이 돼버린다.
            cap = self._cap(m)
            realized_today = (account.daily_realized_pnl(m)
                              if hasattr(account, "daily_realized_pnl")
                              else account.realized_pnl.get(m, 0.0))
            if cap > 0 and realized_today <= -cap * self.daily_loss_limit_pct:
                return GateDecision(False,
                    f"일 손실 한도 도달 (오늘 실현손익 {realized_today:,.0f})")

            # 3b) 드로다운 브레이커: 미실현 손실 포함 총 드로다운이 한도 초과면 신규 매수
            #     차단(보유가 깊은 손실인데 위험을 더 쌓는 것을 막는다). 마크는 감시 루프가
            #     매 틱 갱신 — 마크 없으면 미실현 0(배치 등 비루프 경로는 기존과 동일).
            if self.max_drawdown_pct is not None and cap > 0:
                unreal = (account.unrealized_pnl(m)
                          if hasattr(account, "unrealized_pnl") else 0.0)
                drawdown = account.realized_pnl.get(m, 0.0) + unreal
                if drawdown <= -cap * self.max_drawdown_pct:
                    return GateDecision(False,
                        f"드로다운 한도 도달 (실현누적+미실현 {drawdown:,.0f})")

            # 4) 매수여력(현금) 확인
            if order.notional > account.buying_power(m):
                return GateDecision(False,
                    f"매수여력 부족 ({order.notional:,.0f} > {account.buying_power(m):,.0f})")

            # 노출 한도(비중·총익스포저·섹터)의 기준 — capital 고정 또는 실자산 추종.
            base = self._exposure_base(account, m)

            # 5) 종목당 최대 비중 (체결 후 평가액 기준). 최소 1주 시범매수는 면제.
            post_value = pos.qty * order.price + order.notional
            if (not min_lot and base > 0
                    and post_value > base * self.max_position_pct):
                return GateDecision(False,
                    f"종목 비중 초과 (체결후 {post_value:,.0f} > {base * self.max_position_pct:,.0f})")

            # 6) 동시 보유 종목 수 (신규 진입일 때만)
            if not pos.is_open and account.open_count >= self.max_positions:
                return GateDecision(False,
                    f"최대 보유종목 수 초과 ({account.open_count}/{self.max_positions})")

            # ── 포트폴리오 수준 감독관(선택) — 종목단위 한도를 통과해도 전체 쏠림은 차단 ──
            # 7) 총 익스포저(시장별): 체결 후 투자금이 한도 초과면 거부(현금 버퍼 강제).
            if self.max_gross_exposure is not None and base > 0:
                post_gross = self._invested(account, m) + order.notional
                limit = base * self.max_gross_exposure
                if post_gross > limit:
                    return GateDecision(False,
                        f"총 익스포저 초과 (체결후 {post_gross:,.0f} > {limit:,.0f})")

            # 8) 섹터 집중도: 한 섹터에 과도하게 쏠리면 거부(상관 리스크 프록시).
            sector = self.sector_map.get(order.symbol)
            if self.max_sector_pct is not None and sector and base > 0:
                post_sector = self._invested(account, m, sector) + order.notional
                limit = base * self.max_sector_pct
                if post_sector > limit:
                    return GateDecision(False,
                        f"섹터 집중 초과 [{sector}] (체결후 {post_sector:,.0f} > {limit:,.0f})")

        elif order.side == "SELL":
            if order.qty > pos.qty:
                return GateDecision(False,
                    f"매도수량 초과 (보유 {pos.qty} < 주문 {order.qty})")
        else:
            return GateDecision(False, f"알 수 없는 side: {order.side}")

        return GateDecision(True, "승인")

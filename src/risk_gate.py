"""하드 리스크 게이트 — 모든 주문이 반드시 통과하는 단일 관문.

설계 원칙: 돈을 만지는 한도는 코드로 강제하며, 상위 판단 계층(LLM 에이전트)이
어떤 논리를 펴도 이 게이트를 우회할 수 없다. 주문은 항상 check() 로 검증한 뒤
승인된 것만 집행한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .logging_setup import get_logger
from . import paths as _paths

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
class Reservation:
    """접수됐지만 아직 원장에 반영되지 않은 주문 1건.

    라이브에서 place_order 와 apply_fill 사이에는 수 초의 폴링 구간이 있고, 그
    동안 cash/positions 는 주문 전 그대로다. 게이트가 원장만 보면 그 구간에 들어온
    **다른 종목** 주문이 같은 현금을 다시 쓴다(동일 종목은 in-flight 로 막히지만
    한도는 계좌 단위다). 그래서 미반영 주문을 '예약'으로 들고 게이트에 넘긴다.
    """
    symbol: str
    market: str
    side: str
    qty: float
    price: float
    order_id: str | None = None
    placed_at: float = 0.0

    @property
    def notional(self) -> float:
        return self.qty * self.price


@dataclass
class GateDecision:
    approved: bool
    reason: str


def _normalize_capital(raw) -> dict[str, float]:
    """dict → 시장별 float. 키는 KR/US 등 대문자."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in raw.items():
        try:
            out[str(k).upper()] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def capital_coverage_gaps(capital: dict, markets: list[str]) -> list[str]:
    """capital 키가 없는 시장 목록(KR/US 대문자)."""
    norm = _normalize_capital(capital)
    gaps: list[str] = []
    for mkt in markets:
        m = str(mkt or "").upper()
        if m and m not in norm:
            gaps.append(m)
    return gaps


def warn_capital_coverage(capital: dict, markets: list[str], *,
                          label: str = "risk.capital") -> None:
    """거래 대상 시장에 capital 키가 없으면 5개 한도가 조용히 꺼진다 — 기동 시 경고."""
    for m in capital_coverage_gaps(capital, markets):
        log.warning(
            "%s[%s] 없음 — 일손실·DD·비중·총노출·섹터 한도가 비활성입니다.",
            label, m)


def _normalize_max_positions(raw) -> dict[str, int]:
    """int → {KR,US: n}; dict → 시장별 int. 빈/이상값은 기본 5."""
    if isinstance(raw, dict):
        out: dict[str, int] = {}
        for k, v in raw.items():
            try:
                out[str(k).upper()] = int(v)
            except (TypeError, ValueError):
                continue
        return out or {"KR": 5, "US": 5}
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 5
    return {"KR": n, "US": n}


class RiskGate:
    """계좌 스냅샷 + 제안 주문 -> 승인/거부.

    limits 예:
      capital: {KR: 1e7, US: 1e4}
      max_position_pct: 0.2
      max_positions: 5            # 또는 {KR: 5, US: 3}
      daily_loss_limit_pct: 0.05
      max_order_notional: {KR: 5e6, US: 5e3}
      kill_switch_file: "data/HALT"          # 전역 — BUY/SELL 전부
      # 마켓 pause: kill_switch_file 옆에 HALT.KR / HALT.US (BUY만 차단)
    """

    def __init__(self, limits: dict):
        self.capital = _normalize_capital(limits.get("capital", {}))
        for mkt, val in self.capital.items():
            if val <= 0:
                log.warning(
                    "capital[%s]=%s — 해당 시장 한도(일손실·DD·비중·총노출·섹터)가 "
                    "비활성입니다.",
                    mkt, val)
        self.max_position_pct = float(limits.get("max_position_pct", 0.20))
        self.max_positions = _normalize_max_positions(limits.get("max_positions", 5))
        self.daily_loss_limit_pct = float(limits.get("daily_loss_limit_pct", 0.05))
        # 일손실 판정에 SoD equity 델타를 함께 볼지. realized_pnl 을 우회한 체결
        # (폴링 밖 체결 → 재대사 흡수)을 잡는다. False 면 실현손익만(구 동작).
        self.daily_loss_use_sod_delta = bool(
            limits.get("daily_loss_use_sod_delta", True))
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
        # 드로다운 브레이커(선택): 실현누적+미실현 손실이 SoD equity×한도 초과 시 신규 매수 차단.
        dd = limits.get("max_drawdown_pct")
        self.max_drawdown_pct = float(dd) if dd is not None else None
        # 노출 한도(종목비중·총익스포저·섹터)의 기준: "capital"(고정 자본) | "equity"(실자산).
        # 손실 예산(일손실·드로다운)은 당일 시가(SoD) equity, 없으면 capital 폴백.
        self.exposure_base = str(limits.get("exposure_base", "capital")).lower()
        # 최소 1주 시범매수: qty==min_lot_qty BUY 는 주문상한·종목비중을 면제한다.
        # 고단가 floor=0 보완(US NVDA 1주가 25%를 넘는 경우). 현금·gross·섹터·
        # 보유수는 그대로. 이미 보유 중이면 면제하지 않는다(에피소드당 1회).
        self.allow_min_lot = bool(limits.get("allow_min_lot", False))
        self.min_lot_qty = float(limits.get("min_lot_qty", 1.0))
        # 시범매수 절대 상한(원). 면제와 무관하게 이 금액을 넘는 min_lot 주문은 거부.
        # None 이면 비활성. 시장별 dict 또는 스칼라.
        self.min_lot_max_notional = limits.get("min_lot_max_notional")
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

    def max_positions_for(self, market: str) -> int:
        m = str(market or "").upper()
        if m in self.max_positions:
            return int(self.max_positions[m])
        # 미지정 시장: dict 값 중 최소(보수) 또는 기본 5
        if self.max_positions:
            return int(min(self.max_positions.values()))
        return 5

    def _halt_path(self) -> Path:
        return _paths.resolve("halt", configured=self.kill_switch_file)

    def _market_pause_path(self, market: str) -> Path:
        """전역 HALT 옆 HALT.{KR|US}. 전역은 BUY/SELL 전부, 마켓 pause 는 BUY만."""
        return _paths.resolve_halt_pause(market, configured=self.kill_switch_file)

    def is_globally_halted(self) -> bool:
        return self._halt_path().exists()

    def is_market_paused(self, market: str) -> bool:
        return _paths.halt_pause_exists(market, configured=self.kill_switch_file)

    def pause_status(self) -> str:
        """대시 배지용: ALL | KR | US | KR+US | none."""
        if self.is_globally_halted():
            return "ALL"
        paused = [m for m in ("KR", "US") if self.is_market_paused(m)]
        if not paused:
            return "none"
        return "+".join(paused)

    def _cap(self, market: str) -> float:
        m = str(market or "").upper()
        if m not in self.capital:
            return 0.0
        return float(self.capital[m])

    def _capital_configured(self, market: str) -> bool:
        """config capital 에 시장 키가 있고 값이 양수일 때만 한도 분모를 쓴다."""
        m = str(market or "").upper()
        return m in self.capital and float(self.capital[m]) > 0

    def _min_lot_cap(self, market: str) -> float | None:
        """시범매수 절대 상한. 시장별 dict 또는 스칼라. 없거나 ≤0 이면 None(비활성)."""
        cfg = self.min_lot_max_notional
        if cfg is None:
            return None
        raw = (cfg.get(str(market or "").upper()) if isinstance(cfg, dict) else cfg)
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    def _loss_budget_base(self, account, market: str) -> float:
        """일손실·DD 분모: 당일 시가(SoD) equity, 없으면 config capital 폴백.

        capital 에 시장 키가 없거나 0 이하면 분모 0 — 해당 시장 한도는 비활성(경고 없음).
        """
        if not self._capital_configured(market):
            return 0.0
        base = 0.0
        if hasattr(account, "ensure_sod_equity"):
            try:
                base = float(account.ensure_sod_equity(market) or 0.0)
            except Exception as e:
                log.warning("SoD equity 산출 실패 — capital 폴백(%s): %s", market, e)
                base = 0.0
        if base <= 0:
            base = self._cap(market)
        return base

    def _today_pnl(self, account, market: str) -> tuple[float, str]:
        """당일 손익 판정값 = realized_today 와 SoD equity 델타 중 **나쁜 쪽**.

        realized_pnl 은 봇이 apply_fill 을 본 체결만 센다. 미체결 주문이 폴링
        밖에서 체결되고 주기 재대사가 holdings 로 흡수하면 그 매도의 손익은
        realized 에 안 잡힌다 — 손실이 나도 일손실 게이트가 0 을 본다. cash/
        positions 는 재대사가 실계좌 값으로 덮으므로 equity 델타는 그 체결을
        자동으로 반영한다.

        두 값 중 나쁜 쪽을 쓰는 이유: 델타는 미실현 변동도 섞으므로 단독
        채택은 오탐이 많고, realized 단독은 위 구멍이 남는다. 최소값은
        어느 쪽이 눈이 밝든 손실을 놓치지 않는다(과차단 방향).
        입출금은 PaperAccount.adjust_sod_for_external_cash 가 SoD 기준을
        같이 옮겨 델타에서 빼 둔다(입금으로 우회 손실을 가리는 구멍 차단).
        """
        realized_today = (account.daily_realized_pnl(market)
                          if hasattr(account, "daily_realized_pnl")
                          else account.realized_pnl.get(market, 0.0))
        if not self.daily_loss_use_sod_delta:
            return realized_today, "실현손익"
        delta = None
        if hasattr(account, "sod_equity_delta"):
            try:
                delta = account.sod_equity_delta(market)
            except Exception as e:
                log.warning("SoD 델타 산출 실패 — 실현손익만 사용(%s): %s", market, e)
        if delta is None or delta >= realized_today:
            return realized_today, "실현손익"
        return delta, "자산변화"

    def _exposure_base(self, account, market: str) -> float:
        """노출 한도의 기준 금액. exposure_base='equity' 면 현재 실자산(현금+보유평가).

        고정 capital 은 자산이 불어나도 한도가 그대로라 실효 한도가 헐거워지고, 자산이
        줄면 반대로 과대 노출을 허용한다. equity 는 이를 추종한다. 산출 실패/0 이하면
        capital 로 폴백한다(한도가 조용히 사라지지 않게).

        capital 에 시장 키가 없거나 0 이하면 0 — 비중·총노출·섹터 한도 비활성.
        """
        if not self._capital_configured(market):
            return 0.0
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

    def _reserved_notional(self, reserved, market: str,
                           sector: str | None = None) -> float:
        """예약된 BUY 명목 합. SELL 예약이 만들 현금은 세지 않는다(미체결일 수 있음)."""
        if not reserved:
            return 0.0
        total = 0.0
        for r in reserved:
            if r.side != "BUY" or r.market != market:
                continue
            if sector is not None and self.sector_map.get(r.symbol) != sector:
                continue
            total += r.notional
        return total

    def _reserved_new_symbols(self, reserved, account, market: str | None = None) -> int:
        """예약 중이면서 아직 보유가 아닌 종목 수 — 보유종목 수 한도에 선반영.

        market 이 있으면 해당 시장 예약만 센다(심볼의 Reservation.market 기준).
        """
        if not reserved:
            return 0
        syms = set()
        for r in reserved:
            if r.side != "BUY":
                continue
            if market is not None and str(r.market).upper() != str(market).upper():
                continue
            if account.position(r.symbol).is_open:
                continue
            syms.add(r.symbol)
        return len(syms)

    def check(self, order: Order, account, *, reserved=None) -> GateDecision:
        """account: PaperAccount 호환 객체.
        필요한 속성: buying_power(market), position(symbol), count_open/open_count, realized_pnl(dict).

        reserved: 접수됐지만 원장 미반영인 Reservation 목록(선택). 주면 매수여력·
        총익스포저·섹터·보유종목 수 한도에 선반영한다. None 이면 기존 동작.
        """
        m = order.market
        reserved_buy = self._reserved_notional(reserved, m)

        # 0) 킬스위치 — 전역 HALT 파일이 있으면 BUY/SELL 전부 차단
        if self.is_globally_halted():
            return GateDecision(False, "킬스위치 활성(HALT 파일 존재)")

        # 0b) 마켓 pause — HALT.{market} 있으면 해당 시장 BUY만 차단(청산 SELL 허용)
        if order.side == "BUY" and self.is_market_paused(m):
            return GateDecision(False, f"시장 pause 활성(HALT.{str(m).upper()})")

        # 1) 수량/가격 정합성
        if order.qty <= 0 or order.price <= 0:
            return GateDecision(False, f"비정상 수량/가격(qty={order.qty}, px={order.price})")

        pos = account.position(order.symbol)
        # 최소 1주 시범매수면 주문상한만 면제. 이미 보유 중이면 시범이 아니다.
        min_lot = (self.allow_min_lot and order.side == "BUY"
                   and order.qty == self.min_lot_qty and not pos.is_open)

        if order.side == "BUY":
            # 2) 주문당 최대 금액 — 신규 매수 폭주 방지. SELL에는 미적용.
            #    시장 키 없음·null·≤0 이면 비활성(현금·비중·총노출 게이트로 충분).
            cap_notional = self.max_order_notional.get(m)
            if (not min_lot and cap_notional is not None
                    and float(cap_notional) > 0
                    and order.notional > float(cap_notional)):
                return GateDecision(False,
                    f"주문금액 초과 ({order.notional:,.0f} > {float(cap_notional):,.0f})")

            # 2b) 시범매수 절대 상한 — 면제를 받는 주문일수록 크기를 제한한다.
            lot_cap = self._min_lot_cap(m)
            if min_lot and lot_cap is not None and order.notional > lot_cap:
                return GateDecision(False,
                    f"시범매수 한도 초과 ({order.notional:,.0f} > {lot_cap:,.0f})")

            if order.symbol in self.blocked_symbols:
                return GateDecision(False, f"KRX 경보·관리 차단({order.symbol})")

            # 3) 일 손실 한도 도달 시 신규 매수 차단 — 시장 타임존 날짜로 리셋.
            #    분모=당일 시가(SoD) equity(없으면 capital). 누적 realized_pnl 을
            #    쓰면 영구 차단이 돼버린다.
            base = self._loss_budget_base(account, m)
            today_pnl, pnl_src = self._today_pnl(account, m)
            if base > 0 and today_pnl <= -base * self.daily_loss_limit_pct:
                return GateDecision(False,
                    f"일 손실 한도 도달 (오늘 {pnl_src} {today_pnl:,.0f})")

            # 3b) 드로다운 브레이커: 미실현 손실 포함 총 드로다운이 한도 초과면 신규 매수
            #     차단(보유가 깊은 손실인데 위험을 더 쌓는 것을 막는다). 마크는 감시 루프가
            #     매 틱 갱신 — 마크 없으면 미실현 0(배치 등 비루프 경로는 기존과 동일).
            #     여기는 **누적 축**이다. 당일 SoD 델타를 섞으면 일손실 게이트와
            #     같은 축이 돼 두 브레이커가 하나로 붕괴한다 — 축을 분리해 둔다.
            if self.max_drawdown_pct is not None and base > 0:
                unreal = (account.unrealized_pnl(m)
                          if hasattr(account, "unrealized_pnl") else 0.0)
                drawdown = account.realized_pnl.get(m, 0.0) + unreal
                if drawdown <= -base * self.max_drawdown_pct:
                    return GateDecision(False,
                        f"드로다운 한도 도달 (실현누적+미실현 {drawdown:,.0f})")

            # 4) 매수여력(현금) 확인 — 접수 후 미반영 주문(예약)이 쥔 현금을 뺀다.
            avail = account.buying_power(m) - reserved_buy
            if order.notional > avail:
                held = f" (예약 {reserved_buy:,.0f} 차감)" if reserved_buy else ""
                return GateDecision(False,
                    f"매수여력 부족 ({order.notional:,.0f} > {avail:,.0f}){held}")

            # 노출 한도(비중·총익스포저·섹터)의 기준 — capital 고정 또는 실자산 추종.
            base = self._exposure_base(account, m)

            # 5) 종목당 최대 비중 (체결 후 평가액 기준). 시범 1주(min_lot)만 면제 —
            #    목표비중이 1주도 안 되는 고단가 첫 진입. 2주 이상·추가매수는 그대로.
            post_value = pos.qty * order.price + order.notional
            if (not min_lot and base > 0
                    and post_value > base * self.max_position_pct):
                return GateDecision(False,
                    f"종목 비중 초과 (체결후 {post_value:,.0f} > {base * self.max_position_pct:,.0f})")

            # 6) 동시 보유 종목 수 (신규 진입일 때만). 해당 시장 + 예약된 신규만.
            if not pos.is_open:
                if hasattr(account, "count_open"):
                    open_n = account.count_open(m)
                else:
                    open_n = int(account.open_count)
                open_n += self._reserved_new_symbols(reserved, account, m)
                cap = self.max_positions_for(m)
                if open_n >= cap:
                    return GateDecision(False,
                        f"최대 보유종목 수 초과 ({open_n}/{cap} {m})")

            # ── 포트폴리오 수준 감독관(선택) — 종목단위 한도를 통과해도 전체 쏠림은 차단 ──
            # 7) 총 익스포저(시장별): 체결 후 투자금이 한도 초과면 거부(현금 버퍼 강제).
            if self.max_gross_exposure is not None and base > 0:
                post_gross = self._invested(account, m) + reserved_buy + order.notional
                limit = base * self.max_gross_exposure
                if post_gross > limit:
                    return GateDecision(False,
                        f"총 익스포저 초과 (체결후 {post_gross:,.0f} > {limit:,.0f})")

            # 8) 섹터 집중도: 한 섹터에 과도하게 쏠리면 거부(상관 리스크 프록시).
            sector = self.sector_map.get(order.symbol)
            if self.max_sector_pct is not None and sector and base > 0:
                post_sector = (self._invested(account, m, sector)
                               + self._reserved_notional(reserved, m, sector)
                               + order.notional)
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

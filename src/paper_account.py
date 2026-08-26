"""페이퍼 트레이딩 계좌 — 현금·수수료·슬리피지·손익·거래저널을 갖춘 모의계좌.

'페이퍼 완전자율' 운용의 토대. 실제 주문 없이 폴링 시점 가격에 슬리피지를 더해
체결을 시뮬레이션하고, 현금/포지션/실현손익을 추적하며 모든 체결을 저널에 남긴다.
상태는 data/paper_account.json 에 영속화한다.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from .logging_setup import get_logger
from .market_hours import market_day
from . import paths as _paths
from .strategies.base import Position

log = get_logger("paper")


@dataclass
class Fill:
    ts: str
    symbol: str
    market: str
    side: str
    qty: float
    price: float       # 슬리피지 반영 체결가
    fee: float
    reason: str


class PaperAccount:
    def __init__(self, cash: dict, fee_rate: dict | None = None,
                 slippage_bps: dict | None = None, sell_tax_rate: dict | None = None,
                 state_path: str | Path = "data/paper_account.json",
                 sell_tax_exempt_fn=None):
        self.start_cash = dict(cash)
        self.cash: dict[str, float] = dict(cash)
        self.fee_rate = fee_rate or {}
        self.slippage_bps = slippage_bps or {}
        self.sell_tax_rate = sell_tax_rate or {}   # 매도 시에만 부과(예: KR 증권거래세)
        # (symbol, market)->bool. True 면 합성 매도세를 면제(국내 ETF/ETN 등). None 이면
        # 항상 부과(기존 동작). 라이브는 실세금을 대사로 받으므로 이 경로를 타지 않는다.
        self.sell_tax_exempt_fn = sell_tax_exempt_fn
        self.state_path = _paths.resolve("paper", configured=state_path)
        self.positions: dict[str, Position] = {}
        self.symbol_market: dict[str, str] = {}   # 종목 -> 시장(KR/US)
        self.realized_pnl: dict[str, float] = {}          # 누적(리포팅용)
        self.realized_pnl_today: dict[str, float] = {}    # 오늘 실현손익(일 손실 한도용)
        self._pnl_day: dict[str, str] = {}                # 시장별 '오늘'(시장 타임존 날짜)
        # 당일 시가(SoD) equity — 일손실·DD 분모. 장중 재스냅 금지(영속).
        self._sod_day: dict[str, str] = {}
        self._sod_equity: dict[str, float] = {}
        self.marks: dict[str, float] = {}   # 실시간 평가가(감시 루프가 갱신, 비영속)
        self.journal: list[Fill] = []
        self._load()

    # ── 조회 ─────────────────────────────────────────────
    def position(self, symbol: str) -> Position:
        return self.positions.setdefault(symbol, Position(symbol=symbol))

    def buying_power(self, market: str) -> float:
        return float(self.cash.get(market, 0.0))

    @property
    def open_count(self) -> int:
        return sum(1 for p in self.positions.values() if p.is_open)

    def daily_realized_pnl(self, market: str) -> float:
        """시장 타임존 기준 '오늘' 실현손익. 날짜가 넘어가면 0부터(일 손실 한도의 리셋)."""
        if self._pnl_day.get(market) != market_day(market):
            return 0.0
        return self.realized_pnl_today.get(market, 0.0)

    def set_marks(self, price_of: dict[str, float]) -> None:
        """실시간 평가가 갱신(감시 루프가 매 틱 호출). 미실현 손익 산출 재료.

        마크 반영 후 시장별 SoD equity 를 조기 스냅 — 당일 첫 BUY 전에 분모가
        현금만으로 굳지 않게 한다(이미 오늘 값이 있으면 no-op).
        """
        for sym, px in (price_of or {}).items():
            if px and px > 0:
                self.marks[sym] = float(px)
        markets = set(self.cash) | {m for m in self.symbol_market.values() if m}
        for m in markets:
            self.ensure_sod_equity(m)

    def ensure_sod_equity(self, market: str) -> float:
        """당일 시가 equity. 날짜가 바뀌면 현재 equity(>0)로 한 번만 스냅·영속."""
        day = market_day(market)
        if self._sod_day.get(market) == day:
            return float(self._sod_equity.get(market, 0.0) or 0.0)
        marks = self.marks if self.marks else None
        try:
            eq = float(self.equity(market, marks))
        except Exception:
            return 0.0
        if eq <= 0:
            return 0.0
        self._sod_day[market] = day
        self._sod_equity[market] = eq
        self._save()
        return eq

    def loss_budget_base(self, market: str) -> float:
        """손실예산 분모(SoD). 아직 스냅 전이면 ensure 로 찍거나 0."""
        return self.ensure_sod_equity(market)

    def unrealized_pnl(self, market: str) -> float:
        """보유분 미실현 손익(마크 기준). 마크 없는 종목은 0으로 본다(보수 아님·결정적)."""
        total = 0.0
        for sym, p in list(self.positions.items()):   # 스냅샷(재대사 스레드와 경합 방지)
            if not p.is_open or self.symbol_market.get(sym) != market:
                continue
            mark = self.marks.get(sym)
            if mark:
                total += (mark - p.avg_price) * p.qty
        return total

    def equity(self, market: str, price_lookup: dict[str, float] | None = None) -> float:
        """해당 시장의 현금 + 보유평가액. price_lookup 없으면 평균단가로 평가."""
        total = self.cash.get(market, 0.0)
        for sym, p in list(self.positions.items()):   # 스냅샷(재대사 스레드와 경합 방지)
            if not p.is_open or self.symbol_market.get(sym) != market:
                continue
            px = (price_lookup or {}).get(sym, p.avg_price)
            total += p.qty * px
        return total

    # ── 체결 ─────────────────────────────────────────────
    def fill(self, symbol: str, market: str, side: str, qty: float,
             ref_price: float, reason: str = "") -> Fill:
        """모의 체결: 기준가에 슬리피지·수수료·매도세를 합성해 원장에 반영.

        페이퍼 운용·백테스트의 기본 경로. 라이브 실체결(실가격·실수수료)은 apply_fill 로.
        """
        slip = self.slippage_bps.get(market, 0.0) / 10000.0
        # 매수는 불리하게(높게), 매도는 불리하게(낮게) 체결
        exec_price = ref_price * (1 + slip) if side == "BUY" else ref_price * (1 - slip)
        fee = exec_price * qty * float(self.fee_rate.get(market, 0.0))
        if side == "SELL" and not self._tax_exempt(symbol, market):
            # 매도 거래세(예: KR 증권거래세) — 매도측에만 부과. ETF/ETN 등은 면제.
            fee += exec_price * qty * float(self.sell_tax_rate.get(market, 0.0))
        return self.apply_fill(symbol, market, side, qty, exec_price, fee, reason)

    def _tax_exempt(self, symbol: str, market: str) -> bool:
        """합성 매도세 면제 여부. 판정 예외는 False(부과) — 비용 과소추정을 피한다."""
        if self.sell_tax_exempt_fn is None:
            return False
        try:
            return bool(self.sell_tax_exempt_fn(symbol, market))
        except Exception as e:
            log.warning("매도세 면제 판정 실패(부과로 진행) %s: %s", symbol, e)
            return False

    def apply_fill(self, symbol: str, market: str, side: str, qty: float,
                   exec_price: float, fee: float, reason: str = "") -> Fill:
        """이미 확정된 체결가·수수료로 원장에 반영(현금·포지션·실현손익·저널).

        라이브는 토스 체결 대사(get_order.execution)에서 실체결가·실수수료·실세금을
        그대로 넘긴다 — 합성 슬리피지/수수료를 다시 얹지 않는다(이중계상 방지). 페이퍼
        fill() 은 합성값을 계산해 이 메서드로 넘긴다.
        """
        self.symbol_market[symbol] = market
        pos = self.position(symbol)
        if side == "BUY":
            self.cash[market] = self.cash.get(market, 0.0) - exec_price * qty - fee
            total_cost = pos.avg_price * pos.qty + exec_price * qty
            pos.qty += qty
            pos.avg_price = total_cost / pos.qty if pos.qty else 0.0
        elif side == "SELL":
            sell_qty = min(qty, pos.qty)
            proceeds = exec_price * sell_qty - fee
            self.cash[market] = self.cash.get(market, 0.0) + proceeds
            pnl = (exec_price - pos.avg_price) * sell_qty - fee
            self.realized_pnl[market] = self.realized_pnl.get(market, 0.0) + pnl
            day = market_day(market)
            if self._pnl_day.get(market) != day:      # 날짜 넘어가면 오늘치 리셋
                self._pnl_day[market] = day
                self.realized_pnl_today[market] = 0.0
            self.realized_pnl_today[market] = self.realized_pnl_today.get(market, 0.0) + pnl
            pos.qty -= sell_qty
            if pos.qty <= 0:
                pos.qty, pos.avg_price = 0.0, 0.0

        f = Fill(ts=datetime.now(timezone.utc).isoformat(), symbol=symbol, market=market,
                 side=side, qty=qty, price=exec_price, fee=fee, reason=reason)
        self.journal.append(f)
        self._save()
        return f

    # ── 영속화 ───────────────────────────────────────────
    def _load(self) -> None:
        """저장된 원장 복원. 깨진 파일에 죽지 않는다.

        비원자적 쓰기로 원장이 0바이트가 되면 JSONDecodeError 로 기동마다 즉사한다.
        파싱 실패는 '저장된 상태 없음'으로 강등하고 경고만 남긴다. 라이브는 기동 시
        sync_from_live 가 실계좌로 채운다. 깨진 파일은 .corrupt-<epoch> 로 보존한다.
        """
        if not self.state_path.exists():
            return
        try:
            raw = self.state_path.read_text(encoding="utf-8")
            data = json.loads(raw) if raw.strip() else None
        except (OSError, ValueError) as e:
            data = None
            log.error("원장 파일 손상/읽기 실패 — 저장된 상태 없이 계속: %s (%s)",
                      self.state_path, e)
        if not isinstance(data, dict):
            if self.state_path.exists() and self.state_path.stat().st_size >= 0:
                try:                       # 사후 분석용 보존(실패해도 기동을 막지 않는다)
                    self.state_path.replace(
                        self.state_path.with_suffix(f".corrupt-{int(datetime.now().timestamp())}"))
                except OSError as e:
                    log.warning("손상 원장 보존 실패(무시): %s", e)
            log.error("원장을 복원하지 못했다 — 기본값으로 시작한다. "
                      "라이브면 실계좌 동기화가 현금·보유를 다시 채운다.")
            return
        self.cash = data.get("cash", self.cash)
        self.realized_pnl = data.get("realized_pnl", {})
        self.realized_pnl_today = data.get("realized_pnl_today", {})
        self._pnl_day = data.get("pnl_day", {})
        self._sod_day = {str(k): str(v) for k, v in (data.get("sod_day") or {}).items()}
        raw_sod = data.get("sod_equity") or {}
        self._sod_equity = {}
        for k, v in raw_sod.items():
            try:
                self._sod_equity[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        self.symbol_market = data.get("symbol_market", {})
        for sym, p in data.get("positions", {}).items():
            self.positions[sym] = Position(symbol=sym, qty=p["qty"], avg_price=p["avg_price"])
        self.journal = [Fill(**j) for j in data.get("journal", [])]

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "start_cash": self.start_cash,
            "cash": self.cash,
            "realized_pnl": self.realized_pnl,
            "realized_pnl_today": self.realized_pnl_today,
            "pnl_day": self._pnl_day,
            "sod_day": dict(self._sod_day),
            "sod_equity": dict(self._sod_equity),
            "symbol_market": self.symbol_market,
            "positions": {s: {"qty": p.qty, "avg_price": p.avg_price}
                          for s, p in self.positions.items() if p.is_open},
            "journal": [asdict(f) for f in self.journal[-500:]],  # 최근 500건만
        }
        # 원자적 쓰기: tmp 에 완전히 쓴 뒤 os.replace. 대상에 직접 쓰면 중단 시 0바이트가 된다.
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

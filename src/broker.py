"""주문 집행기 — 모든 주문을 하드 리스크 게이트로 검증한 뒤 집행한다.

  mode="paper": 페이퍼 계좌에만 기록 (실주문 없음). '페이퍼 완전자율'의 기본.
  mode="live" : 토스 API 로 실주문 + 페이퍼 계좌에 미러링(=실계좌 미러). 단, 실주문
                접수가 확인된 뒤에만 원장(fill)을 기록한다 — 거부/실패가 원장에 체결로
                남지 않게 한다(치명 버그 방지). live_markets 밖 시장은 실주문도 원장
                기록도 하지 않는다(원장=실계좌 미러 원칙).

알려진 한계: 라이브 미체결 잔량은 주기 재대사(reconcile)가 반영한다.
execute() 는 ExecuteResult 를 반환하고, executor 는 store_fill.mirror_symbol_to_store 로
account→store 를 즉시 맞춘다(부분체결 포함).
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from .fill_result import ExecuteResult
from .logging_setup import get_logger
from .market_hours import current_session
from .paper_account import PaperAccount
from .risk_gate import RiskGate, Order, Reservation
from .strategies.base import Position
from .toss_client import TossClient

log = get_logger("broker")

# 토스 주문 생성(POST /api/v1/orders) 성공 응답의 주문 식별자 키(openapi.json OrderResponse:
# required=[orderId]). client.place_order 가 ApiResponse.result 를 벗겨 반환하므로 최상위에 온다.
_ORDER_ID_KEYS = ("orderId", "orderNo")

# OrderStatus(openapi.json) — 더 폴링해도 상태가 안 바뀌는 종결군. PARTIAL_FILLED 는
# 체결분(filledQuantity>0)이 있어 별도로 조기 종료한다(잔량은 주기 재대사가 반영).
_TERMINAL = {"FILLED", "CANCELED", "REJECTED", "CANCEL_REJECTED", "REPLACE_REJECTED"}
_PENDING = {"PENDING", "PENDING_CANCEL", "PENDING_REPLACE", "PARTIAL_FILLED", "REPLACED"}


def _num(v: Any) -> float | None:
    """토스 문자열 수치("70000")를 float 로. 빈값/None/비정상은 None."""
    if v is None:
        return None
    try:
        s = str(v).strip()
        return float(s) if s else None
    except (TypeError, ValueError):
        return None


class Broker:
    def __init__(self, account: PaperAccount, gate: RiskGate,
                 client: TossClient | None = None, mode: str = "paper",
                 account_seq: int | str | None = None,
                 live_markets: list[str] | None = None, store=None,
                 tradable_fn: Callable[[str, str], tuple[bool, str]] | None = None,
                 limit_slippage_pct: float = 0.01,
                 max_spread_pct_extended: float = 0.02,
                 reconcile_poll_attempts: int = 5,
                 reconcile_poll_sec: float = 0.4,
                 reservation_ttl_sec: float = 300.0):
        self.account = account
        self.gate = gate
        self.client = client
        self.mode = mode
        self.account_seq = account_seq
        # 실주문 허용 시장(라이브 한정). 이 밖의 시장은 실주문·원장기록 모두 스킵.
        self.live_markets = list(live_markets) if live_markets is not None else ["KR"]
        # 라이브 주문 이벤트(live_order/live_order_error) 기록용 store(선택). 없으면 로그만.
        self.store = store
        # 매수 안전가드: (symbol, market)->(매수가능, 사유). None(기본)이면 가드 비활성(하위호환).
        # 부적격 종목(관리/거래정지/상폐예정/ETF·ETN 등) 매수를 게이트 통과 후 최종 차단한다.
        self.tradable_fn = tradable_fn
        # 마켓터블 리밋: 최우선호가 대비 이 비율 안의 호가레벨까지만 훑어 리밋가를 잡는다
        # (시장가의 무제한 슬리피지 대신 상·하한을 둠 → 게이트 notional 이 실체결 상한을 검증).
        self.limit_slippage_pct = float(limit_slippage_pct)
        # 시간외(프리/애프터/데이마켓) 스프레드 상한. 최우선호가끼리 이 비율 넘게 벌어져 있으면
        # 주문을 스킵한다 — 마켓터블 리밋은 '최우선호가 대비' 상한이라 최우선호가 자체가
        # 적정가에서 멀면 그대로 나쁜 가격에 체결된다. 0 이하면 가드 비활성. 정규장은 미적용.
        self.max_spread_pct_extended = float(max_spread_pct_extended)
        # 라이브 체결 대사 폴링(주문 접수 후 실체결 수량·평균가·수수료를 읽어 원장에 반영).
        self.reconcile_poll_attempts = int(reconcile_poll_attempts)
        self.reconcile_poll_sec = float(reconcile_poll_sec)
        # 뇌 워커(진입)와 감시 루프(코드 청산)가 동시에 execute 할 수 있어 직렬화.
        # 주기 재대사(reconcile)도 이 락을 잡아 gate.check/체결과 원자적으로 계좌를 병합한다.
        self._lock = threading.Lock()
        # 접수됐지만 원장 미반영인 주문 {symbol: Reservation}. 동일 종목 중복 주문을
        # 막을 뿐 아니라, 게이트가 다른 종목 주문을 볼 때 이 예약분을 현금·노출에서
        # 미리 뺀다(J1). 심볼 집합만으로는 계좌 단위 한도를 지킬 수 없다.
        self._inflight: dict[str, Reservation] = {}
        # 예약이 새면 매수가 영구히 막히므로 TTL 로 강제 회수(+경보). 0 이하면 비활성.
        self.reservation_ttl_sec = float(reservation_ttl_sec)
        # 주문 시작·종료마다 증가. 재대사 API 조회(락 밖) 중 주문이 시작·끝나
        # apply 시점 inflight 가 비어도, 조회 스냅샷이 낡은지 판별한다.
        self._activity_gen: int = 0
        # 직전 execute 가 거부된 사유(한글). 성공 시 "". 저널/이벤트가 thesis 대신 기록.
        self.last_reject_reason: str = ""
        self.last_result: ExecuteResult | None = None

    # 게이트/러너가 참조하는 계좌 상태 위임
    def position(self, symbol: str) -> Position:
        return self.account.position(symbol)

    @property
    def open_count(self) -> int:
        return self.account.open_count

    @property
    def realized_pnl(self) -> dict:
        return self.account.realized_pnl

    def execute(self, order: Order, reason: str, *,
                store=None,
                armed_id: int | None = None,
                plan_fn=None,
                exit_reason: str | None = None) -> ExecuteResult:
        """주문 집행. store 가 주어지면 apply_fill 과 mirror 를 **같은 락 구간**에서 처리."""
        mirror_st = store if store is not None else None
        base_kw = {"order_qty": float(order.qty), "limit_price": float(order.price)}

        with self._lock:
            if self._reject_inflight(order, base_kw):
                return self.last_result

        if self.mode == "live":
            if not self._prepare_live_order(order):
                with self._lock:
                    if not self.last_reject_reason:
                        self.last_reject_reason = "라이브 주문 준비 실패"
                    self.last_result = ExecuteResult.rejected(
                        self.last_reject_reason, **base_kw)
                return self.last_result

        with self._lock:
            prep = self._begin_execute_locked(order, reason, base_kw)
        if prep is None:
            return self.last_result

        sym = order.symbol
        if prep["kind"] == "paper":
            try:
                with self._lock:
                    res = self._finish_paper(order, reason, prep["base_kw"])
                    self._mirror_after_fill(mirror_st, order, res, armed_id, plan_fn, exit_reason)
                    return res
            finally:
                with self._lock:
                    self._clear_inflight(sym)

        try:
            filled_qty, avg_px, fee, status = self._reconcile_order(prep["order_id"])
            with self._lock:
                res = self._finish_live(
                    order, reason, prep["order_id"], prep["base_kw"],
                    filled_qty, avg_px, fee, status,
                    qty_before=prep.get("qty_before"),
                    exit_reason=exit_reason)
                self._mirror_after_fill(mirror_st, order, res, armed_id, plan_fn, exit_reason)
                return res
        finally:
            with self._lock:
                self._clear_inflight(sym)

    def execute_with_mirror(
        self, order: Order, reason: str, *,
        store=None,
        armed_id: int | None = None,
        plan_fn=None,
        exit_reason: str | None = None,
    ) -> ExecuteResult:
        """execute(..., store=...) 와 동일 — 하위호환 별칭."""
        return self.execute(order, reason, store=store, armed_id=armed_id,
                            plan_fn=plan_fn, exit_reason=exit_reason)

    def _mirror_after_fill(self, store, order: Order, res: ExecuteResult,
                           armed_id, plan_fn, exit_reason) -> None:
        if store is None or not res.ok:
            return
        from .store_fill import mirror_symbol_to_store
        mirror_symbol_to_store(
            store, self, order.symbol, fill=res,
            armed_id=armed_id, plan_fn=plan_fn, exit_reason=exit_reason)

    def set_marks(self, price_of: dict[str, float]) -> None:
        """실시간 평가가 갱신 — gate.check 와 reconcile 이 같은 marks 를 보도록 락 안에서.

        account.set_marks 가 SoD equity 조기 스냅까지 수행(손실예산 분모).
        """
        with self._lock:
            self.account.set_marks(price_of)

    def run_locked(self, fn: Callable[[PaperAccount], Any]) -> Any:
        """account 읽기/동기화를 broker 락 안에서 실행(reconcile/sync 공용)."""
        with self._lock:
            return fn(self.account)

    def sync_from_live(self, gateway, store=None, *, markets=("KR", "US")) -> dict:
        """기동 동기화 — API fetch(락 밖) + apply( run_locked ). sync_from_live 직접 호출 금지."""
        from .broker_sync import apply_sync_from_live, fetch_live_account_data
        data = fetch_live_account_data(gateway, self.account_seq, markets=markets)
        return self.run_locked(
            lambda acct: apply_sync_from_live(acct, store, data, markets=markets))

    def activity_generation(self) -> int:
        """주문 활동 세대(락 안 스냅샷). 재대사 fetch 직전 캡처용."""
        with self._lock:
            return self._activity_gen

    def _mark_inflight(self, order: Order, order_id: str | None = None) -> None:
        """락 안: 예약 등록 + activity_gen 증가."""
        self._inflight[order.symbol] = Reservation(
            symbol=order.symbol, market=order.market, side=order.side,
            qty=float(order.qty), price=float(order.price),
            order_id=order_id, placed_at=time.time())
        self._activity_gen += 1

    def _clear_inflight(self, symbol: str) -> None:
        """락 안: 예약 해제. 실제로 빠져 나갔을 때만 gen 증가."""
        if self._inflight.pop(symbol, None) is None:
            return
        self._activity_gen += 1

    def _active_reservations(self) -> list[Reservation]:
        """락 안: 만료분을 회수한 뒤 현재 예약 목록.

        해제 누락(예외·프로세스 이상)으로 예약이 남으면 그 현금이 영구히 묶여
        매수가 통째로 막힌다. 과차단이 과주문보다는 낫지만 조용해선 안 되므로
        TTL 로 회수하고 반드시 경보를 남긴다.
        """
        if self._inflight and self.reservation_ttl_sec > 0:
            now = time.time()
            for sym, r in list(self._inflight.items()):
                if now - r.placed_at <= self.reservation_ttl_sec:
                    continue
                self._inflight.pop(sym, None)
                self._activity_gen += 1
                log.error("[예약 만료] %s %s x%s (id=%s, %.0f초 경과) — 강제 회수",
                          r.side, sym, r.qty, r.order_id, now - r.placed_at)
                self._emit_symbol("reservation_expired", sym, {
                    "symbol": sym, "side": r.side, "qty": r.qty,
                    "price": r.price, "order_id": r.order_id,
                    "age_sec": round(now - r.placed_at, 1)})
        return list(self._inflight.values())

    def reconcile(self, reconcile_fn: Callable[[PaperAccount], Any],
                  *, expect_gen: int | None = None) -> Any:
        """주기 재대사를 broker 락 안에서 실행 — gate.check/체결과 원자적으로 원장을 병합.

        reconcile_fn(account) 이 실계좌(holdings/buying-power)를 account.cash/positions 에
        병합한다(봇 관리 포지션의 thesis/손절은 보존, 고아는 채택). 락 밖에서 계좌를
        갈아끼우면 진행 중인 gate._invested 순회와 경합하므로 반드시 이 경로로만 병합한다.

        in-flight 주문(체결 폴링 중)이 있으면 apply 를 연기한다 — live holdings 가 이미
        체결을 반영한 뒤 _finish_live 가 apply_fill 을 중복 적용하는 레이스 방지.

        expect_gen 이 주어지면 fetch 직전 activity_generation() 과 같아야 한다. 조회
        동안 주문이 시작·끝나 inflight 가드에 안 걸려도, 낡은 API 스냅샷 apply 를 막는다.
        """
        with self._lock:
            if self._active_reservations():
                syms = sorted(self._inflight)
                log.debug("재대사 연기 — in-flight %s", syms)
                return {"deferred": True, "reason": "inflight", "inflight": syms}
            if expect_gen is not None and expect_gen != self._activity_gen:
                log.debug("재대사 연기 — stale snapshot expect_gen=%s now=%s",
                          expect_gen, self._activity_gen)
                return {"deferred": True, "reason": "stale_snapshot",
                        "expect_gen": expect_gen, "activity_gen": self._activity_gen}
            return reconcile_fn(self.account)

    def _ledger_already_has_fill(self, order: Order, filled_qty: float,
                                 qty_before: float | None) -> bool:
        """주기 재대사가 live holdings 로 이미 체결을 반영했는지(이중 apply_fill 방지)."""
        if qty_before is None:
            return False
        pos = self.account.position(order.symbol)
        eps = 1e-9
        if order.side == "BUY":
            expected = qty_before + filled_qty
            return abs(pos.qty - expected) < eps and abs(pos.qty - qty_before) > eps
        sell_qty = min(filled_qty, qty_before)
        expected = max(0.0, qty_before - sell_qty)
        return abs(pos.qty - expected) < eps and sell_qty > eps

    def _reject_inflight(self, order: Order, base_kw: dict) -> bool:
        """in-flight 거부. True 이면 last_result 설정됨."""
        self._active_reservations()          # 만료 회수 후 판정
        if order.symbol not in self._inflight:
            return False
        self.last_reject_reason = "동일 종목 주문 처리 중(in-flight)"
        log.info("[거부] %s %s — in-flight", order.side, order.symbol)
        self.last_result = ExecuteResult.rejected(self.last_reject_reason, **base_kw)
        return True

    def _begin_execute_locked(self, order: Order, reason: str,
                              base_kw: dict) -> dict | None:
        """락 안: 게이트·주문 접수까지. 라이브 prep(I/O)은 execute()에서 락 밖 선행."""
        self.last_reject_reason = ""
        self.last_result = None
        if self._reject_inflight(order, base_kw):
            return None

        decision = self.gate.check(order, self.account,
                                   reserved=self._active_reservations())
        if not decision.approved:
            self.last_reject_reason = decision.reason or "리스크게이트 거부"
            log.info("[거부] %s %s x%s @ %.2f — %s",
                     order.side, order.symbol, order.qty, order.price, decision.reason)
            self.last_result = ExecuteResult.rejected(
                self.last_reject_reason, **base_kw)
            return None

        if order.side == "BUY" and self.tradable_fn is not None:
            try:
                ok, block_reason = self.tradable_fn(order.symbol, order.market)
            except Exception as e:
                log.warning("[매수가드] 판정 예외(fail-open, 매수 허용) %s: %s", order.symbol, e)
                ok, block_reason = True, ""
            if not ok:
                self.last_reject_reason = block_reason or "매수가드 차단"
                log.warning("[매수차단] %s %s — %s", order.side, order.symbol, block_reason)
                self._emit("buy_blocked", order, {"symbol": order.symbol, "reason": block_reason})
                self.last_result = ExecuteResult.rejected(
                    self.last_reject_reason, **base_kw)
                return None

        qty_before = float(self.account.position(order.symbol).qty)
        if self.mode != "live":
            self._mark_inflight(order)
            return {"kind": "paper", "base_kw": base_kw, "qty_before": qty_before}

        order_id = self._place_live_order(order, reason)
        if order_id is None:
            return None
        self._mark_inflight(order, order_id)  # place 직후(락 안) — 중복 주문 race 차단
        return {"kind": "live", "order_id": order_id, "base_kw": base_kw,
                "qty_before": qty_before}

    def _finish_paper(self, order: Order, reason: str, base_kw: dict) -> ExecuteResult:
        fill = self.account.fill(order.symbol, order.market, order.side,
                                 order.qty, order.price, reason)
        log.info("[PAPER] %s %s x%s @ %.2f (fee %.2f) - %s",
                 fill.side, fill.symbol, fill.qty, fill.price, fill.fee, reason)
        self.last_result = ExecuteResult.from_fill(
            fill_qty=fill.qty, fill_price=fill.price, fee=fill.fee,
            status="FILLED", side=order.side, **base_kw)
        return self.last_result

    def _finish_live(self, order: Order, reason: str, order_id: str, base_kw: dict,
                     filled_qty: float, avg_px: float | None, fee: float,
                     status: str, *, qty_before: float | None = None,
                     exit_reason: str | None = None) -> ExecuteResult:
        if filled_qty > 0 and avg_px and avg_px > 0:
            if self._ledger_already_has_fill(order, filled_qty, qty_before):
                log.info("[LIVE] 체결 id=%s — 원장 이미 반영(재대사), apply_fill 스킵",
                         order_id)
            else:
                fill = self.account.apply_fill(order.symbol, order.market, order.side,
                                               filled_qty, avg_px, fee, reason)
                log.info("[LIVE] 체결 id=%s status=%s — %s %s x%s @ %.2f (fee %.2f) - %s",
                         order_id, status, fill.side, fill.symbol, fill.qty, fill.price,
                         fill.fee, reason)
            payload = {
                "symbol": order.symbol, "side": order.side, "qty": filled_qty,
                "price": avg_px, "fee": fee, "order_id": order_id,
                "status": status, "limit_price": order.price,
                "reason": reason or "",
            }
            if exit_reason:
                payload["exit_reason"] = exit_reason
            self._emit("live_order", order, payload)
            self.last_result = ExecuteResult.from_fill(
                fill_qty=filled_qty, fill_price=avg_px, fee=fee,
                order_qty=order.qty, limit_price=order.price,
                status=status, order_id=order_id, side=order.side)
            return self.last_result

        kind = "live_order_pending" if status in _PENDING else "live_order_error"
        log.warning("[LIVE] 미체결 id=%s status=%s — 원장 무변(주기 재대사가 반영): %s %s x%s",
                    order_id, status, order.side, order.symbol, order.qty)
        self._emit(kind, order,
                   {"symbol": order.symbol, "side": order.side, "qty": order.qty,
                    "order_id": order_id, "status": status, "reason": reason,
                    **({"exit_reason": exit_reason} if exit_reason else {})})
        self.last_result = ExecuteResult.rejected(
            f"미체결({status})", order_qty=order.qty, limit_price=order.price)
        return self.last_result

    def _execute_locked(self, order: Order, reason: str) -> ExecuteResult:
        """하위호환 — execute() 가 _begin_execute_locked/_finish_* 로 분리됨."""
        base_kw = {"order_qty": float(order.qty), "limit_price": float(order.price)}
        if self.mode == "live" and not self._prepare_live_order(order):
            if not self.last_reject_reason:
                self.last_reject_reason = "라이브 주문 준비 실패"
            self.last_result = ExecuteResult.rejected(self.last_reject_reason, **base_kw)
            return self.last_result
        prep = self._begin_execute_locked(order, reason, base_kw)
        if prep is None:
            return self.last_result
        if prep["kind"] == "paper":
            return self._finish_paper(order, reason, prep["base_kw"])
        filled_qty, avg_px, fee, status = self._reconcile_order(prep["order_id"])
        return self._finish_live(order, reason, prep["order_id"], prep["base_kw"],
                                 filled_qty, avg_px, fee, status)

    def _adopt_ledger_market(self, order: Order) -> None:
        """보유 종목이면 원장 symbol_market 을 market 권위로 삼는다.

        재대사가 실계좌 marketCountry 로 symbol_market 을 갱신하므로 보유분에 대해선
        이쪽이 사실이다. 상류에서 잘못된 라벨(예: 국내주에 US)이 붙으면 live_markets
        밖으로 판정돼 **청산이 조용히 스킵**된다 — 보유 중인데 못 파는 상태.
        """
        held = self.account.symbol_market.get(order.symbol)
        if not held or held == order.market:
            return
        if self.account.position(order.symbol).qty <= 0:
            return
        log.warning("[market 교정] %s %s: 주문 %s → 원장 %s",
                    order.side, order.symbol, order.market, held)
        self._emit("market_mismatch", order,
                   {"symbol": order.symbol, "side": order.side,
                    "ordered": order.market, "ledger": held})
        order.market = held

    def _prepare_live_order(self, order: Order) -> bool:
        """라이브 주문을 게이트 이전에 실조건으로 보정. 진행 가능하면 True.

        - client/account_seq 없거나 live_markets 밖이면 False(집행 스킵).
        - SELL: 실 매도가능 수량(get_sellable)으로 클램프 — 원장 드리프트로 인한 오버셀·
          고아 포지션을 막는다. 매도가능 0 이면 스킵.
        - 시간외 세션: 호가 스프레드가 상한을 넘으면 스킵(얇은 호가 방어).
        - 주문가: 호가북 마켓터블 리밋가로 갱신(없으면 기존 견적가 유지, 폴백).
        """
        if self.client is None or self.account_seq is None:
            log.error("live 모드인데 client/account_seq 가 없습니다. 집행 중단.")
            self.last_reject_reason = "라이브 client/account_seq 없음"
            return False
        self._adopt_ledger_market(order)
        if order.market not in self.live_markets:
            log.warning("[LIVE-차단] %s 시장은 live_markets(%s) 밖 — 주문 스킵 (%s %s x%s)",
                        order.market, self.live_markets, order.side, order.symbol, order.qty)
            self.last_reject_reason = f"live_markets 밖 ({order.market})"
            return False

        if order.side == "SELL":
            sellable = None
            try:
                resp = self.client.get_sellable(self.account_seq, order.symbol) or {}
                sellable = _num(resp.get("sellableQuantity"))
            except Exception as e:
                log.warning("[LIVE] 매도가능 수량 조회 실패 — 원장 수량으로 진행(%s): %s",
                            order.symbol, e)
            if sellable is not None:
                if sellable <= 0:
                    log.warning("[LIVE] %s 매도가능 0 — 매도 스킵", order.symbol)
                    self.last_reject_reason = "매도가능 0"
                    self._emit("sell_skipped", order,
                               {"symbol": order.symbol, "reason": "sellable=0"})
                    return False
                if sellable < order.qty:
                    log.warning("[LIVE] %s 매도수량 클램프 %s→%s (실 매도가능)",
                                order.symbol, order.qty, sellable)
                    order.qty = sellable

        # 호가북은 여기서 1번만 조회해 스프레드 가드와 리밋가 산정이 함께 쓴다(MARKET_DATA 절약).
        ob = self._fetch_orderbook(order.symbol)
        if not self._spread_ok(order, ob):
            self.last_reject_reason = "시간외 스프레드 초과"
            return False

        if ob is not None:                    # 조회 실패면 견적가 유지(기존 폴백 동작)
            px = self._marketable_limit(order, ob)
            if px and px > 0:
                order.price = px
        return True

    def _fetch_orderbook(self, symbol: str) -> dict | None:
        """호가북 조회. 실패하면 None(호출측이 폴백/가드 미적용으로 처리)."""
        try:
            return self.client.orderbook(symbol) or {}
        except Exception as e:
            log.warning("[LIVE] 호가 조회 실패 → 리밋가 폴백(견적가 사용) %s: %s", symbol, e)
            return None

    def _spread_ok(self, order: Order, ob: dict | None) -> bool:
        """시간외 세션 스프레드 가드. 주문을 내도 되면 True.

        정규장(current_session == "regular")에는 절대 발동하지 않는다. 시간외에서만
        (ask-bid)/중간가 가 max_spread_pct_extended 를 넘으면 False(주문 스킵) +
        wide_spread_skip 이벤트. 호가북 조회 실패·한쪽 호가 없음 등으로 스프레드를
        계산할 수 없으면 가드를 적용하지 않는다(가드 오작동으로 정상 주문을 막는 게 더 나쁘다).
        """
        if self.max_spread_pct_extended <= 0:
            return True
        session = current_session(order.market)
        if session == "regular":
            return True
        ask = self._best_price(ob, "asks")
        bid = self._best_price(ob, "bids")
        if not ask or not bid or ask <= 0 or bid <= 0:
            return True                       # 스프레드 계산 불가 → 통과(기존 폴백 동작 유지)
        mid = (ask + bid) / 2
        spread = (ask - bid) / mid if mid > 0 else 0.0
        if spread <= self.max_spread_pct_extended:
            return True
        log.warning("[LIVE] %s 시간외(%s) 스프레드 %.2f%% > 상한 %.2f%% — 주문 스킵 (%s x%s)",
                    order.symbol, session, spread * 100,
                    self.max_spread_pct_extended * 100, order.side, order.qty)
        self._emit("wide_spread_skip", order,
                   {"symbol": order.symbol, "side": order.side, "qty": order.qty,
                    "session": session, "bid": bid, "ask": ask, "spread_pct": spread,
                    "limit_pct": self.max_spread_pct_extended})
        return False

    @staticmethod
    def _best_price(ob: dict | None, key: str) -> float | None:
        """호가북에서 최우선호가(asks[0]/bids[0])의 가격. 없으면 None."""
        levels = (ob or {}).get(key) or []
        if not isinstance(levels, (list, tuple)):
            return None                       # 형태가 예상과 다르면 가드 미적용(통과)
        for lv in levels:
            px = _num(lv.get("price")) if isinstance(lv, dict) else None
            if px and px > 0:
                return px
        return None

    def _marketable_limit(self, order: Order, ob: dict | None = None) -> float | None:
        """호가북 기반 마켓터블 리밋가. 없으면 None(견적가 폴백).

        BUY 는 매도호가(asks, 오름차순)를, SELL 은 매수호가(bids, 내림차순)를 수량만큼
        훑되 최우선호가 대비 limit_slippage_pct 안의 레벨까지만 본다. 반환값은 항상 실제
        호가 레벨가(유효 틱)라 지정가 거부가 없다. 얕은 호가로 전량을 못 덮으면 그 안에서
        가장 깊은 레벨가(부분체결분만 잡히고 잔량은 주기 재대사가 반영).

        ob 를 주면 그 호가북을 쓴다(호출측이 이미 조회한 경우 — 중복 호출 방지).
        """
        if ob is None:
            ob = self._fetch_orderbook(order.symbol)
        if ob is None:
            return None
        levels = ob.get("asks" if order.side == "BUY" else "bids") or []
        parsed: list[tuple[float, float]] = []
        for lv in levels:
            px, vol = _num(lv.get("price")), _num(lv.get("volume"))
            if px and px > 0 and vol and vol > 0:
                parsed.append((px, vol))
        if not parsed:
            return None
        best = parsed[0][0]
        cap = (best * (1 + self.limit_slippage_pct) if order.side == "BUY"
               else best * (1 - self.limit_slippage_pct))
        picked, covered = best, 0.0
        for px, vol in parsed:
            if order.side == "BUY" and px > cap:
                break
            if order.side == "SELL" and px < cap:
                break
            picked, covered = px, covered + vol
            if covered >= order.qty:
                break
        return picked

    def _place_live_order(self, order: Order, reason: str) -> str | None:
        """라이브 주문 접수만(락 안). orderId 또는 None(실패 시 last_result 설정)."""
        base_kw = {"order_qty": float(order.qty), "limit_price": float(order.price)}
        try:
            resp = self.client.place_order(
                account_seq=self.account_seq, symbol=order.symbol, side=order.side,
                qty=order.qty, order_type="LIMIT", price=order.price)
        except Exception as e:
            log.error("[LIVE] 주문 전송 실패 — %s %s x%s @ %.2f: %s",
                      order.side, order.symbol, order.qty, order.price, e)
            self._emit("live_order_error", order, {"error": str(e), "reason": reason})
            self.last_result = ExecuteResult.rejected(
                "주문 전송 실패", **base_kw)
            return None

        order_id = self._order_id(resp)
        if order_id is None:
            log.error("[LIVE] 주문 응답에 주문식별자(orderId) 없음 — 실패 처리: %s", resp)
            self._emit("live_order_error", order,
                       {"error": "응답에 orderId 없음", "resp": str(resp)[:300],
                        "reason": reason})
            self.last_result = ExecuteResult.rejected("orderId 없음", **base_kw)
            return None
        return order_id

    def _reconcile_order(self, order_id: str) -> tuple[float, float | None, float, str]:
        """주문을 폴링해 (체결수량, 평균체결가, 수수료+세금, status).

        종결(FILLED/거부/취소) 또는 체결분 발생 시 조기 종료. 조회 실패가 반복되면
        (0, None, 0, 'UNKNOWN') — 호출측이 원장 무변으로 처리하고 주기 재대사에 맡긴다.
        """
        last: dict | None = None
        status = "UNKNOWN"
        for _ in range(max(1, self.reconcile_poll_attempts)):
            try:
                last = self.client.get_order(self.account_seq, order_id) or {}
            except Exception as e:
                log.warning("[LIVE] 주문 조회 실패(재시도) id=%s: %s", order_id, e)
                time.sleep(self.reconcile_poll_sec)
                continue
            status = str(last.get("status") or "UNKNOWN")
            fq = _num((last.get("execution") or {}).get("filledQuantity"))
            if status in _TERMINAL or (fq and fq > 0):
                break
            time.sleep(self.reconcile_poll_sec)
        ex = (last or {}).get("execution") or {}
        fq = _num(ex.get("filledQuantity")) or 0.0
        avg = _num(ex.get("averageFilledPrice"))
        fee = (_num(ex.get("commission")) or 0.0) + (_num(ex.get("tax")) or 0.0)
        return fq, avg, fee, status

    @staticmethod
    def _order_id(resp) -> str | None:
        """토스 주문 응답에서 주문 식별자를 뽑는다. 없으면 None(=실패 신호)."""
        if isinstance(resp, dict):
            for k in _ORDER_ID_KEYS:
                v = resp.get(k)
                if v:
                    return str(v)
        return None

    def _emit(self, kind: str, order: Order, payload: dict) -> None:
        """store 가 있으면 라이브 주문 이벤트 기록(없으면 로그만). 기록 실패는 삼킨다."""
        self._emit_symbol(kind, order.symbol, payload)

    def _emit_symbol(self, kind: str, symbol: str, payload: dict) -> None:
        if self.store is None:
            return
        try:
            self.store.log_event(kind, symbol, payload)
        except Exception as e:
            log.warning("store 이벤트 기록 실패(무시) [%s %s]: %s", kind, symbol, e)

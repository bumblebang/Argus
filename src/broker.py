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
from .risk_gate import RiskGate, Order
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
                 reconcile_poll_sec: float = 0.4):
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

    def execute(self, order: Order, reason: str) -> ExecuteResult:
        with self._lock:
            return self._execute_locked(order, reason)

    def reconcile(self, reconcile_fn: Callable[[PaperAccount], Any]) -> Any:
        """주기 재대사를 broker 락 안에서 실행 — gate.check/체결과 원자적으로 원장을 병합.

        reconcile_fn(account) 이 실계좌(holdings/buying-power)를 account.cash/positions 에
        병합한다(봇 관리 포지션의 thesis/손절은 보존, 고아는 채택). 락 밖에서 계좌를
        갈아끼우면 진행 중인 gate._invested 순회와 경합하므로 반드시 이 경로로만 병합한다.
        """
        with self._lock:
            return reconcile_fn(self.account)

    def _execute_locked(self, order: Order, reason: str) -> ExecuteResult:
        self.last_reject_reason = ""
        self.last_result = None
        base_kw = {"order_qty": float(order.qty), "limit_price": float(order.price)}
        # 라이브: 게이트 '이전에' 실체결 조건을 반영한다 — SELL 은 실 매도가능 수량으로
        # 클램프, 주문가는 호가북 마켓터블 리밋가로 갱신. 그래야 하드 게이트(notional·비중·
        # 매도수량)가 명목 견적가가 아닌 실체결 상한/실보유를 검증한다.
        if self.mode == "live":
            if not self._prepare_live_order(order):
                if not self.last_reject_reason:
                    self.last_reject_reason = "라이브 주문 준비 실패"
                self.last_result = ExecuteResult.rejected(
                    self.last_reject_reason, **base_kw)
                return self.last_result

        decision = self.gate.check(order, self.account)
        if not decision.approved:
            self.last_reject_reason = decision.reason or "리스크게이트 거부"
            log.info("[거부] %s %s x%s @ %.2f — %s",
                     order.side, order.symbol, order.qty, order.price, decision.reason)
            self.last_result = ExecuteResult.rejected(
                self.last_reject_reason, **base_kw)
            return self.last_result

        # 매수 안전가드(BUY 한정): 부적격 종목이면 게이트 통과 후에도 최종 차단한다. SELL 은
        # 가드하지 않는다(위험 종목이어도 청산=위험축소는 허용). 판정 예외는 fail-open(매수 허용).
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
                return self.last_result

        if self.mode == "live":
            self.last_result = self._execute_live(order, reason)
            return self.last_result

        fill = self.account.fill(order.symbol, order.market, order.side,
                                 order.qty, order.price, reason)
        log.info("[PAPER] %s %s x%s @ %.2f (fee %.2f) - %s",
                 fill.side, fill.symbol, fill.qty, fill.price, fill.fee, reason)
        self.last_result = ExecuteResult.from_fill(
            fill_qty=fill.qty, fill_price=fill.price, fee=fill.fee,
            status="FILLED", **base_kw)
        return self.last_result

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

    def _execute_live(self, order: Order, reason: str) -> ExecuteResult:
        """라이브 실주문 집행. _prepare_live_order + 게이트 통과 후 호출된다(지정가).

        주문 접수 후 get_order 로 체결을 대사해 **실체결 수량·평균가·수수료·세금**으로만
        원장을 기록한다 — 거부/미체결(orderId 없음·펜딩)은 원장에 남지 않고, 부분체결은
        체결분만 기록한다(잔량은 주기 재대사가 반영). 이로써 원장이 실계좌를 정확히 미러한다.
        """
        try:
            resp = self.client.place_order(
                account_seq=self.account_seq, symbol=order.symbol, side=order.side,
                qty=order.qty, order_type="LIMIT", price=order.price)
        except Exception as e:
            log.error("[LIVE] 주문 전송 실패 — %s %s x%s @ %.2f: %s",
                      order.side, order.symbol, order.qty, order.price, e)
            self._emit("live_order_error", order, {"error": str(e), "reason": reason})
            return ExecuteResult.rejected("주문 전송 실패", order_qty=order.qty,
                                          limit_price=order.price)

        order_id = self._order_id(resp)
        if order_id is None:
            log.error("[LIVE] 주문 응답에 주문식별자(orderId) 없음 — 실패 처리: %s", resp)
            self._emit("live_order_error", order,
                       {"error": "응답에 orderId 없음", "resp": str(resp)[:300],
                        "reason": reason})
            return ExecuteResult.rejected("orderId 없음", order_qty=order.qty,
                                          limit_price=order.price)

        filled_qty, avg_px, fee, status = self._reconcile_order(order_id)
        if filled_qty > 0 and avg_px and avg_px > 0:
            fill = self.account.apply_fill(order.symbol, order.market, order.side,
                                           filled_qty, avg_px, fee, reason)
            log.info("[LIVE] 체결 id=%s status=%s — %s %s x%s @ %.2f (fee %.2f) - %s",
                     order_id, status, fill.side, fill.symbol, fill.qty, fill.price,
                     fill.fee, reason)
            self._emit("live_order", order,
                       {"symbol": order.symbol, "side": order.side, "qty": filled_qty,
                        "price": avg_px, "fee": fee, "order_id": order_id,
                        "status": status, "limit_price": order.price})
            return ExecuteResult.from_fill(
                fill_qty=filled_qty, fill_price=avg_px, fee=fee,
                order_qty=order.qty, limit_price=order.price,
                status=status, order_id=order_id)

        # 미체결(펜딩/거부/취소) — 원장 무변. 펜딩은 주기 재대사가 이후 체결을 반영한다.
        kind = "live_order_pending" if status in _PENDING else "live_order_error"
        log.warning("[LIVE] 미체결 id=%s status=%s — 원장 무변(주기 재대사가 반영): %s %s x%s",
                    order_id, status, order.side, order.symbol, order.qty)
        self._emit(kind, order,
                   {"symbol": order.symbol, "side": order.side, "qty": order.qty,
                    "order_id": order_id, "status": status, "reason": reason})
        return ExecuteResult.rejected(
            f"미체결({status})", order_qty=order.qty, limit_price=order.price)

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
        if self.store is None:
            return
        try:
            self.store.log_event(kind, order.symbol, payload)
        except Exception as e:
            log.warning("store 이벤트 기록 실패(무시) [%s %s]: %s", kind, order.symbol, e)

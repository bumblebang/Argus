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
# place 성공 뒤 get_order 폴링이 전부 실패하면 status=UNKNOWN. _PENDING 에 없어
# 레지스트리·예약을 건너뛰면 J1(이중지출)·J2(재발주)가 동시에 풀린다.
# REJECTED 등 확정 종결은 여기 넣지 않는다.
_TRACK_WORKING = _PENDING | {"UNKNOWN"}


def _more_aggressive(side: str, new_px: float, old_px: float) -> bool:
    """재지정가가 기존 미체결보다 공격적인지. SELL 은 더 낮은 가, BUY 는 더 높은 가."""
    if old_px <= 0 or new_px <= 0:
        return False
    if str(side).upper() == "SELL":
        return new_px < old_px
    return new_px > old_px


def _num(v: Any) -> float | None:
    """토스 문자열 수치("70000")를 float 로. 빈값/None/비정상은 None."""
    if v is None:
        return None
    try:
        s = str(v).strip()
        return float(s) if s else None
    except (TypeError, ValueError):
        return None


def _parse_execution(info: dict | None) -> tuple[str, float, float | None, float]:
    """주문 조회 응답 -> (status, 누적체결수량, 누적평균체결가, 누적 수수료+세금)."""
    ex = (info or {}).get("execution") or {}
    return (str((info or {}).get("status") or "UNKNOWN"),
            _num(ex.get("filledQuantity")) or 0.0,
            _num(ex.get("averageFilledPrice")),
            (_num(ex.get("commission")) or 0.0) + (_num(ex.get("tax")) or 0.0))


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
                 reservation_ttl_sec: float = 300.0,
                 working_order_ttl_sec: float = 60.0,
                 block_on_working_order: bool = True,
                 attribution_ttl_sec: float = 1800.0,
                 working_order_abandon_ttl_sec: float = 1800.0):
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
        # 미체결 주문 방치 시간. 넘으면 취소한다. 0 이면 즉시 취소, 음수면 취소 안 함.
        # 즉시 취소는 얇은 호가·시간외에서 정상 체결 기회를 버리므로 기본은 유예.
        self.working_order_ttl_sec = float(working_order_ttl_sec)
        # 미체결 주문이 살아 있는 종목에 재발주를 막는다(매 틱 중복 발주 차단).
        self.block_on_working_order = bool(block_on_working_order)
        # 종결됐지만 원장 귀속(J3)이 안 된 체결분을 얼마나 들고 있을지. 재대사가
        # 수량 감소를 못 보면 영구히 남으므로 만료 회수(+경보). 음수면 무제한.
        self.attribution_ttl_sec = float(attribution_ttl_sec)
        # 미체결(settled_at 없음) 행 강제 회수. 취소 실패·조회 불능이어도 이 시간이
        # 지나면 레지스트리에서 버리고 경보 — 한 행이 매수여력을 영구 홀드하면
        # 전 종목 매수가 죽는다. 음수면 비활성(구동작). 0 이면 즉시 회수.
        self.working_order_abandon_ttl_sec = float(working_order_abandon_ttl_sec)
        # 주문 시작·종료마다 증가. 재대사 API 조회(락 밖) 중 주문이 시작·끝나
        # apply 시점 inflight 가 비어도, 조회 스냅샷이 낡은지 판별한다.
        self._activity_gen: int = 0
        # 직전 execute 가 거부된 사유(한글). 성공 시 "". 저널/이벤트가 thesis 대신 기록.
        self.last_reject_reason: str = ""
        self.last_result: ExecuteResult | None = None
        # 재대사가 실계좌 buying_power 로 cash 를 덮은 시각. 그 이전 미체결 BUY 는
        # 이미 BP 에 홀드돼 있어 _working_reservations 에서 빼면 이중 차감(과차단).
        self._cash_reconciled_at: float | None = None

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

    def _prune_expired_reservations(self) -> None:
        """락 안: 만료된 in-flight 예약 회수.

        해제 누락(예외·프로세스 이상)으로 예약이 남으면 그 현금이 영구히 묶여
        매수가 통째로 막히고 재대사까지 연기된다. 과차단이 과주문보다는 낫지만
        조용해선 안 되므로 TTL 로 회수하고 반드시 경보를 남긴다.
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

    def _active_reservations(self) -> list[Reservation]:
        """락 안: 게이트에 넘길 예약 목록 = in-flight + 미체결 잔량."""
        self._prune_expired_reservations()
        self._prune_abandoned_working_orders()
        return list(self._inflight.values()) + self._working_reservations()

    def _working_age(self, row: dict, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return now - float(row.get("placed_at") or now)

    def _should_abandon_working(self, row: dict, now: float | None = None) -> bool:
        """미체결 행을 강제 회수할지. settled 행은 _expire_settled 담당."""
        if self.working_order_abandon_ttl_sec < 0:
            return False
        if row.get("settled_at"):
            return False
        return self._working_age(row, now) >= self.working_order_abandon_ttl_sec

    def _abandon_working_order(self, row: dict, now: float, *, why: str) -> None:
        """취소·조회가 안 되는 미체결 행을 레지스트리에서 버리고 경보.

        증권사 쪽 주문이 아직 살아 있을 수 있다(고아 위험). 그래도 한 행이
        매수여력을 영구 홀드해 전 종목 매수를 죽이는 쪽이 더 비싸다 — inflight
        reservation_ttl 과 같은 취지. 이벤트·에러 로그로 조용히 넘어가지 않는다.
        """
        oid = row["order_id"]
        age = self._working_age(row, now)
        log.error("[미체결 강제회수] %s %s x%s @ %s (id=%s, %.0f초, %s) — 예약 해제",
                  row.get("side"), row.get("symbol"), row.get("qty"),
                  row.get("price"), oid, age, why)
        self._store_call(self.store.delete_working_order, oid)
        self._emit_symbol("working_order_abandoned", row.get("symbol"), {
            "order_id": oid, "side": row.get("side"), "qty": row.get("qty"),
            "price": row.get("price"), "filled_qty": row.get("filled_qty"),
            "status": row.get("status"), "age_sec": round(age, 1), "why": why})

    def _prune_abandoned_working_orders(self) -> None:
        """락 안: abandon TTL 지난 미체결 행 회수(게이트 직전 방어).

        sweep 이 취소 실패만 반복하면 예약이 남는다. execute 경로에서도
        같은 TTL 로 비워 전 종목 매수 동결을 끊는다.
        """
        if self.store is None or self.working_order_abandon_ttl_sec < 0:
            return
        try:
            rows = self.store.get_working_orders(settled=False)
        except Exception:
            return
        now = time.time()
        for row in rows:
            if self._should_abandon_working(row, now):
                self._abandon_working_order(row, now, why="ttl_prune")

    def _working_reservations(self) -> list[Reservation]:
        """미체결 주문의 잔량도 예약으로 본다.

        접수된 주문은 증권사가 현금을 홀드하지만 로컬 원장 cash 는 그대로다.
        다음 재대사가 buying_power 를 실계좌 값으로 덮기 전까지, 다른 종목 주문이
        그 현금을 다시 쓸 수 있다. in-flight 로 이미 잡힌 종목은 중복 제외.

        재대사 **이후** 접수분(placed_at > _cash_reconciled_at)만 예약한다 — 그 이전
        미체결은 이미 실계좌 BP 에 홀드돼 cash 덮기에 반영됐으므로 또 빼면 과차단.
        """
        if self.store is None:
            return []
        try:
            rows = self.store.get_working_orders(settled=False)
        except Exception:
            return []
        out: list[Reservation] = []
        now = time.time()
        since = self._cash_reconciled_at
        for row in rows:
            # abandon 대상은 예약에서 제외(직전 prune 이 지웠어도 경합 대비).
            if self._should_abandon_working(row, now):
                continue
            if row["symbol"] in self._inflight:
                continue
            placed = float(row["placed_at"] or 0.0)
            if since is not None and placed <= since:
                continue
            remaining = float(row["qty"]) - float(row["filled_qty"] or 0.0)
            if remaining <= 0:
                continue
            out.append(Reservation(
                symbol=row["symbol"], market=row["market"], side=row["side"],
                qty=remaining, price=float(row["price"]),
                order_id=row["order_id"], placed_at=placed))
        return out

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
            # 연기 판정은 in-flight(폴링 중)만 본다. 미체결 주문으로 연기하면
            # buying_power 갱신이 막혀 오히려 원장이 더 오래 틀린다.
            self._prune_expired_reservations()
            if self._inflight:
                syms = sorted(self._inflight)
                log.debug("재대사 연기 — in-flight %s", syms)
                return {"deferred": True, "reason": "inflight", "inflight": syms}
            if expect_gen is not None and expect_gen != self._activity_gen:
                log.debug("재대사 연기 — stale snapshot expect_gen=%s now=%s",
                          expect_gen, self._activity_gen)
                return {"deferred": True, "reason": "stale_snapshot",
                        "expect_gen": expect_gen, "activity_gen": self._activity_gen}
            result = reconcile_fn(self.account)
            # deferred 가 아닌 적용분만 — cash 가 실계좌 BP 기준이 됐음을 표시.
            if not (isinstance(result, dict) and result.get("deferred")):
                self._cash_reconciled_at = time.time()
            return result

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

    # ── 미체결 주문 레지스트리 (J2) ────────────────────────────
    def _register_working_order(self, order: Order, order_id: str, status: str,
                                filled_qty: float, reason: str, *,
                                avg_px: float | None = None,
                                fee: float = 0.0) -> None:
        """미체결/부분체결을 영속 레지스트리에 남긴다.

        토스 API 에 미체결 주문 **목록** 조회가 없다(order_get 단건뿐). 프로세스가
        죽으면 접수된 주문을 다시 찾을 방법이 이 표뿐이므로, 인메모리로는 안 된다.

        filled_qty 는 이 시점 _finish_live 가 apply_fill 로 **이미 원장에 넣은**
        수량이다. applied_* 로 함께 박아 두면 이후 추가 체결분만 정확히 귀속할 수
        있다(J3) — 누적 평균가에서 반영분을 빼면 증분 실체결가가 나온다.
        """
        if self.store is None:
            return
        applied_notional = float(filled_qty) * float(avg_px or 0.0)
        try:
            self.store.upsert_working_order(
                order_id=order_id, symbol=order.symbol, market=order.market,
                side=order.side, qty=float(order.qty), price=float(order.price),
                status=status, filled_qty=float(filled_qty), filled_avg=avg_px,
                fee=float(fee), applied_qty=float(filled_qty),
                applied_notional=applied_notional, applied_fee=float(fee),
                reason=reason)
        except Exception as e:
            log.error("미체결 주문 기록 실패 — 고아 주문 위험 id=%s: %s", order_id, e)

    def _reject_working_order(self, order: Order, base_kw: dict) -> bool:
        """같은 종목·같은 방향 미체결이 있으면 재발주 거부.

        side 구분 없이 막으면 미체결 BUY 가 ExitExecutor 손절 SELL 을
        게이트보다 먼저 차단한다(실재현). 반대 방향은 통과.

        같은 방향이어도 (a) TTL 경과 (b) 더 공격적 재지정가 이면
        재대사 타이머를 기다리지 않고 여기서 취소 시도한다.
        working_order_ttl_sec=60 인데 sweep 만 reconcile_sec(기본 300)에
        묶이면 손절 재시도가 60~360초 밀리고, 가격이 더 빠져도 새 주문이
        안 나간다. 취소 실패 시에는 여전히 거부(이중 주문 방지).
        """
        if not self.block_on_working_order or self.store is None:
            return False
        try:
            rows = self.store.get_working_orders(
                order.symbol, side=order.side, settled=False)
        except Exception as e:
            log.warning("미체결 조회 실패(통과) %s: %s", order.symbol, e)
            return False
        if not rows:
            return False
        if self._try_release_same_side_working(order, rows):
            try:
                if not self.store.has_working_order(order.symbol, side=order.side):
                    return False
            except Exception as e:
                log.warning("미체결 재조회 실패(통과) %s: %s", order.symbol, e)
                return False
        self.last_reject_reason = "동일 종목·방향 미체결 주문 존재"
        log.info("[거부] %s %s — 같은 방향 미체결 대기 중", order.side, order.symbol)
        self.last_result = ExecuteResult.rejected(self.last_reject_reason, **base_kw)
        return True

    def _try_release_same_side_working(self, order: Order, rows: list) -> bool:
        """TTL 경과·공격 재지정가 working 을 execute 경로에서 즉시 취소."""
        if self.client is None or self.account_seq is None:
            return False
        now = time.time()
        released = False
        for row in rows:
            age = now - float(row.get("placed_at") or now)
            ttl_due = (self.working_order_ttl_sec >= 0
                       and age >= self.working_order_ttl_sec)
            aggressive = _more_aggressive(
                order.side, float(order.price), float(row.get("price") or 0.0))
            if not ttl_due and not aggressive:
                continue
            why = "ttl" if ttl_due else "aggressive_replace"
            log.info("[LIVE] 미체결 즉시 해제 시도(%s) id=%s %s %s @ %s → 새 %s",
                     why, row["order_id"], row["side"], row["symbol"],
                     row.get("price"), order.price)
            if self._cancel_and_confirm(row["order_id"], row):
                released = True
        return released

    def _cancel_opposing_working(self, order: Order) -> None:
        """반대 방향 미체결 취소(SELL→BUY). 실패해도 본 주문은 막지 않는다."""
        if order.side != "SELL" or self.store is None:
            return
        if self.client is None or self.account_seq is None:
            return
        try:
            rows = self.store.get_working_orders(
                order.symbol, side="BUY", settled=False)
        except Exception as e:
            log.warning("반대편 미체결 조회 실패 %s: %s", order.symbol, e)
            return
        for row in rows:
            log.info("[LIVE] 청산 전 반대편 BUY 취소 id=%s %s x%s @ %s",
                     row["order_id"], row["symbol"], row["qty"], row.get("price"))
            if not self._cancel_and_confirm(row["order_id"], row):
                log.warning("[LIVE] 반대편 BUY 취소 실패 — SELL 은 계속 id=%s",
                            row["order_id"])

    def _reject_inflight(self, order: Order, base_kw: dict) -> bool:
        """in-flight 거부(같은 방향만). True 이면 last_result 설정됨."""
        self._prune_expired_reservations()    # 만료 회수 후 판정
        cur = self._inflight.get(order.symbol)
        if cur is None:
            return False
        if str(cur.side).upper() != str(order.side).upper():
            # 미체결 폴링 중 BUY 가 손절 SELL 을 막지 않게.
            return False
        self.last_reject_reason = "동일 종목 주문 처리 중(in-flight)"
        log.info("[거부] %s %s — in-flight", order.side, order.symbol)
        self.last_result = ExecuteResult.rejected(self.last_reject_reason, **base_kw)
        return True

    def sweep_working_orders(self) -> dict:
        """레지스트리 정산 — 기동 시 1회 + 주기 재대사마다.

        상태를 재조회해 종결분을 정산하고, TTL 초과 미체결은 취소한다. **원장 수량은
        건드리지 않는다** — 체결 반영은 재대사(live holdings)가 단일 소유자이고,
        여기서 apply_fill 하면 이중 계상이 된다. 이 표는 (a) 재발주 차단,
        (b) 고아 주문 회수, (c) J3 귀속용 실체결가 출처의 세 역할을 한다.

        (c) 때문에 원장 미반영 체결분이 남은 종결 주문은 삭제하지 않고 settled_at
        만 찍는다. 재대사가 실체결가로 소비한 뒤 지운다. 소비되지 않은 채 오래
        남으면 attribution_ttl 로 버리고 unattributed_fill 을 남긴다.

        매도 주문 조회가 한 건이라도 실패하면 ``block_reconcile=True`` — 재대사가
        보유 감소를 먼저 흡수하면 settled 출처가 없어 손익이 영구 구멍 난다.
        """
        if self.store is None or self.client is None or self.account_seq is None:
            return {"skipped": True, "block_reconcile": False}
        try:
            rows = self.store.get_working_orders()
        except Exception as e:
            log.warning("미체결 목록 조회 실패: %s", e)
            return {"error": str(e), "block_reconcile": True}
        out = {"checked": 0, "settled": 0, "canceled": 0, "cancel_failed": 0,
               "working": 0, "awaiting_attribution": 0, "dropped": 0,
               "abandoned": 0, "fetch_failed": 0, "block_reconcile": False}
        now = time.time()
        for row in rows:
            oid = row["order_id"]
            if row.get("settled_at"):
                if self._expire_settled(row, now):
                    out["dropped"] += 1
                else:
                    out["awaiting_attribution"] += 1
                continue
            out["checked"] += 1
            info = self._fetch_order(oid)
            if info is None:
                if self._should_abandon_working(row, now):
                    self._abandon_working_order(row, now, why="fetch_failed")
                    out["abandoned"] += 1
                else:
                    out["working"] += 1
                    out["fetch_failed"] += 1
                    # SELL 체결가를 못 찍은 채 holdings 를 덮으면 귀속 대상(감소)이 사라진다.
                    if str(row.get("side") or "").upper() == "SELL":
                        out["block_reconcile"] = True
                continue
            status, filled, avg, fee = _parse_execution(info)
            self._store_call(self.store.update_working_order, oid, status=status,
                             filled_qty=filled, filled_avg=avg, fee=fee)
            if status in _TERMINAL:
                out["settled"] += 1
                if self._settle_or_drop(oid, row, filled, now):
                    out["awaiting_attribution"] += 1
                self._emit_symbol("working_order_settled", row["symbol"], {
                    "order_id": oid, "status": status, "filled_qty": filled,
                    "avg_price": avg, "qty": row["qty"], "side": row["side"]})
                continue
            age = now - float(row["placed_at"] or now)
            if self.working_order_ttl_sec < 0 or age < self.working_order_ttl_sec:
                # 취소 유예 중이라도 abandon TTL 이면 강제 회수(영구 동결 방지).
                if self._should_abandon_working(row, now):
                    self._abandon_working_order(row, now, why="ttl_no_cancel")
                    out["abandoned"] += 1
                else:
                    out["working"] += 1
                continue
            if self._cancel_and_confirm(oid, row):
                out["canceled"] += 1
            elif self._should_abandon_working(row, now):
                self._abandon_working_order(row, now, why="cancel_failed")
                out["abandoned"] += 1
            else:
                out["cancel_failed"] += 1
                out["working"] += 1
        return out

    def _settle_or_drop(self, order_id: str, row: dict,
                        filled: float, now: float) -> bool:
        """종결 주문 처리. 원장 미반영 체결분이 남았으면 귀속 대기로 보존(True)."""
        if filled - float(row.get("applied_qty") or 0.0) > 1e-9:
            self._store_call(self.store.update_working_order, order_id,
                             settled_at=now)
            return True
        self._store_call(self.store.delete_working_order, order_id)
        return False

    def _expire_settled(self, row: dict, now: float) -> bool:
        """귀속 대기분 만료 회수. 버렸으면 True.

        재대사가 수량 감소를 못 봤다는 뜻이다(직전 재대사가 이미 흡수, 또는 수동
        개입). 추정으로 채우지 않고 버리되 조용히 지우지는 않는다 — 실체결가를
        알았는데 원장에 못 넣었다는 기록이 남아야 리포트에서 구멍이 보인다.
        """
        if self.attribution_ttl_sec < 0:
            return False
        age = now - float(row.get("settled_at") or now)
        if age < self.attribution_ttl_sec:
            return False
        self._store_call(self.store.delete_working_order, row["order_id"])
        log.warning("[귀속 실패] %s %s 체결 %s @ %s — 재대사가 수량 감소를 못 봄(%.0f초)",
                    row["side"], row["symbol"], row["filled_qty"],
                    row.get("filled_avg"), age)
        self._emit_symbol("unattributed_fill", row["symbol"], {
            "order_id": row["order_id"], "side": row["side"],
            "filled_qty": row["filled_qty"], "applied_qty": row.get("applied_qty"),
            "avg_price": row.get("filled_avg"), "age_sec": round(age, 1)})
        return True

    def _fetch_order(self, order_id: str) -> dict | None:
        try:
            return self.client.get_order(self.account_seq, order_id) or {}
        except Exception as e:
            log.warning("미체결 주문 조회 실패 id=%s: %s", order_id, e)
            return None

    def _cancel_and_confirm(self, order_id: str, row: dict) -> bool:
        """취소 요청 후 재조회로 확인. 확인 못 하면 레지스트리에 남긴다(재발주 계속 차단)."""
        try:
            self.client.cancel_order(self.account_seq, order_id)
        except Exception as e:
            log.error("[LIVE] 미체결 취소 실패 id=%s (%s %s) — working 유지: %s",
                      order_id, row["side"], row["symbol"], e)
            self._emit_symbol("working_order_cancel_failed", row["symbol"],
                              {"order_id": order_id, "error": str(e)})
            return False
        info = self._fetch_order(order_id) or {}
        status, filled, avg, fee = _parse_execution(info)
        self._store_call(self.store.update_working_order, order_id, status=status,
                         filled_qty=filled, filled_avg=avg, fee=fee)
        if status not in _TERMINAL:
            log.warning("[LIVE] 취소 미확인 id=%s status=%s — working 유지", order_id, status)
            return False
        # 취소 전 일부 체결됐으면 실체결가를 귀속에 넘겨야 한다 — 바로 지우지 않는다.
        self._settle_or_drop(order_id, row, filled, time.time())
        log.info("[LIVE] 미체결 취소 확인 id=%s status=%s (체결 %s/%s)",
                 order_id, status, filled, row["qty"])
        self._emit_symbol("working_order_canceled", row["symbol"], {
            "order_id": order_id, "status": status, "filled_qty": filled,
            "qty": row["qty"], "side": row["side"]})
        return True

    @staticmethod
    def _store_call(fn, *args, **kw) -> None:
        try:
            fn(*args, **kw)
        except Exception as e:
            log.warning("미체결 레지스트리 갱신 실패(무시): %s", e)

    def _begin_execute_locked(self, order: Order, reason: str,
                              base_kw: dict) -> dict | None:
        """락 안: 게이트·주문 접수까지. 라이브 prep(I/O)은 execute()에서 락 밖 선행."""
        self.last_reject_reason = ""
        self.last_result = None
        self._prune_expired_reservations()
        self._prune_abandoned_working_orders()
        if self._reject_inflight(order, base_kw):
            return None
        if self._reject_working_order(order, base_kw):
            return None
        # 손절 SELL: 같은 종목 미체결 BUY 는 통과만으론 부족 — 잔여 매수가
        # 체결되면 청산 직후 다시 롱이 된다. best-effort 취소(실패해도 SELL 진행).
        self._cancel_opposing_working(order)

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
            # 부분체결은 잔량이 아직 살아 있다 — 레지스트리에 남겨 재발주를 막고
            # 만료 시 취소한다. 지금까지는 성공 반환 후 잔량을 잊었다.
            # UNKNOWN(조회 실패)도 잔량 추적 — 아니면 inflight 해제 후 J1/J2 공백.
            if status in _TRACK_WORKING and filled_qty < float(order.qty):
                self._register_working_order(order, order_id, status,
                                             filled_qty, reason,
                                             avg_px=avg_px, fee=fee)
            self.last_result = ExecuteResult.from_fill(
                fill_qty=filled_qty, fill_price=avg_px, fee=fee,
                order_qty=order.qty, limit_price=order.price,
                status=status, order_id=order_id, side=order.side)
            return self.last_result

        # UNKNOWN 도 표에 남긴다. 이벤트는 조회 실패를 드러내 live_order_error.
        kind = "live_order_pending" if status in _PENDING else "live_order_error"
        if status in _TRACK_WORKING:
            self._register_working_order(order, order_id, status, 0.0, reason)
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
        (0, None, 0, 'UNKNOWN') — 호출측은 원장 무변 + working_orders 등록으로
        J1/J2 를 유지하고, 상태 확정은 sweep/주기 재대사에 맡긴다.
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

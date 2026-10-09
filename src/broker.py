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

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
import json
import math
import threading
import time
import uuid
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
# REJECTED 등 확정 종결은 여기 넣지 않는다. QUARANTINED=취소 미확인 격리.
_TRACK_WORKING = _PENDING | {"UNKNOWN", "QUARANTINED"}
_LOCAL_ORDER_PREFIX = "local:"
_KST = timezone(timedelta(hours=9))
# local: 행 해소 — 서버 주문 목록에서 짝을 찾을 때 허용하는 시각 창(초).
# 전송 재시도(멱등키)가 수십 초 안에 끝나므로 넉넉히 잡는다. 시계 오차 여유 포함.
_LOCAL_MATCH_BEFORE_SEC = 30.0
_LOCAL_MATCH_AFTER_SEC = 300.0
# '없음' 확정용 느슨한 창 — 이 창에 같은 종목·방향 주문이 하나라도 있으면(가격 절삭·
# 시계 오차로 엄격 대조가 빗나갔을 수 있음) 지우지 않고 미확인으로 둔다.
_LOCAL_LOOSE_BEFORE_SEC = 300.0
_LOCAL_LOOSE_AFTER_SEC = 900.0
# 서버 목록 반영 지연 대비 — 이보다 어린 local 행은 '없음' 확정을 미룬다.
_LOCAL_ABSENT_GRACE_SEC = 60.0
_LOCAL_LIST_MAX_PAGES = 5


def _is_local_order_id(order_id: str | None) -> bool:
    return bool(order_id) and str(order_id).startswith(_LOCAL_ORDER_PREFIX)


def _is_blocking_working_status(status: str | None) -> bool:
    s = str(status or "").upper()
    return s in ("UNKNOWN", "QUARANTINED") or s.startswith("LOCAL")


# KRX 단일가(동시호가) 접수 창(KST 분) → 창 끝이 체결 시각. 이 창에 낸 지정가는 체결
# 시각 전에는 채워질 수 없다 — 60s TTL 로 취소하면 close_scan(15:20~) 매수가 영영
# 체결되지 않는다(취소가 415 로 전부 실패하던 동안 가려져 있었다).
_KR_CALL_AUCTIONS = ((8 * 60 + 50, 9 * 60), (15 * 60 + 20, 15 * 60 + 30))
# 체결 시각 뒤 여유 — 체결 통보·조회 반영 지연.
_CALL_AUCTION_GRACE_SEC = 120.0


def _call_auction_hold_until(market: str | None, placed_at: float) -> float | None:
    """KR 동시호가 창에 접수된 주문이면 TTL 취소를 미룰 시각(epoch), 아니면 None."""
    if str(market or "").upper() != "KR":
        return None
    dt = datetime.fromtimestamp(float(placed_at), tz=_KST)
    minute = dt.hour * 60 + dt.minute
    for start, end in _KR_CALL_AUCTIONS:
        if start <= minute < end:
            match = dt.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
            return match.timestamp() + _CALL_AUCTION_GRACE_SEC
    return None


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


def _row_meta(row: dict) -> dict:
    meta = row.get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (TypeError, ValueError):
            meta = None
    return meta if isinstance(meta, dict) else {}


def _parse_ts(v: Any) -> float | None:
    """ISO8601(오프셋 포함) → epoch. 실패하면 None."""
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_KST)
    return dt.timestamp()


def _same_num(a: Any, b: Any, tol: float = 1e-6) -> bool:
    x, y = _num(a), _num(b)
    if x is None or y is None:
        return False
    return abs(x - y) <= tol * max(1.0, abs(x), abs(y))


def _limit_price_matches(server_px: Any, sent_px: Any) -> bool:
    """서버 주문가가 보낸 지정가와 같은가. US 는 서버가 소수 자릿수를 절삭한다
    ($1 이상 둘째 자리, 미만 넷째 자리 — 스펙 v1.2.17) — 절삭값도 같은 주문으로 본다."""
    if _same_num(server_px, sent_px):
        return True
    x, y = _num(server_px), _num(sent_px)
    if x is None or y is None or y <= 0:
        return False
    digits = 2 if y >= 1 else 4
    q = Decimal(1).scaleb(-digits)
    trunc = float(Decimal(str(y)).quantize(q, rounding=ROUND_DOWN))
    return _same_num(x, trunc)


def _is_fractional_qty(qty: float) -> bool:
    """부동소수점 잡음을 제외하고 실제 소수점 수량인지 판별."""
    q = float(qty)
    return abs(q - round(q)) > 1e-9


def incremental_fill(filled_qty: float, avg_px: float, fee: float,
                     applied_qty: float = 0.0,
                     applied_notional: float = 0.0,
                     applied_fee: float = 0.0,
                     ) -> tuple[float, float, float] | None:
    """누적 체결(filled×avg)에서 미반영분의 (증분수량, 증분단가, 증분수수료).

    토스 averageFilledPrice 는 누적 VWAP 이라, 이미 원장에 넣은 분을 빼고
    남은 구간의 실체결가를 써야 한다. 오차는 부분체결 사이 가격 변동폭에 비례.
    미반영분이 없으면 None.
    """
    delta = float(filled_qty) - float(applied_qty or 0.0)
    if delta <= 1e-9 or not avg_px or float(avg_px) <= 0:
        return None
    inc_notional = float(avg_px) * float(filled_qty) - float(applied_notional or 0.0)
    if inc_notional <= 0:
        return None
    px = inc_notional / delta
    if px <= 0:
        return None
    inc_fee = max(0.0, float(fee or 0.0) - float(applied_fee or 0.0))
    return delta, px, inc_fee


def whole_share_buy_qty(order) -> float | None:
    """소수점 BUY 를 온주로 절사한 수량. 절사할 필요가 없으면 None.

    토스는 종목 단위로 소수점 거래를 막는다(422 `stock-restricted`). 1주 값이 예산보다
    싸면(=수량 1 이상) 소수점을 고집할 이유가 없으니 온주 지정가로 내린다. 수량 1 미만
    (1주 값 > 예산)일 때만 소수점 금액 주문이 유일한 매수 수단이라 그대로 둔다.
    SELL 은 보유한 소수점 잔량을 털어야 하므로 손대지 않는다.
    """
    if str(order.side).upper() != "BUY":
        return None
    q = float(order.qty)
    if not _is_fractional_qty(q) or q < 1:
        return None
    return float(math.floor(q))


def _usd_order_amount(qty: float, price: float) -> str:
    """소수점 BUY의 승인 명목을 센트 단위로 내림해 문자열로 반환."""
    amount = Decimal(str(qty)) * Decimal(str(price))
    return format(amount.quantize(Decimal("0.01"), rounding=ROUND_DOWN), "f")


def _field(obj: Any, *keys: str) -> Any:
    """dict 또는 객체에서 첫 매칭 필드. dict 가 아니면 getattr — AttributeError 없음."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if v is not None:
                return v
        return None
    for k in keys:
        try:
            v = getattr(obj, k)
        except AttributeError:
            continue
        if v is not None:
            return v
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
                 working_order_abandon_ttl_sec: float = 1800.0,
                 sync_stale_error_sec: float = 3600.0):
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
        # 적정가에서 멀면 그대로 나쁜 가격에 체결된다. 0 이하면 가드 비활성.
        # 정규장 온주는 미적용. 단 소수점(정규장 한정·시장가)은 정규장에도 적용.
        self.max_spread_pct_extended = float(max_spread_pct_extended)
        # 라이브 체결 대사 폴링(주문 접수 후 실체결 수량·평균가·수수료를 읽어 원장에 반영).
        self.reconcile_poll_attempts = int(reconcile_poll_attempts)
        self.reconcile_poll_sec = float(reconcile_poll_sec)
        # 뇌 워커(진입)와 감시 루프(코드 청산)가 동시에 execute 할 수 있어 직렬화.
        # 주기 재대사(reconcile)도 이 락을 잡아 gate.check/체결과 원자적으로 계좌를 병합한다.
        self._lock = threading.Lock()
        # 접수됐지만 원장 미반영인 주문 {reservation_id: Reservation}.
        # 키는 uuid(심볼이 아님) — 반대 방향 주문이 서로 예약을 덮지 않게.
        self._inflight: dict[str, Reservation] = {}
        # upsert_working_order 실패 시 메모리에 남겨 심볼 양방향 차단 + heartbeat 노출.
        self._register_failed_symbols: set[str] = set()
        # 이미 QUARANTINED 행의 quarantine_alert 이벤트 스로틀(종목:oid → last emit ts).
        self._quarantine_alert_ts: dict[str, float] = {}
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
        # 시장별로 재대사가 실계좌 buying_power 로 cash 를 덮은 시각. 그 이전 미체결
        # BUY 는 이미 BP 에 홀드돼 있어 현금 예약에서 빼야 한다(이중 차감). 시장 단위 —
        # US BP 조회가 실패한 재대사가 US 예약까지 풀면 같은 현금을 두 번 쓴다.
        self._cash_reconciled_at: dict[str, float] = {}
        # 실계좌 조회 건강도. 조회가 실패해도 봇은 마지막 성공 스냅샷으로 계속 돌지만,
        # 그 사실이 어디에도 안 남으면 게이트·사이징이 낡은 값을 진실로 믿는다.
        self.sync_health: dict = {
            "cash_ok": True, "holdings_ok": True, "failed_markets": [],
            "errors": {}, "last_ok_ts": None, "consecutive_failures": 0}
        # 마지막 성공 조회가 이보다 오래되면 로그·이벤트를 error 로 승급.
        self.sync_stale_error_sec = float(sync_stale_error_sec)

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
        whole = whole_share_buy_qty(order)
        if whole is not None:
            # 사이징을 안 거친 경로(뇌 지정 수량 등)까지 막는 마지막 관문.
            log.info("[주문] 소수점 매수 → 온주 절사 %s %s %.4f → %.0f주"
                     " (소수점 거래 제한 종목 대비)",
                     order.market, order.symbol, order.qty, whole)
            self._emit_symbol("fractional_floored", order.symbol, {
                "market": order.market, "side": order.side,
                "requested_qty": float(order.qty), "qty": whole,
                "price": float(order.price)})
            order.qty = whole
        base_kw = {"order_qty": float(order.qty), "limit_price": float(order.price)}

        with self._lock:
            if self._reject_inflight(order, base_kw):
                return self.last_result

        if self.mode == "live":
            if not self._prepare_live_order(order, exit_reason=exit_reason):
                with self._lock:
                    if not self.last_reject_reason:
                        self.last_reject_reason = "라이브 주문 준비 실패"
                    # prepare 가 qty/price 를 바꿨을 수 있음 — 거부 스냅샷도 최신값.
                    base_kw = {"order_qty": float(order.qty),
                               "limit_price": float(order.price)}
                    self.last_result = ExecuteResult.rejected(
                        self.last_reject_reason, **base_kw)
                return self.last_result
            base_kw = {"order_qty": float(order.qty),
                       "limit_price": float(order.price)}

        with self._lock:
            prep = self._begin_execute_locked(order, reason, base_kw)
        if prep is None:
            return self.last_result

        rid = prep.get("reservation_id")
        if prep["kind"] == "paper":
            try:
                with self._lock:
                    res = self._finish_paper(order, reason, prep["base_kw"])
                    self._mirror_after_fill(mirror_st, order, res, armed_id, plan_fn, exit_reason)
                    return res
            finally:
                with self._lock:
                    self._clear_inflight(rid)

        try:
            filled_qty, avg_px, fee, status = self._reconcile_order(prep["order_id"])
            with self._lock:
                res = self._finish_live(
                    order, reason, prep["order_id"], prep["base_kw"],
                    filled_qty, avg_px, fee, status,
                    qty_before=prep.get("qty_before"),
                    avg_before=prep.get("avg_before"),
                    exit_reason=exit_reason)
                self._mirror_after_fill(mirror_st, order, res, armed_id, plan_fn, exit_reason)
                return res
        finally:
            with self._lock:
                self._clear_inflight(rid)

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
        """기동 동기화 — sweep → API fetch(락 밖) → apply( run_locked ).

        종료 중·재기동 전 체결분이 working 에 남아 있으면 holdings 만 덮을 때
        BUY applied·SELL 귀속이 영구히 어긋난다. 주기 재대사와 같이 **sweep 을
        먼저** 돌린 뒤 동기화한다.

        sweep 이 ``block_reconcile`` 이면 매도 귀속 대기 심볼의 holdings 덮기를
        보류한다(다음 주기 재대사). 그 심볼만 덮으면 실현손익 구멍이 난다.
        """
        from .broker_sync import apply_sync_from_live, fetch_live_account_data
        sw: dict = {}
        try:
            sw = self.sweep_working_orders()
        except Exception as e:
            log.warning("기동 sweep 실패(동기화는 계속): %s", e)
            sw = {"error": str(e), "block_reconcile": True}
        defer = bool(sw.get("block_reconcile"))
        defer_syms = set(sw.get("defer_symbols") or [])
        if defer:
            log.warning("기동 sync: sweep 미완(block_reconcile) — sell 귀속 대기 "
                        "심볼 holdings 덮기 보류(fetch_failed=%s)",
                        sw.get("fetch_failed"))
        elif defer_syms:
            log.warning("기동 sync: 매도 결과 불명 심볼 holdings 덮기 보류 — %s",
                        sorted(defer_syms))
        data = fetch_live_account_data(gateway, self.account_seq, markets=markets)
        self.note_sync_result(data)
        out = self.run_locked(
            lambda acct: apply_sync_from_live(
                acct, store, data, markets=markets,
                defer_sell_holdings=defer, defer_symbols=defer_syms))
        if isinstance(out, dict):
            out["sweep"] = sw
        return out

    def note_sync_result(self, data: dict) -> dict:
        """실계좌 조회 결과의 실패 비트를 sync_health 에 기록. 갱신된 health 반환.

        조회가 실패해도 주문을 막지는 않는다(마지막 성공 스냅샷으로 계속 운행).
        대신 실패했다는 사실과 마지막 성공 시각은 반드시 남긴다 — 5분 낡은 현금과
        6시간 낡은 현금이 구분되지 않으면 드리프트를 아무도 못 본다.
        """
        h = self.sync_health
        cash_ok = bool(data.get("cash_ok", True))
        holdings_ok = bool(data.get("holdings_ok", True))
        h["cash_ok"] = cash_ok
        h["holdings_ok"] = holdings_ok
        h["failed_markets"] = list(data.get("failed_markets") or [])
        h["errors"] = dict(data.get("errors") or {})
        if cash_ok and holdings_ok:
            h["last_ok_ts"] = time.time()
            h["consecutive_failures"] = 0
        else:
            h["consecutive_failures"] = int(h.get("consecutive_failures", 0)) + 1
        return dict(h)

    def sync_stale_sec(self, now: float | None = None) -> float | None:
        """마지막 성공 조회 이후 경과(초). 한 번도 성공한 적 없으면 None."""
        ts = self.sync_health.get("last_ok_ts")
        if not ts:
            return None
        return max(0.0, (now if now is not None else time.time()) - float(ts))

    def trading_health(self) -> dict:
        """heartbeat/alert_check 용 거래 건강도(시세 ok 와 별개)."""
        unknown: list[str] = []
        quarantined: list[str] = []
        store_error = None
        if self.store is not None:
            try:
                for row in self.store.get_working_orders(settled=False) or []:
                    sym = str(row.get("symbol") or "")
                    st = str(row.get("status") or "").upper()
                    oid = str(row.get("order_id") or "")
                    if st == "QUARANTINED":
                        if sym and sym not in quarantined:
                            quarantined.append(sym)
                    elif st == "UNKNOWN" or _is_local_order_id(oid):
                        if sym and sym not in unknown:
                            unknown.append(sym)
            except Exception as e:
                store_error = str(e)
        gate = self.gate
        halted = bool(gate and hasattr(gate, "is_globally_halted")
                      and gate.is_globally_halted())
        paused = []
        if gate and hasattr(gate, "pause_status"):
            ps = gate.pause_status()
            if ps and ps not in ("none", "ALL"):
                paused = ps.split("+")
            elif ps == "ALL":
                paused = ["KR", "US"]
        elif gate:
            for m in ("KR", "US"):
                if hasattr(gate, "is_market_paused") and gate.is_market_paused(m):
                    paused.append(m)
        return {
            "mode": self.mode,
            "halted": halted,
            "paused": paused,
            "sync_health": dict(self.sync_health),
            "unknown_symbols": sorted(unknown),
            "quarantined_symbols": sorted(quarantined),
            "register_failed_symbols": sorted(self._register_failed_symbols),
            "store_error": store_error,
        }

    def activity_generation(self) -> int:
        """주문 활동 세대(락 안 스냅샷). 재대사 fetch 직전 캡처용."""
        with self._lock:
            return self._activity_gen

    def _mark_inflight(self, order: Order, order_id: str | None = None) -> str:
        """락 안: 예약 등록 + activity_gen 증가. reservation_id(uuid) 반환."""
        rid = str(uuid.uuid4())
        self._inflight[rid] = Reservation(
            symbol=order.symbol, market=order.market, side=order.side,
            qty=float(order.qty), price=float(order.price),
            order_id=order_id, placed_at=time.time())
        self._activity_gen += 1
        return rid

    def _clear_inflight(self, reservation_id: str | None) -> None:
        """락 안: 자기 reservation_id 만 해제."""
        if not reservation_id:
            return
        if self._inflight.pop(reservation_id, None) is None:
            return
        self._activity_gen += 1

    def _inflight_for(self, symbol: str, side: str | None = None) -> list[tuple[str, Reservation]]:
        out: list[tuple[str, Reservation]] = []
        for rid, r in self._inflight.items():
            if r.symbol != symbol:
                continue
            if side is not None and str(r.side).upper() != str(side).upper():
                continue
            out.append((rid, r))
        return out

    def _prune_expired_reservations(self) -> None:
        """락 안: 만료된 in-flight 예약 회수.

        해제 누락(예외·프로세스 이상)으로 예약이 남으면 그 현금이 영구히 묶여
        매수가 통째로 막히고 재대사까지 연기된다. 과차단이 과주문보다는 낫지만
        조용해선 안 되므로 TTL 로 회수하고 반드시 경보를 남긴다.
        """
        if self._inflight and self.reservation_ttl_sec > 0:
            now = time.time()
            for rid, r in list(self._inflight.items()):
                if now - r.placed_at <= self.reservation_ttl_sec:
                    continue
                self._inflight.pop(rid, None)
                self._activity_gen += 1
                log.error("[예약 만료] %s %s x%s (id=%s, %.0f초 경과) — 강제 회수",
                          r.side, r.symbol, r.qty, r.order_id, now - r.placed_at)
                self._emit_symbol("reservation_expired", r.symbol, {
                    "symbol": r.symbol, "side": r.side, "qty": r.qty,
                    "price": r.price, "order_id": r.order_id,
                    "reservation_id": rid,
                    "age_sec": round(now - r.placed_at, 1)})

    def _active_reservations(self) -> list[Reservation]:
        """락 안: 게이트에 넘길 예약 목록 = in-flight + 미체결 잔량."""
        self._prune_expired_reservations()
        self._prune_abandoned_working_orders()
        return list(self._inflight.values()) + self._working_reservations()

    def _working_age(self, row: dict, now: float | None = None) -> float:
        now = time.time() if now is None else now
        return now - float(row.get("placed_at") or now)

    def _working_ttl_due(self, row: dict, now: float) -> bool:
        """미체결 TTL 취소 대상인지. KR 동시호가 접수분은 단일가 체결 시각까지 보류."""
        if self.working_order_ttl_sec < 0:
            return False
        if self._working_age(row, now) < self.working_order_ttl_sec:
            return False
        hold = _call_auction_hold_until(row.get("market"),
                                        float(row.get("placed_at") or now))
        return hold is None or now >= hold

    def _should_abandon_working(self, row: dict, now: float | None = None) -> bool:
        """미체결 행을 강제 회수할지. settled 행은 _expire_settled 담당."""
        if self.working_order_abandon_ttl_sec < 0:
            return False
        if row.get("settled_at"):
            return False
        return self._working_age(row, now) >= self.working_order_abandon_ttl_sec

    def _abandon_working_order(self, row: dict, now: float, *, why: str) -> None:
        """취소·조회가 안 되는 미체결 행을 QUARANTINED 로 격리(삭제 금지).

        증권사 쪽 주문이 아직 살아 있을 수 있다. 삭제하면 재발주·이중주문이 열린다.
        QUARANTINED 는 _TRACK_WORKING 에 포함 → 예약·양방향 차단 유지. 다음 sweep 이
        조회로 해소하고, 미해소면 경보를 반복한다.
        """
        oid = row["order_id"]
        status = str(row.get("status") or "").upper()
        if status == "QUARANTINED":
            # 이미 격리 — prune 재호출 시 이벤트 노이즈 방지. 경보만 주기적으로(3h).
            age = self._working_age(row, now)
            log.error("[미체결 격리 유지] %s %s (id=%s, %.0f초, %s)",
                      row.get("side"), row.get("symbol"), oid, age, why)
            key = f"{row.get('symbol')}:{oid}"
            last = float(self._quarantine_alert_ts.get(key) or 0)
            if now - last >= 3 * 3600:
                self._quarantine_alert_ts[key] = now
                self._emit_symbol("working_order_quarantine_alert", row.get("symbol"), {
                    "order_id": oid, "side": row.get("side"), "qty": row.get("qty"),
                    "price": row.get("price"), "filled_qty": row.get("filled_qty"),
                    "status": "QUARANTINED", "age_sec": round(age, 1), "why": why})
            return
        age = self._working_age(row, now)
        log.error("[미체결 격리] %s %s x%s @ %s (id=%s, %.0f초, %s) — QUARANTINED",
                  row.get("side"), row.get("symbol"), row.get("qty"),
                  row.get("price"), oid, age, why)
        self._store_call(self.store.update_working_order, oid, status="QUARANTINED")
        self._emit_symbol("working_order_quarantined", row.get("symbol"), {
            "order_id": oid, "side": row.get("side"), "qty": row.get("qty"),
            "price": row.get("price"), "filled_qty": row.get("filled_qty"),
            "status": "QUARANTINED", "age_sec": round(age, 1), "why": why})

    def _prune_abandoned_working_orders(self) -> None:
        """락 안: abandon TTL 지난 미체결 행 격리(게이트 직전 방어).

        이미 QUARANTINED 인 행은 건너뛴다(매 execute 재격리 이벤트 노이즈 방지).
        """
        if self.store is None or self.working_order_abandon_ttl_sec < 0:
            return
        try:
            rows = self.store.get_working_orders(settled=False)
        except Exception:
            return
        now = time.time()
        for row in rows:
            if str(row.get("status") or "").upper() == "QUARANTINED":
                continue
            if self._should_abandon_working(row, now):
                self._abandon_working_order(row, now, why="ttl_prune")

    def _working_reservations(self) -> list[Reservation]:
        """미체결 주문의 잔량도 예약으로 본다.

        접수된 주문은 증권사가 현금을 홀드하지만 로컬 원장 cash 는 그대로다.
        다음 재대사가 buying_power 를 실계좌 값으로 덮기 전까지, 다른 종목 주문이
        그 현금을 다시 쓸 수 있다.

        그 시장 재대사(BP 덮기) **이전** 접수분은 bp_held=True — 이미 실계좌 BP 에
        홀드돼 cash 덮기에 반영됐으므로 현금 차감에선 빠진다(과차단 방지). 다만 보유엔
        아직 없는 미체결 매수라 노출 한도(총익스포저·섹터·종목 수)에는 계속 들어간다.

        QUARANTINED(abandon) 행도 예약한다 — 증권사 쪽 주문이 살아 있을 수 있다.
        in-flight 중복 제외는 **같은 주문(order_id)** 만 — 같은 종목이라도 다른
        주문(예: SELL 처리 중 살아 있는 BUY 미체결)의 홀드를 지우면 안 된다.
        """
        if self.store is None:
            return []
        try:
            rows = self.store.get_working_orders(settled=False)
        except Exception:
            return []
        out: list[Reservation] = []
        inflight_ids = {str(r.order_id) for r in self._inflight.values() if r.order_id}
        for row in rows:
            if str(row.get("order_id") or "") in inflight_ids:
                continue
            placed = float(row["placed_at"] or 0.0)
            remaining = float(row["qty"]) - float(row["filled_qty"] or 0.0)
            if remaining <= 0:
                continue
            since = self._cash_reconciled_at.get(str(row["market"] or ""))
            out.append(Reservation(
                symbol=row["symbol"], market=row["market"], side=row["side"],
                qty=remaining, price=float(row["price"]),
                order_id=row["order_id"], placed_at=placed,
                bp_held=since is not None and placed <= since))
        return out

    def reconcile(self, reconcile_fn: Callable[[PaperAccount], Any],
                  *, expect_gen: int | None = None,
                  cash_markets: list[str] | None = None) -> Any:
        """주기 재대사를 broker 락 안에서 실행 — gate.check/체결과 원자적으로 원장을 병합.

        reconcile_fn(account) 이 실계좌(holdings/buying-power)를 account.cash/positions 에
        병합한다(봇 관리 포지션의 thesis/손절은 보존, 고아는 채택). 락 밖에서 계좌를
        갈아끼우면 진행 중인 gate._invested 순회와 경합하므로 반드시 이 경로로만 병합한다.

        in-flight 주문(체결 폴링 중)이 있으면 apply 를 연기한다 — live holdings 가 이미
        체결을 반영한 뒤 _finish_live 가 apply_fill 을 중복 적용하는 레이스 방지.

        expect_gen 이 주어지면 fetch 직전 activity_generation() 과 같아야 한다. 조회
        동안 주문이 시작·끝나 inflight 가드에 안 걸려도, 낡은 API 스냅샷 apply 를 막는다.

        cash_markets: 이번 조회에서 BP 를 받아 cash 를 덮은 시장. None 이면 결과의
        cash_markets / failed_markets 로 판단한다.
        """
        with self._lock:
            # 연기 판정은 in-flight(폴링 중)만 본다. 미체결 주문으로 연기하면
            # buying_power 갱신이 막혀 오히려 원장이 더 오래 틀린다.
            self._prune_expired_reservations()
            if self._inflight:
                syms = sorted({r.symbol for r in self._inflight.values()})
                log.debug("재대사 연기 — in-flight %s", syms)
                return {"deferred": True, "reason": "inflight", "inflight": syms}
            if expect_gen is not None and expect_gen != self._activity_gen:
                log.debug("재대사 연기 — stale snapshot expect_gen=%s now=%s",
                          expect_gen, self._activity_gen)
                return {"deferred": True, "reason": "stale_snapshot",
                        "expect_gen": expect_gen, "activity_gen": self._activity_gen}
            result = reconcile_fn(self.account)
            # deferred 가 아닌 적용분만 — cash 가 실계좌 BP 기준이 됐음을 표시.
            # BP 를 실제로 덮은 시장만(조회 실패 시장의 예약은 그대로 둔다).
            if not (isinstance(result, dict) and result.get("deferred")):
                now = time.time()
                for m in self._cash_markets(result, cash_markets):
                    self._cash_reconciled_at[m] = now
            return result

    def _cash_markets(self, result: Any, cash_markets) -> list[str]:
        """재대사가 BP 로 cash 를 덮은 시장. 명시가 없으면 결과의 실패 시장을 뺀다."""
        if cash_markets is not None:
            return [str(m) for m in cash_markets]
        if not isinstance(result, dict):
            return []
        if "cash_markets" in result:
            return [str(m) for m in (result.get("cash_markets") or [])]
        failed = {str(m) for m in (result.get("failed_markets") or [])}
        if result.get("cash_ok") is False and not failed:
            return []
        cash = result.get("cash")
        markets = cash.keys() if isinstance(cash, dict) else self.account.cash.keys()
        return [str(m) for m in markets if str(m) not in failed]

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

    def _sell_pnl_already_booked(self, order_id: str, symbol: str,
                                 filled_qty: float, avg_px: float) -> bool:
        """재대사(J3) 또는 이전 finish 가 이미 이 매도 손익을 저널에 넣었는지.

        세 다리: (1) finish_live_skip/order_id 저널 (2) J3 reason 에 order_id
        (3) working_orders.applied_qty ≥ filled. 가격 비교는 누적 VWAP 과 증분가가
        달라 멱등이 깨지므로 쓰지 않는다.
        """
        tag = f"finish_live_skip:{order_id}"
        booked = 0.0
        for f in reversed(self.account.journal):
            if f.symbol != symbol or f.side != "SELL":
                continue
            reason = str(getattr(f, "reason", "") or "")
            if order_id and (tag in reason or f"reconcile_attribution:{order_id}" in reason
                             or (order_id in reason and (
                                 "finish_live_skip" in reason
                                 or "reconcile_attribution" in reason))):
                booked += float(f.qty)
                if booked + 1e-9 >= float(filled_qty):
                    return True
                continue
            # 구 J3(order_id 없는 reason) — 같은 수량·근사가만 최신 1건 인정.
            if (abs(float(f.qty) - float(filled_qty)) < 1e-9
                    and abs(float(f.price) - float(avg_px)) < 0.01
                    and reason == "reconcile_attribution"):
                return True
            break
        if self.store is None or not order_id:
            return False
        try:
            rows = self.store.get_working_orders(symbol)
        except Exception:
            return False
        for row in rows or []:
            if str(row.get("order_id") or "") != str(order_id):
                continue
            applied = float(row.get("applied_qty") or 0.0)
            return applied + 1e-9 >= float(filled_qty)
        return False

    def _working_applied(self, order_id: str, symbol: str
                         ) -> tuple[float, float, float]:
        """working_orders 의 (applied_qty, applied_notional, applied_fee). 없으면 0."""
        if self.store is None or not order_id:
            return 0.0, 0.0, 0.0
        try:
            rows = self.store.get_working_orders(symbol)
        except Exception:
            return 0.0, 0.0, 0.0
        for row in rows or []:
            if str(row.get("order_id") or "") != str(order_id):
                continue
            return (float(row.get("applied_qty") or 0.0),
                    float(row.get("applied_notional") or 0.0),
                    float(row.get("applied_fee") or 0.0))
        return 0.0, 0.0, 0.0

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
        # avg 없으면 원장 미반영(고스트/place 직후) — applied_* 는 0. avg 있으면
        # 호출부가 이미 apply_fill 한 수량으로 본다(J3 증분 귀속).
        if avg_px and float(avg_px) > 0:
            applied_qty = float(filled_qty)
            applied_notional = applied_qty * float(avg_px)
            applied_fee = float(fee)
        else:
            applied_qty = 0.0
            applied_notional = 0.0
            applied_fee = 0.0
        try:
            self.store.upsert_working_order(
                order_id=order_id, symbol=order.symbol, market=order.market,
                side=order.side, qty=float(order.qty), price=float(order.price),
                status=status, filled_qty=float(filled_qty), filled_avg=avg_px,
                fee=float(fee), applied_qty=applied_qty,
                applied_notional=applied_notional, applied_fee=applied_fee,
                reason=reason)
            self._register_failed_symbols.discard(order.symbol)
        except Exception as e:
            log.error("미체결 주문 기록 실패 — 메모리 격리·심볼 차단 id=%s: %s",
                      order_id, e)
            self._register_failed_symbols.add(order.symbol)
            raise

    def _reject_working_order(self, order: Order, base_kw: dict) -> bool:
        """같은 종목·같은 방향 미체결이 있으면 재발주 거부.

        UNKNOWN/QUARANTINED/local: 행과 등록실패 심볼은 block_on_working_order=False
        로도 우회 불가(양방향 차단).
        """
        if order.symbol in self._register_failed_symbols:
            self.last_reject_reason = "미체결 등록 실패 심볼(격리)"
            log.info("[거부] %s %s — 등록 실패 격리", order.side, order.symbol)
            self.last_result = ExecuteResult.rejected(self.last_reject_reason, **base_kw)
            return True
        if self.store is None:
            return False
        try:
            all_rows = self.store.get_working_orders(order.symbol, settled=False)
        except Exception as e:
            log.warning("미체결 조회 실패(통과) %s: %s", order.symbol, e)
            return False
        for row in all_rows or []:
            st = str(row.get("status") or "").upper()
            oid = str(row.get("order_id") or "")
            if st in ("UNKNOWN", "QUARANTINED") or _is_local_order_id(oid):
                self.last_reject_reason = f"미확인/격리 주문 존재({st or 'local'})"
                log.info("[거부] %s %s — UNKNOWN/QUARANTINED 양방향 차단 id=%s",
                         order.side, order.symbol, oid)
                self.last_result = ExecuteResult.rejected(
                    self.last_reject_reason, **base_kw)
                return True
        if not self.block_on_working_order:
            return False
        rows = [r for r in (all_rows or [])
                if str(r.get("side") or "").upper() == str(order.side).upper()]
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
            if _is_local_order_id(row.get("order_id")):
                continue
            if str(row.get("status") or "").upper() in ("UNKNOWN", "QUARANTINED"):
                continue
            ttl_due = self._working_ttl_due(row, now)
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
        """반대 방향 미체결 취소(SELL→BUY). 실패해도 본 주문은 막지 않는다.

        place→get_order 폴링 구간에는 store working 이 아직 없을 수 있다.
        그 창에서 손절 SELL 이 나가면 실주문 BUY+SELL 이 동시에 산다 → inflight
        반대편 order_id 도 같이 취소한다.
        """
        if order.side != "SELL":
            return
        if self.client is None or self.account_seq is None:
            return
        rows: list = []
        if self.store is not None:
            try:
                rows = list(self.store.get_working_orders(
                    order.symbol, side="BUY", settled=False) or [])
            except Exception as e:
                log.warning("반대편 미체결 조회 실패 %s: %s", order.symbol, e)
        # place-poll 창: store 반영 전 inflight BUY
        for _rid, cur in self._inflight_for(order.symbol, side="BUY"):
            if cur.order_id and not _is_local_order_id(cur.order_id) and not any(
                    r.get("order_id") == cur.order_id for r in rows):
                rows.append({
                    "order_id": cur.order_id, "symbol": order.symbol,
                    "market": order.market or cur.market, "side": "BUY",
                    "qty": float(cur.qty), "price": float(cur.price),
                })
        for row in rows:
            if _is_local_order_id(row.get("order_id")):
                log.warning("[LIVE] local 키 반대편 BUY — API 취소 불가 id=%s",
                            row.get("order_id"))
                continue
            log.info("[LIVE] 청산 전 반대편 BUY 취소 id=%s %s x%s @ %s",
                     row["order_id"], row["symbol"], row["qty"], row.get("price"))
            if not self._cancel_and_confirm(row["order_id"], row):
                log.warning("[LIVE] 반대편 BUY 취소 실패 — SELL 은 계속 id=%s",
                            row["order_id"])

    def _reject_inflight(self, order: Order, base_kw: dict) -> bool:
        """in-flight 거부(같은 방향만). True 이면 last_result 설정됨."""
        self._prune_expired_reservations()    # 만료 회수 후 판정
        same = self._inflight_for(order.symbol, side=order.side)
        if not same:
            return False
        self.last_reject_reason = "동일 종목 주문 처리 중(in-flight)"
        log.info("[거부] %s %s — in-flight", order.side, order.symbol)
        self.last_result = ExecuteResult.rejected(self.last_reject_reason, **base_kw)
        return True

    def sweep_working_orders(self) -> dict:
        """레지스트리 정산 — 기동 시 1회 + 주기 재대사마다.

        상태를 재조회해 종결분을 정산하고, TTL 초과 미체결은 취소한다. **원장 수량은
        건드리지 않는다**. local: 키는 API 에 보내지 않고 격리·경보만 유지.

        조회는 락 밖, 적용은 락 안 + 행 재읽기(stale 응답 미적용).
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
               "abandoned": 0, "quarantined": 0, "fetch_failed": 0,
               "block_reconcile": False, "local_orphan": 0}
        # 매도 결과가 불명인 심볼 — 재대사가 이 심볼만 수량 덮기를 보류한다.
        # 해소(체결 확인·미접수 확정)될 때까지 격리 TTL 과 무관하게 유지한다.
        defer: set[str] = set()
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
            prefetched: dict | None = None
            if _is_local_order_id(oid):
                verdict, match = self._resolve_local_order(row, now)
                if verdict == "absent":
                    with self._lock:
                        fresh = self._working_row_snapshot(oid)
                        if fresh is None or fresh.get("settled_at"):
                            continue
                        self._store_call(self.store.delete_working_order, oid)
                    out["local_absent"] = out.get("local_absent", 0) + 1
                    log.warning("[local 해소] id=%s %s %s — 서버 주문 목록에 없음(미접수 확정), 삭제",
                                oid, row.get("side"), row.get("symbol"))
                    self._emit_symbol("working_order_local_absent", row.get("symbol"), {
                        "order_id": oid, "side": row.get("side"),
                        "age_sec": round(self._working_age(row, now), 1)})
                    continue
                if verdict == "found":
                    real_id = str(match.get("orderId"))
                    with self._lock:
                        fresh = self._working_row_snapshot(oid)
                        if fresh is None or fresh.get("settled_at"):
                            continue
                        try:
                            self.store.rekey_working_order(oid, real_id, status="PENDING")
                        except Exception as e:
                            log.error("[local 해소] rekey 실패 %s → %s: %s", oid, real_id, e)
                            out["working"] += 1
                            continue
                    out["local_resolved"] = out.get("local_resolved", 0) + 1
                    log.warning("[local 해소] id=%s → %s (%s %s status=%s)", oid, real_id,
                                row.get("side"), row.get("symbol"), match.get("status"))
                    self._emit_symbol("working_order_local_resolved", row.get("symbol"), {
                        "order_id": real_id, "local_order_id": oid,
                        "side": row.get("side"), "status": match.get("status")})
                    # 실 id 로 바뀌었다 — 아래 일반 경로가 단건 상세로 정산한다(목록 항목은
                    # 대조용일 뿐, 상세 조회 실패 시에만 목록 값으로 대신한다).
                    oid = real_id
                    row = {**row, "order_id": real_id, "status": "PENDING"}
                    prefetched = self._fetch_order(real_id) or match
                else:
                    out["local_orphan"] += 1
                    out["working"] += 1
                    log.error("[미확인 local 주문] id=%s %s %s — 서버 조회로 해소 못함, 격리 유지",
                              oid, row.get("side"), row.get("symbol"))
                    self._emit_symbol("working_order_local_orphan", row.get("symbol"), {
                        "order_id": oid, "side": row.get("side"), "status": row.get("status"),
                        "age_sec": round(self._working_age(row, now), 1)})
                    if str(row.get("side") or "").upper() == "SELL":
                        # 체결됐을 수 있는 매도 — 재대사가 감소를 먼저 흡수하면 손익이
                        # 영구 소실된다. 해소될 때까지 이 심볼만 재대사를 미룬다.
                        defer.add(str(row.get("symbol")))
                    if self._should_abandon_working(row, now):
                        with self._lock:
                            fresh = self._working_row_snapshot(oid)
                            if fresh is None or fresh.get("settled_at"):
                                continue
                            self._abandon_working_order(fresh, now, why="local_orphan")
                            out["quarantined"] += 1
                    continue
            info = prefetched if prefetched is not None else self._fetch_order(oid)
            with self._lock:
                fresh = self._working_row_snapshot(oid)
                if fresh is None:
                    continue
                if (fresh.get("status") != row.get("status")
                        or float(fresh.get("filled_qty") or 0)
                        != float(row.get("filled_qty") or 0)
                        or fresh.get("settled_at")):
                    # 행이 바뀌었으면 옛 응답 적용 안 함
                    out["working"] += 1
                    continue
                if info is None:
                    if str(fresh.get("side") or "").upper() == "SELL":
                        defer.add(str(fresh.get("symbol")))
                    if self._should_abandon_working(fresh, now):
                        self._abandon_working_order(fresh, now, why="fetch_failed")
                        out["quarantined"] += 1
                        out["abandoned"] += 1
                    else:
                        out["working"] += 1
                        out["fetch_failed"] += 1
                    continue
                status, filled, avg, fee = _parse_execution(info)
                self._store_call(self.store.update_working_order, oid, status=status,
                                 filled_qty=filled, filled_avg=avg, fee=fee)
                if status in _TERMINAL:
                    out["settled"] += 1
                    self._emit_deferred_fill(fresh, status, filled, avg, fee)
                    if self._settle_or_drop(oid, fresh, filled, now):
                        out["awaiting_attribution"] += 1
                    self._emit_symbol("working_order_settled", fresh["symbol"], {
                        "order_id": oid, "status": status, "filled_qty": filled,
                        "avg_price": avg, "qty": fresh["qty"], "side": fresh["side"]})
                    continue
                if not self._working_ttl_due(fresh, now):
                    if (str(fresh.get("status") or "").upper() != "QUARANTINED"
                            and self._should_abandon_working(fresh, now)):
                        self._abandon_working_order(fresh, now, why="ttl_no_cancel")
                        out["quarantined"] += 1
                        out["abandoned"] += 1
                    else:
                        out["working"] += 1
                    continue
            # 취소는 락 밖 I/O
            if self._cancel_and_confirm(oid, row):
                out["canceled"] += 1
            else:
                with self._lock:
                    fresh = self._working_row_snapshot(oid)
                    if fresh is None or fresh.get("settled_at"):
                        continue
                    if self._should_abandon_working(fresh, now):
                        self._abandon_working_order(fresh, now, why="cancel_failed")
                        out["quarantined"] += 1
                        out["abandoned"] += 1
                    else:
                        out["cancel_failed"] += 1
                        out["working"] += 1
        out["defer_symbols"] = sorted(s for s in defer if s)
        cleared = self._clear_register_failed()
        if cleared:
            out["register_failed_cleared"] = cleared
        return out

    def _clear_register_failed(self) -> list[str]:
        """등록 실패로 막아 둔 심볼을 서버 미체결 목록으로 확인해 푼다(락 밖 I/O).

        기록이 실패한 주문은 레지스트리에 없거나 local 키로만 남는다. 그 종목의 OPEN
        주문이 전부 레지스트리에 있고(local 해소로 rekey 된 것 포함) 미확인 local 행도
        없으면 더는 모르는 주문이 없다 — 차단을 푼다. 조회 실패면 유지.
        """
        syms = sorted(self._register_failed_symbols)
        lister = getattr(self.client, "list_orders", None)
        if not syms or lister is None or self.store is None:
            return []
        cleared: list[str] = []
        for sym in syms:
            try:
                res = lister(self.account_seq, status="OPEN", symbol=sym) or {}
                rows = self.store.get_working_orders(sym, settled=False) or []
            except Exception as e:
                log.warning("등록실패 심볼 확인 실패 %s: %s", sym, e)
                continue
            if res.get("hasNext"):
                continue
            known = {str(r.get("order_id")) for r in rows}
            if any(_is_local_order_id(r.get("order_id")) for r in rows):
                continue
            unknown = [o for o in (res.get("orders") or [])
                       if isinstance(o, dict) and str(o.get("orderId")) not in known]
            if unknown:
                log.error("등록실패 심볼 %s — 레지스트리에 없는 미체결 %d건, 차단 유지",
                          sym, len(unknown))
                continue
            with self._lock:
                self._register_failed_symbols.discard(sym)
            cleared.append(sym)
            log.warning("등록실패 심볼 %s — 서버 미체결이 레지스트리와 일치, 차단 해제", sym)
            self._emit_symbol("register_failed_cleared", sym, {"symbol": sym})
        return cleared

    def _resolve_local_order(self, row: dict, now: float
                             ) -> tuple[str, dict | None]:
        """local:<uuid> 행을 서버 주문 목록(GET /orders)과 대조한다(락 밖 I/O).

        전송 예외 뒤 접수 여부가 불명인 행은 단건 조회(orderId 모름)로는 영영 못 푼다.
        그대로 두면 그 종목은 매도 포함 양방향 영구 차단이다. 같은 종목·방향·수량·가격
        (금액 주문은 금액)이고 접수 시각이 local 기록 직후 창 안인 주문을 찾는다.

        반환: ("found", order) 정확히 1건 / ("absent", None) OPEN·CLOSED 목록을
        끝까지 봤는데 없음(유예 경과 후만) / ("unknown", None) 조회 실패·후보 복수.
        """
        lister = getattr(self.client, "list_orders", None)
        if lister is None:
            return "unknown", None
        placed = float(row.get("placed_at") or now)
        meta = _row_meta(row)
        side = str(row.get("side") or "").upper()
        day_from = datetime.fromtimestamp(
            placed - _LOCAL_LOOSE_BEFORE_SEC, tz=_KST).strftime("%Y-%m-%d")
        day_to = datetime.fromtimestamp(now, tz=_KST).strftime("%Y-%m-%d")
        orders: list[dict] = []
        complete = True
        try:
            res = lister(self.account_seq, status="OPEN", symbol=row["symbol"]) or {}
            orders.extend(res.get("orders") or [])
            # 스펙상 OPEN 은 전량 반환(hasNext 항상 false). 어겨지면 '없음' 확정 금지.
            if res.get("hasNext"):
                complete = False
            cursor = None
            for _ in range(_LOCAL_LIST_MAX_PAGES):
                res = lister(self.account_seq, status="CLOSED", symbol=row["symbol"],
                             date_from=day_from, date_to=day_to,
                             cursor=cursor, limit=100) or {}
                orders.extend(res.get("orders") or [])
                cursor = res.get("nextCursor")
                if not res.get("hasNext") or not cursor:
                    break
            else:
                complete = False
        except Exception as e:
            log.warning("local 주문 해소 — 목록 조회 실패 id=%s: %s", row.get("order_id"), e)
            return "unknown", None
        known: set[str] = set()
        try:
            known = {str(r.get("order_id")) for r in self.store.get_working_orders()}
        except Exception:
            pass
        amount = meta.get("order_amount")
        cands = []
        loose = 0
        for o in orders:
            if not isinstance(o, dict) or not o.get("orderId"):
                continue
            if str(o.get("orderId")) in known:
                continue
            if str(o.get("symbol") or row["symbol"]) != str(row["symbol"]):
                continue
            if str(o.get("side") or "").upper() != side:
                continue
            ts = _parse_ts(o.get("orderedAt"))
            if ts is None or (placed - _LOCAL_LOOSE_BEFORE_SEC <= ts
                              <= placed + _LOCAL_LOOSE_AFTER_SEC):
                loose += 1
            if ts is None or not (placed - _LOCAL_MATCH_BEFORE_SEC <= ts
                                  <= placed + _LOCAL_MATCH_AFTER_SEC):
                continue
            if amount is not None:
                if not _same_num(o.get("orderAmount"), amount):
                    continue
            else:
                if not _same_num(o.get("quantity"), row.get("qty")):
                    continue
                if (str(meta.get("order_type") or "LIMIT").upper() == "LIMIT"
                        and not _limit_price_matches(o.get("price"), row.get("price"))):
                    continue
            cands.append(o)
        if len(cands) == 1:
            return "found", cands[0]
        if cands:
            log.error("local 주문 해소 — 후보 %d건(모호) id=%s", len(cands), row.get("order_id"))
            return "unknown", None
        if loose:
            log.error("local 주문 해소 — 엄격 대조 0건·근접 주문 %d건(가격 절삭 등) id=%s",
                      loose, row.get("order_id"))
            return "unknown", None
        if complete and now - placed >= _LOCAL_ABSENT_GRACE_SEC:
            return "absent", None
        return "unknown", None

    def _working_row_snapshot(self, order_id: str) -> dict | None:
        """락 안: order_id 현재 행. 없으면 None."""
        if self.store is None:
            return None
        try:
            rows = self.store.get_working_orders()
        except Exception:
            return None
        for r in rows or []:
            if r.get("order_id") == order_id:
                return r
        return None

    def _emit_deferred_fill(self, row: dict, status: str, filled: float,
                            avg: float | None, fee: float) -> None:
        """폴링 창 밖에서 채워진 체결분(filled − applied)을 live_order 로 알린다.

        즉시 체결분은 _finish_live 가 live_order 를 냈고 applied_qty 로 남아 있다.
        종결 확인 시 그 위의 증분만 낸다 — 없으면 체결 알림(ntfy)·대시보드 체결 표에서
        종가 동시호가 체결 같은 지연 체결이 통째로 빠진다(09-30 001820).
        """
        applied = float(row.get("applied_qty") or 0.0)
        inc = float(filled or 0.0) - applied
        if inc <= 1e-9:
            return
        log.info("[LIVE] 지연 체결 id=%s status=%s — %s %s x%s @ %s (누적 filled=%s)",
                 row.get("order_id"), status, row.get("side"), row.get("symbol"),
                 inc, avg, filled)
        self._emit_symbol("live_order", row.get("symbol"), {
            "symbol": row.get("symbol"), "side": row.get("side"), "qty": inc,
            "price": avg, "fee": fee, "order_id": row.get("order_id"),
            "status": status, "limit_price": row.get("price"),
            "reason": row.get("reason") or "", "deferred": True,
            "filled_qty": filled})

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

        전량 반영(applied≥filled)된 행은 허위 '귀속 실패' 없이 삭제한다 — BUY
        holdings 보정 후 삭제가 누락돼도 TTL 에 경고가 나면 안 된다.

        그 외는 재대사가 수량 감소를 못 봤다는 뜻이다. 추정으로 채우지 않고
        버리되 조용히 지우지는 않는다 — 실체결가를 알았는데 원장에 못 넣었다는
        기록이 남아야 리포트에서 구멍이 보인다.
        """
        if self.attribution_ttl_sec < 0:
            return False
        age = now - float(row.get("settled_at") or now)
        if age < self.attribution_ttl_sec:
            return False
        filled = float(row.get("filled_qty") or 0.0)
        applied = float(row.get("applied_qty") or 0.0)
        if filled - applied <= 1e-9:
            # 전량 반영된 고아 — 허위 '귀속 실패' 없이 삭제
            self._store_call(self.store.delete_working_order, row["order_id"])
            return True
        self._store_call(self.store.delete_working_order, row["order_id"])
        side = str(row.get("side") or "")
        why = ("holdings 보정이 미반영분을 못 흡수"
               if side == "BUY" else "재대사가 수량 감소를 못 봄")
        log.warning("[귀속 실패] %s %s 체결 %s @ %s — %s(%.0f초)",
                    side, row["symbol"], row["filled_qty"],
                    row.get("filled_avg"), why, age)
        self._emit_symbol("unattributed_fill", row["symbol"], {
            "order_id": row["order_id"], "side": side,
            "filled_qty": row["filled_qty"], "applied_qty": row.get("applied_qty"),
            "avg_price": row.get("filled_avg"), "age_sec": round(age, 1)})
        return True

    def _fetch_order(self, order_id: str) -> dict | None:
        if _is_local_order_id(order_id):
            return None
        try:
            return self.client.get_order(self.account_seq, order_id) or {}
        except Exception as e:
            log.warning("미체결 주문 조회 실패 id=%s: %s", order_id, e)
            return None

    def _cancel_and_confirm(self, order_id: str, row: dict) -> bool:
        """취소 요청 후 재조회로 확인. 확인 못 하면 레지스트리에 남긴다(재발주 계속 차단)."""
        if _is_local_order_id(order_id):
            log.warning("[LIVE] local 키 취소 불가 id=%s", order_id)
            return False
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
        self._emit_deferred_fill(row, status, filled, avg, fee)
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
                if self.mode == "live":
                    # 라이브: 가드를 못 보면 사는 쪽이 더 위험(정지/주의 종목).
                    log.error("[매수가드] 판정 예외(fail-closed, 매수 차단) %s: %s",
                              order.symbol, e)
                    ok, block_reason = False, f"매수가드 예외: {e}"
                else:
                    log.warning("[매수가드] 판정 예외(fail-open, 매수 허용) %s: %s",
                                order.symbol, e)
                    ok, block_reason = True, ""
            if not ok:
                self.last_reject_reason = block_reason or "매수가드 차단"
                log.warning("[매수차단] %s %s — %s", order.side, order.symbol, block_reason)
                self._emit("buy_blocked", order, {"symbol": order.symbol, "reason": block_reason})
                self.last_result = ExecuteResult.rejected(
                    self.last_reject_reason, **base_kw)
                return None

        qty_before = float(self.account.position(order.symbol).qty)
        avg_before = float(self.account.position(order.symbol).avg_price or 0.0)
        if self.mode != "live":
            rid = self._mark_inflight(order)
            return {"kind": "paper", "base_kw": base_kw, "reservation_id": rid,
                    "qty_before": qty_before, "avg_before": avg_before}

        placed = self._place_live_order(order, reason)
        if placed is None:
            return None
        order_id = placed["order_id"]
        rid = self._mark_inflight(order, order_id)
        return {"kind": "live", "order_id": order_id, "base_kw": base_kw,
                "reservation_id": rid,
                "qty_before": qty_before, "avg_before": avg_before}

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
                     avg_before: float | None = None,
                     exit_reason: str | None = None) -> ExecuteResult:
        if filled_qty > 0 and avg_px and avg_px > 0:
            applied_q, applied_n, applied_f = self._working_applied(
                order_id, order.symbol)
            inc = incremental_fill(
                filled_qty, avg_px, fee, applied_q, applied_n, applied_f)
            # 증분 없으면(이미 전량 반영) 스킵 경로의 손익 보강도 불필요.
            book_qty, book_px, book_fee = (
                (inc[0], inc[1], inc[2]) if inc is not None
                else (0.0, float(avg_px), 0.0))
            if self._ledger_already_has_fill(order, filled_qty, qty_before):
                log.info("[LIVE] 체결 id=%s — 원장 이미 반영(재대사), apply_fill 스킵",
                         order_id)
                # qty/cash 는 재대사가 맞췄고 apply_fill 을 다시 하면 이중 계상.
                # 매도 실현손익·저널만 비어 있을 수 있으니 증분만 보강(J3 와 멱등).
                if (order.side == "SELL" and book_qty > 1e-9
                        and avg_before is not None and float(avg_before) > 0
                        and not self._sell_pnl_already_booked(
                            order_id, order.symbol, filled_qty, avg_px)):
                    self.account.record_exit_attribution(
                        order.symbol, order.market, float(book_qty),
                        float(book_px), float(avg_before), float(book_fee),
                        reason=f"finish_live_skip:{order_id}")
                    log.info("[LIVE] 체결 id=%s — apply_fill 스킵분 손익 귀속 "
                             "증분 qty=%s @ %.4f (누적 filled=%s avg=%.4f)",
                             order_id, book_qty, book_px, filled_qty, avg_px)
            elif inc is not None:
                fill = self.account.apply_fill(
                    order.symbol, order.market, order.side,
                    book_qty, book_px, book_fee, reason)
                log.info("[LIVE] 체결 id=%s status=%s — %s %s x%s @ %.4f "
                         "(fee %.2f, 누적 filled=%s avg=%.4f) - %s",
                         order_id, status, fill.side, fill.symbol, fill.qty,
                         fill.price, fill.fee, filled_qty, avg_px, reason)
            else:
                log.info("[LIVE] 체결 id=%s — 증분 없음(이미 반영), apply_fill 스킵",
                         order_id)
            payload = {
                "symbol": order.symbol, "side": order.side, "qty": filled_qty,
                "price": avg_px, "fee": fee, "order_id": order_id,
                "status": status, "limit_price": order.price,
                "reason": reason or "",
            }
            if exit_reason:
                payload["exit_reason"] = exit_reason
            if book_qty > 1e-9 and abs(book_px - float(avg_px)) > 1e-9:
                payload["incremental_qty"] = book_qty
                payload["incremental_price"] = book_px
            self._emit("live_order", order, payload)
            # 부분체결은 잔량이 아직 살아 있다 — 레지스트리에 남겨 재발주를 막고
            # 만료 시 취소한다. 지금까지는 성공 반환 후 잔량을 잊었다.
            # UNKNOWN(조회 실패)도 잔량 추적 — 아니면 inflight 해제 후 J1/J2 공백.
            if status in _TRACK_WORKING and filled_qty < float(order.qty):
                self._register_working_order(order, order_id, status,
                                             filled_qty, reason,
                                             avg_px=avg_px, fee=fee)
            elif self.store is not None:
                # place 직후 PENDING 등록분 — 전량 체결이면 레지스트리에서 제거.
                self._store_call(self.store.delete_working_order, order_id)
            self.last_result = ExecuteResult.from_fill(
                fill_qty=filled_qty, fill_price=avg_px, fee=fee,
                order_qty=order.qty, limit_price=order.price,
                status=status, order_id=order_id, side=order.side)
            return self.last_result

        # 체결수량만 있고 평균가 없음 → 원장 미반영(고스트) 금지. working 유지해
        # sweep/재조회가 avg 를 받을 때까지 추적. FILLED 여도 _TRACK_WORKING 밖이면
        # UNKNOWN 으로 남겨 종결 삭제되지 않게 한다.
        if filled_qty > 0:
            track_status = status if status in _TRACK_WORKING else "UNKNOWN"
            self._register_working_order(order, order_id, track_status,
                                         filled_qty, reason, avg_px=None, fee=fee)
            log.error("[LIVE] 체결가 미수신 id=%s status=%s filled=%s — 원장 보류(working 유지)",
                      order_id, status, filled_qty)
            self._emit("live_order_avg_missing", order, {
                "symbol": order.symbol, "side": order.side, "qty": filled_qty,
                "order_id": order_id, "status": status, "reason": reason,
                **({"exit_reason": exit_reason} if exit_reason else {}),
            })
            self.last_result = ExecuteResult.rejected(
                f"체결가 미수신({status})", order_qty=order.qty,
                limit_price=order.price, order_id=order_id, status=track_status)
            return self.last_result

        # UNKNOWN 도 표에 남긴다. 이벤트는 조회 실패를 드러내 live_order_error.
        # place 직후 PENDING 등록분이 있으므로: 추적 대상이면 upsert, 확정 종결(거부 등)이면 삭제.
        kind = "live_order_pending" if status in _PENDING else "live_order_error"
        if status in _TRACK_WORKING:
            self._register_working_order(order, order_id, status, 0.0, reason)
        elif status in _TERMINAL and self.store is not None:
            self._store_call(self.store.delete_working_order, order_id)
        log.warning("[LIVE] 미체결 id=%s status=%s — 원장 무변(주기 재대사가 반영): %s %s x%s",
                    order_id, status, order.side, order.symbol, order.qty)
        self._emit(kind, order,
                   {"symbol": order.symbol, "side": order.side, "qty": order.qty,
                    "order_id": order_id, "status": status, "reason": reason,
                    **({"exit_reason": exit_reason} if exit_reason else {})})
        self.last_result = ExecuteResult.rejected(
            f"미체결({status})", order_qty=order.qty, limit_price=order.price,
            order_id=order_id, status=status)
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
                                 filled_qty, avg_px, fee, status,
                                 qty_before=prep.get("qty_before"),
                                 avg_before=prep.get("avg_before"))

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

    def _prepare_live_order(self, order: Order, *,
                            exit_reason: str | None = None) -> bool:
        """라이브 주문을 게이트 이전에 실조건으로 보정. 진행 가능하면 True.

        - client/account_seq 없거나 live_markets 밖이면 False(집행 스킵).
        - SELL: 실 매도가능 수량(get_sellable)으로 클램프 — 원장 드리프트로 인한 오버셀·
          고아 포지션을 막는다. 매도가능 0 이면 스킵.
        - 시간외 세션(및 정규장 소수점 BUY): 호가 스프레드가 상한을 넘으면 스킵.
          exit_reason 있는 SELL(스탑 등)은 정규장 소수점에서도 면제.
        - 주문가/금액 산정 기준: 호가북 마켓터블 리밋가로 갱신
          (없으면 기존 견적가 유지, 폴백). BUY 는 notional_cap 이 있으면 상향가
          기준으로 qty 를 재절사한다.
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
                sellable = _num(_field(resp, "sellableQuantity"))
                if sellable is None and resp:
                    keys = list(resp.keys()) if isinstance(resp, dict) else type(resp).__name__
                    log.warning(
                        "[LIVE] 매도가능 응답에 sellableQuantity 없음 — 원장 수량으로 진행"
                        "(%s keys=%s)", order.symbol, keys)
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
        if not self._spread_ok(order, ob, exit_reason=exit_reason):
            self.last_reject_reason = "스프레드 초과"
            return False

        if ob is not None:                    # 조회 실패면 견적가 유지(기존 폴백 동작)
            px = self._marketable_limit(order, ob)
            if px and px > 0:
                order.price = px
        if not self._retarget_buy_qty_to_cap(order):
            return False
        return True

    def _retarget_buy_qty_to_cap(self, order: Order) -> bool:
        """BUY notional_cap 이 있으면 현재가 기준 qty 재절사. 진행 가능하면 True.

        마켓터블 리밋가 상향 뒤 qty×price 가 사이징 캡을 넘지 않게 한다. 재절사 결과가
        0 이면 주문 스킵.
        """
        if order.side != "BUY":
            return True
        cap_raw = getattr(order, "notional_cap", None)
        if cap_raw is None:
            return True
        try:
            cap = float(cap_raw)
        except (TypeError, ValueError):
            return True
        if cap < 0 or order.price <= 0:
            return True
        if float(order.qty) * float(order.price) <= cap + 1e-9:
            return True
        raw = cap / float(order.price)
        if _is_fractional_qty(order.qty) and raw < 1:
            new_qty = float(Decimal(str(raw)).quantize(
                Decimal("0.0001"), rounding=ROUND_DOWN))
        else:
            new_qty = float(math.floor(raw + 1e-12))
        if new_qty < float(order.qty):
            log.warning("[LIVE] %s 리밋가 상향 후 qty 재절사 %s→%s (cap=%s @%s)",
                        order.symbol, order.qty, new_qty, cap, order.price)
            order.qty = new_qty
        if float(order.qty) <= 0:
            log.warning("[LIVE] %s notional_cap 재절사 결과 0 — 매수 스킵 (cap=%s @%s)",
                        order.symbol, cap, order.price)
            self.last_reject_reason = "notional_cap 재절사 0"
            return False
        return True

    def _fetch_orderbook(self, symbol: str) -> dict | None:
        """호가북 조회. 실패하면 None(호출측이 폴백/가드 미적용으로 처리)."""
        try:
            return self.client.orderbook(symbol) or {}
        except Exception as e:
            log.warning("[LIVE] 호가 조회 실패 → 리밋가 폴백(견적가 사용) %s: %s", symbol, e)
            return None

    def _spread_ok(self, order: Order, ob: dict | None, *,
                   exit_reason: str | None = None) -> bool:
        """스프레드 가드. 주문을 내도 되면 True.

        기본은 시간외만. 정규장 온주는 발동하지 않는다. 단 소수점 수량은 정규장
        한정·시장가라 정규장에도 같은 상한을 적용한다(보호가 필요한 주문과 가드가
        없는 시간대가 겹치는 구멍 방지). exit_reason 있는 SELL(스탑·트레일 등)은
        정규장 소수점에서도 면제 — 청산을 스프레드로 막지 않는다. BUY·시간외는 유지.

        (ask-bid)/중간가 가 max_spread_pct_extended 를 넘으면 False(주문 스킵) +
        wide_spread_skip 이벤트. 호가북 조회 실패·한쪽 호가 없음 등으로 스프레드를
        계산할 수 없으면 가드를 적용하지 않는다(가드 오작동으로 정상 주문을 막는 게 더 나쁘다).
        """
        if self.max_spread_pct_extended <= 0:
            return True
        session = current_session(order.market)
        fractional = _is_fractional_qty(order.qty)
        if session == "regular" and not fractional:
            return True
        # 정규장 소수점 스탑/트레일 청산은 스프레드로 막지 않는다.
        if (session == "regular" and fractional
                and order.side == "SELL" and exit_reason):
            return True
        ask = self._best_price(ob, "asks")
        bid = self._best_price(ob, "bids")
        if not ask or not bid or ask <= 0 or bid <= 0:
            return True                       # 스프레드 계산 불가 → 통과(기존 폴백 동작 유지)
        mid = (ask + bid) / 2
        spread = (ask - bid) / mid if mid > 0 else 0.0
        if spread <= self.max_spread_pct_extended:
            return True
        why = "소수점" if (session == "regular" and fractional) else f"시간외({session})"
        log.warning("[LIVE] %s %s 스프레드 %.2f%% > 상한 %.2f%% — 주문 스킵 (%s x%s)",
                    order.symbol, why, spread * 100,
                    self.max_spread_pct_extended * 100, order.side, order.qty)
        self._emit("wide_spread_skip", order,
                   {"symbol": order.symbol, "side": order.side, "qty": order.qty,
                    "session": session, "fractional": fractional,
                    "bid": bid, "ask": ask, "spread_pct": spread,
                    "limit_pct": self.max_spread_pct_extended})
        return False

    @staticmethod
    def _best_price(ob: dict | None, key: str) -> float | None:
        """호가북에서 최우선호가(asks[0]/bids[0])의 가격. 없으면 None."""
        levels = (ob or {}).get(key) or []
        if not isinstance(levels, (list, tuple)):
            return None                       # 형태가 예상과 다르면 가드 미적용(통과)
        for lv in levels:
            px = _num(_field(lv, "price"))
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
            px, vol = _num(_field(lv, "price")), _num(_field(lv, "volume"))
            if px and px > 0 and vol and vol > 0:
                parsed.append((px, vol))
        if not parsed and levels:
            sample = levels[0]
            sk = (list(sample.keys()) if isinstance(sample, dict)
                  else type(sample).__name__)
            log.warning(
                "[LIVE] 호가 레벨 파싱 실패(price/volume) → 견적가 폴백"
                " %s %s sample_keys=%s",
                order.symbol, order.side, sk)
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

    def _place_live_order(self, order: Order, reason: str) -> dict | None:
        """라이브 주문 접수(락 안).

        POST 전 local:<uuid> UNKNOWN 을 commit 한 뒤에만 전송. orderId 받으면
        rekey UPDATE. 전송 예외·orderId 없음·등록 실패는 ExecuteResult.unknown.
        반환: {"order_id": str} 또는 None(last_result 설정됨).
        """
        base_kw = {"order_qty": float(order.qty), "limit_price": float(order.price)}
        fractional_us = (
            str(order.market).upper() == "US"
            and _is_fractional_qty(order.qty)
        )
        order_type = "MARKET" if fractional_us else "LIMIT"
        order_amount = (
            _usd_order_amount(order.qty, order.price)
            if fractional_us and order.side == "BUY"
            else None
        )
        client_order_id = str(uuid.uuid4())
        local_id = f"{_LOCAL_ORDER_PREFIX}{client_order_id}"
        request_meta = {
            "side": order.side,
            "qty": order.qty,
            "order_type": order_type,
            "client_order_id": client_order_id,
            **({"order_amount": order_amount} if order_amount is not None else {}),
        }
        if self.store is None:
            self.last_result = ExecuteResult.unknown(
                "working_orders Store 없음 — 실주문 거부", **base_kw, side=order.side)
            return None
        try:
            self.store.upsert_working_order(
                order_id=local_id, symbol=order.symbol, market=order.market,
                side=order.side, qty=float(order.qty), price=float(order.price),
                status="UNKNOWN", filled_qty=0.0, reason=reason,
                meta={"client_order_id": client_order_id, "order_type": order_type,
                      **({"order_amount": order_amount}
                         if order_amount is not None else {}),
                      **({"entry_plan": order.entry_plan}
                         if order.side == "BUY" and order.entry_plan else {})})
        except Exception as e:
            log.error("[LIVE] local UNKNOWN commit 실패 — POST 안 함: %s", e)
            self.last_result = ExecuteResult.unknown(
                "local working commit 실패", **base_kw, side=order.side)
            return None

        # 전송 시작 = 주문 활동. 결과 불명으로 끝나 _mark_inflight 를 안 거쳐도,
        # 조회 중이던 재대사 스냅샷(주문 전 BP)이 이 주문의 홀드를 지우지 않게 한다.
        self._activity_gen += 1
        try:
            if order_amount is not None:
                resp = self.client.place_order(
                    account_seq=self.account_seq, symbol=order.symbol, side=order.side,
                    order_amount=order_amount, order_type=order_type,
                    client_order_id=client_order_id)
            else:
                resp = self.client.place_order(
                    account_seq=self.account_seq, symbol=order.symbol, side=order.side,
                    qty=order.qty, order_type=order_type,
                    price=(order.price if order_type == "LIMIT" else None),
                    client_order_id=client_order_id)
        except Exception as e:
            if getattr(e, "definitive", False):
                return self._reject_definitive(order, e, local_id, request_meta,
                                               reason, base_kw)
            log.error("[LIVE] 주문 전송 실패 — %s %s x%s @ %.2f (%s%s): %s",
                      order.side, order.symbol, order.qty, order.price, order_type,
                      f", amount={order_amount}" if order_amount is not None else "", e)
            self._emit("live_order_error", order, {
                **request_meta, "error": str(e), "reason": reason,
                "local_order_id": local_id})
            self.last_result = ExecuteResult.unknown(
                "주문 전송 실패(미확인)", order_id=local_id, status="UNKNOWN",
                side=order.side, **base_kw)
            return None

        order_id = self._order_id(resp)
        if order_id is None:
            log.error("[LIVE] 주문 응답에 주문식별자(orderId) 없음 — UNKNOWN: %s", resp)
            self._emit("live_order_error", order,
                       {**request_meta, "error": "응답에 orderId 없음",
                        "resp": str(resp)[:300], "reason": reason,
                        "local_order_id": local_id})
            self.last_result = ExecuteResult.unknown(
                "orderId 없음(미확인)", order_id=local_id, status="UNKNOWN",
                side=order.side, **base_kw)
            return None

        try:
            self.store.rekey_working_order(local_id, order_id, status="PENDING")
        except Exception as e:
            log.error("[LIVE] orderId rekey 실패 local=%s → %s: %s", local_id, order_id, e)
            self._register_failed_symbols.add(order.symbol)
            self.last_result = ExecuteResult.unknown(
                "접수 후 등록 실패", order_id=order_id, status="UNKNOWN",
                side=order.side, **base_kw)
            return None
        return {"order_id": order_id}

    def _reject_definitive(self, order: Order, err: Exception, local_id: str,
                           request_meta: dict, reason: str, base_kw: dict) -> None:
        """서버가 주문을 받지 않았음이 확정(4xx 등) — local 행 삭제 후 거부로 끝낸다.

        UNKNOWN 으로 남기면 그 종목이 매도 포함 양방향 영구 차단된다(손절 불가).
        """
        status = getattr(err, "status", None)
        code = getattr(err, "code", "") or ""
        try:
            self.store.delete_working_order(local_id)
        except Exception as de:
            # 못 지우면 UNKNOWN 이 남아 차단된다 — 보수적으로 그대로 둔다.
            log.error("[LIVE] 확정 거부 local 행 삭제 실패 %s: %s", local_id, de)
        log.warning("[LIVE] 주문 거부(미접수 확정) — %s %s x%s @ %.2f: %s %s",
                    order.side, order.symbol, order.qty, order.price, status, code or err)
        self._emit("live_order_rejected", order, {
            **request_meta, "status": status, "code": code,
            "error": str(err)[:500], "reason": reason})
        self.last_reject_reason = f"주문 거부({status}{' ' + code if code else ''})"
        self.last_result = ExecuteResult.rejected(
            self.last_reject_reason, status="REJECTED", **base_kw)
        return None

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

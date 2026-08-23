"""체결 후 store ↔ account 정합 — executor 가 주문 의도 대신 실보유를 mirror."""
from __future__ import annotations

import json
from typing import Any, Callable

from .store_sync import RECONCILE_THESIS
from .fill_result import ExecuteResult
from .logging_setup import get_logger

log = get_logger("store_fill")


def _parse_meta(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw) or {}
        except (ValueError, TypeError):
            pass
    return {}


def _last_sell_fill(account, symbol: str):
    for f in reversed(account.journal):
        if f.symbol == symbol and f.side == "SELL":
            return f
    return None


def _last_sell_price(account, symbol: str) -> float | None:
    f = _last_sell_fill(account, symbol)
    return float(f.price) if f else None


def _last_sell_fee(account, symbol: str) -> float:
    f = _last_sell_fill(account, symbol)
    return float(f.fee) if f else 0.0


def _open_row(store, symbol: str):
    for row in store.get_open_positions():
        if row["symbol"] == symbol:
            return row
    return None


def _armed_row(store, symbol: str, armed_id: int | None):
    if armed_id is not None:
        for row in store.get_armed():
            if row["id"] == armed_id:
                return row
    for row in store.get_armed():
        if row["symbol"] == symbol:
            return row
    return None


def mirror_symbol_to_store(
    store,
    broker,
    symbol: str,
    *,
    fill: ExecuteResult | None = None,
    armed_id: int | None = None,
    plan_fn: Callable[[float, str, dict | None], tuple[float | None, float | None]] | None = None,
    exit_reason: str | None = None,
) -> str:
    """account 보유를 store 에 반영. broker 락 안에서 호출할 것.

    fill 스냅샷이 있으면 qty/avg/plan_fn 입력에 우선 사용(reconcile race 방지).
    반환: promoted|updated|closed|opened|partial|noop.
    """
    if store is None or not symbol:
        return "noop"

    acct = broker.account.position(symbol)
    market = broker.account.symbol_market.get(symbol, "KR")
    open_row = _open_row(store, symbol)

    if acct.qty > 0:
        store.disarm_symbol(symbol, exclude_id=armed_id)

    # 부분 매도 귀속 — fill.side 로 판별(account race 없이)
    if (open_row is not None and fill and fill.ok and fill.side == "SELL"
            and fill.filled_qty > 0 and fill.avg_price and acct.qty > 0):
        old_qty = float(open_row["qty"] or 0)
        if old_qty > acct.qty + 1e-9:
            fee = float(fill.fee) if fill and fill.fee else _last_sell_fee(broker.account, symbol)
            store.record_partial_exit(
                int(open_row["id"]), fill.filled_qty, fill.avg_price,
                reason=exit_reason or "partial_exit", fee=fee)
            open_row = _open_row(store, symbol)
            log.debug("store mirror %s → partial sell qty=%s @ %s",
                      symbol, fill.filled_qty, fill.avg_price)

    if acct.qty <= 0:
        if open_row is None:
            return "noop"
        exit_px = (fill.avg_price if fill and fill.avg_price else None
                   or _last_sell_price(broker.account, symbol))
        fee = float(fill.fee) if fill and fill.fee else _last_sell_fee(broker.account, symbol)
        store.close_position(open_row["id"], exit_price=exit_px,
                             reason=exit_reason or "exit", fee=fee)
        log.debug("store mirror %s → closed (exit_px=%s)", symbol, exit_px)
        return "closed"

    qty = acct.qty
    avg = acct.avg_price
    entry_px = (fill.avg_price if fill and fill.avg_price else avg)

    armed_row = _armed_row(store, symbol, armed_id) if armed_id is not None else None
    if armed_row is not None and open_row is None:
        meta = _parse_meta(armed_row["meta"])
        horizon = str(meta.get("horizon") or "day")
        stop = target = None
        if plan_fn:
            stop, target = plan_fn(entry_px, horizon, meta.get("params"))
        store.promote_armed(int(armed_row["id"]), qty, avg,
                            target_price=target, stop_price=stop)
        log.debug("store mirror %s → promoted qty=%s avg=%s entry_px=%s",
                  symbol, qty, avg, entry_px)
        return "promoted"

    if open_row is not None:
        if (abs(float(open_row["qty"] or 0) - qty) > 1e-9
                or abs(float(open_row["avg_price"] or 0) - avg) > 1e-9):
            store.update_position(open_row["id"], qty=qty, avg_price=avg)
            log.debug("store mirror %s → updated qty=%s avg=%s", symbol, qty, avg)
            return "updated"
        return "noop"

    meta = {"source": "fill_mirror", "entry_thesis": RECONCILE_THESIS}
    store.open_position(symbol, market, qty, avg,
                        strategy=None, thesis=RECONCILE_THESIS,
                        target_price=None, stop_price=None, meta=meta)
    log.info("store mirror %s → opened orphan qty=%s", symbol, qty)
    return "opened"


def fill_event_payload(fill: ExecuteResult | None, **extra: Any) -> dict:
    """events payload — 체결 스냅샷 공통 필드."""
    out = dict(extra)
    if fill is None:
        return out
    out.update({
        "filled_qty": fill.filled_qty,
        "order_qty": fill.order_qty,
        "avg_price": fill.avg_price,
        "status": fill.status,
        "partial": fill.partial,
        "order_id": fill.order_id,
        "side": fill.side,
    })
    return out

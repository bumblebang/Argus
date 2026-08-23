"""체결 후 store ↔ account 정합 — executor 가 주문 의도 대신 실보유를 mirror."""
from __future__ import annotations

import json
from typing import Any, Callable

from .broker_sync import RECONCILE_THESIS
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


def _last_sell_price(account, symbol: str) -> float | None:
    for f in reversed(account.journal):
        if f.symbol == symbol and f.side == "SELL":
            return float(f.price)
    return None


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
    """account 보유를 store 에 반영. 반환: promoted|updated|closed|opened|noop."""
    if store is None or not symbol:
        return "noop"

    acct = broker.account.position(symbol)
    market = broker.account.symbol_market.get(symbol, "KR")
    open_row = _open_row(store, symbol)

    if acct.qty <= 0:
        if open_row is None:
            return "noop"
        exit_px = (fill.avg_price if fill and fill.avg_price else None
                   or _last_sell_price(broker.account, symbol))
        store.close_position(open_row["id"], exit_price=exit_px,
                             reason=exit_reason or "exit")
        log.debug("store mirror %s → closed (exit_px=%s)", symbol, exit_px)
        return "closed"

    armed_row = _armed_row(store, symbol, armed_id) if armed_id is not None else None
    if armed_row is not None and open_row is None:
        meta = _parse_meta(armed_row["meta"])
        horizon = str(meta.get("horizon") or "day")
        stop = target = None
        if plan_fn:
            stop, target = plan_fn(acct.avg_price, horizon, meta.get("params"))
        store.promote_armed(int(armed_row["id"]), acct.qty, acct.avg_price,
                            target_price=target, stop_price=stop)
        log.debug("store mirror %s → promoted qty=%s avg=%s",
                  symbol, acct.qty, acct.avg_price)
        return "promoted"

    if open_row is not None:
        if (abs(float(open_row["qty"] or 0) - acct.qty) > 1e-9
                or abs(float(open_row["avg_price"] or 0) - acct.avg_price) > 1e-9):
            store.update_position(open_row["id"], qty=acct.qty, avg_price=acct.avg_price)
            log.debug("store mirror %s → updated qty=%s avg=%s",
                      symbol, acct.qty, acct.avg_price)
            return "updated"
        return "noop"

    meta = {"source": "fill_mirror", "entry_thesis": RECONCILE_THESIS}
    store.open_position(symbol, market, acct.qty, acct.avg_price,
                      strategy=None, thesis=RECONCILE_THESIS,
                      target_price=None, stop_price=None, meta=meta)
    log.info("store mirror %s → opened orphan qty=%s", symbol, acct.qty)
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
    })
    return out

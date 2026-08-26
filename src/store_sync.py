"""store 수량 동기화 + 부분매도 귀속 — mirror/reconcile/sync 공통."""
from __future__ import annotations

import json

RECONCILE_THESIS = "라이브 재대사 시 발견된 미추적 보유 — 뇌 재평가 필요"
SYNC_THESIS = "라이브 전환 시 기존 보유 — 뇌 재평가 필요"

_ORPHAN_SOURCES = frozenset({"fill_mirror", "reconcile_adopted", "synced", "sync"})


def _row_get(row, key: str, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


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


def _last_sell_fee(account, symbol: str) -> float:
    for f in reversed(account.journal):
        if f.symbol == symbol and f.side == "SELL":
            return float(f.fee)
    return 0.0


def is_orphan_store_row(row) -> bool:
    """뇌/코드가 관리하지 않는 adopt·mirror 행 — sync 에서 thesis/stop 으로 승격 대상."""
    if row is None:
        return False
    thesis = str(_row_get(row, "thesis") or "")
    if thesis in (RECONCILE_THESIS, SYNC_THESIS):
        return True
    meta = _parse_meta(_row_get(row, "meta"))
    return str(meta.get("source") or "") in _ORPHAN_SOURCES


def sync_open_qty(
    store,
    row,
    symbol: str,
    new_qty: float,
    new_avg: float,
    account,
    *,
    exit_price: float | None = None,
    allow_journal_fallback: bool = True,
    reason: str = "sync",
) -> None:
    """open 행 qty 갱신. 감소 시 exit_price(또는 journal SELL)로 partial 귀속.

    allow_journal_fallback=False 면 exit_price 가 없을 때 저널을 뒤지지 않는다.
    재대사 경로는 청산가 판정을 직접 하므로(귀속 실체결가 > 최근 매도가) 여기서
    임의로 오래된 매도가를 끌어오면 pnl 이 조용히 틀린다.
    """
    old_qty = float(row["qty"] or 0)
    if old_qty > new_qty + 1e-9:
        px = exit_price
        if px is None and allow_journal_fallback:
            px = _last_sell_price(account, symbol)
        if px:
            sell_qty = min(old_qty - new_qty, old_qty)
            fee = _last_sell_fee(account, symbol)
            store.record_partial_exit(int(row["id"]), sell_qty, float(px),
                                    reason=reason, fee=fee)
            fresh = next((r for r in store.get_open_positions() if r["symbol"] == symbol), None)
            if fresh is not None:
                row = fresh
            elif new_qty <= 1e-9:
                store.disarm_symbol(symbol)
                return
    if new_qty > 1e-9:
        store.update_position(int(row["id"]), qty=new_qty, avg_price=new_avg)
    store.disarm_symbol(symbol)

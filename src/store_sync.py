"""store 수량 동기화 + 부분매도 귀속 — mirror/reconcile/sync 공통."""
from __future__ import annotations

import json
import time

from .logging_setup import get_logger

log = get_logger("store_sync")

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
    if bool(meta.get("provisional_stop")):
        return True
    return str(meta.get("source") or "") in _ORPHAN_SOURCES


def adopt_live_position(store, symbol: str, market: str, qty: float, avg: float,
                        *, source: str, thesis: str) -> str:
    """실계좌에서 발견한 보유를 store 에 채택. 'promoted'|'opened' 반환.

    봇 자기 주문이 폴링 창 밖에서 체결되면 store open 행이 없고 armed 계획만
    남아 있다. 그때 disarm 먼저 하면 손절가가 사라지므로, **armed 가 있으면
    promote 로 복원**하고, 없을 때만 swing 기본 손절을 임시로 씌운다.
    """
    from .agents.wiring import entry_stop_target
    from .engine.entry_basis import BASIS_ORPHAN
    from .shadow_ledger import cancel_shadow_on_fill

    armed = None
    try:
        for row in store.get_armed():
            if row["symbol"] == symbol:
                armed = row
                break
    except Exception as e:
        log.warning("채택: armed 조회 실패 %s: %s", symbol, e)

    if armed is not None:
        meta = _parse_meta(armed["meta"])
        horizon = str(meta.get("horizon") or "swing")
        params = meta.get("params") if isinstance(meta.get("params"), dict) else None
        stop, target = entry_stop_target(float(avg), horizon, params)
        aid = int(armed["id"])
        store.promote_armed(aid, float(qty), float(avg),
                            target_price=target, stop_price=stop)
        store.disarm_symbol(symbol, exclude_id=aid)
        cancel_shadow_on_fill(store, symbol)
        log.info("채택 %s → armed 계획 승격 stop=%s target=%s source=%s",
                 symbol, stop, target, source)
        return "promoted"

    stop, target = entry_stop_target(float(avg), "swing", None)
    meta = {
        "source": source,
        "entry_thesis": thesis,
        "synced_ts": time.time(),
        "provisional_stop": True,
        "entry_basis": BASIS_ORPHAN,
    }
    store.open_position(symbol, market, float(qty), float(avg),
                        strategy=None, thesis=thesis,
                        target_price=target, stop_price=stop, meta=meta)
    store.disarm_symbol(symbol)
    cancel_shadow_on_fill(store, symbol)
    log.info("채택 %s → 임시손절 고아 개설 stop=%s source=%s", symbol, stop, source)
    return "opened"


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

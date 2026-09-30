"""store 수량 동기화 + 부분매도 귀속 — mirror/reconcile/sync 공통."""
from __future__ import annotations

import json
import time

from .logging_setup import get_logger

log = get_logger("store_sync")

RECONCILE_THESIS = "라이브 재대사 시 발견된 미추적 보유 — 뇌 재평가 필요"
SYNC_THESIS = "라이브 전환 시 기존 보유 — 뇌 재평가 필요"

_ORPHAN_SOURCES = frozenset({"fill_mirror", "reconcile_adopted", "synced", "sync"})
# BUY working 행의 진입 계획 유효기간 — 당일 주문(종가 동시호가 → 장후 체결)과
# 다음 기동 채택까지 덮는다.
ENTRY_PLAN_MAX_AGE_SEC = 24 * 3600.0


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


def plan_position_fields(plan: dict, avg: float
                         ) -> tuple[str | None, str | None, float | None,
                                    float | None, dict]:
    """진입 계획 + 실체결 평단 → (strategy, thesis, stop, target, meta).

    즉시 체결 개설(cycle_runner)과 지연 체결 채택(adopt_live_position)이 같은 규칙을
    쓰게 하는 단일 지점. 손절·목표는 계획 시점 가격이 아니라 **실제 평단**으로 잡는다.

    밸류 계획은 stop_pct(하드손절 %)·target_price(적정가 하단)를 직접 싣는다 — 밸류는
    도시에 레벨이 아니라 자기 규칙으로 청산선을 정한다.
    """
    from .agents.wiring import combine_stop_target
    meta = dict(plan.get("meta") or {})
    horizon = str(meta.get("horizon") or "swing")
    params = meta.get("params") if isinstance(meta.get("params"), dict) else None
    note = None
    if plan.get("stop_pct") is not None:
        stop = round(float(avg) * (1 - float(plan["stop_pct"])), 2) if avg else None
        target = plan.get("target_price")
    else:
        stop, target, note = combine_stop_target(
            float(avg), horizon, params, plan.get("invalidation"), plan.get("target"))
    meta["horizon"] = horizon
    if "manager_epoch" in plan:
        meta["manager_epoch"] = plan.get("manager_epoch")
    if note:
        meta["stop_note"] = note
    return plan.get("strategy"), plan.get("thesis"), stop, target, meta


def _pending_entry_plan(store, symbol: str,
                        now: float | None = None) -> tuple[dict, dict] | None:
    """이 종목 BUY working 행(미결·귀속대기) 중 진입 계획이 있는 최신 것 → (plan, row).

    오래된 행(격리 잔존 등)의 계획은 쓰지 않는다 — 며칠 뒤 수동 매수를 옛 논거로 여는
    오귀속보다 고아(뇌 재평가)가 낫다.
    """
    try:
        rows = store.get_working_orders(symbol, side="BUY") or []
    except Exception as e:
        log.warning("채택: BUY working 조회 실패 %s: %s", symbol, e)
        return None
    now = time.time() if now is None else now
    best = None
    for row in rows:
        plan = _parse_meta(_row_get(row, "meta")).get("entry_plan")
        if not isinstance(plan, dict) or not plan:
            continue
        if now - float(_row_get(row, "placed_at") or 0) > ENTRY_PLAN_MAX_AGE_SEC:
            continue
        if best is None or float(_row_get(row, "placed_at") or 0) > float(
                _row_get(best[1], "placed_at") or 0):
            best = (plan, row)
    return best


def adopt_live_position(store, symbol: str, market: str, qty: float, avg: float,
                        *, source: str, thesis: str) -> str:
    """실계좌에서 발견한 보유를 store 에 채택. 'planned'|'promoted'|'opened' 반환.

    봇 자기 주문이 폴링 창 밖에서 체결되면 store open 행이 없다(종가 동시호가 BUY 는
    항상 그렇다). 우선순위:
      1) 그 종목 BUY working 행에 **진입 계획**(뇌 thesis·horizon·전략·도시에 레벨)이
         있으면 그 계획으로 개설 — 고아로 두면 뇌가 '미추적 보유'로 보고 판다(09-30).
      2) armed 계획이 있으면 promote 로 복원(disarm 먼저 하면 손절가가 사라진다).
      3) 둘 다 없을 때만 swing 기본 손절을 임시로 씌운 고아.
    """
    from .agents.wiring import entry_stop_target
    from .engine.entry_basis import BASIS_ORPHAN
    from .shadow_ledger import cancel_shadow_on_fill

    hit = _pending_entry_plan(store, symbol)
    if hit is not None:
        plan, wrow = hit
        strat, plan_thesis, stop, target, meta = plan_position_fields(plan, float(avg))
        meta["deferred_fill"] = {"order_id": _row_get(wrow, "order_id"),
                                 "source": source, "ts": time.time()}
        store.open_position(symbol, market, float(qty), float(avg),
                            strategy=strat, thesis=plan_thesis or thesis,
                            target_price=target, stop_price=stop, meta=meta)
        store.disarm_symbol(symbol)
        cancel_shadow_on_fill(store, symbol)
        try:
            store.log_event("deferred_entry_opened", symbol, {
                "order_id": _row_get(wrow, "order_id"), "qty": float(qty),
                "avg_price": float(avg), "strategy": strat,
                "horizon": meta.get("horizon"), "stop": stop, "target": target,
                "source": source})
        except Exception as e:
            log.warning("채택: 이벤트 기록 실패(무시) %s: %s", symbol, e)
        log.info("채택 %s → 지연 체결 진입 계획 개설 (%s, horizon=%s) stop=%s target=%s "
                 "source=%s", symbol, strat, meta.get("horizon"), stop, target, source)
        return "planned"

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

"""실계좌 → 봇 원장(PaperAccount)/store 동기화 — 라이브 전환·재동기화용.

fetch_* = API(락 밖), apply_* = account/store 병합(broker 락 안).
"""
from __future__ import annotations

import time
from datetime import datetime

from .logging_setup import get_logger
from .store_sync import (RECONCILE_THESIS, SYNC_THESIS, sync_open_qty,
                         _last_sell_fee)
from .strategies.base import Position

log = get_logger("broker.sync")


def _num(v, default: float | None = 0.0) -> float | None:
    if v is None:
        return default
    try:
        s = str(v).strip()
        return float(s) if s else default
    except (TypeError, ValueError):
        return default


def should_sync(broker) -> bool:
    return getattr(broker, "mode", "paper") == "live"


def _last_sell_price(account, symbol: str) -> float | None:
    for f in reversed(account.journal):
        if f.symbol == symbol and f.side == "SELL":
            return float(f.price)
    return None


# 저널 최근 매도를 청산가로 인정하는 시간 창. 재대사 주기(기본 300초)보다 넉넉히
# 크되 무제한은 아니게 — 며칠 전 매도가를 지금 감소분에 찍으면 pnl 이 조용히 틀린다.
_SELL_FALLBACK_MAX_AGE_SEC = 900.0


def _recent_sell_price(account, symbol: str,
                       max_age_sec: float = _SELL_FALLBACK_MAX_AGE_SEC) -> float | None:
    """최근 max_age_sec 안의 저널 매도가. 없거나 오래됐으면 None."""
    now = time.time()
    for f in reversed(account.journal):
        if f.symbol != symbol or f.side != "SELL":
            continue
        try:
            ts = datetime.fromisoformat(str(f.ts)).timestamp()
        except (TypeError, ValueError):
            return None
        return float(f.price) if now - ts <= max_age_sec else None
    return None


def fetch_live_account_data(client, account_seq, *, markets=("KR", "US")) -> dict:
    """실계좌 API 조회만(락 밖). client 또는 TossGateway."""
    cash: dict[str, float] = {}
    for market in markets:
        try:
            bp = client.get_buying_power(account_seq, market) or {}
            c = _num(bp.get("cashBuyingPower"), default=None)
            if c is None:
                log.warning("조회: %s 매수가능금액 파싱 실패(%r)", market, bp)
                continue
            cash[market] = c
        except Exception as e:
            log.warning("조회: %s 매수가능금액 실패 — %s", market, e)

    holdings_ok = True
    items: list = []
    try:
        holdings = client.get_holdings(account_seq) or {}
        items = holdings.get("items") or []
    except Exception as e:
        log.error("조회: 보유 실패 — %s", e)
        holdings_ok = False

    return {"cash": cash, "items": items, "holdings_ok": holdings_ok}


def _parse_holdings_items(items: list) -> tuple[dict, dict]:
    positions: dict[str, Position] = {}
    symbol_market: dict[str, str] = {}
    for it in items:
        try:
            sym = it.get("symbol")
            if not sym:
                continue
            qty = _num(it.get("quantity")) or 0.0
            if qty <= 0:
                continue
            avg = _num(it.get("averagePurchasePrice")) or 0.0
            market = it.get("marketCountry") or "KR"
            positions[sym] = Position(symbol=sym, qty=qty, avg_price=avg)
            symbol_market[sym] = market
        except Exception as e:
            log.warning("보유 항목 처리 실패(생략) %r: %s", it, e)
    return positions, symbol_market


def _items_to_synced(items: list) -> list[dict]:
    synced: list[dict] = []
    for it in items:
        sym = it.get("symbol")
        if not sym:
            continue
        qty = _num(it.get("quantity")) or 0.0
        if qty <= 0:
            continue
        synced.append({
            "symbol": sym,
            "qty": qty,
            "avg": _num(it.get("averagePurchasePrice")) or 0.0,
            "market": it.get("marketCountry") or "KR",
        })
    return synced


def apply_sync_from_live(account, store, data: dict, *, markets=("KR", "US")) -> dict:
    """기동 동기화 apply — broker.run_locked/reconcile 안에서 호출."""
    for market, cash in (data.get("cash") or {}).items():
        account.cash[market] = cash

    holdings_ok = bool(data.get("holdings_ok"))
    items = data.get("items") or []
    synced = _items_to_synced(items)

    if holdings_ok:
        new_positions, new_mkt = _parse_holdings_items(items)
        account.positions = new_positions
        account.symbol_market = new_mkt
    account._save()

    if store is not None and holdings_ok:
        _sync_store(store, synced, account)

    return {"cash": dict(account.cash),
            "positions": [{"symbol": s["symbol"], "qty": s["qty"], "avg": s["avg"]}
                          for s in synced],
            "synced": len(synced)}


def sync_from_live(client, account_seq, account, store=None,
                   *, markets=("KR", "US")) -> dict:
    """레거시/테스트용. 라이브 데몬은 broker.sync_from_live(gateway) 로 락 안 apply."""
    data = fetch_live_account_data(client, account_seq, markets=markets)
    return apply_sync_from_live(account, store, data, markets=markets)


def _sync_store(store, synced: list[dict], account) -> None:
    now = time.time()
    open_rows = {r["symbol"]: r for r in store.get_open_positions()}
    live_syms = set()
    for s in synced:
        sym = s["symbol"]
        live_syms.add(sym)
        try:
            row = open_rows.get(sym)
            if row is not None:
                sync_open_qty(store, row, sym, s["qty"], s["avg"], account,
                              reason="live_sync")
                continue
            meta = {"source": "synced", "entry_thesis": SYNC_THESIS, "synced_ts": now}
            store.open_position(sym, s["market"], s["qty"], s["avg"],
                                strategy=None, thesis=SYNC_THESIS,
                                target_price=None, stop_price=None, meta=meta)
            store.disarm_symbol(sym)
            from .shadow_ledger import cancel_shadow_on_fill
            cancel_shadow_on_fill(store, sym)
        except Exception as e:
            log.warning("동기화: store 미러 실패(생략) %s: %s", sym, e)
    for sym, row in open_rows.items():
        if sym not in live_syms:
            try:
                exit_px = _last_sell_price(account, sym)
                store.close_position(row["id"], exit_price=exit_px, reason="live_sync",
                                     fee=_last_sell_fee(account, sym))
            except Exception as e:
                log.warning("동기화: store 청산 실패(생략) %s: %s", sym, e)


def _consume_settled_sells(store, symbol: str, need: float) -> list[dict]:
    """귀속 대기 중인 매도 체결분을 need 만큼 소비. 실체결가 불명 행은 건너뛴다."""
    picked: list[dict] = []
    try:
        rows = store.get_working_orders(symbol, settled=True)
    except Exception as e:
        log.warning("귀속: 체결분 조회 실패 %s: %s", symbol, e)
        return picked
    for row in rows:
        if need <= 1e-9:
            break
        if row["side"] != "SELL":
            continue
        filled = float(row["filled_qty"] or 0.0)
        applied = float(row["applied_qty"] or 0.0)
        avail = filled - applied
        avg = row["filled_avg"]
        if avail <= 1e-9 or not avg or float(avg) <= 0:
            continue                      # 실체결가 없으면 추정하지 않는다
        # 증분 실체결가: 누적 명목 − 이미 원장에 반영된 명목. 부분체결을 먼저
        # apply_fill 로 넣은 주문에서도 남은 분의 실제 단가가 나온다.
        inc_notional = float(avg) * filled - float(row["applied_notional"] or 0.0)
        inc_fee = max(0.0, float(row["fee"] or 0.0) - float(row["applied_fee"] or 0.0))
        px = inc_notional / avail
        if px <= 0:
            continue
        take = min(avail, need)
        picked.append({"qty": take, "price": px, "fee": inc_fee * (take / avail),
                       "order_id": row["order_id"]})
        need -= take
        try:
            if take >= avail - 1e-9:
                store.delete_working_order(row["order_id"])
            else:                          # 일부만 소비 — 남은 분은 다음 재대사로
                store.update_working_order(
                    row["order_id"], applied_qty=applied + take,
                    applied_notional=float(row["applied_notional"] or 0.0) + px * take,
                    applied_fee=float(row["applied_fee"] or 0.0) + inc_fee * (take / avail))
        except Exception as e:
            log.warning("귀속: 레지스트리 정리 실패 %s: %s", row["order_id"], e)
    return picked


def _attribute_exits(account, store, before: dict, live_pos: dict) -> dict:
    """재대사가 흡수한 보유 감소를 실체결가로 귀속(J3).

    재대사는 cash/positions 를 실계좌 값으로 덮으므로 수량·현금은 맞지만, 폴링
    밖에서 체결된 매도의 **손익**은 아무 데도 안 남는다(apply_fill 을 안 봤으니
    realized_pnl·저널·store pnl 전부 구멍). 감소 수량은 실계좌가 권위이고,
    체결가는 working_orders 에 정산된 주문에서 가져온다.

    order_id 로 못 붙는 감소(수동매도, 장기 고아)는 **추정하지 않는다** —
    unattributed_delta 이벤트만 남긴다. 숫자를 채우고 틀리는 게 더 나쁘다.
    """
    resolved: dict[str, dict] = {}
    for sym, (old_qty, old_avg, market) in before.items():
        new_qty = live_pos[sym].qty if sym in live_pos else 0.0
        dec = old_qty - new_qty
        if dec <= 1e-9:
            continue
        picked = _consume_settled_sells(store, sym, dec) if store is not None else []
        got = sum(p["qty"] for p in picked)
        if got > 1e-9:
            fee = sum(p["fee"] for p in picked)
            px = sum(p["qty"] * p["price"] for p in picked) / got
            account.record_exit_attribution(sym, market, got, px, old_avg, fee,
                                            reason="reconcile_attribution")
            resolved[sym] = {"qty": got, "price": px, "fee": fee}
            log.info("[귀속] %s 매도 %s @ %.2f (수수료 %.2f) — 실체결가로 기입",
                     sym, got, px, fee)
            _emit(store, "exit_attributed", sym, {
                "symbol": sym, "market": market, "qty": got, "price": px,
                "fee": fee, "avg_price": old_avg,
                "order_ids": [p["order_id"] for p in picked]})
        residual = dec - got
        if residual > 1e-9:
            pending = False
            if store is not None:
                try:
                    pending = store.has_working_order(sym)
                except Exception:
                    pending = False
            log.warning("[귀속 불가] %s 보유 %s -> %s (감소 %s) — 실체결가 미상, 추정하지 않음",
                        sym, old_qty, new_qty, residual)
            _emit(store, "unattributed_delta", sym, {
                "symbol": sym, "market": market, "qty_before": old_qty,
                "qty_after": new_qty, "unattributed_qty": residual,
                "avg_price": old_avg, "attributed_qty": got,
                "pending_order": pending})
    return resolved


def _exit_price(account, symbol: str, attributed: dict) -> float | None:
    """store pnl 에 쓸 청산가. 귀속된 실체결가 > 최근 저널 매도가 > None(pnl 미확정).

    저널 폴백은 시간 창을 둔다 — 코드 청산기가 방금 apply_fill 한 매도는 정당한
    출처지만, 며칠 전 매도가를 오늘 감소분에 찍으면 pnl 이 조용히 틀린다.
    """
    hit = attributed.get(symbol)
    if hit:
        return float(hit["price"])
    return _recent_sell_price(account, symbol)


def _emit(store, kind: str, symbol: str, payload: dict) -> None:
    if store is None:
        return
    try:
        store.log_event(kind, symbol, payload)
    except Exception as e:
        log.warning("이벤트 기록 실패(무시) [%s %s]: %s", kind, symbol, e)


def apply_reconcile_from_live(account, store, data: dict,
                              *, markets=("KR", "US")) -> dict:
    """주기 재대사 apply — broker.run_locked/reconcile 안에서 호출."""
    for market, cash in (data.get("cash") or {}).items():
        account.cash[market] = cash

    if not data.get("holdings_ok"):
        return {"cash": dict(account.cash), "holdings": 0,
                "adopted": [], "updated": [], "closed": [], "attributed": {},
                "error": data.get("error", "holdings fetch failed")}

    items = data.get("items") or []
    live_pos, live_mkt = _parse_holdings_items(items)

    # 덮어쓰기 전 평균단가 스냅 — 손익 귀속의 원가 기준(덮으면 사라진다).
    before = {sym: (float(p.qty), float(p.avg_price),
                    account.symbol_market.get(sym, "KR"))
              for sym, p in account.positions.items() if p.is_open}

    account.positions = dict(live_pos)
    account.symbol_market.update(live_mkt)
    for sym in list(account.symbol_market):
        if sym not in live_pos:
            account.symbol_market.pop(sym, None)
    account._save()

    # store 병합 전에 귀속 — 저널에 실체결 매도가 먼저 들어가야 partial/close 의
    # pnl 이 그 가격을 쓴다.
    attributed = _attribute_exits(account, store, before, live_pos)

    adopted: list[str] = []
    updated: list[str] = []
    closed: list[str] = []

    if store is not None:
        open_rows = {r["symbol"]: r for r in store.get_open_positions()}
        for sym, pos in live_pos.items():
            try:
                row = open_rows.get(sym)
                if row is not None:
                    old_qty = float(row["qty"] or 0)
                    if (abs(old_qty - pos.qty) > 1e-9
                            or abs(float(row["avg_price"] or 0) - pos.avg_price) > 1e-9):
                        sync_open_qty(store, row, sym, pos.qty, pos.avg_price, account,
                                      exit_price=_exit_price(account, sym, attributed),
                                      allow_journal_fallback=False,
                                      reason="reconcile")
                        updated.append(sym)
                    else:
                        store.disarm_symbol(sym)
                    continue
                meta = {"source": "reconcile_adopted", "entry_thesis": RECONCILE_THESIS,
                        "synced_ts": time.time()}
                store.open_position(sym, live_mkt.get(sym, "KR"), pos.qty, pos.avg_price,
                                    strategy=None, thesis=RECONCILE_THESIS,
                                    target_price=None, stop_price=None, meta=meta)
                store.disarm_symbol(sym)
                from .shadow_ledger import cancel_shadow_on_fill
                cancel_shadow_on_fill(store, sym)
                adopted.append(sym)
            except Exception as e:
                log.warning("재대사: store 병합 실패(생략) %s: %s", sym, e)
        for sym, row in open_rows.items():
            if sym not in live_pos:
                try:
                    exit_px = _exit_price(account, sym, attributed)
                    store.close_position(row["id"], exit_price=exit_px, reason="reconcile",
                                         fee=_last_sell_fee(account, sym) if exit_px else 0.0)
                    closed.append(sym)
                except Exception as e:
                    log.warning("재대사: store 유령 청산 실패(생략) %s: %s", sym, e)

    if adopted or closed:
        log.info("재대사 병합 — 채택=%s, 청산(유령)=%s, 갱신=%s", adopted, closed, updated)
    return {"cash": dict(account.cash), "holdings": len(live_pos),
            "adopted": adopted, "updated": updated, "closed": closed,
            "attributed": attributed}


def reconcile_from_live(client, account_seq, account, store=None,
                        *, markets=("KR", "US")) -> dict:
    data = fetch_live_account_data(client, account_seq, markets=markets)
    return apply_reconcile_from_live(account, store, data, markets=markets)

"""실계좌 → 봇 원장(PaperAccount)/store 동기화 — 라이브 전환·재동기화용.

fetch_* = API(락 밖), apply_* = account/store 병합(broker 락 안).
"""
from __future__ import annotations

import time

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


def apply_reconcile_from_live(account, store, data: dict,
                              *, markets=("KR", "US")) -> dict:
    """주기 재대사 apply — broker.run_locked/reconcile 안에서 호출."""
    for market, cash in (data.get("cash") or {}).items():
        account.cash[market] = cash

    if not data.get("holdings_ok"):
        return {"cash": dict(account.cash), "holdings": 0,
                "adopted": [], "updated": [], "closed": [],
                "error": data.get("error", "holdings fetch failed")}

    items = data.get("items") or []
    live_pos, live_mkt = _parse_holdings_items(items)

    account.positions = dict(live_pos)
    account.symbol_market.update(live_mkt)
    for sym in list(account.symbol_market):
        if sym not in live_pos:
            account.symbol_market.pop(sym, None)
    account._save()

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
                    exit_px = _last_sell_price(account, sym)
                    store.close_position(row["id"], exit_price=exit_px, reason="reconcile",
                                         fee=_last_sell_fee(account, sym))
                    closed.append(sym)
                except Exception as e:
                    log.warning("재대사: store 유령 청산 실패(생략) %s: %s", sym, e)

    if adopted or closed:
        log.info("재대사 병합 — 채택=%s, 청산(유령)=%s, 갱신=%s", adopted, closed, updated)
    return {"cash": dict(account.cash), "holdings": len(live_pos),
            "adopted": adopted, "updated": updated, "closed": closed}


def reconcile_from_live(client, account_seq, account, store=None,
                        *, markets=("KR", "US")) -> dict:
    data = fetch_live_account_data(client, account_seq, markets=markets)
    return apply_reconcile_from_live(account, store, data, markets=markets)

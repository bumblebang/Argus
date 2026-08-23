"""실계좌 → 봇 원장(PaperAccount)/store 동기화 — 라이브 전환·재동기화용.

라이브에서 봇 원장은 실계좌의 미러여야 한다. config 초기 현금·보유가 실계좌와 어긋나면
사이징이 틀린다. 데몬 기동 시 1회 실계좌를 읽어 현금·포지션을 맞춘다.

실계좌가 진실: 기존 원장/store 상태는 실계좌 기준으로 교체한다.
동기화된 보유는 thesis 가 없을 수 있으므로 코드 청산을 비활성(stop/target=None)하고
재평가 필요로 표시해, 뇌가 홀드/청산을 판단하게 한다.
"""
from __future__ import annotations

import time

from .logging_setup import get_logger
from .strategies.base import Position

log = get_logger("broker.sync")

# 동기화된 보유의 진입 사유 — store.positions.thesis 로 저장돼 pipeline._portfolio 가
# 뇌에게 entry_thesis 로 넘긴다(뇌가 재평가해 홀드/청산 판단).
SYNC_THESIS = "라이브 전환 시 기존 보유 — 뇌 재평가 필요"
# 주기 재대사 중 발견된, 봇이 모르던 실보유(고아) 채택 시의 진입 사유.
RECONCILE_THESIS = "라이브 재대사 시 발견된 미추적 보유 — 뇌 재평가 필요"


def _num(v, default: float | None = 0.0) -> float | None:
    """문자열/None 안전 숫자 파싱. 빈 문자열·None·비정상값은 default."""
    if v is None:
        return default
    try:
        s = str(v).strip()
        return float(s) if s else default
    except (TypeError, ValueError):
        return default


def should_sync(broker) -> bool:
    """라이브 브로커일 때만 실계좌 동기화 대상(페이퍼면 config 초기값 유지)."""
    return getattr(broker, "mode", "paper") == "live"


def sync_from_live(client, account_seq, account, store=None,
                   *, markets=("KR", "US")) -> dict:
    """실계좌(holdings+buying-power)를 봇 원장(PaperAccount)+store 로 미러링.

    반환 요약 {cash, positions:[{symbol,qty,avg}], synced:n}. 예외는 시장/종목 단위로
    삼켜 로깅한다(한 종목·한 시장 실패가 전체 동기화를 죽이지 않게).
    """
    # 1) 현금 동기화 — 시장별 매수가능금액. 실패한 시장은 기존값 유지 + 경고.
    for market in markets:
        try:
            bp = client.get_buying_power(account_seq, market) or {}
            cash = _num(bp.get("cashBuyingPower"), default=None)
            if cash is None:
                log.warning("동기화: %s 매수가능금액 파싱 실패(%r) — 기존값 유지", market, bp)
                continue
            account.cash[market] = cash
        except Exception as e:
            log.warning("동기화: %s 매수가능금액 조회 실패 — 기존값 유지: %s", market, e)

    # 2) 보유 종목 동기화 — 실계좌 items 로 포지션 전면 재구성(실계좌가 진실).
    holdings_ok = True
    items: list = []
    try:
        holdings = client.get_holdings(account_seq) or {}
        items = holdings.get("items") or []
    except Exception as e:
        # 보유 조회 실패 시 원장 포지션을 통째로 비우면 위험 → 기존 포지션 유지.
        log.error("동기화: 보유 조회 실패 — 포지션 미러링 생략(기존 포지션 유지): %s", e)
        holdings_ok = False

    new_positions: dict[str, Position] = {}
    new_symbol_market: dict[str, str] = {}
    synced: list[dict] = []
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
            new_positions[sym] = Position(symbol=sym, qty=qty, avg_price=avg)
            new_symbol_market[sym] = market
            synced.append({"symbol": sym, "qty": qty, "avg": avg, "market": market})
        except Exception as e:
            log.warning("동기화: 보유 항목 처리 실패(생략) %r: %s", it, e)

    if holdings_ok:
        # 실계좌 기준으로 포지션 교체 — 봇 원장의 유령 보유 제거.
        account.positions = new_positions
        account.symbol_market = new_symbol_market
    account._save()      # 현금은 조회 성공분 반영됐으므로 항상 저장.

    # 3) store 미러링(선택) — 멱등.
    if store is not None and holdings_ok:
        _sync_store(store, synced)

    return {"cash": dict(account.cash),
            "positions": [{"symbol": s["symbol"], "qty": s["qty"], "avg": s["avg"]}
                          for s in synced],
            "synced": len(synced)}


def _sync_store(store, synced: list[dict]) -> None:
    """store.positions 를 실보유로 미러(멱등). 실계좌에 없는 open 포지션은 청산."""
    now = time.time()
    open_rows = {r["symbol"]: r for r in store.get_open_positions()}
    live_syms = set()
    for s in synced:
        sym = s["symbol"]
        live_syms.add(sym)
        try:
            row = open_rows.get(sym)
            if row is not None:              # 이미 있으면 수량/평단만 갱신
                store.update_position(row["id"], qty=s["qty"], avg_price=s["avg"])
                continue
            # 코드 청산 비활성(stop/target=None) — thesis 없는 보유는 뇌가 재평가해 청산 판단.
            meta = {"source": "synced", "entry_thesis": SYNC_THESIS, "synced_ts": now}
            store.open_position(sym, s["market"], s["qty"], s["avg"],
                                strategy=None, thesis=SYNC_THESIS,
                                target_price=None, stop_price=None, meta=meta)
        except Exception as e:
            log.warning("동기화: store 미러 실패(생략) %s: %s", sym, e)
    # 실계좌에 없는데 store 에 open 인 종목 → 청산(실계좌가 진실).
    for sym, row in open_rows.items():
        if sym not in live_syms:
            try:
                store.close_position(row["id"], reason="live_sync")
            except Exception as e:
                log.warning("동기화: store 청산 실패(생략) %s: %s", sym, e)


def _parse_holdings_items(items: list) -> tuple[dict, dict]:
    """holdings.items → (positions{sym:Position}, symbol_market{sym:market}). 항목 예외는 생략."""
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
            log.warning("재대사: 보유 항목 처리 실패(생략) %r: %s", it, e)
    return positions, symbol_market


def reconcile_from_live(client, account_seq, account, store=None,
                        *, markets=("KR", "US")) -> dict:
    """주기 재대사(병합) — 실계좌를 봇 원장에 병합해 드리프트를 지운다.

    기동 동기화(sync_from_live)가 원장을 전면 교체하는 것과 달리, 재대사는 봇이 관리 중인
    포지션의 store thesis/손절/목표를 **보존**하고 수량·평단만 실계좌 값으로 맞춘다. 봇이
    모르던 실보유(고아)는 **채택**해 store 에 등록(재평가 thesis, 코드청산 비활성)하고,
    실계좌에 없는 원장/store 포지션은 **청산**(유령 제거). 현금은 실 매수가능금액으로 갱신.

    반드시 broker.reconcile(락) 경유로 호출한다 — 락 밖에서 account.positions 를 갈아끼우면
    진행 중인 gate 순회와 경합한다. 예외는 시장/종목 단위로 삼켜 로깅(한 실패가 전체를
    죽이지 않게). 반환 {cash, holdings, adopted, updated, closed}.
    """
    # 1) 현금(시장별 매수가능금액). 실패한 시장은 기존값 유지.
    for market in markets:
        try:
            bp = client.get_buying_power(account_seq, market) or {}
            cash = _num(bp.get("cashBuyingPower"), default=None)
            if cash is None:
                log.warning("재대사: %s 매수가능금액 파싱 실패(%r) — 기존값 유지", market, bp)
                continue
            account.cash[market] = cash
        except Exception as e:
            log.warning("재대사: %s 매수가능금액 조회 실패 — 기존값 유지: %s", market, e)

    # 2) 실 보유 조회 — 실패 시 이번 주기 포지션 병합 생략(현금은 위에서 이미 반영).
    try:
        holdings = client.get_holdings(account_seq) or {}
        items = holdings.get("items") or []
    except Exception as e:
        log.error("재대사: 보유 조회 실패 — 이번 주기 포지션 병합 생략: %s", e)
        return {"cash": dict(account.cash), "holdings": 0,
                "adopted": [], "updated": [], "closed": [], "error": str(e)}

    live_pos, live_mkt = _parse_holdings_items(items)

    # 3) 원장(account) 병합 — 실 보유는 수량/평단을 실계좌로 맞추고, 실계좌에 없는 원장
    #    보유는 제거(유령). account 는 gate 가 읽는 진실이므로 실계좌와 정확히 일치시킨다.
    account.positions = dict(live_pos)
    account.symbol_market.update(live_mkt)
    for sym in list(account.symbol_market):
        if sym not in live_pos:
            account.symbol_market.pop(sym, None)
    account._save()

    adopted: list[str] = []
    updated: list[str] = []
    closed: list[str] = []

    # 4) store 병합(선택) — 봇 관리 포지션은 수량/평단만 갱신(thesis/손절/목표 보존),
    #    고아는 채택(등록), 실계좌에 없는 open 은 청산.
    if store is not None:
        open_rows = {r["symbol"]: r for r in store.get_open_positions()}
        for sym, pos in live_pos.items():
            try:
                row = open_rows.get(sym)
                if row is not None:                 # 봇 관리 — 수량/평단만(계획 레벨 보존)
                    if (abs((row["qty"] or 0.0) - pos.qty) > 1e-9
                            or abs((row["avg_price"] or 0.0) - pos.avg_price) > 1e-9):
                        store.update_position(row["id"], qty=pos.qty,
                                              avg_price=pos.avg_price)
                        updated.append(sym)
                    continue
                # 고아 채택 — 코드청산 비활성(stop/target=None), 뇌가 재평가.
                meta = {"source": "reconcile_adopted", "entry_thesis": RECONCILE_THESIS,
                        "synced_ts": time.time()}
                store.open_position(sym, live_mkt.get(sym, "KR"), pos.qty, pos.avg_price,
                                    strategy=None, thesis=RECONCILE_THESIS,
                                    target_price=None, stop_price=None, meta=meta)
                adopted.append(sym)
            except Exception as e:
                log.warning("재대사: store 병합 실패(생략) %s: %s", sym, e)
        for sym, row in open_rows.items():
            if sym not in live_pos:
                try:
                    store.close_position(row["id"], reason="reconcile")
                    closed.append(sym)
                except Exception as e:
                    log.warning("재대사: store 유령 청산 실패(생략) %s: %s", sym, e)

    if adopted or closed:
        log.info("재대사 병합 — 채택=%s, 청산(유령)=%s, 갱신=%s", adopted, closed, updated)
    return {"cash": dict(account.cash), "holdings": len(live_pos),
            "adopted": adopted, "updated": updated, "closed": closed}

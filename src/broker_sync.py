"""실계좌 → 봇 원장(PaperAccount)/store 동기화 — 라이브 전환·재동기화용.

fetch_* = API(락 밖), apply_* = account/store 병합(broker 락 안).
"""
from __future__ import annotations

import time
from datetime import datetime

from .logging_setup import get_logger
from .store_sync import (RECONCILE_THESIS, SYNC_THESIS, adopt_live_position,
                         sync_open_qty, _last_sell_fee)
from .strategies.base import Position

log = get_logger("broker.sync")

_EXT_CASH_EPS = 1.0


def _num(v, default: float | None = 0.0) -> float | None:
    if v is None:
        return default
    try:
        s = str(v).strip()
        return float(s) if s else default
    except (TypeError, ValueError):
        return default


def _qty_map(positions: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for sym, p in (positions or {}).items():
        try:
            q = float(getattr(p, "qty", 0) or 0)
        except (TypeError, ValueError):
            continue
        if q > 0:
            out[sym] = q
    return out


def _note_external_cash(account, new_cash: dict, new_pos: dict,
                        new_mkt: dict) -> dict[str, float]:
    """매매로 설명되지 않는 현금 증감 → SoD 기준 이동.

    입금: 현금↑ + 해당 시장 매도 없음. 출금: 현금↓ + 해당 시장 매수 없음.
    같은 창에 매매+입출금이 겹치면 보수적으로 스킵(오보정 방지).
    """
    noted: dict[str, float] = {}
    if not hasattr(account, "adjust_sod_for_external_cash"):
        return noted
    old_qty = _qty_map(account.positions)
    new_qty = _qty_map(new_pos)
    old_mkt = dict(getattr(account, "symbol_market", {}) or {})
    for market, raw in (new_cash or {}).items():
        try:
            old_c = float(account.cash.get(market, 0) or 0)
            new_c = float(raw)
        except (TypeError, ValueError):
            continue
        d_cash = new_c - old_c
        if abs(d_cash) < _EXT_CASH_EPS:
            continue
        sold = bought = False
        for sym in set(old_qty) | set(new_qty):
            sm = (new_mkt or {}).get(sym) or old_mkt.get(sym) or "KR"
            if sm != market:
                continue
            oq, nq = old_qty.get(sym, 0.0), new_qty.get(sym, 0.0)
            if nq < oq - 1e-9:
                sold = True
            if nq > oq + 1e-9:
                bought = True
        if d_cash > 0 and not sold:
            account.adjust_sod_for_external_cash(market, d_cash)
            noted[market] = d_cash
        elif d_cash < 0 and not bought:
            account.adjust_sod_for_external_cash(market, d_cash)
            noted[market] = d_cash
    return noted


def should_sync(broker) -> bool:
    return getattr(broker, "mode", "paper") == "live"


def halt_after_live_sync_failure(broker, store, error: BaseException) -> str:
    """라이브 기동 동기화 실패 → 전역 HALT + 이벤트. HALT 경로 문자열 반환.

    원장이 실계좌와 어긋난 채 주문이 나가면 안 된다. 데몬은 떠 있어도 게이트가
    BUY/SELL 을 전부 막는다. 운영자가 HALT 파일을 지우고 재동기화해야 한다.
    """
    gate = getattr(broker, "gate", None)
    if gate is None or not hasattr(gate, "engage_halt"):
        raise RuntimeError("broker.gate.engage_halt 없음 — HALT 불가") from error
    halt_path = gate.engage_halt(f"live_sync_failed: {error}")
    log.error("실계좌 동기화 실패 — 전역 HALT 활성(%s): %s", halt_path, error)
    if store is not None:
        try:
            store.log_event("error", None, {
                "where": "live_sync", "error": str(error), "halt": str(halt_path)})
        except Exception as e:
            log.warning("live_sync HALT 이벤트 기록 실패: %s", e)
    return str(halt_path)


def startup_sync_halt_reason(sync: dict, markets=()) -> str | None:
    """기동 동기화 결과가 HALT 대상이면 사유 문자열, 아니면 None.

    보유 조회 실패 또는 **요청한 모든 시장**의 현금 조회 실패 — 이때는 신뢰할
    '마지막 성공 스냅샷'이 없다. 일부 시장만 실패면 경고만(주기 재대사가 이어서 재시도).
    """
    if not sync.get("holdings_ok", True):
        err = (sync.get("errors") or {}).get("holdings") or "holdings fetch failed"
        return str(err)
    want = {str(m) for m in (markets or ())}
    failed = {str(m) for m in (sync.get("failed_markets") or [])}
    if want and failed >= want:
        return f"cash fetch failed: {', '.join(sorted(failed))}"
    return None


def record_sync_visibility(broker, store, *, prev_failures: int = 0) -> None:
    """주기 재대사 조회 건강도를 이벤트·로그로 남긴다. 주문은 막지 않는다."""
    health = getattr(broker, "sync_health", None) or {}
    cash_ok = bool(health.get("cash_ok", True))
    holdings_ok = bool(health.get("holdings_ok", True))
    age = broker.sync_stale_sec() if hasattr(broker, "sync_stale_sec") else None
    stale_limit = float(getattr(broker, "sync_stale_error_sec", 3600.0) or 3600.0)
    if cash_ok and holdings_ok:
        if prev_failures > 0 and store is not None:
            try:
                store.log_event("sync_recovered", None, {
                    "last_ok_age_sec": age,
                    "prev_consecutive_failures": prev_failures})
            except Exception as e:
                log.warning("sync_recovered 이벤트 기록 실패: %s", e)
        return
    severity = "error" if (age is None or age >= stale_limit) else "warning"
    payload = {
        "cash_ok": cash_ok, "holdings_ok": holdings_ok,
        "failed_markets": list(health.get("failed_markets") or []),
        "errors": dict(health.get("errors") or {}),
        "last_ok_age_sec": age,
        "consecutive_failures": int(health.get("consecutive_failures") or 0),
        "severity": severity,
    }
    msg = ("실계좌 조회 실패 — 마지막 성공 스냅샷으로 운행 "
           "age=%s failed=%s holdings_ok=%s")
    if severity == "error":
        log.error(msg, age, payload["failed_markets"], holdings_ok)
    else:
        log.warning(msg, age, payload["failed_markets"], holdings_ok)
    if store is not None:
        try:
            store.log_event("sync_degraded", None, payload)
        except Exception as e:
            log.warning("sync_degraded 이벤트 기록 실패: %s", e)


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


# HTTP 레이어(toss_client)가 이미 타임아웃·5xx·429 를 재시도한 뒤의 마지막 보루.
# 토큰 재발급 경합처럼 한 박자 쉬면 풀리는 실패를 여기서 한 번 더 흡수한다.
_FETCH_RETRY_WAITS: tuple[float, ...] = (0.5, 1.5)


def _retry_fetch(what: str, fn):
    """조회를 짧게 재시도. (값, 마지막 오류) — 성공이면 오류는 None."""
    attempts = len(_FETCH_RETRY_WAITS) + 1
    last_err: Exception | None = None
    for i in range(attempts):
        if i:
            time.sleep(_FETCH_RETRY_WAITS[i - 1])
        try:
            return fn(), None
        except Exception as e:
            last_err = e
            log.warning("조회: %s 실패(%d/%d) — %s", what, i + 1, attempts, e)
    return None, last_err


def fetch_live_account_data(client, account_seq, *, markets=("KR", "US")) -> dict:
    """실계좌 API 조회만(락 밖). client 또는 TossGateway.

    실패는 반드시 cash_ok/holdings_ok/failed_markets 로 드러낸다. 현금 조회 실패가
    표식 없이 넘어가면 게이트·사이징이 낡은 현금을 진실로 믿는다 — 보유 조회만
    holdings_ok 를 달고 현금은 조용히 넘어가던 게 원장 드리프트의 출발점이었다.
    """
    cash: dict[str, float] = {}
    failed_markets: list[str] = []
    errors: dict[str, str] = {}
    for market in markets:
        bp, err = _retry_fetch(
            f"{market} 매수가능금액",
            lambda m=market: client.get_buying_power(account_seq, m) or {})
        if err is not None:
            failed_markets.append(market)
            errors[market] = str(err)
            continue
        c = _num((bp or {}).get("cashBuyingPower"), default=None)
        if c is None:
            log.warning("조회: %s 매수가능금액 파싱 실패(%r)", market, bp)
            failed_markets.append(market)
            errors[market] = f"파싱 실패({bp!r})"
            continue
        cash[market] = c

    holdings, herr = _retry_fetch(
        "보유", lambda: client.get_holdings(account_seq) or {})
    holdings_ok = herr is None
    items: list = []
    if holdings_ok:
        items = (holdings or {}).get("items") or []
    else:
        log.error("조회: 보유 실패 — %s", herr)
        errors["holdings"] = str(herr)

    return {"cash": cash, "items": items, "holdings_ok": holdings_ok,
            "cash_ok": not failed_markets, "failed_markets": failed_markets,
            "errors": errors}


def _health_fields(data: dict) -> dict:
    """fetch 결과의 실패 비트만 추려 apply 반환에 그대로 실어 보낸다."""
    return {"cash_ok": bool(data.get("cash_ok", True)),
            "holdings_ok": bool(data.get("holdings_ok")),
            "failed_markets": list(data.get("failed_markets") or []),
            "errors": dict(data.get("errors") or {})}


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
            market = str(it.get("marketCountry") or "KR").upper()
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
            "market": str(it.get("marketCountry") or "KR").upper(),
        })
    return synced


def apply_sync_from_live(account, store, data: dict, *, markets=("KR", "US")) -> dict:
    """기동 동기화 apply — broker.run_locked/reconcile 안에서 호출."""
    before = {sym: (float(p.qty), float(p.avg_price),
                    account.symbol_market.get(sym, "KR"))
              for sym, p in account.positions.items() if p.is_open}

    for market, cash in (data.get("cash") or {}).items():
        account.cash[market] = cash

    holdings_ok = bool(data.get("holdings_ok"))
    items = data.get("items") or []
    synced = _items_to_synced(items)
    live_pos: dict = {}

    if holdings_ok:
        new_positions, new_mkt = _parse_holdings_items(items)
        live_pos = new_positions
        account.positions = new_positions
        account.symbol_market = new_mkt
    account._save()

    if store is not None and holdings_ok:
        _sync_store(store, synced, account)
        # 주기 재대사와 같이 BUY working applied 보정 — 기동만 빠져 이중예약.
        _sync_buy_working_applied(store, before, live_pos)

    return {"cash": dict(account.cash),
            "positions": [{"symbol": s["symbol"], "qty": s["qty"], "avg": s["avg"]}
                          for s in synced],
            "synced": len(synced), **_health_fields(data)}


def sync_from_live(client, account_seq, account, store=None,
                   *, markets=("KR", "US")) -> dict:
    """레거시/테스트용. 라이브 데몬은 broker.sync_from_live(gateway) 로 락 안 apply."""
    data = fetch_live_account_data(client, account_seq, markets=markets)
    return apply_sync_from_live(account, store, data, markets=markets)


def _sync_store(store, synced: list[dict], account) -> None:
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
            adopt_live_position(
                store, sym, s["market"], s["qty"], s["avg"],
                source="synced", thesis=SYNC_THESIS)
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
        from .broker import incremental_fill
        inc = incremental_fill(
            filled, float(avg), float(row["fee"] or 0.0),
            applied, float(row["applied_notional"] or 0.0),
            float(row["applied_fee"] or 0.0))
        if inc is None:
            continue
        avail, px, inc_fee_full = inc
        take = min(avail, need)
        picked.append({"qty": take, "price": px, "fee": inc_fee_full * (take / avail),
                       "order_id": row["order_id"]})
        need -= take
        try:
            if take >= avail - 1e-9:
                store.delete_working_order(row["order_id"])
            else:                          # 일부만 소비 — 남은 분은 다음 재대사로
                store.update_working_order(
                    row["order_id"], applied_qty=applied + take,
                    applied_notional=float(row["applied_notional"] or 0.0) + px * take,
                    applied_fee=float(row["applied_fee"] or 0.0) + inc_fee_full * (take / avail))
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
            # order_id 를 reason 에 박아 finish_live 멱등 가드가 누적VWAP≠증분가
            # 여도 같은 주문을 재귀속하지 않게 한다(#65 세 다리 보강).
            oids = [str(p["order_id"]) for p in picked if p.get("order_id")]
            why = ("reconcile_attribution:" + ",".join(oids)
                   if oids else "reconcile_attribution")
            account.record_exit_attribution(sym, market, got, px, old_avg, fee,
                                            reason=why)
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


def _scrub_fully_applied_buy_working(store) -> None:
    """applied≥filled 인 settled BUY 고아 행 삭제(TTL 허위 귀속실패 방지).

    need=0 인 재기동에서도 돌아야 한다 — 이전 보정으로 applied 만 맞추고
    삭제를 빼먹으면 30분 뒤 unattributed_fill 경고가 난다.
    """
    try:
        rows = store.get_working_orders(side="BUY", settled=True) or []
    except Exception as e:
        log.warning("재대사: settled BUY 스크럽 조회 실패: %s", e)
        return
    for row in rows:
        filled = float(row.get("filled_qty") or 0.0)
        applied = float(row.get("applied_qty") or 0.0)
        if filled - applied > 1e-9:
            continue
        try:
            store.delete_working_order(row["order_id"])
        except Exception as e:
            log.warning("재대사: BUY 전량반영 행 삭제 실패 %s: %s",
                        row.get("order_id"), e)


def _bump_buy_working_applied(store, row: dict, take: float, px: float,
                              fee_take: float) -> bool:
    """applied_* 올리고, 미반영분이 없고(settled 또는 전량체결)면 삭제. 성공 시 True.

    PARTIAL 미결 행은 filled==applied 여도 qty>filled 잔량이 남으므로 유지한다 —
    지우면 working room 선차감이 빠진다.
    """
    oid = row["order_id"]
    filled = float(row.get("filled_qty") or 0.0)
    applied = float(row.get("applied_qty") or 0.0)
    applied_n = float(row.get("applied_notional") or 0.0)
    applied_f = float(row.get("applied_fee") or 0.0)
    qty = float(row.get("qty") or 0.0)
    new_applied = applied + take
    try:
        store.update_working_order(
            oid,
            applied_qty=new_applied,
            applied_notional=applied_n + max(0.0, px) * take,
            applied_fee=applied_f + fee_take)
        if filled - new_applied <= 1e-9:
            settled = row.get("settled_at") is not None
            fully_filled = filled >= qty - 1e-9 and qty > 1e-9
            if settled or fully_filled:
                store.delete_working_order(oid)
        return True
    except Exception as e:
        log.warning("재대사: BUY applied 갱신 실패 %s: %s", oid, e)
        return False


def _sync_buy_working_applied(store, before: dict, live_pos: dict) -> None:
    """재대사/기동이 holdings 로 흡수한 BUY 증가분만큼 working.applied_* 를 올린다.

    흐름(주기 재대사·기동 sync 공통):
      1) sweep 이 종결/취소를 반영(기동은 broker.sync_from_live 가 선행)
      2) holdings 증가분(need)을 BUY working 의 filled−applied 에서 소비
      3) settled/미결 모두 대상 — sweep 선 settled 누락 시 30분 이중예약
      4) filled_avg 없으면 주문가(price) 폴백 — 가격도 없으면 수량만이라도 반영
         (continue 하면 need 를 버려 영구 미보정·이중예약)
      5) applied≥filled 이면 행 삭제 + settled 고아 스크럽
         (잔존 시 TTL 허위 귀속실패 경고)

    applied_notional 은 incremental_fill 증분 명목(가능하면). 폴백은 주문가×take.
    """
    if store is None:
        return
    from .broker import incremental_fill
    _scrub_fully_applied_buy_working(store)
    syms = set(before) | set(live_pos)
    for sym in syms:
        old_qty = float(before[sym][0]) if sym in before else 0.0
        new_qty = float(live_pos[sym].qty) if sym in live_pos else 0.0
        need = new_qty - old_qty
        if need <= 1e-9:
            continue
        rows: list = []
        try:
            rows.extend(store.get_working_orders(sym, side="BUY", settled=False) or [])
            rows.extend(store.get_working_orders(sym, side="BUY", settled=True) or [])
        except Exception as e:
            log.warning("재대사: BUY working 조회 실패 %s: %s", sym, e)
            continue
        for row in rows:
            if need <= 1e-9:
                break
            filled = float(row.get("filled_qty") or 0.0)
            applied = float(row.get("applied_qty") or 0.0)
            avail_qty = filled - applied
            if avail_qty <= 1e-9:
                continue
            avg = row.get("filled_avg")
            fee = float(row.get("fee") or 0.0)
            applied_n = float(row.get("applied_notional") or 0.0)
            applied_f = float(row.get("applied_fee") or 0.0)
            limit_px = float(row.get("price") or 0.0)
            px = 0.0
            fee_take = 0.0
            take = min(avail_qty, need)
            if avg and float(avg) > 0:
                inc = incremental_fill(
                    filled, float(avg), fee, applied, applied_n, applied_f)
                if inc is not None:
                    avail_qty, px, inc_fee_full = inc
                    take = min(avail_qty, need)
                    fee_take = (inc_fee_full * (take / avail_qty)
                                if avail_qty > 1e-9 else 0.0)
                else:
                    px = limit_px
            else:
                # avg 결측 — 주문가 폴백. 가격도 없으면 수량만이라도 applied 반영.
                px = limit_px
            if take <= 1e-9:
                continue
            if not _bump_buy_working_applied(store, row, take, px, fee_take):
                continue
            need -= take
    _scrub_fully_applied_buy_working(store)


def apply_reconcile_from_live(account, store, data: dict,
                              *, markets=("KR", "US")) -> dict:
    """주기 재대사 apply — broker.run_locked/reconcile 안에서 호출."""
    items = data.get("items") or []
    live_pos, live_mkt = (
        _parse_holdings_items(items) if data.get("holdings_ok") else ({}, {}))
    new_cash = dict(data.get("cash") or {})
    # 현금 덮기 전에 입출금 보정 — SoD 델타가 입금을 이익으로 위장하지 않게.
    ext = _note_external_cash(account, new_cash, live_pos, live_mkt) if new_cash else {}

    for market, cash in new_cash.items():
        account.cash[market] = cash

    if not data.get("holdings_ok"):
        return {"cash": dict(account.cash), "holdings": 0,
                "adopted": [], "updated": [], "closed": [], "attributed": {},
                "external_cash": ext,
                "error": data.get("error", "holdings fetch failed"),
                **_health_fields(data)}

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
    _sync_buy_working_applied(store, before, live_pos)

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
                adopt_live_position(
                    store, sym, live_mkt.get(sym, "KR"), pos.qty, pos.avg_price,
                    source="reconcile_adopted", thesis=RECONCILE_THESIS)
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
            "attributed": attributed, "external_cash": ext,
            **_health_fields(data)}


def reconcile_from_live(client, account_seq, account, store=None,
                        *, markets=("KR", "US")) -> dict:
    data = fetch_live_account_data(client, account_seq, markets=markets)
    return apply_reconcile_from_live(account, store, data, markets=markets)

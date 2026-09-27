"""09-28 #79 후속 점검 — 주문 재시도 분류·local 해소·예약·재대사 보류·데이 트랙·청산 순서·그림자.

1) 멱등 재시도 전제: 공식 스펙 v1.2.17 — 409 는 request-in-progress(불명)와
   opposite-pending-order-exists(확정 거부)가 섞여 있고, 422 idempotency-key-conflict 는 불명.
2) 재시도 중 결과 불명 시도가 있었으면 이후 401·429·토큰 실패도 '미접수 확정'이 아니다.
5) 주문 전송 총 데드라인(브로커+게이트웨이 락 점유 상한).
11) 주문 429 는 Retry-After 를 줄이지 않는다 — 데드라인을 넘기면 대기 대신 종료.
"""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from src import toss_client as tc
from src.broker import Broker, _limit_price_matches
from src.broker_sync import apply_reconcile_from_live
from src.engine.store import Store
from src.risk_gate import Order, Reservation, RiskGate
from src.toss_client import TossAPIError
from tests.test_order_safety_0927 import (_Client, _acct, _err, _gate, _hold, _local_row,
                                          _order, _order_client, _resp, _setup)

KST = timezone(timedelta(hours=9))


def _body(code):
    return json.dumps({"error": {"code": code, "message": "x"}})


# ── 1) 에러 코드 분류(공식 스펙) ─────────────────────────────────
def test_opposite_pending_409_is_definitive_and_not_retried(tmp_path, monkeypatch):
    client = _order_client(tmp_path, monkeypatch)
    calls = []
    client.session.request = lambda *a, **k: (calls.append(1),
                                               _resp(409, _body("opposite-pending-order-exists")))[1]
    with pytest.raises(TossAPIError) as ei:
        client._request("order_create", json={"symbol": "A", "clientOrderId": "k"})
    assert ei.value.definitive and len(calls) == 1


def test_request_in_progress_409_is_retried_and_ambiguous(tmp_path, monkeypatch):
    client = _order_client(tmp_path, monkeypatch)
    client.session.request = lambda *a, **k: _resp(409, _body("request-in-progress"))
    with pytest.raises(TossAPIError) as ei:
        client._request("order_create", json={"symbol": "A", "clientOrderId": "k"})
    assert not ei.value.definitive


def test_idempotency_key_conflict_is_not_definitive():
    assert not _err(422, "idempotency-key-conflict").definitive
    assert _err(409, "opposite-pending-order-exists").definitive


# ── 2) 불명 시도 뒤 실패는 확정 거부가 아니다 ─────────────────────
def test_timeout_then_429_is_not_definitive(tmp_path, monkeypatch):
    client = _order_client(tmp_path, monkeypatch)
    seq = [tc.requests.exceptions.ReadTimeout("slow"), _resp(429, "x"), _resp(429, "x")]

    def req(*a, **k):
        r = seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    client.session.request = req
    with pytest.raises(TossAPIError) as ei:
        client._request("order_create", json={"symbol": "A", "clientOrderId": "k"})
    assert ei.value.status == 429 and not ei.value.definitive


def test_5xx_then_business_4xx_is_not_definitive(tmp_path, monkeypatch):
    """첫 요청이 5xx 로 접수됐을 수 있으면, 재시도의 422 도 '미접수'를 증명하지 않는다."""
    client = _order_client(tmp_path, monkeypatch)
    seq = [_resp(500, "x"), _resp(422, _body("insufficient-buying-power"))]
    client.session.request = lambda *a, **k: seq.pop(0)
    with pytest.raises(TossAPIError) as ei:
        client._request("order_create", json={"symbol": "A", "clientOrderId": "k"})
    assert not ei.value.definitive


def test_token_failure_after_ambiguous_attempt_is_not_definitive(tmp_path, monkeypatch):
    client = _order_client(tmp_path, monkeypatch)
    n = {"h": 0}
    real_headers = client._headers

    def headers(seq=None):
        n["h"] += 1
        if n["h"] >= 2:
            raise RuntimeError("token endpoint down")
        return real_headers(seq)

    client._headers = headers

    def req(*a, **k):
        raise tc.requests.exceptions.ReadTimeout("slow")

    client.session.request = req
    with pytest.raises(TossAPIError) as ei:
        client._request("order_create", json={"symbol": "A", "clientOrderId": "k"})
    assert not ei.value.definitive


def test_connect_timeout_is_unsent_so_429_stays_definitive(tmp_path, monkeypatch):
    client = _order_client(tmp_path, monkeypatch)
    seq = [tc.requests.exceptions.ConnectTimeout("no route"), _resp(429, "x"),
           _resp(429, "x")]

    def req(*a, **k):
        r = seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    client.session.request = req
    with pytest.raises(TossAPIError) as ei:
        client._request("order_create", json={"symbol": "A", "clientOrderId": "k"})
    assert ei.value.definitive


def test_broker_keeps_local_row_when_failure_not_definitive(tmp_path):
    """불명 실패면 local 행을 지우지 않는다 — 지우면 같은 주문을 또 낼 수 있다."""
    client = _Client(place_exc=TossAPIError(429, "x", definitive=False))
    store, broker = _setup(tmp_path, client)
    res = broker.execute(Order("005930", "KR", "BUY", 1, 70_000), "entry")
    assert not res.ok and res.status == "UNKNOWN"
    rows = store.get_working_orders()
    assert len(rows) == 1 and rows[0]["order_id"].startswith("local:")


# ── 5·11) 데드라인·Retry-After ────────────────────────────────
def test_order_retries_stop_at_deadline(tmp_path, monkeypatch):
    client = _order_client(tmp_path, monkeypatch)
    t = {"now": 0.0}
    monkeypatch.setattr(tc.time, "monotonic", lambda: t["now"])
    calls = []

    def req(*a, **k):
        calls.append(k.get("timeout"))
        t["now"] += 6.0                                # 시도마다 6초(응답 타임아웃 근처)
        return _resp(503, "x")

    client.session.request = req
    with pytest.raises(TossAPIError) as ei:
        client._request("order_create", json={"symbol": "A", "clientOrderId": "k"})
    assert len(calls) == 2                             # 3회째는 데드라인(10초) 초과
    assert calls[0] == tc._ORDER_TIMEOUT
    assert not ei.value.definitive


def test_order_429_long_retry_after_not_truncated(tmp_path, monkeypatch):
    client = _order_client(tmp_path, monkeypatch)
    slept = []
    monkeypatch.setattr(tc.time, "sleep", lambda s: slept.append(s))
    calls = []
    client.session.request = lambda *a, **k: (calls.append(1), _resp(
        429, "x", headers={"Retry-After": "30"}))[1]
    with pytest.raises(TossAPIError) as ei:
        client._request("order_create", json={"symbol": "A", "clientOrderId": "k"})
    assert len(calls) == 1 and slept == []             # 2초로 줄여 또 429 를 맞지 않는다
    assert ei.value.definitive


def test_expired_token_401_reissues_once(tmp_path, monkeypatch):
    client = _order_client(tmp_path, monkeypatch)
    inval = []
    client._invalidate_token = lambda: inval.append(1)
    seq = [_resp(401, _body("expired-token")), _resp(200, "{}", {"result": {"orderId": "OK"}})]
    client.session.request = lambda *a, **k: seq.pop(0)
    assert client._request("order_create", json={"symbol": "A"}) == {"orderId": "OK"}
    assert inval == [1]


# ── 4) local 해소 ────────────────────────────────────────────
def test_local_resolution_settles_from_order_detail(tmp_path):
    """목록 항목은 대조용 — 정산은 단건 상세로(목록이 PENDING 이어도 상세가 FILLED)."""
    client = _Client()
    store, broker = _setup(tmp_path, client)
    placed = _local_row(store)
    client.orders = {"OPEN": [_order(placed, status="PENDING", filled=0)], "CLOSED": []}
    client.detail = {"status": "FILLED",
                     "execution": {"filledQuantity": "10", "averageFilledPrice": "70100",
                                   "commission": "10", "tax": "0"}}
    out = broker.sweep_working_orders()
    assert out.get("local_resolved") == 1 and out["settled"] == 1


def test_local_absent_not_confirmed_when_open_list_paginated(tmp_path):
    class _Paged(_Client):
        def list_orders(self, account_seq, *, status, symbol=None, **kw):
            return {"orders": [], "nextCursor": "x" if status == "OPEN" else None,
                    "hasNext": status == "OPEN"}

    store, broker = _setup(tmp_path, _Paged())
    _local_row(store, side="BUY")
    out = broker.sweep_working_orders()
    assert out.get("local_absent") is None
    assert len(store.get_working_orders()) == 1


def test_us_truncated_limit_price_matches():
    assert _limit_price_matches("185.50", 185.509)
    assert _limit_price_matches("0.1234", 0.12349)
    assert not _limit_price_matches("185.40", 185.509)


# ── 6) 예약: 시장별 BP 재대사·노출 한도 ────────────────────────
def test_cash_reconcile_is_per_market(tmp_path):
    store, broker = _setup(tmp_path, _Client(), working_order_abandon_ttl_sec=-1)
    past = time.time() - 60
    for oid, mkt in (("K1", "KR"), ("U1", "US")):
        store.upsert_working_order(order_id=oid, symbol=oid, market=mkt, side="BUY",
                                   qty=1, price=100, status="PENDING", placed_at=past)
    # US 매수가능금액 조회 실패 — KR 만 덮었다.
    broker.reconcile(lambda acct: {"cash": {"KR": 1.0}, "failed_markets": ["US"],
                                   "cash_ok": False})
    held = {r.order_id: r.bp_held for r in broker._working_reservations()}
    assert held == {"K1": True, "U1": False}


def test_bp_held_reservation_counts_for_exposure_not_cash(tmp_path):
    gate = RiskGate({"capital": {"KR": 1_000_000}, "max_position_pct": 1.0,
                     "max_positions": 5, "daily_loss_limit_pct": 0.5,
                     "max_gross_exposure": 0.5,
                     "kill_switch_file": str(tmp_path / "HALT")})
    acct = _acct(tmp_path, cash=1_000_000)
    held = Reservation("A", "KR", "BUY", 4, 100_000, order_id="W", bp_held=True)
    # 현금: bp_held 는 이미 BP 에 반영 — 900k 주문도 현금으론 통과해야 한다.
    # 노출: 400k(미체결) + 200k > 500k 한도 → 거부.
    d = gate.check(Order("B", "KR", "BUY", 2, 100_000), acct, reserved=[held])
    assert not d.approved and "익스포저" in d.reason
    d2 = gate.check(Order("B", "KR", "BUY", 1, 90_000), acct, reserved=[held])
    assert d2.approved


# ── 7) 미해소 매도: 심볼 단위 재대사 보류 ──────────────────────
def _holdings(items, cash=900_000):
    return {"cash": {"KR": cash}, "items": items, "holdings_ok": True,
            "cash_ok": True, "failed_markets": []}


def _item(sym, qty, avg=70_000):
    return {"symbol": sym, "quantity": str(qty), "averagePurchasePrice": str(avg),
            "marketCountry": "KR", "currency": "KRW"}


def test_reconcile_defers_only_unresolved_sell_symbol(tmp_path):
    store = Store(tmp_path / "t.db")
    acct = _acct(tmp_path)
    acct.apply_fill("005930", "KR", "BUY", 10, 70_000, 0.0, "seed")
    acct.apply_fill("000660", "KR", "BUY", 5, 100_000, 0.0, "seed")
    live = _holdings([_item("000660", 7, 100_000)])       # 005930 은 실계좌에서 사라짐
    res = apply_reconcile_from_live(acct, store, live, markets=("KR",),
                                    defer_symbols={"005930"})
    assert acct.position("005930").qty == 10.0            # 보류 — 손익 귀속 전 흡수 금지
    assert acct.position("000660").qty == 7.0             # 나머지는 정상 재대사
    assert acct.cash["KR"] == 900_000
    assert res["deferred_sell_symbols"] == ["005930"]
    assert res["cash_markets"] == ["KR"]


def test_partial_fill_of_live_sell_is_attributed(tmp_path):
    """살아 있는 지정가 매도의 재대사 사이 부분체결 — 감소를 실체결가로 귀속."""
    store = Store(tmp_path / "t.db")
    acct = _acct(tmp_path)
    acct.apply_fill("005930", "KR", "BUY", 10, 70_000, 0.0, "seed")
    store.upsert_working_order(order_id="S1", symbol="005930", market="KR", side="SELL",
                               qty=10, price=72_000, status="PARTIAL_FILLED",
                               filled_qty=4, filled_avg=72_000, fee=40)
    res = apply_reconcile_from_live(acct, store, _holdings([_item("005930", 6)]),
                                    markets=("KR",))
    assert res["attributed"]["005930"]["qty"] == 4.0
    rows = store.get_working_orders()
    assert len(rows) == 1 and rows[0]["applied_qty"] == 4.0   # 살아 있는 행은 안 지움


# ── 8) 등록 실패 심볼 해제 ───────────────────────────────────
def test_register_failed_symbol_cleared_when_registry_matches(tmp_path):
    client = _Client()
    store, broker = _setup(tmp_path, client)
    broker._register_failed_symbols.add("005930")
    store.upsert_working_order(order_id="R1", symbol="005930", market="KR", side="BUY",
                               qty=1, price=70_000, status="PENDING")
    client.orders = {"OPEN": [{"orderId": "R1", "symbol": "005930"}], "CLOSED": []}
    client.detail = {"status": "PENDING", "execution": {"filledQuantity": "0"}}
    out = broker.sweep_working_orders()
    assert out.get("register_failed_cleared") == ["005930"]
    assert "005930" not in broker._register_failed_symbols


def test_register_failed_symbol_kept_with_unknown_open_order(tmp_path):
    client = _Client()
    store, broker = _setup(tmp_path, client)
    broker._register_failed_symbols.add("005930")
    client.orders = {"OPEN": [{"orderId": "GHOST", "symbol": "005930"}], "CLOSED": []}
    broker.sweep_working_orders()
    assert "005930" in broker._register_failed_symbols


# ── 9) live_order_rejected 소비자 ──────────────────────────────
def test_rejected_event_counted_and_formatted():
    from scripts.alert_check import _format_live_order_msg
    from src.ops_exec import summarize_exec
    out = summarize_exec([{"ts": time.time(), "kind": "live_order_rejected",
                           "symbol": "005930",
                           "payload": {"side": "SELL", "code": "price-out-of-range"}}])
    assert out["n_rejected"] == 1
    msg = _format_live_order_msg("live_order_rejected", "005930",
                                 {"side": "SELL", "code": "price-out-of-range"}, {})
    assert "거부" in msg and "price-out-of-range" in msg


def test_dashboard_chip_for_rejected():
    from scripts.dashboard import _live_trade_chip
    assert "매도거부" in _live_trade_chip("live_order_rejected", {"side": "SELL"})

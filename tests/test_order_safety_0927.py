"""09-27 전반 점검 — 주문 안전 구멍.

1) 4xx 확정 거부가 local UNKNOWN 으로 영속 → 그 종목 매도 포함 양방향 영구 차단(손절 불가)
2) order_create 재시도 1회 고정 → 401 재발급·429 백오프까지 꺼져 1) 로 이어짐
6) QUARANTINED 행이 예약에서 빠져 살아 있을 수 있는 주문의 홀드를 다른 종목이 재사용
7) 같은 종목 in-flight 가 하나라도 있으면 working 행 전체가 예약에서 빠짐
8) 결과 불명 경로가 _mark_inflight 전에 반환 → activity_gen 미증가 → 낡은 재대사 스냅샷 미검출
9) local SELL 이 sweep 에서 block_reconcile 을 안 걸어 재대사가 감소를 먼저 흡수(손익 소실)
   + local 행을 서버 주문 목록으로 해소(영구 고아 방지)
"""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from src.broker import Broker
from src.engine.store import Store
from src.paper_account import PaperAccount
from src.risk_gate import Order, Reservation, RiskGate
from src import toss_client as tc
from src.toss_client import TossAPIError, TossClient
from src.config import TossCredentials

KST = timezone(timedelta(hours=9))


def _gate(tmp_path):
    return RiskGate({"capital": {"KR": 10_000_000}, "max_position_pct": 1.0,
                     "max_positions": 5, "daily_loss_limit_pct": 0.5,
                     "kill_switch_file": str(tmp_path / "HALT")})


def _acct(tmp_path, cash=1_000_000):
    return PaperAccount(cash={"KR": cash}, fee_rate={"KR": 0.0},
                        slippage_bps={"KR": 0.0}, state_path=tmp_path / "a.json")


def _err(status, code):
    return TossAPIError(status, json.dumps({"error": {"code": code, "message": "x"}}))


class _Client:
    def __init__(self, place_exc=None, detail=None, orders=None, list_exc=None):
        self.place_exc = place_exc
        self.detail = detail or {"status": "FILLED",
                                 "execution": {"filledQuantity": 1,
                                               "averageFilledPrice": 70_000,
                                               "commission": 0, "tax": 0}}
        self.orders = orders or {"OPEN": [], "CLOSED": []}
        self.list_exc = list_exc
        self.placed = []

    def get_sellable(self, seq, symbol):
        return {"sellableQuantity": 100}

    def orderbook(self, symbol):
        return None

    def place_order(self, **kw):
        self.placed.append(kw)
        if self.place_exc is not None:
            exc, self.place_exc = self.place_exc, None
            raise exc
        return {"orderId": f"O{len(self.placed)}"}

    def get_order(self, account_seq, order_id):
        return self.detail

    def cancel_order(self, account_seq, order_id):
        return {}

    def list_orders(self, account_seq, *, status, symbol=None, **kw):
        if self.list_exc is not None:
            raise self.list_exc
        return {"orders": list(self.orders.get(status, [])),
                "nextCursor": None, "hasNext": False}


def _setup(tmp_path, client, **kw):
    store = Store(tmp_path / "t.db")
    broker = Broker(account=_acct(tmp_path), gate=_gate(tmp_path), client=client,
                    mode="live", account_seq="A1", live_markets=["KR"], store=store,
                    reconcile_poll_attempts=1, reconcile_poll_sec=0.0, **kw)
    return store, broker


def _hold(broker, symbol="005930", qty=10, px=70_000):
    broker.account.apply_fill(symbol, "KR", "BUY", qty, px, 0.0, "seed")


# ── 1) 확정 거부 ─────────────────────────────────────────────
def test_definitive_4xx_is_rejected_not_unknown(tmp_path):
    client = _Client(place_exc=_err(422, "price-out-of-range"))
    store, broker = _setup(tmp_path, client)
    _hold(broker)
    res = broker.execute(Order("005930", "KR", "SELL", 10, 70_000), "stop",
                         exit_reason="stop_hit")
    assert not res.ok and res.status == "REJECTED"
    assert "price-out-of-range" in res.reject_reason
    assert store.get_working_orders() == []              # local 행 정리
    kinds = [r["kind"] for r in store.conn.execute("SELECT kind FROM events")]
    assert "live_order_rejected" in kinds
    # 다음 손절은 막히지 않는다
    again = broker.execute(Order("005930", "KR", "SELL", 10, 70_000), "stop",
                           exit_reason="stop_hit")
    assert again.ok and len(client.placed) == 2


@pytest.mark.parametrize("exc", [
    TossAPIError(500, "boom"),
    TossAPIError(409, "request-in-progress"),
    tc.requests.exceptions.ReadTimeout("slow"),
])
def test_ambiguous_failure_stays_unknown_and_blocks(tmp_path, exc):
    client = _Client(place_exc=exc)
    store, broker = _setup(tmp_path, client)
    _hold(broker)
    res = broker.execute(Order("005930", "KR", "SELL", 10, 70_000), "stop")
    assert not res.ok and res.status == "UNKNOWN"
    rows = store.get_working_orders()
    assert len(rows) == 1 and rows[0]["order_id"].startswith("local:")
    assert not broker.execute(Order("005930", "KR", "SELL", 10, 70_000), "again").ok
    assert len(client.placed) == 1


def test_token_failure_before_send_is_definitive(tmp_path, monkeypatch):
    """토큰 발급 실패는 주문이 안 나간 것 — 미접수 확정."""
    monkeypatch.setattr(tc, "_TOKEN_CACHE", tmp_path / ".token.json")
    monkeypatch.setattr(tc.time, "sleep", lambda _s: None)
    client = TossClient(TossCredentials(base_url="https://x", client_id="i",
                                        client_secret="s", account_no="1"))

    def boom():
        raise tc.requests.exceptions.ConnectionError("dns")

    client._ensure_token = boom
    sent = []
    client.session.request = lambda *a, **k: sent.append(1)
    with pytest.raises(TossAPIError) as ei:
        client.place_order(account_seq=1, symbol="005930", side="SELL", qty=1,
                           order_type="LIMIT", price=1, client_order_id="c1")
    assert ei.value.definitive and sent == []


# ── 2) order_create 재시도 ────────────────────────────────────
def _resp(status, body="{}", json_body=None, headers=None):
    from unittest.mock import MagicMock
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.text = body
    r.content = body.encode()
    r.json.return_value = json_body if json_body is not None else {}
    return r


def _order_client(tmp_path, monkeypatch):
    monkeypatch.setattr(tc, "_TOKEN_CACHE", tmp_path / ".token.json")
    monkeypatch.setattr(tc.time, "sleep", lambda _s: None)
    client = TossClient(TossCredentials(base_url="https://x", client_id="i",
                                        client_secret="s", account_no="1"))
    client._token, client._token_exp = "tok", time.time() + 3600
    client._token_issued_at = time.time() - 600
    return client


def test_order_create_with_client_order_id_retries_transient(tmp_path, monkeypatch):
    client = _order_client(tmp_path, monkeypatch)
    seq = [tc.requests.exceptions.ReadTimeout("slow"), _resp(503, "x"),
           _resp(200, "{}", {"result": {"orderId": "OK1"}})]
    bodies = []

    def fake(method, url, json=None, **kw):
        bodies.append(json)
        r = seq.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    client.session.request = fake
    out = client._request("order_create", json={"symbol": "A", "clientOrderId": "k1"})
    assert out == {"orderId": "OK1"}
    assert len(bodies) == 3 and all(b["clientOrderId"] == "k1" for b in bodies)


def test_order_create_retries_401_and_429_without_key(tmp_path, monkeypatch):
    """401 invalid-token·429 는 서버가 처리 전 거절 — 멱등키 없어도 재시도 안전."""
    client = _order_client(tmp_path, monkeypatch)
    client._ensure_token = lambda: "tok"
    seq = [_resp(401, "invalid-token"), _resp(429, "x", headers={"Retry-After": "30"}),
           _resp(200, "{}", {"result": {"orderId": "OK2"}})]
    client.session.request = lambda *a, **k: seq.pop(0)
    assert client._request("order_create", json={"symbol": "A"}) == {"orderId": "OK2"}


def test_order_create_429_exhausted_is_definitive(tmp_path, monkeypatch):
    client = _order_client(tmp_path, monkeypatch)
    client.session.request = lambda *a, **k: _resp(429, "x")
    with pytest.raises(TossAPIError) as ei:
        client._request("order_create", json={"symbol": "A", "clientOrderId": "k"})
    assert ei.value.status == 429 and ei.value.definitive


def test_error_definitive_classification():
    assert _err(422, "insufficient-buying-power").definitive
    assert _err(422, "insufficient-buying-power").code == "insufficient-buying-power"
    assert _err(400, "invalid-request").definitive
    assert not TossAPIError(409, "x").definitive
    assert not TossAPIError(408, "x").definitive
    assert not TossAPIError(500, "x").definitive


# ── 6·7) 예약 ─────────────────────────────────────────────────
def test_inflight_sell_does_not_drop_working_buy_hold(tmp_path):
    store, broker = _setup(tmp_path, _Client())
    store.upsert_working_order(order_id="B1", symbol="005930", market="KR",
                               side="BUY", qty=5, price=100_000, status="PENDING")
    with broker._lock:
        broker._inflight["r1"] = Reservation(
            symbol="005930", market="KR", side="SELL", qty=3, price=100_000,
            order_id="S1", placed_at=time.time())
        res = broker._active_reservations()
    sides = sorted((r.side, r.order_id) for r in res)
    assert sides == [("BUY", "B1"), ("SELL", "S1")]


def test_inflight_same_order_not_double_counted(tmp_path):
    store, broker = _setup(tmp_path, _Client())
    store.upsert_working_order(order_id="B1", symbol="005930", market="KR",
                               side="BUY", qty=5, price=100_000, status="PENDING")
    with broker._lock:
        broker._inflight["r1"] = Reservation(
            symbol="005930", market="KR", side="BUY", qty=5, price=100_000,
            order_id="B1", placed_at=time.time())
        res = broker._active_reservations()
    assert [r.order_id for r in res] == ["B1"]


def test_quarantined_row_keeps_hold_until_cash_reconciled(tmp_path):
    store, broker = _setup(tmp_path, _Client(), working_order_abandon_ttl_sec=60.0)
    store.upsert_working_order(order_id="Q1", symbol="005930", market="KR",
                               side="BUY", qty=1, price=600_000, status="QUARANTINED",
                               placed_at=time.time() - 3600)
    with broker._lock:
        assert [r.order_id for r in broker._active_reservations()] == ["Q1"]
    broker.reconcile(lambda acct: {})
    with broker._lock:
        assert broker._active_reservations() == []


# ── 8) activity_gen ──────────────────────────────────────────
def test_unknown_send_bumps_activity_generation(tmp_path):
    """결과 불명으로 끝나도 전송 시작이 세대를 올려 조회 중 재대사를 연기시킨다."""
    client = _Client(place_exc=TossAPIError(500, "boom"))
    store, broker = _setup(tmp_path, client)
    gen = broker.activity_generation()
    broker.execute(Order("005930", "KR", "BUY", 1, 70_000), "entry")
    assert broker.activity_generation() > gen
    out = broker.reconcile(lambda acct: {"applied": True}, expect_gen=gen)
    assert out.get("deferred") and out["reason"] == "stale_snapshot"


# ── 9) local 행 해소 ─────────────────────────────────────────
def _local_row(store, side="SELL", age=120.0, qty=10, price=70_000):
    placed = time.time() - age
    store.upsert_working_order(
        order_id="local:abc", symbol="005930", market="KR", side=side, qty=qty,
        price=price, status="UNKNOWN", placed_at=placed,
        meta={"client_order_id": "abc", "order_type": "LIMIT"})
    return placed


def _order(placed, *, oid="REAL1", side="SELL", status="FILLED", filled=10,
           qty="10", price="70000"):
    return {"orderId": oid, "symbol": "005930", "side": side, "orderType": "LIMIT",
            "status": status, "price": price, "quantity": qty,
            "orderedAt": datetime.fromtimestamp(placed + 1, tz=KST).isoformat(),
            "execution": {"filledQuantity": str(filled),
                          "averageFilledPrice": "70100" if filled else None,
                          "commission": "10", "tax": "0"}}


def test_sweep_resolves_local_row_from_order_list(tmp_path):
    client = _Client()
    store, broker = _setup(tmp_path, client)
    placed = _local_row(store)
    filled = _order(placed)
    client.orders = {"OPEN": [], "CLOSED": [filled]}
    client.detail = filled
    out = broker.sweep_working_orders()
    assert out.get("local_resolved") == 1
    rows = store.get_working_orders()
    assert len(rows) == 1 and rows[0]["order_id"] == "REAL1"
    # 종결 체결분은 귀속 대기(settled)로 남아 재대사가 실체결가로 손익을 넣는다
    assert rows[0]["settled_at"] is not None and rows[0]["filled_qty"] == 10.0
    assert not out["block_reconcile"]


def test_sweep_drops_local_row_absent_from_server(tmp_path):
    client = _Client()
    store, broker = _setup(tmp_path, client)
    placed = _local_row(store)
    # 한참 전 주문·반대 방향 주문은 짝도 근접도 아니다
    client.orders = {"OPEN": [_order(placed - 3600, oid="OLD")],
                     "CLOSED": [_order(placed, oid="OTHER", side="BUY")]}
    out = broker.sweep_working_orders()
    assert out.get("local_absent") == 1
    assert store.get_working_orders() == []


def test_near_miss_order_blocks_absent_verdict(tmp_path):
    """US 지정가 절삭(12.345→12.34) 등으로 엄격 대조가 빗나가도 근접 주문이 있으면 지우지 않는다."""
    client = _Client()
    store, broker = _setup(tmp_path, client)
    placed = _local_row(store, price=70_050)
    client.orders = {"OPEN": [_order(placed, oid="NEAR", price="70000")], "CLOSED": []}
    out = broker.sweep_working_orders()
    assert out.get("local_absent") is None and out["local_orphan"] == 1
    assert store.get_working_orders()[0]["order_id"] == "local:abc"


def test_sweep_keeps_young_local_row_within_grace(tmp_path):
    store, broker = _setup(tmp_path, _Client())
    _local_row(store, age=5.0)
    out = broker.sweep_working_orders()
    assert out.get("local_absent") is None
    assert len(store.get_working_orders()) == 1
    assert out["block_reconcile"]                         # SELL — 재대사 보류


def test_unresolved_local_sell_blocks_reconcile(tmp_path):
    client = _Client(list_exc=RuntimeError("ORDER_HISTORY down"))
    store, broker = _setup(tmp_path, client)
    _local_row(store, side="SELL")
    out = broker.sweep_working_orders()
    assert out["block_reconcile"] and out["local_orphan"] == 1


def test_unresolved_local_buy_does_not_block_reconcile(tmp_path):
    client = _Client(list_exc=RuntimeError("ORDER_HISTORY down"))
    store, broker = _setup(tmp_path, client)
    _local_row(store, side="BUY")
    out = broker.sweep_working_orders()
    assert not out["block_reconcile"] and out["local_orphan"] == 1


def test_ambiguous_local_match_not_resolved(tmp_path):
    client = _Client()
    store, broker = _setup(tmp_path, client)
    placed = _local_row(store)
    client.orders = {"OPEN": [], "CLOSED": [_order(placed, oid="R1"),
                                            _order(placed, oid="R2")]}
    out = broker.sweep_working_orders()
    assert out["local_orphan"] == 1
    assert store.get_working_orders()[0]["order_id"] == "local:abc"


def test_local_row_meta_records_client_order_id(tmp_path):
    client = _Client(place_exc=TossAPIError(500, "boom"))
    store, broker = _setup(tmp_path, client)
    broker.execute(Order("005930", "KR", "BUY", 1, 70_000), "entry")
    row = store.get_working_orders()[0]
    meta = json.loads(row["meta"]) if isinstance(row["meta"], str) else row["meta"]
    assert meta["client_order_id"] == client.placed[0]["client_order_id"]
    assert meta["order_type"] == "LIMIT"

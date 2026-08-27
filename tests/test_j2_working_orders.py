"""J2 — 미체결 주문이 어디에도 기록되지 않아 고아가 되고 매 틱 재발주되던 결함.

재현(수정 전):
  - _finish_live 가 filled=0 이면 이벤트만 남기고 주문을 잊는다
  - 취소하지 않으므로 접수된 주문이 장중 내내 살아 있다
  - 존 진입기/청산기는 미체결 시 armed·보유를 유지하고 매 틱 다시 execute
  - 토스 API 에 미체결 '목록' 조회가 없어 재시작 후 발견 불가
"""
import time

from src.broker import Broker
from src.engine.store import Store
from src.paper_account import PaperAccount
from src.risk_gate import Order, RiskGate


def _gate(tmp_path):
    return RiskGate({"capital": {"KR": 10_000_000}, "max_position_pct": 1.0,
                     "max_positions": 5, "daily_loss_limit_pct": 0.5,
                     "kill_switch_file": str(tmp_path / "HALT")})


def _acct(tmp_path, cash=1_000_000):
    return PaperAccount(cash={"KR": cash}, fee_rate={"KR": 0.0},
                        slippage_bps={"KR": 0.0}, state_path=tmp_path / "a.json")


class _Client:
    """place 는 성공, 체결 조회는 지정 상태를 반환하는 스텁."""

    def __init__(self, detail=None):
        self.detail = detail or {"status": "PENDING",
                                 "execution": {"filledQuantity": 0}}
        self.placed = []
        self.canceled = []
        self.details_after_cancel = None

    def get_sellable(self, seq, symbol):
        return {"sellableQuantity": 100}

    def orderbook(self, symbol, market=None):
        return None

    def place_order(self, **kw):
        self.placed.append(kw)
        return {"orderId": f"O{len(self.placed)}"}

    def get_order(self, account_seq, order_id):
        if self.canceled and self.details_after_cancel is not None:
            return self.details_after_cancel
        return self.detail

    def cancel_order(self, account_seq, order_id):
        self.canceled.append(order_id)
        return {"ok": True}


def _broker(tmp_path, store, client, **kw):
    return Broker(account=_acct(tmp_path), gate=_gate(tmp_path), client=client,
                  mode="live", account_seq="A1", live_markets=["KR"],
                  store=store, reconcile_poll_attempts=1, reconcile_poll_sec=0.0,
                  **kw)


def _setup(tmp_path, client=None, **kw):
    store = Store(tmp_path / "t.db")
    client = client or _Client()
    return store, client, _broker(tmp_path, store, client, **kw)


# ── 레지스트리 기록 ─────────────────────────────────────────────
def test_unfilled_order_is_recorded(tmp_path):
    store, client, broker = _setup(tmp_path)
    res = broker.execute(Order("005930", "KR", "BUY", 1, 70_000), "entry")
    assert not res.ok
    rows = store.get_working_orders()
    assert len(rows) == 1
    r = rows[0]
    assert (r["order_id"], r["symbol"], r["side"], r["qty"]) == ("O1", "005930", "BUY", 1.0)
    assert r["status"] == "PENDING" and r["filled_qty"] == 0.0


def test_partial_fill_leaves_remainder_working(tmp_path):
    """부분체결은 성공 반환이지만 잔량이 살아 있다 — 예전엔 잊었다."""
    client = _Client({"status": "PARTIAL_FILLED",
                      "execution": {"filledQuantity": 3,
                                    "averageFilledPrice": 70_000,
                                    "commission": 0, "tax": 0}})
    store, client, broker = _setup(tmp_path, client=client)
    res = broker.execute(Order("005930", "KR", "BUY", 10, 70_000), "entry")
    assert res.ok and res.filled_qty == 3
    rows = store.get_working_orders()
    assert len(rows) == 1 and rows[0]["filled_qty"] == 3.0


def test_fully_filled_order_not_recorded(tmp_path):
    client = _Client({"status": "FILLED",
                      "execution": {"filledQuantity": 1,
                                    "averageFilledPrice": 70_000,
                                    "commission": 0, "tax": 0}})
    store, client, broker = _setup(tmp_path, client=client)
    assert broker.execute(Order("005930", "KR", "BUY", 1, 70_000), "entry").ok
    assert store.get_working_orders() == []


def test_rejected_order_not_recorded(tmp_path):
    client = _Client({"status": "REJECTED", "execution": {"filledQuantity": 0}})
    store, client, broker = _setup(tmp_path, client=client)
    broker.execute(Order("005930", "KR", "BUY", 1, 70_000), "entry")
    assert store.get_working_orders() == []


def test_unknown_poll_failure_is_recorded(tmp_path):
    """place 성공 + get_order 전부 실패 → UNKNOWN.

    등록 조건이 _PENDING 만이면 표·예약이 비고 inflight 해제와 함께
    J1(이중지출)·J2(재발주)가 동시에 열린다. 주문조회가 흔들릴 때가
    미체결 잔존과 겹치기 쉬운 구간이다.
    """
    class _FailPoll(_Client):
        def get_order(self, account_seq, order_id):
            raise RuntimeError("ORDER_HISTORY down")

    store, client, broker = _setup(tmp_path, client=_FailPoll())
    res = broker.execute(Order("005930", "KR", "BUY", 1, 70_000), "entry")
    assert not res.ok
    rows = store.get_working_orders()
    assert len(rows) == 1
    assert rows[0]["order_id"] == "O1" and rows[0]["status"] == "UNKNOWN"


def test_unknown_blocks_reorder_and_holds_buying_power(tmp_path):
    """UNKNOWN 등록 후 동심볼 재발주 거부 + 타심볼 매수여력 홀드."""
    class _FailPoll(_Client):
        def get_order(self, account_seq, order_id):
            raise RuntimeError("ORDER_HISTORY down")

    store = Store(tmp_path / "t.db")
    client = _FailPoll()
    broker = _broker(tmp_path, store, client)  # cash 1_000_000
    broker.execute(Order("005930", "KR", "BUY", 1, 600_000), "tick1")
    assert len(store.get_working_orders()) == 1
    assert len(client.placed) == 1

    again = broker.execute(Order("005930", "KR", "BUY", 1, 600_000), "tick2")
    assert not again.ok and "미체결" in (again.reject_reason or "")
    assert len(client.placed) == 1

    other = broker.execute(Order("000660", "KR", "BUY", 1, 600_000), "other")
    assert not other.ok and "매수여력" in (other.reject_reason or "")
    assert len(client.placed) == 1


# ── 재발주 차단 (매 틱 중복 발주 재현) ──────────────────────────
def test_reorder_blocked_while_working(tmp_path):
    store, client, broker = _setup(tmp_path)
    broker.execute(Order("005930", "KR", "BUY", 1, 70_000), "tick1")
    assert len(client.placed) == 1
    res = broker.execute(Order("005930", "KR", "BUY", 1, 70_000), "tick2")
    assert not res.ok and "미체결" in (res.reject_reason or "")
    assert len(client.placed) == 1, "두 번째 틱에서 발주되면 안 된다"


def test_working_buy_does_not_block_stop_sell(tmp_path):
    """미체결 BUY 가 같은 종목 손절 SELL 을 막던 구멍 — ExitExecutor 경로 재현."""
    from src.engine.execution import ExitExecutor

    class _Trig:
        kind = "stop_hit"

    class _SellFills(_Client):
        def get_order(self, account_seq, order_id):
            if order_id == "WB1":
                return {"status": "PENDING", "execution": {"filledQuantity": 0}}
            return {"status": "FILLED",
                    "execution": {"filledQuantity": 10, "averageFilledPrice": 65_000,
                                  "commission": 0, "tax": 0}}

    store = Store(tmp_path / "t.db")
    client = _SellFills()
    broker = _broker(tmp_path, store, client)
    broker.account.apply_fill("005930", "KR", "BUY", 10, 70_000, 0.0, "seed")
    store.open_position("005930", "KR", 10, 70_000, stop_price=66_000)
    store.upsert_working_order(order_id="WB1", symbol="005930", market="KR",
                               side="BUY", qty=1, price=70_000, status="PENDING")

    ex = ExitExecutor(broker, store)
    assert ex("005930", "KR", 65_000, _Trig()) is True
    assert len(client.placed) == 1
    assert client.placed[0]["side"] == "SELL"
    assert store.has_working_order("005930", side="BUY")
    assert broker.position("005930").qty == 0


def test_stop_sell_cancels_opposing_working_buy(tmp_path):
    """손절 SELL 직전 같은 종목 미체결 BUY 를 취소 — 청산 후 재롱 방지."""
    from src.engine.execution import ExitExecutor

    class _Trig:
        kind = "stop_hit"

    class _CancelBuyThenSell(_Client):
        def get_order(self, account_seq, order_id):
            if order_id in self.canceled or order_id == "WB1":
                if order_id in self.canceled:
                    return {"status": "CANCELED",
                            "execution": {"filledQuantity": 0}}
                return {"status": "PENDING", "execution": {"filledQuantity": 0}}
            return {"status": "FILLED",
                    "execution": {"filledQuantity": 10, "averageFilledPrice": 65_000,
                                  "commission": 0, "tax": 0}}

    store = Store(tmp_path / "t.db")
    client = _CancelBuyThenSell()
    broker = _broker(tmp_path, store, client)
    broker.account.apply_fill("005930", "KR", "BUY", 10, 70_000, 0.0, "seed")
    store.open_position("005930", "KR", 10, 70_000, stop_price=66_000)
    store.upsert_working_order(order_id="WB1", symbol="005930", market="KR",
                               side="BUY", qty=1, price=70_000, status="PENDING")

    ex = ExitExecutor(broker, store)
    assert ex("005930", "KR", 65_000, _Trig()) is True
    assert client.canceled == ["WB1"]
    assert not store.has_working_order("005930", side="BUY")
    assert len(client.placed) == 1 and client.placed[0]["side"] == "SELL"


def test_same_side_sell_still_blocks_without_replace(tmp_path):
    """같은 방향·같은(비공격) 가격 미체결 SELL 은 재발주 차단 유지."""
    store = Store(tmp_path / "t.db")
    client = _Client({"status": "FILLED",
                      "execution": {"filledQuantity": 1,
                                    "averageFilledPrice": 65_000,
                                    "commission": 0, "tax": 0}})
    broker = _broker(tmp_path, store, client)
    broker.account.apply_fill("005930", "KR", "BUY", 1, 70_000, 0.0, "seed")
    store.upsert_working_order(order_id="WS1", symbol="005930", market="KR",
                               side="SELL", qty=1, price=65_000, status="PENDING")
    res = broker.execute(Order("005930", "KR", "SELL", 1, 65_000), "stop")
    assert not res.ok and "미체결" in (res.reject_reason or "")
    assert client.placed == [] and client.canceled == []


def test_aggressive_sell_cancels_working_without_waiting_sweep(tmp_path):
    """가격이 더 빠진 공격 지정가 — 재대사 sweep 전에 execute 경로에서 교체."""
    store = Store(tmp_path / "t.db")

    class _Replace(_Client):
        def get_order(self, account_seq, order_id):
            if order_id in self.canceled:
                return {"status": "CANCELED", "execution": {"filledQuantity": 0}}
            return self.detail

    client = _Replace({"status": "FILLED",
                       "execution": {"filledQuantity": 1,
                                     "averageFilledPrice": 60_000,
                                     "commission": 0, "tax": 0}})
    broker = _broker(tmp_path, store, client, working_order_ttl_sec=600.0)
    broker.account.apply_fill("005930", "KR", "BUY", 1, 70_000, 0.0, "seed")
    store.upsert_working_order(order_id="WS1", symbol="005930", market="KR",
                               side="SELL", qty=1, price=65_000, status="PENDING")
    res = broker.execute(Order("005930", "KR", "SELL", 1, 60_000), "stop_reprice")
    assert res.ok
    assert client.canceled == ["WS1"]
    assert len(client.placed) == 1
    assert all(r["order_id"] != "WS1" for r in store.get_working_orders())


def test_ttl_release_on_execute_not_only_reconcile_timer(tmp_path):
    """TTL 경과 working 은 reconcile_sec 를 기다리지 않고 execute 때 취소."""
    store = Store(tmp_path / "t.db")

    class _Replace(_Client):
        def get_order(self, account_seq, order_id):
            if order_id in self.canceled:
                return {"status": "CANCELED", "execution": {"filledQuantity": 0}}
            return self.detail

    client = _Replace({"status": "FILLED",
                       "execution": {"filledQuantity": 1,
                                     "averageFilledPrice": 65_000,
                                     "commission": 0, "tax": 0}})
    broker = _broker(tmp_path, store, client, working_order_ttl_sec=60.0)
    broker.account.apply_fill("005930", "KR", "BUY", 1, 70_000, 0.0, "seed")
    store.upsert_working_order(order_id="WS1", symbol="005930", market="KR",
                               side="SELL", qty=1, price=65_000, status="PENDING",
                               placed_at=time.time() - 120)
    res = broker.execute(Order("005930", "KR", "SELL", 1, 65_000), "stop_retry")
    assert res.ok
    assert client.canceled == ["WS1"]
    assert len(client.placed) == 1


def test_other_symbol_still_allowed(tmp_path):
    client = _Client({"status": "FILLED",
                      "execution": {"filledQuantity": 1,
                                    "averageFilledPrice": 1_000,
                                    "commission": 0, "tax": 0}})
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path, store, client)
    store.upsert_working_order(order_id="X1", symbol="005930", market="KR",
                               side="BUY", qty=1, price=1_000, status="PENDING")
    assert broker.execute(Order("000660", "KR", "BUY", 1, 1_000), "other").ok


def test_block_can_be_disabled(tmp_path):
    store, client, broker = _setup(tmp_path, block_on_working_order=False)
    broker.execute(Order("005930", "KR", "BUY", 1, 70_000), "tick1")
    broker.execute(Order("005930", "KR", "BUY", 1, 70_000), "tick2")
    assert len(client.placed) == 2


# ── 미체결 잔량이 게이트 예약으로 잡히는지 ──────────────────────
def test_working_order_holds_buying_power(tmp_path):
    """접수된 주문은 증권사가 현금을 홀드하지만 로컬 cash 는 그대로다."""
    store = Store(tmp_path / "t.db")
    client = _Client({"status": "FILLED",
                      "execution": {"filledQuantity": 1,
                                    "averageFilledPrice": 600_000,
                                    "commission": 0, "tax": 0}})
    broker = _broker(tmp_path, store, client)      # cash 1,000,000
    store.upsert_working_order(order_id="X1", symbol="005930", market="KR",
                               side="BUY", qty=1, price=600_000, status="PENDING")
    res = broker.execute(Order("000660", "KR", "BUY", 1, 600_000), "second")
    assert not res.ok and "매수여력" in (res.reject_reason or "")


def test_filled_portion_excluded_from_hold(tmp_path):
    store = Store(tmp_path / "t.db")
    client = _Client({"status": "FILLED",
                      "execution": {"filledQuantity": 1,
                                    "averageFilledPrice": 500_000,
                                    "commission": 0, "tax": 0}})
    broker = _broker(tmp_path, store, client)
    store.upsert_working_order(order_id="X1", symbol="005930", market="KR",
                               side="BUY", qty=10, price=100_000,
                               status="PARTIAL_FILLED", filled_qty=9)
    # 잔량 1주×100,000 만 홀드 → 500,000 주문은 통과
    assert broker.execute(Order("000660", "KR", "BUY", 1, 500_000), "x").ok


# ── 정산/취소 스윕 ──────────────────────────────────────────────
def test_sweep_deletes_terminal_order(tmp_path):
    store = Store(tmp_path / "t.db")
    client = _Client({"status": "CANCELED", "execution": {"filledQuantity": 0}})
    broker = _broker(tmp_path, store, client)
    store.upsert_working_order(order_id="X1", symbol="005930", market="KR",
                               side="BUY", qty=1, price=1_000, status="PENDING")
    out = broker.sweep_working_orders()
    assert out["settled"] == 1
    assert store.get_working_orders() == []


def test_sweep_cancels_after_ttl(tmp_path):
    store = Store(tmp_path / "t.db")
    client = _Client({"status": "PENDING", "execution": {"filledQuantity": 0}})
    client.details_after_cancel = {"status": "CANCELED",
                                  "execution": {"filledQuantity": 0}}
    broker = _broker(tmp_path, store, client, working_order_ttl_sec=60.0)
    store.upsert_working_order(order_id="X1", symbol="005930", market="KR",
                               side="BUY", qty=1, price=1_000, status="PENDING",
                               placed_at=time.time() - 120)
    out = broker.sweep_working_orders()
    assert out["canceled"] == 1 and client.canceled == ["X1"]
    assert store.get_working_orders() == []


def test_sweep_keeps_order_within_ttl(tmp_path):
    store = Store(tmp_path / "t.db")
    client = _Client({"status": "PENDING", "execution": {"filledQuantity": 0}})
    broker = _broker(tmp_path, store, client, working_order_ttl_sec=600.0)
    store.upsert_working_order(order_id="X1", symbol="005930", market="KR",
                               side="BUY", qty=1, price=1_000, status="PENDING")
    out = broker.sweep_working_orders()
    assert out["working"] == 1 and client.canceled == []
    assert len(store.get_working_orders()) == 1


def test_negative_ttl_never_cancels(tmp_path):
    store = Store(tmp_path / "t.db")
    client = _Client({"status": "PENDING", "execution": {"filledQuantity": 0}})
    broker = _broker(tmp_path, store, client, working_order_ttl_sec=-1.0)
    store.upsert_working_order(order_id="X1", symbol="005930", market="KR",
                               side="BUY", qty=1, price=1_000, status="PENDING",
                               placed_at=time.time() - 100_000)
    broker.sweep_working_orders()
    assert client.canceled == [] and len(store.get_working_orders()) == 1


def test_cancel_failure_keeps_order_working(tmp_path):
    """취소가 실패하면 지우지 않는다 — 지우면 재발주가 풀린다."""
    class _BadCancel(_Client):
        def cancel_order(self, account_seq, order_id):
            raise RuntimeError("취소 거부")

    store = Store(tmp_path / "t.db")
    client = _BadCancel({"status": "PENDING", "execution": {"filledQuantity": 0}})
    broker = _broker(tmp_path, store, client, working_order_ttl_sec=0.0)
    store.upsert_working_order(order_id="X1", symbol="005930", market="KR",
                               side="BUY", qty=1, price=1_000, status="PENDING",
                               placed_at=time.time() - 10)
    out = broker.sweep_working_orders()
    assert out["cancel_failed"] == 1
    assert len(store.get_working_orders()) == 1


def test_unconfirmed_cancel_keeps_order_working(tmp_path):
    """취소 요청은 갔지만 종결 확인이 안 되면 남긴다."""
    store = Store(tmp_path / "t.db")
    client = _Client({"status": "PENDING", "execution": {"filledQuantity": 0}})
    client.details_after_cancel = {"status": "PENDING_CANCEL",
                                   "execution": {"filledQuantity": 0}}
    broker = _broker(tmp_path, store, client, working_order_ttl_sec=0.0)
    store.upsert_working_order(order_id="X1", symbol="005930", market="KR",
                               side="BUY", qty=1, price=1_000, status="PENDING",
                               placed_at=time.time() - 10)
    out = broker.sweep_working_orders()
    assert out["cancel_failed"] == 1
    rows = store.get_working_orders()
    assert len(rows) == 1 and rows[0]["status"] == "PENDING_CANCEL"


def test_sweep_records_partial_fill_without_touching_ledger(tmp_path):
    """체결 반영은 재대사가 단일 소유자 — 스윕이 apply_fill 하면 이중 계상."""
    store = Store(tmp_path / "t.db")
    client = _Client({"status": "PARTIAL_FILLED",
                      "execution": {"filledQuantity": 4,
                                    "averageFilledPrice": 1_000}})
    broker = _broker(tmp_path, store, client, working_order_ttl_sec=-1.0)
    store.upsert_working_order(order_id="X1", symbol="005930", market="KR",
                               side="BUY", qty=10, price=1_000, status="PENDING")
    broker.sweep_working_orders()
    assert store.get_working_orders()[0]["filled_qty"] == 4.0
    assert broker.account.position("005930").qty == 0
    assert broker.account.journal == []


def test_sweep_survives_api_failure(tmp_path):
    class _Boom(_Client):
        def get_order(self, account_seq, order_id):
            raise RuntimeError("api down")

    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path, store, _Boom(), working_order_ttl_sec=0.0)
    store.upsert_working_order(order_id="X1", symbol="005930", market="KR",
                               side="BUY", qty=1, price=1_000, status="PENDING")
    out = broker.sweep_working_orders()
    assert out["working"] == 1
    assert len(store.get_working_orders()) == 1     # 조회 실패 시 지우지 않는다


def test_sweep_noop_in_paper_mode(tmp_path):
    store = Store(tmp_path / "t.db")
    broker = Broker(account=_acct(tmp_path), gate=_gate(tmp_path), mode="paper",
                    store=store)
    assert broker.sweep_working_orders() == {"skipped": True}


# ── 재시작 복구: 이 표가 유일한 경로 ────────────────────────────
def test_restart_recovers_orphan_order(tmp_path):
    """프로세스가 죽어도 새 브로커가 레지스트리로 고아 주문을 찾아 취소한다."""
    db = tmp_path / "t.db"
    store1 = Store(db)
    client1 = _Client()
    broker1 = _broker(tmp_path, store1, client1)
    broker1.execute(Order("005930", "KR", "BUY", 1, 70_000), "before crash")
    store1.close()

    store2 = Store(db)
    client2 = _Client({"status": "PENDING", "execution": {"filledQuantity": 0}})
    client2.details_after_cancel = {"status": "CANCELED",
                                    "execution": {"filledQuantity": 0}}
    broker2 = _broker(tmp_path, store2, client2, working_order_ttl_sec=0.0)
    assert len(store2.get_working_orders()) == 1, "재시작 후에도 남아 있어야 한다"
    out = broker2.sweep_working_orders()
    assert out["canceled"] == 1 and client2.canceled == ["O1"]


def test_reconcile_not_deferred_by_working_orders(tmp_path):
    """미체결 주문으로 재대사를 막으면 buying_power 갱신이 멈춘다."""
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path, store, _Client())
    store.upsert_working_order(order_id="X1", symbol="005930", market="KR",
                               side="BUY", qty=1, price=1_000, status="PENDING")
    assert broker.reconcile(lambda acct: {"applied": True}) == {"applied": True}


def test_upsert_is_idempotent(tmp_path):
    store = Store(tmp_path / "t.db")
    for st, fq in (("PENDING", 0), ("PARTIAL_FILLED", 5)):
        store.upsert_working_order(order_id="X1", symbol="005930", market="KR",
                                   side="BUY", qty=10, price=1_000,
                                   status=st, filled_qty=fq)
    rows = store.get_working_orders()
    assert len(rows) == 1
    assert rows[0]["status"] == "PARTIAL_FILLED" and rows[0]["filled_qty"] == 5.0

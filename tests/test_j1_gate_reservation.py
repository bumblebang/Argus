"""J1 — 접수~체결 사이 구간에서 계좌 단위 한도가 두 번 통과되던 결함.

재현(수정 전): 라이브 place_order 후 apply_fill 까지 수 초가 걸리고 그동안
cash/positions 는 주문 전 그대로다. in-flight 는 **같은 심볼만** 막으므로,
다른 심볼 주문이 같은 현금을 다시 쓰고 게이트를 통과한다 → 현금 음수.
"""
import threading
import time

import pytest

from src.broker import Broker
from src.paper_account import PaperAccount
from src.risk_gate import Order, Reservation, RiskGate


def _gate(tmp_path, **over):
    limits = {"capital": {"KR": 1_000_000}, "max_position_pct": 1.0,
              "max_positions": 5, "daily_loss_limit_pct": 0.5,
              "kill_switch_file": str(tmp_path / "HALT")}
    limits.update(over)
    return RiskGate(limits)


def _acct(tmp_path, cash=100_000):
    return PaperAccount(cash={"KR": cash}, fee_rate={"KR": 0.0},
                        slippage_bps={"KR": 0.0}, state_path=tmp_path / "a.json")


def _res(symbol, qty, price, side="BUY", market="KR", age=0.0):
    return Reservation(symbol=symbol, market=market, side=side, qty=qty,
                       price=price, order_id="O", placed_at=time.time() - age)


# ── 게이트: 예약 반영 ───────────────────────────────────────────
def test_buying_power_counts_reservation(tmp_path):
    gate, acct = _gate(tmp_path), _acct(tmp_path, 100_000)
    order = Order("000660", "KR", "BUY", 1, 60_000)
    assert gate.check(order, acct).approved              # 예약 없으면 통과
    d = gate.check(order, acct, reserved=[_res("005930", 1, 60_000)])
    assert not d.approved and "매수여력" in d.reason


def test_reservation_of_other_market_ignored(tmp_path):
    gate, acct = _gate(tmp_path), _acct(tmp_path, 100_000)
    order = Order("000660", "KR", "BUY", 1, 60_000)
    d = gate.check(order, acct,
                   reserved=[_res("AAPL", 1, 60_000, market="US")])
    assert d.approved


def test_sell_reservation_does_not_free_cash(tmp_path):
    """미체결 SELL 예약을 현금으로 미리 세지 않는다(보수)."""
    gate, acct = _gate(tmp_path), _acct(tmp_path, 50_000)
    d = gate.check(Order("000660", "KR", "BUY", 1, 60_000), acct,
                   reserved=[_res("005930", 10, 10_000, side="SELL")])
    assert not d.approved


def test_gross_exposure_counts_reservation(tmp_path):
    gate = _gate(tmp_path, max_gross_exposure=0.5, exposure_base="capital")
    acct = _acct(tmp_path, 1_000_000)
    order = Order("000660", "KR", "BUY", 1, 300_000)
    assert gate.check(order, acct).approved
    d = gate.check(order, acct, reserved=[_res("005930", 1, 300_000)])
    assert not d.approved and "총 익스포저" in d.reason


def test_sector_concentration_counts_reservation(tmp_path):
    gate = _gate(tmp_path, max_sector_pct=0.4, exposure_base="capital",
                 sector_map={"005930": "반도체", "000660": "반도체"})
    acct = _acct(tmp_path, 1_000_000)
    order = Order("000660", "KR", "BUY", 1, 300_000)
    assert gate.check(order, acct).approved
    d = gate.check(order, acct, reserved=[_res("005930", 1, 300_000)])
    assert not d.approved and "섹터 집중" in d.reason


def test_max_positions_counts_reserved_new_symbols(tmp_path):
    gate = _gate(tmp_path, max_positions=1)
    acct = _acct(tmp_path, 1_000_000)
    order = Order("000660", "KR", "BUY", 1, 1_000)
    assert gate.check(order, acct).approved
    d = gate.check(order, acct, reserved=[_res("005930", 1, 1_000)])
    assert not d.approved and "최대 보유종목 수" in d.reason


def test_reserved_held_symbol_not_double_counted(tmp_path):
    """이미 보유 중인 종목의 예약은 신규 종목 수에 더하지 않는다."""
    gate = _gate(tmp_path, max_positions=2)
    acct = _acct(tmp_path, 1_000_000)
    acct.apply_fill("005930", "KR", "BUY", 1, 1_000, 0.0, "seed")
    d = gate.check(Order("000660", "KR", "BUY", 1, 1_000), acct,
                   reserved=[_res("005930", 1, 1_000)])
    assert d.approved


def test_reserved_none_keeps_legacy_behaviour(tmp_path):
    gate, acct = _gate(tmp_path), _acct(tmp_path, 100_000)
    assert gate.check(Order("000660", "KR", "BUY", 1, 60_000), acct).approved


# ── 브로커: 실제 동시 주문 재현 ─────────────────────────────────
class _HoldClient:
    """place 는 즉시 성공, 첫 체결 조회에서 멈춰 있는 클라이언트."""

    def __init__(self, release):
        self._release = release
        self._held = False
        self.placed = []

    def get_sellable(self, seq, symbol):
        return {"sellableQuantity": 0}

    def orderbook(self, symbol, market=None):
        return None

    def place_order(self, **kw):
        self.placed.append(kw)
        return {"orderId": f"O{len(self.placed)}"}

    def get_order(self, account_seq, order_id):
        if not self._held:
            self._held = True
            self._release.wait(timeout=5)
        return {"status": "FILLED",
                "execution": {"filledQuantity": 1, "averageFilledPrice": 60_000,
                              "commission": 0, "tax": 0}}


def _live_broker(tmp_path, cash=100_000, **kw):
    return Broker(account=_acct(tmp_path, cash), gate=_gate(tmp_path),
                  mode="live", account_seq="A1", live_markets=["KR"], **kw)


def test_second_symbol_blocked_while_first_order_inflight(tmp_path):
    """핵심 재현: 60,000 두 건이 현금 100,000 에서 모두 통과하면 안 된다."""
    release = threading.Event()
    client = _HoldClient(release)
    broker = _live_broker(tmp_path, client=client)
    first: dict = {}

    def _first():
        first["r"] = broker.execute(
            Order("005930", "KR", "BUY", 1, 60_000), "first")

    t = threading.Thread(target=_first)
    t.start()
    try:
        for _ in range(500):
            if client.placed:
                break
            time.sleep(0.01)
        assert client.placed, "첫 주문이 place 까지 가야 한다"

        second = broker.execute(Order("000660", "KR", "BUY", 1, 60_000), "second")
        assert not second.ok
        assert "매수여력" in (second.reject_reason or "")
        assert len(client.placed) == 1, "두 번째 주문은 발주되면 안 된다"
    finally:
        release.set()
        t.join(timeout=10)

    assert first["r"].ok
    assert broker.account.cash["KR"] >= 0


def test_reservation_released_allows_next_order(tmp_path):
    release = threading.Event()
    release.set()
    client = _HoldClient(release)
    broker = _live_broker(tmp_path, cash=200_000, client=client)
    assert broker.execute(Order("005930", "KR", "BUY", 1, 60_000), "a").ok
    assert broker._inflight == {}
    assert broker.execute(Order("000660", "KR", "BUY", 1, 60_000), "b").ok


# ── 예약 TTL ────────────────────────────────────────────────────
def test_expired_reservation_is_reclaimed(tmp_path):
    broker = _live_broker(tmp_path)
    broker.reservation_ttl_sec = 1.0
    with broker._lock:
        broker._mark_inflight(Order("005930", "KR", "BUY", 1, 60_000))
        broker._inflight["005930"].placed_at -= 5.0
        assert broker._active_reservations() == []
    assert broker._inflight == {}


def test_ttl_zero_disables_reclaim(tmp_path):
    broker = _live_broker(tmp_path)
    broker.reservation_ttl_sec = 0.0
    with broker._lock:
        broker._mark_inflight(Order("005930", "KR", "BUY", 1, 60_000))
        broker._inflight["005930"].placed_at -= 10_000
        assert len(broker._active_reservations()) == 1


def test_expired_reservation_unblocks_reconcile(tmp_path):
    """예약이 새면 재대사까지 영구 연기된다 — TTL 이 풀어야 한다."""
    broker = _live_broker(tmp_path)
    broker.reservation_ttl_sec = 1.0
    with broker._lock:
        broker._mark_inflight(Order("005930", "KR", "BUY", 1, 60_000))
        broker._inflight["005930"].placed_at -= 5.0
    assert broker.reconcile(lambda acct: {"applied": True}) == {"applied": True}


def test_expired_reservation_bumps_activity_gen(tmp_path):
    broker = _live_broker(tmp_path)
    broker.reservation_ttl_sec = 1.0
    with broker._lock:
        broker._mark_inflight(Order("005930", "KR", "BUY", 1, 60_000))
        broker._inflight["005930"].placed_at -= 5.0
        gen = broker._activity_gen
        broker._active_reservations()
        assert broker._activity_gen == gen + 1


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_mark_inflight_records_order_fields(tmp_path, side):
    broker = _live_broker(tmp_path)
    with broker._lock:
        broker._mark_inflight(Order("005930", "KR", side, 3, 1_000), order_id="X9")
    r = broker._inflight["005930"]
    assert (r.side, r.qty, r.price, r.order_id) == (side, 3.0, 1_000.0, "X9")
    assert r.notional == 3_000.0

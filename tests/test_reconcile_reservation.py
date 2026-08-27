"""재대사 후 미체결 예약 이중 차감(과차단) 회귀."""
import time

from src.broker import Broker
from src.engine.store import Store
from src.paper_account import PaperAccount
from src.risk_gate import Order, RiskGate


def _gate(tmp_path):
    return RiskGate({"capital": {"KR": 10_000_000}, "max_position_pct": 1.0,
                     "max_positions": 5, "daily_loss_limit_pct": 0.5,
                     "kill_switch_file": str(tmp_path / "HALT")})


def test_working_reservation_skipped_after_reconcile(tmp_path):
    """재대사가 BP 로 cash 를 덮은 뒤 pre-reconcile 미체결은 예약에서 빠진다."""
    store = Store(tmp_path / "t.db")
    acct = PaperAccount(cash={"KR": 1_000_000}, fee_rate={"KR": 0.0},
                        slippage_bps={"KR": 0.0}, state_path=tmp_path / "a.json")
    broker = Broker(account=acct, gate=_gate(tmp_path), mode="live",
                    account_seq="A1", live_markets=["KR"], store=store,
                    client=object(), working_order_abandon_ttl_sec=-1)
    t0 = time.time() - 10
    store.upsert_working_order(order_id="W1", symbol="005930", market="KR",
                               side="BUY", qty=10, price=70_000,
                               status="PENDING", filled_qty=0, placed_at=t0)
    assert len(broker._working_reservations()) == 1

    def _apply(a):
        a.cash["KR"] = 300_000  # 실계좌 BP(이미 미체결 홀드 반영)
        return {"cash": dict(a.cash)}

    broker.reconcile(_apply)
    assert broker._cash_reconciled_at is not None
    assert broker._working_reservations() == []  # 이중 차감 금지

    # 재대사 **이후** 신규 미체결은 여전히 예약
    store.upsert_working_order(order_id="W2", symbol="000660", market="KR",
                               side="BUY", qty=1, price=50_000,
                               status="PENDING", filled_qty=0,
                               placed_at=broker._cash_reconciled_at + 1)
    assert len(broker._working_reservations()) == 1


def test_gate_not_double_blocked_after_reconcile(tmp_path):
    store = Store(tmp_path / "t.db")
    acct = PaperAccount(cash={"KR": 1_000_000}, fee_rate={"KR": 0.0},
                        slippage_bps={"KR": 0.0}, state_path=tmp_path / "a.json")
    gate = _gate(tmp_path)
    broker = Broker(account=acct, gate=gate, mode="live", account_seq="A1",
                    live_markets=["KR"], store=store, client=object(),
                    working_order_abandon_ttl_sec=-1)
    store.upsert_working_order(order_id="W1", symbol="005930", market="KR",
                               side="BUY", qty=10, price=70_000,
                               status="PENDING", filled_qty=0,
                               placed_at=time.time() - 5)

    def _apply(a):
        a.cash["KR"] = 800_000
        return {"ok": True}

    broker.reconcile(_apply)
    order = Order("000660", "KR", "BUY", 1, 100_000)
    d = gate.check(order, broker.account, reserved=broker._active_reservations())
    assert d.approved, d.reason

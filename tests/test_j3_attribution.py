"""J3-귀속 — 폴링 밖에서 체결된 매도의 손익이 어디에도 안 남던 결함.

재현(수정 전): 매도 주문이 미체결로 남고 폴링이 끝난다. 다음 주기 재대사가
live holdings/buying-power 로 cash·positions 를 덮는다 — 수량과 현금은 맞지만
그 매도의 손익은 realized_pnl·저널·store pnl 어디에도 없다. 게이트는 손실을
못 보고, 성과귀속(track_record)에서는 거래 자체가 사라진다.

수정: 재대사가 본 감소 수량을 working_orders 의 정산된 주문(실체결가)에 붙여
기입한다. 못 붙는 감소는 추정하지 않고 unattributed_delta 만 남긴다.
"""
from src.broker import Broker
from src.broker_sync import apply_reconcile_from_live
from src.engine.store import Store
from src.paper_account import PaperAccount
from src.risk_gate import Order, RiskGate
from src.strategies.base import Position


def _gate(tmp_path):
    return RiskGate({"capital": {"KR": 10_000_000}, "max_position_pct": 1.0,
                     "max_positions": 5, "daily_loss_limit_pct": 0.5,
                     "kill_switch_file": str(tmp_path / "HALT")})


def _acct(tmp_path, cash=1_000_000):
    return PaperAccount(cash={"KR": cash}, fee_rate={"KR": 0.0},
                        slippage_bps={"KR": 0.0}, state_path=tmp_path / "a.json")


class _Client:
    def __init__(self, detail=None):
        self.detail = detail or {"status": "PENDING",
                                 "execution": {"filledQuantity": 0}}
        self.placed = []
        self.canceled = []

    def get_sellable(self, seq, symbol):
        return {"sellableQuantity": 100}

    def orderbook(self, symbol, market=None):
        return None

    def place_order(self, **kw):
        self.placed.append(kw)
        return {"orderId": f"O{len(self.placed)}"}

    def get_order(self, account_seq, order_id):
        return self.detail

    def cancel_order(self, account_seq, order_id):
        self.canceled.append(order_id)
        return {"ok": True}


def _broker(tmp_path, store, client, **kw):
    return Broker(account=_acct(tmp_path), gate=_gate(tmp_path), client=client,
                  mode="live", account_seq="A1", live_markets=["KR"],
                  store=store, reconcile_poll_attempts=1, reconcile_poll_sec=0.0,
                  **kw)


def _holdings(items):
    return {"cash": {"KR": 900_000}, "items": items, "holdings_ok": True}


def _item(symbol="005930", qty=1, avg=70_000):
    return {"symbol": symbol, "quantity": str(qty),
            "averagePurchasePrice": str(avg), "marketCountry": "KR"}


def _sell_working(store, *, order_id="O1", symbol="005930", qty=10,
                  filled=10, avg=72_000, fee=100.0, applied=0.0):
    store.upsert_working_order(order_id=order_id, symbol=symbol, market="KR",
                               side="SELL", qty=qty, price=71_000,
                               status="FILLED", filled_qty=filled,
                               filled_avg=avg, fee=fee, applied_qty=applied,
                               applied_notional=applied * (avg or 0.0),
                               applied_fee=0.0, reason="exit")
    store.update_working_order(order_id, settled_at=1.0)


def _seed(store, acct, symbol="005930", qty=10, avg=70_000):
    acct.positions[symbol] = Position(symbol=symbol, qty=qty, avg_price=avg)
    acct.symbol_market[symbol] = "KR"
    return store.open_position(symbol, "KR", qty, avg, thesis="tracked")


def _closed(store, symbol="005930"):
    return store.conn.execute(
        "SELECT qty, exit_price, pnl FROM positions "
        "WHERE symbol=? AND state='closed' AND pnl IS NOT NULL", (symbol,)).fetchall()


def _events(store, kind):
    return [dict(r) for r in store.conn.execute(
        "SELECT * FROM events WHERE kind=?", (kind,)).fetchall()]


# ── 전체 청산 귀속 ──────────────────────────────────────────────
def test_full_exit_attributed_with_real_fill_price(tmp_path):
    store, acct = Store(tmp_path / "t.db"), _acct(tmp_path)
    _seed(store, acct)
    _sell_working(store)

    res = apply_reconcile_from_live(acct, store, _holdings([]), markets=("KR",))

    assert res["attributed"]["005930"]["qty"] == 10.0
    assert res["attributed"]["005930"]["price"] == 72_000.0
    # 실현손익 = (72,000-70,000)*10 - 100
    assert acct.realized_pnl["KR"] == 19_900.0
    assert acct.daily_realized_pnl("KR") == 19_900.0
    rows = _closed(store)
    assert len(rows) == 1 and rows[0]["exit_price"] == 72_000.0
    assert rows[0]["pnl"] == 19_900.0
    assert store.get_working_orders() == []      # 소비 후 삭제


def test_journal_gets_the_exit_fill(tmp_path):
    store, acct = Store(tmp_path / "t.db"), _acct(tmp_path)
    _seed(store, acct)
    _sell_working(store)
    apply_reconcile_from_live(acct, store, _holdings([]), markets=("KR",))
    f = acct.journal[-1]
    assert (f.symbol, f.side, f.qty, f.price) == ("005930", "SELL", 10.0, 72_000.0)


def test_cash_is_not_touched_by_attribution(tmp_path):
    """현금·수량은 실계좌가 권위 — 귀속이 다시 더하면 이중 계상."""
    store, acct = Store(tmp_path / "t.db"), _acct(tmp_path)
    _seed(store, acct)
    _sell_working(store)
    apply_reconcile_from_live(acct, store, _holdings([]), markets=("KR",))
    assert acct.cash["KR"] == 900_000          # 재대사가 넘긴 값 그대로
    assert not acct.position("005930").is_open


# ── 부분 청산 귀속 ──────────────────────────────────────────────
def test_partial_exit_attributed(tmp_path):
    store, acct = Store(tmp_path / "t.db"), _acct(tmp_path)
    _seed(store, acct, qty=10)
    _sell_working(store, qty=4, filled=4, avg=72_000, fee=40.0)

    apply_reconcile_from_live(acct, store, _holdings([_item(qty=6)]), markets=("KR",))

    rows = _closed(store)
    assert len(rows) == 1
    assert rows[0]["qty"] == 4.0 and rows[0]["exit_price"] == 72_000.0
    assert acct.realized_pnl["KR"] == 2 * 4 * 1000 - 40.0
    assert store.get_open_positions()[0]["qty"] == 6.0


def test_increment_price_backs_out_already_applied_fill(tmp_path):
    """부분체결 3주를 _finish_live 가 이미 원장에 넣었다면 남은 7주만 귀속."""
    store, acct = Store(tmp_path / "t.db"), _acct(tmp_path)
    _seed(store, acct, qty=7)                 # 3주는 이미 원장에서 빠졌다
    # 누적 10주 @ 71,000 / 반영분 3주 @ 70,000 -> 증분 7주 @ 71,428.57
    store.upsert_working_order(
        order_id="O1", symbol="005930", market="KR", side="SELL", qty=10,
        price=70_000, status="FILLED", filled_qty=10, filled_avg=71_000,
        fee=100.0, applied_qty=3, applied_notional=3 * 70_000, applied_fee=30.0)
    store.update_working_order("O1", settled_at=1.0)

    res = apply_reconcile_from_live(acct, store, _holdings([]), markets=("KR",))

    got = res["attributed"]["005930"]
    assert got["qty"] == 7.0
    assert abs(got["price"] - (71_000 * 10 - 210_000) / 7) < 1e-6
    assert abs(got["fee"] - 70.0) < 1e-9


def test_leftover_fill_stays_for_next_reconcile(tmp_path):
    """감소분보다 체결분이 많으면 남은 분을 다음 재대사로 넘긴다(과대 귀속 금지)."""
    store, acct = Store(tmp_path / "t.db"), _acct(tmp_path)
    _seed(store, acct, qty=10)
    _sell_working(store, qty=10, filled=10, avg=72_000, fee=100.0)

    res = apply_reconcile_from_live(acct, store, _holdings([_item(qty=6)]),
                                    markets=("KR",))
    assert res["attributed"]["005930"]["qty"] == 4.0
    row = store.get_working_orders()[0]
    assert row["applied_qty"] == 4.0
    assert abs(row["applied_fee"] - 40.0) < 1e-9


# ── 귀속 불가: 추정 금지 ────────────────────────────────────────
def test_unmatched_drop_emits_event_and_no_pnl(tmp_path):
    """수동매도 등 order_id 없는 감소 — 숫자를 채우지 않는다."""
    store, acct = Store(tmp_path / "t.db"), _acct(tmp_path)
    _seed(store, acct, qty=10)

    res = apply_reconcile_from_live(acct, store, _holdings([]), markets=("KR",))

    assert res["attributed"] == {}
    assert acct.realized_pnl.get("KR", 0.0) == 0.0
    assert _closed(store) == []                # pnl NULL 로 닫힌다
    ev = _events(store, "unattributed_delta")
    assert len(ev) == 1 and ev[0]["symbol"] == "005930"


def test_stale_journal_sell_is_not_used_as_exit_price(tmp_path):
    """며칠 전 매도가를 오늘 감소분에 찍으면 pnl 이 조용히 틀린다."""
    from src.paper_account import Fill
    store, acct = Store(tmp_path / "t.db"), _acct(tmp_path)
    _seed(store, acct, qty=10)
    acct.journal.append(Fill(ts="2020-01-01T00:00:00+00:00", symbol="005930",
                             market="KR", side="SELL", qty=1, price=999_999,
                             fee=0, reason="old"))
    apply_reconcile_from_live(acct, store, _holdings([]), markets=("KR",))
    assert _closed(store) == []


def test_fill_without_price_is_not_estimated(tmp_path):
    """실체결가를 모르는 정산분은 귀속하지 않는다(평단 추정 금지)."""
    store, acct = Store(tmp_path / "t.db"), _acct(tmp_path)
    _seed(store, acct, qty=10)
    _sell_working(store, avg=None)

    res = apply_reconcile_from_live(acct, store, _holdings([]), markets=("KR",))
    assert res["attributed"] == {}
    assert len(_events(store, "unattributed_delta")) == 1


def test_buy_side_working_order_does_not_attribute(tmp_path):
    """매수 주문은 손익 귀속 대상이 아니다 — 원가는 실계좌 평단이 권위."""
    store, acct = Store(tmp_path / "t.db"), _acct(tmp_path)
    _seed(store, acct, qty=10)
    store.upsert_working_order(order_id="O9", symbol="005930", market="KR",
                               side="BUY", qty=5, price=70_000, status="FILLED",
                               filled_qty=5, filled_avg=70_000, fee=0.0)
    store.update_working_order("O9", settled_at=1.0)
    res = apply_reconcile_from_live(acct, store, _holdings([]), markets=("KR",))
    assert res["attributed"] == {}


def test_increase_does_not_attribute(tmp_path):
    store, acct = Store(tmp_path / "t.db"), _acct(tmp_path)
    _seed(store, acct, qty=5)
    _sell_working(store)
    res = apply_reconcile_from_live(acct, store, _holdings([_item(qty=9)]),
                                    markets=("KR",))
    assert res["attributed"] == {}
    assert _events(store, "unattributed_delta") == []


def test_pending_order_flagged_on_unattributed(tmp_path):
    """정산 전 주문이 살아 있으면 수동매도와 구분되게 표시."""
    store, acct = Store(tmp_path / "t.db"), _acct(tmp_path)
    _seed(store, acct, qty=10)
    store.upsert_working_order(order_id="O1", symbol="005930", market="KR",
                               side="SELL", qty=10, price=71_000,
                               status="PENDING", filled_qty=0)
    apply_reconcile_from_live(acct, store, _holdings([]), markets=("KR",))
    import json
    ev = _events(store, "unattributed_delta")
    assert json.loads(ev[0]["payload"])["pending_order"] is True


# ── 게이트 연결 ─────────────────────────────────────────────────
def test_attributed_loss_reaches_daily_gate(tmp_path):
    """귀속된 손실이 일손실 게이트에 잡힌다(J3-게이트와의 접점)."""
    gate = RiskGate({"capital": {"KR": 1_000_000}, "max_position_pct": 1.0,
                     "max_positions": 5, "daily_loss_limit_pct": 0.05,
                     "daily_loss_use_sod_delta": False,
                     "kill_switch_file": str(tmp_path / "HALT")})
    store, acct = Store(tmp_path / "t.db"), _acct(tmp_path)
    _seed(store, acct, qty=10, avg=70_000)
    base = acct.ensure_sod_equity("KR")            # 현금 1,000,000 + 보유 700,000
    _sell_working(store, avg=60_000, fee=0.0)      # -100,000 > 5% of 1,700,000

    apply_reconcile_from_live(acct, store, _holdings([]), markets=("KR",))

    assert acct.daily_realized_pnl("KR") == -100_000.0 < -base * 0.05
    d = gate.check(Order("000660", "KR", "BUY", 1, 1_000), acct)
    assert not d.approved and "일 손실 한도" in d.reason


# ── 레지스트리 정산 흐름 (broker) ───────────────────────────────
def test_settled_sell_is_kept_for_attribution(tmp_path):
    """체결분이 남은 종결 주문은 지우지 않는다 — 실체결가 출처."""
    store = Store(tmp_path / "t.db")
    client = _Client({"status": "PENDING", "execution": {"filledQuantity": 0}})
    broker = _broker(tmp_path, store, client)
    broker.account.positions["005930"] = Position(symbol="005930", qty=10,
                                                  avg_price=70_000)
    broker.account.symbol_market["005930"] = "KR"
    broker.execute(Order("005930", "KR", "SELL", 10, 71_000), "exit")
    assert len(store.get_working_orders()) == 1

    client.detail = {"status": "FILLED",
                     "execution": {"filledQuantity": 10,
                                   "averageFilledPrice": 72_000,
                                   "commission": 90, "tax": 10}}
    sw = broker.sweep_working_orders()
    assert sw["settled"] == 1 and sw["awaiting_attribution"] == 1
    row = store.get_working_orders()[0]
    assert row["settled_at"] and row["filled_avg"] == 72_000.0
    assert row["fee"] == 100.0
    assert not store.has_working_order("005930")   # 재발주는 막지 않는다


def test_settled_terminal_without_fill_is_deleted(tmp_path):
    store = Store(tmp_path / "t.db")
    client = _Client()
    broker = _broker(tmp_path, store, client)
    broker.execute(Order("005930", "KR", "BUY", 1, 70_000), "entry")
    client.detail = {"status": "CANCELED", "execution": {"filledQuantity": 0}}
    sw = broker.sweep_working_orders()
    assert sw["settled"] == 1 and sw["awaiting_attribution"] == 0
    assert store.get_working_orders() == []


def test_settled_fill_expires_with_event(tmp_path):
    """재대사가 감소를 못 본 채 오래 남으면 버리고 기록만 남긴다."""
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path, store, _Client(), attribution_ttl_sec=0.0)
    _sell_working(store)
    sw = broker.sweep_working_orders()
    assert sw["dropped"] == 1
    assert store.get_working_orders() == []
    assert len(_events(store, "unattributed_fill")) == 1


def test_settled_fill_kept_within_ttl(tmp_path):
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path, store, _Client(), attribution_ttl_sec=-1)
    _sell_working(store)
    sw = broker.sweep_working_orders()
    assert sw["dropped"] == 0 and sw["awaiting_attribution"] == 1
    assert len(store.get_working_orders()) == 1


def test_settled_row_not_counted_as_reservation(tmp_path):
    """귀속 대기분은 이미 체결된 주문 — 현금을 다시 잡으면 과차단."""
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path, store, _Client())
    store.upsert_working_order(order_id="O1", symbol="005930", market="KR",
                               side="BUY", qty=10, price=70_000,
                               status="PENDING", filled_qty=0)
    assert len(broker._working_reservations()) == 1
    store.update_working_order("O1", settled_at=1.0)
    assert broker._working_reservations() == []

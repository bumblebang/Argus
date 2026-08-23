"""engine.execution — Entry/Exit: broker 체결 후 account→store mirror."""
from src.engine.execution import ExitExecutor, EntryExecutor
from src.engine.store import Store
from src.paper_account import PaperAccount
from src.risk_gate import RiskGate, Order
from src.broker import Broker
from src.risk import RiskManager


class _Trig:
    def __init__(self, kind):
        self.kind = kind


class FakeGW:
    def __init__(self, candles):
        self._c = candles

    def candles(self, sym, interval="1m", count=20):
        return self._c


def _ohlcv(closes):
    return [{"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1000}
            for c in closes]


def _broker(tmp_path, mode="paper", client=None, store=None, **kw):
    acct = PaperAccount(cash={"KR": 10_000_000, "US": 10_000},
                        state_path=tmp_path / "pa.json")
    gate = RiskGate({"capital": {"KR": 10_000_000}, "max_position_pct": 0.5,
                     "max_order_notional": {"KR": 10_000_000},
                     "kill_switch_file": str(tmp_path / "HALT")})
    return Broker(account=acct, gate=gate, client=client, mode=mode, store=store,
                  account_seq=kw.get("account_seq", 1),
                  live_markets=kw.get("live_markets", ["KR"]),
                  reconcile_poll_attempts=1, reconcile_poll_sec=0)


def test_exit_sells_and_closes_store(tmp_path):
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path)
    broker.account.fill("005930", "KR", "BUY", 3, 70000)
    store.open_position("005930", "KR", 3, 70000, stop_price=69000)

    ex = ExitExecutor(broker, store)
    assert ex("005930", "KR", 68000, _Trig("stop_hit")) is True
    assert broker.position("005930").qty == 0
    assert store.get_open_positions() == []
    kinds = {r["kind"] for r in store.conn.execute("SELECT kind FROM events").fetchall()}
    assert "exit" in kinds


def test_exit_noop_when_flat(tmp_path):
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path)
    ex = ExitExecutor(broker, store)
    assert ex("005930", "KR", 68000, _Trig("stop_hit")) is False
    assert ex("005930", "KR", None, _Trig("stop_hit")) is False


def _arm(store, **kw):
    return store.arm_candidate("005930", "KR", strategy="rsi_reversion",
                               meta={"horizon": "day", "params": {"period": 14, "oversold": 30},
                                     "target_weight": 0.2}, **kw)


def test_entry_buys_and_promotes_armed(tmp_path):
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path)
    risk = RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.2)
    _arm(store)
    gw = FakeGW(_ohlcv([float(x) for x in range(60, 20, -1)]))
    ex = EntryExecutor(gw, broker, risk, store,
                       plan_fn=lambda p, h, params: (round(p * 0.98, 2), round(p * 1.03, 2)))
    r = ex.evaluate(dict(store.get_armed()[0]), "KR")
    assert r["executed"] is True and r["action"] == "buy"
    assert broker.position("005930").qty > 0
    assert store.get_armed() == []
    opens = store.get_open_positions()
    assert len(opens) == 1 and opens[0]["state"] == "open"
    assert opens[0]["stop_price"] and opens[0]["target_price"]
    acct = broker.position("005930")
    assert abs(opens[0]["qty"] - acct.qty) < 1e-9
    assert abs(opens[0]["avg_price"] - acct.avg_price) < 1e-9


def test_entry_holds_when_no_buy_signal(tmp_path):
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path)
    risk = RiskManager(capital={"KR": 1_000_000})
    _arm(store)
    gw = FakeGW(_ohlcv([50, 51, 49, 50, 51, 49, 50, 51, 49, 50,
                        51, 49, 50, 51, 49, 50, 51, 49, 50, 51]))
    ex = EntryExecutor(gw, broker, risk, store)
    r = ex.evaluate(dict(store.get_armed()[0]), "KR")
    assert r["executed"] is False
    assert broker.position("005930").qty == 0
    assert len(store.get_armed()) == 1


def test_entry_skip_when_no_strategy(tmp_path):
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path)
    risk = RiskManager(capital={"KR": 1_000_000})
    store.arm_candidate("005930", "KR", strategy=None, meta={})
    ex = EntryExecutor(FakeGW(_ohlcv([1, 2, 3])), broker, risk, store)
    r = ex.evaluate(dict(store.get_armed()[0]), "KR")
    assert r["action"] == "skip" and r["executed"] is False
    assert broker.position("005930").qty == 0


# ── live partial fill → store mirror ─────────────────────────────────────

def _filled(qty, avg, status="FILLED"):
    return {"status": status, "execution": {
        "filledQuantity": str(qty), "averageFilledPrice": str(avg),
        "commission": "0", "tax": "0"}}


class _MockClient:
    def __init__(self, resp, order_detail=None, orderbook=None, sellable=None):
        self.resp = resp
        self.order_detail = order_detail or _filled(0, None, "PENDING")
        self._ob = orderbook or {"asks": [{"price": "70000", "volume": "100"}],
                                 "bids": [{"price": "69900", "volume": "100"}]}
        self.sellable = sellable
        self.calls = []

    def place_order(self, **kw):
        self.calls.append(kw)
        return self.resp

    def get_order(self, account_seq, order_id):
        return self.order_detail

    def orderbook(self, symbol):
        return self._ob

    def get_sellable(self, account_seq, symbol):
        return self.sellable or {}


def test_mirror_after_live_partial_buy(tmp_path):
    """broker 부분체결 후 mirror — EntryExecutor RSI 경로와 분리."""
    from src.store_fill import mirror_symbol_to_store
    from src.risk_gate import Order

    store = Store(tmp_path / "t.db")
    client = _MockClient(resp={"orderId": "P1"},
                         order_detail=_filled(2, 70100, "PARTIAL_FILLED"))
    broker = _broker(tmp_path, mode="live", client=client, store=store)
    store.arm_candidate("005930", "KR", strategy="rsi_reversion",
                        meta={"horizon": "day", "params": {}, "target_weight": 0.2})
    armed_id = store.get_armed()[0]["id"]
    res = broker.execute(Order("005930", "KR", "BUY", 5, 70000.0), "test")
    assert res.partial
    mirror_symbol_to_store(store, broker, "005930", fill=res, armed_id=armed_id,
                           plan_fn=lambda p, h, params: (p * 0.98, p * 1.03))
    assert broker.position("005930").qty == 2
    opens = store.get_open_positions()
    assert len(opens) == 1 and opens[0]["qty"] == 2
    assert opens[0]["avg_price"] == 70100
    assert store.get_armed() == []


def test_exit_partial_keeps_store_open(tmp_path):
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path, mode="live",
                     client=_MockClient(
                         resp={"orderId": "S1"},
                         order_detail=_filled(3, 71000, "PARTIAL_FILLED"),
                         sellable={"sellableQuantity": "10"}),
                     store=store)
    broker.account.apply_fill("005930", "KR", "BUY", 10, 70000, 0, "seed")
    store.open_position("005930", "KR", 10, 70000, stop_price=69000)

    ex = ExitExecutor(broker, store)
    assert ex("005930", "KR", 71000, _Trig("stop_hit")) is True
    assert broker.position("005930").qty == 7
    opens = store.get_open_positions()
    assert len(opens) == 1
    assert opens[0]["qty"] == 7

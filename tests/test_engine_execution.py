"""engine.execution — 코드 진입/청산기: armed→open 진입, 보유 전량 청산."""
from src.engine.execution import ExitExecutor, EntryExecutor
from src.engine.store import Store
from src.paper_account import PaperAccount
from src.risk_gate import RiskGate
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


def _broker(tmp_path):
    acct = PaperAccount(cash={"KR": 10_000_000, "US": 10_000},
                        state_path=tmp_path / "pa.json")
    gate = RiskGate({"capital": {"KR": 1_000_000}, "max_order_notional": {},
                     "kill_switch_file": str(tmp_path / "HALT")})
    return Broker(account=acct, gate=gate, mode="paper")


def test_exit_sells_and_closes_store(tmp_path):
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path)
    broker.account.fill("005930", "KR", "BUY", 3, 70000)
    store.open_position("005930", "KR", 3, 70000, stop_price=69000)

    ex = ExitExecutor(broker, store)
    assert ex("005930", "KR", 68000, _Trig("stop_hit")) is True
    assert broker.position("005930").qty == 0          # 전량 청산
    assert store.get_open_positions() == []             # store 포지션 닫힘
    kinds = {r["kind"] for r in store.conn.execute("SELECT kind FROM events").fetchall()}
    assert "exit" in kinds


def test_exit_noop_when_flat(tmp_path):
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path)
    ex = ExitExecutor(broker, store)
    assert ex("005930", "KR", 68000, _Trig("stop_hit")) is False   # 보유 없음
    assert ex("005930", "KR", None, _Trig("stop_hit")) is False     # 가격 없음


# ── EntryExecutor: armed 종목에 전략 BUY 신호 시 코드 진입 ───────────
def _arm(store, **kw):
    return store.arm_candidate("005930", "KR", strategy="rsi_reversion",
                               meta={"horizon": "day", "params": {"period": 14, "oversold": 30},
                                     "target_weight": 0.2}, **kw)


def test_entry_buys_and_promotes_armed(tmp_path):
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path)
    risk = RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.2)
    _arm(store)
    gw = FakeGW(_ohlcv([float(x) for x in range(60, 20, -1)]))   # 지속 하락 -> RSI 과매도 -> BUY
    ex = EntryExecutor(gw, broker, risk, store,
                       plan_fn=lambda p, h, params: (round(p * 0.98, 2), round(p * 1.03, 2)))
    r = ex.evaluate(dict(store.get_armed()[0]), "KR")
    assert r["executed"] is True and r["action"] == "buy"
    assert broker.position("005930").qty > 0
    assert store.get_armed() == []                      # armed -> open 승격
    opens = store.get_open_positions()
    assert len(opens) == 1 and opens[0]["state"] == "open"
    assert opens[0]["stop_price"] and opens[0]["target_price"]
    kinds = {e["kind"] for e in store.conn.execute("SELECT kind FROM events").fetchall()}
    assert "entry" in kinds


def test_entry_holds_when_no_buy_signal(tmp_path):
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path)
    risk = RiskManager(capital={"KR": 1_000_000})
    _arm(store)
    # 횡보 -> RSI 중립 -> 진입 신호 없음
    gw = FakeGW(_ohlcv([50, 51, 49, 50, 51, 49, 50, 51, 49, 50,
                        51, 49, 50, 51, 49, 50, 51, 49, 50, 51]))
    ex = EntryExecutor(gw, broker, risk, store)
    r = ex.evaluate(dict(store.get_armed()[0]), "KR")
    assert r["executed"] is False
    assert broker.position("005930").qty == 0
    assert len(store.get_armed()) == 1                  # 진입대기 유지(다음 틱 재시도)


def test_entry_skip_when_no_strategy(tmp_path):
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path)
    risk = RiskManager(capital={"KR": 1_000_000})
    store.arm_candidate("005930", "KR", strategy=None, meta={})
    ex = EntryExecutor(FakeGW(_ohlcv([1, 2, 3])), broker, risk, store)
    r = ex.evaluate(dict(store.get_armed()[0]), "KR")
    assert r["action"] == "skip" and r["executed"] is False
    assert broker.position("005930").qty == 0

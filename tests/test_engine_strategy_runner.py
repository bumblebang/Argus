"""engine.strategy_runner — 보유분에 배정전략을 돌려 신호기반 청산 집행."""
from src.engine.strategy_runner import StrategyRunner
from src.engine.store import Store
from src.paper_account import PaperAccount
from src.risk_gate import RiskGate
from src.broker import Broker


class FakeGW:
    def __init__(self, candles):
        self._c = candles

    def candles(self, sym, interval="1m", count=20):
        return self._c


def _broker(tmp_path):
    acct = PaperAccount(cash={"KR": 10_000_000}, state_path=tmp_path / "pa.json")
    gate = RiskGate({"capital": {"KR": 1_000_000}, "max_order_notional": {},
                     "kill_switch_file": str(tmp_path / "HALT")})
    return Broker(account=acct, gate=gate, mode="paper")


def _ohlcv(closes):
    return [{"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1000}
            for c in closes]


def test_exits_on_strategy_sell_signal(tmp_path):
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path)
    broker.account.fill("005930", "KR", "BUY", 3, 40)
    store.open_position("005930", "KR", 3, 40, strategy="rsi_reversion",
                        meta={"params": {"period": 14, "oversold": 30, "overbought": 70}})
    gw = FakeGW(_ohlcv([float(x) for x in range(20, 60)]))   # 지속 상승 -> RSI 과매수
    sr = StrategyRunner(gw, broker, store)

    pos = dict(store.get_open_positions()[0])
    r = sr.evaluate(pos, "KR")
    assert r["executed"] is True and r["action"] == "sell"
    assert broker.position("005930").qty == 0
    assert store.get_open_positions() == []
    kinds = {e["kind"] for e in store.conn.execute("SELECT kind FROM events").fetchall()}
    assert "strategy_exit" in kinds


def test_holds_when_signal_not_sell(tmp_path):
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path)
    broker.account.fill("005930", "KR", "BUY", 3, 50)
    store.open_position("005930", "KR", 3, 50, strategy="rsi_reversion",
                        meta={"params": {"period": 14, "overbought": 70}})
    gw = FakeGW(_ohlcv([50, 51, 49, 50, 51, 49, 50, 51, 49, 50,
                        51, 49, 50, 51, 49, 50, 51, 49, 50, 51]))   # 횡보 -> RSI 중립
    sr = StrategyRunner(gw, broker, store)
    r = sr.evaluate(dict(store.get_open_positions()[0]), "KR")
    assert r["executed"] is False
    assert broker.position("005930").qty == 3
    assert len(store.get_open_positions()) == 1


def test_skip_when_no_strategy(tmp_path):
    store = Store(tmp_path / "t.db")
    broker = _broker(tmp_path)
    sr = StrategyRunner(FakeGW(_ohlcv([1, 2, 3])), broker, store)
    r = sr.evaluate({"symbol": "005930", "qty": 3, "avg_price": 40}, "KR")
    assert r["action"] == "skip" and r["executed"] is False

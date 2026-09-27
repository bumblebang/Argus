"""engine.strategy_runner — 보유분에 배정전략을 돌려 신호기반 청산 집행.

신호 청산 권한은 진입 근거에 묶인다(engine.entry_basis) — 전략 신호로 진입한
자리만 신호로 청산한다. 여기 테스트는 그 계약을 고정한다.
"""
from src.engine.entry_basis import SignalExitConfig
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
                        meta={"entry_basis": "signal",
                              "params": {"period": 14, "oversold": 30, "overbought": 70}})
    gw = FakeGW(_ohlcv([float(x) for x in range(20, 60)]))   # 지속 상승 -> RSI 과매수
    sr = StrategyRunner(gw, broker, store, cfg=SignalExitConfig(min_hold_sec=0))

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
                        meta={"entry_basis": "signal",
                              "params": {"period": 14, "overbought": 70}})
    gw = FakeGW(_ohlcv([50, 51, 49, 50, 51, 49, 50, 51, 49, 50,
                        51, 49, 50, 51, 49, 50, 51, 49, 50, 51]))   # 횡보 -> RSI 중립
    sr = StrategyRunner(gw, broker, store, cfg=SignalExitConfig(min_hold_sec=0))
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


def test_rejected_signal_exit_backs_off(tmp_path):
    """거부된 신호 청산은 백오프 — 라운드로빈마다 재주문·error 이벤트 스팸 방지."""
    from src.fill_result import ExecuteResult

    class _Rejecting:
        def __init__(self):
            self.calls = 0

        def execute_with_mirror(self, order, **kw):
            self.calls += 1
            return ExecuteResult.rejected("스프레드 초과")

    store = Store(tmp_path / "t.db")
    store.open_position("005930", "KR", 3, 40, strategy="rsi_reversion",
                        meta={"entry_basis": "signal",
                              "params": {"period": 14, "oversold": 30, "overbought": 70}})
    clock = {"t": 1_000.0}
    broker = _Rejecting()
    sr = StrategyRunner(FakeGW(_ohlcv([float(x) for x in range(20, 60)])), broker, store,
                        cfg=SignalExitConfig(min_hold_sec=0), now_fn=lambda: clock["t"],
                        retry_base_sec=10, retry_max_sec=40)
    pos = dict(store.get_open_positions()[0])
    for _ in range(31):
        sr.evaluate(pos, "KR")
        clock["t"] += 1
    assert broker.calls == 3                              # t=0, 10, 30

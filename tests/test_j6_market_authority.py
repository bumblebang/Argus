"""J6 — LLM proposal.market 이 자본 풀·한도·live 판정 기준을 바꾸던 결함.

백로그 재현(수정 전):
  - Proposal(005930, market='US') 가 스키마를 통과하고 그대로 흐른다
  - US 종목에 'KR' 라벨 → KR 원 자본으로 사이징 → 과대 주문
  - KR 보유분에 'US' 라벨 SELL → live_markets 밖 스킵 → 청산 불능
"""
import json

from src.agents import (DecisionAgent, DecisionOutput, MockLLM, Proposal,
                        ValidationAgent, ValidationOutput, ValidationVerdict)
from src.agents.cycle import run_cycle
from src.broker import Broker
from src.paper_account import PaperAccount
from src.risk import RiskManager
from src.risk_gate import Order, RiskGate


def _broker(tmp_path, mode="paper", **kw):
    acct = PaperAccount(cash={"KR": 1_000_000, "US": 0}, fee_rate={"KR": 0.0},
                        slippage_bps={"KR": 0.0}, state_path=tmp_path / "acct.json")
    gate = RiskGate({"capital": {"KR": 1_000_000, "US": 0}, "max_position_pct": 0.5,
                     "max_positions": 5, "daily_loss_limit_pct": 0.05,
                     "kill_switch_file": str(tmp_path / "HALT")})
    return Broker(account=acct, gate=gate, mode=mode, **kw)


def _responder(decision):
    def r(schema, system, user):
        if schema is DecisionOutput:
            return decision
        if schema is ValidationOutput:
            syms = [p["symbol"] for p in json.loads(user)["proposals"]]
            return ValidationOutput(verdicts=[
                ValidationVerdict(symbol=s, approved=True, reason="ok") for s in syms])
        raise AssertionError(schema)
    return r


def _decision(market):
    return DecisionOutput(market_view="중립", proposals=[Proposal(
        symbol="005930", market=market, side="BUY", conviction=0.8,
        horizon="swing", target_weight=0.2, thesis="t")])


def _cycle(tmp_path, decision, market_fn):
    llm = MockLLM(_responder(decision))
    broker = _broker(tmp_path)
    res = run_cycle(context_json="{}", decision_agent=DecisionAgent(llm),
                    validation_agent=ValidationAgent(llm, min_conviction=0.6),
                    broker=broker,
                    risk=RiskManager(capital={"KR": 1_000_000, "US": 0},
                                     max_position_pct=0.5),
                    price_lookup={"005930": 1000},
                    journal_path=tmp_path / "d.jsonl",
                    market_fn=market_fn)
    return res, broker


# ── run_cycle: 코드 권위 ────────────────────────────────────────
def test_wrong_llm_market_is_corrected(tmp_path):
    """국내주에 US 라벨이 붙어도 코드가 KR 로 되돌린다."""
    res, broker = _cycle(tmp_path, _decision("US"), market_fn=lambda s: "KR")
    assert res.executed[0]["status"] == "filled"
    assert res.decision.proposals[0].market == "KR"
    assert broker.account.symbol_market["005930"] == "KR"


def test_unknown_symbol_is_rejected_not_guessed(tmp_path):
    """코드가 모르는 심볼은 6자리=KR 로 추측하지 않고 거부한다."""
    res, broker = _cycle(tmp_path, _decision("KR"), market_fn=lambda s: None)
    assert res.executed[0]["status"] == "market_unknown"
    assert broker.account.position("005930").qty == 0


def test_market_fn_absent_keeps_legacy_behaviour(tmp_path):
    res, _ = _cycle(tmp_path, _decision("KR"), market_fn=None)
    assert res.executed[0]["status"] == "filled"


def test_market_fn_exception_treated_as_unknown(tmp_path):
    def boom(_s):
        raise RuntimeError("universe down")
    res, _ = _cycle(tmp_path, _decision("KR"), market_fn=boom)
    assert res.executed[0]["status"] == "market_unknown"


def test_correct_market_avoids_wrong_capital_pool(tmp_path):
    """US 라벨이 살아 있으면 US 자본 0 이라 사이징이 0 이 된다."""
    res, broker = _cycle(tmp_path, _decision("US"), market_fn=lambda s: "KR")
    assert broker.account.position("005930").qty > 0


# ── CycleRunner.market_of ──────────────────────────────────────
def test_market_of_prefers_universe(tmp_path):
    from src.agents.cycle_runner import CycleRunner

    runner = object.__new__(CycleRunner)
    runner.universe_fn = lambda: {"US": [{"symbol": "AAPL", "name": "Apple"}]}
    runner.cfg = type("C", (), {"universe": {}})()
    runner.broker = _broker(tmp_path)
    assert runner.market_of("AAPL") == "US"


def test_market_of_falls_back_to_ledger(tmp_path):
    from src.agents.cycle_runner import CycleRunner

    runner = object.__new__(CycleRunner)
    runner.universe_fn = lambda: {}
    runner.cfg = type("C", (), {"universe": {}})()
    runner.broker = _broker(tmp_path)
    runner.broker.account.symbol_market["005930"] = "KR"
    assert runner.market_of("005930") == "KR"
    assert runner.market_of("없는종목") is None


def test_universe_item_carries_market(tmp_path):
    from src.agents.cycle_runner import CycleRunner

    runner = object.__new__(CycleRunner)
    runner.universe_fn = lambda: {"KR": [{"symbol": "005930", "source": "gem"}]}
    runner.cfg = type("C", (), {"universe": {}})()
    item = runner._universe_item("005930")
    assert item["market"] == "KR" and item["source"] == "gem"


# ── 라이브 청산: 원장 market 권위 ───────────────────────────────
class _StubClient:
    def __init__(self):
        self.placed = []

    def get_sellable(self, seq, symbol):
        return {"sellableQuantity": 10}

    def orderbook(self, symbol, market=None):
        return None

    def place_order(self, **kw):
        self.placed.append(kw)
        return {"orderId": "O1"}

    def get_order(self, account_seq, order_id):
        return {"status": "FILLED",
                "execution": {"filledQuantity": 10, "averageFilledPrice": 1000,
                              "commission": 0, "tax": 0}}


def test_sell_with_wrong_market_label_still_executes(tmp_path):
    """KR 보유분에 US 라벨이 붙어도 청산이 스킵되지 않는다."""
    client = _StubClient()
    broker = _broker(tmp_path, mode="live", client=client, account_seq="A1",
                     live_markets=["KR"])
    broker.account.apply_fill("005930", "KR", "BUY", 10, 1000, 0.0, "seed")

    res = broker.execute(Order("005930", "US", "SELL", 10, 1000), reason="exit")
    assert res.ok, res.reject_reason
    assert client.placed, "주문이 나가야 한다(live_markets 밖 스킵이면 빈 목록)"
    assert broker.account.position("005930").qty == 0


def test_sell_untouched_when_ledger_agrees(tmp_path):
    client = _StubClient()
    broker = _broker(tmp_path, mode="live", client=client, account_seq="A1",
                     live_markets=["KR"])
    broker.account.apply_fill("005930", "KR", "BUY", 10, 1000, 0.0, "seed")
    order = Order("005930", "KR", "SELL", 10, 1000)
    broker.execute(order, reason="exit")
    assert order.market == "KR"


def test_unheld_symbol_market_not_rewritten(tmp_path):
    """보유하지 않은 심볼은 원장 라벨로 덮어쓰지 않는다(스킵 판정 유지)."""
    client = _StubClient()
    broker = _broker(tmp_path, mode="live", client=client, account_seq="A1",
                     live_markets=["KR"])
    broker.account.symbol_market["AAPL"] = "KR"      # 과거 오라벨 잔재
    order = Order("AAPL", "US", "BUY", 1, 200)
    res = broker.execute(order, reason="entry")
    assert not res.ok and order.market == "US"

"""고단가 floor=0 → 최소 1주 시범매수(min_lot) 사이징·사이클."""
import json
from pathlib import Path

from src.agents import (DecisionAgent, ValidationAgent, MockLLM, DecisionOutput,
                        Proposal, ValidationOutput, ValidationVerdict)
from src.agents.cycle import run_cycle
from src.broker import Broker
from src.paper_account import PaperAccount
from src.risk import RiskManager
from src.risk_gate import RiskGate


def test_size_buy_min_qty_bumps_high_price():
    risk = RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.2)
    # 목표비중 12% → 12만 / 35.7만 = floor 0
    assert risk.size_buy("KR", 357_000, 0.12) == 0
    assert risk.size_buy("KR", 357_000, 0.12, min_qty=1) == 1
    # 자본보다 비싸면 올리지 않음
    assert risk.size_buy("KR", 2_000_000, 0.12, min_qty=1) == 0


def _responder(decision):
    def r(schema, system, user):
        if schema is DecisionOutput:
            return decision
        if schema is ValidationOutput:
            syms = [p["symbol"] for p in json.loads(user)["proposals"]]
            return ValidationOutput(verdicts=[ValidationVerdict(
                symbol=s, approved=True, reason="ok") for s in syms])
        raise AssertionError(schema)
    return r


def test_cycle_min_lot_fills_one_share_of_expensive_name(tmp_path):
    """확신도 OK + 고단가 → 목표비중 상향 + qty=1 체결(allow_min_lot)."""
    decision = DecisionOutput(market_view="밸류", proposals=[Proposal(
        symbol="004370", market="KR", side="BUY", conviction=0.62,
        horizon="position", target_weight=0.15, thesis="저평가 시범",
        key_risks=["유동성"])])
    llm = MockLLM(_responder(decision))
    acct = PaperAccount(cash={"KR": 1_000_000}, fee_rate={"KR": 0.0},
                        slippage_bps={"KR": 0.0}, state_path=tmp_path / "a.json")
    gate = RiskGate({"capital": {"KR": 1_000_000}, "max_position_pct": 0.2,
                     "max_positions": 5, "daily_loss_limit_pct": 0.05,
                     "max_order_notional": {"KR": 200_000},
                     "allow_min_lot": True,
                     "kill_switch_file": str(tmp_path / "HALT")})
    broker = Broker(account=acct, gate=gate, client=None, mode="paper")
    risk = RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.2)
    res = run_cycle(
        context_json="{}", decision_agent=DecisionAgent(llm),
        validation_agent=ValidationAgent(llm, min_conviction=0.6),
        broker=broker, risk=risk, price_lookup={"004370": 357_000},
        journal_path=tmp_path / "d.jsonl",
        conviction_sizing=True, min_lot_conviction=0.6)
    assert res.executed[0]["status"] == "filled"
    assert broker.account.position("004370").qty == 1
    # 목표비중이 1주분(≥0.357)으로 올라갔는지
    assert decision.proposals[0].target_weight >= 0.357


def test_cycle_min_lot_skipped_when_conviction_low(tmp_path):
    """확신도 문턱 미달이면 기존대로 qty=0 → gate_rejected."""
    decision = DecisionOutput(market_view="관망", proposals=[Proposal(
        symbol="004370", market="KR", side="BUY", conviction=0.55,
        horizon="position", target_weight=0.15, thesis="약확신",
        key_risks=[])])
    llm = MockLLM(_responder(decision))
    # 검증이 0.55를 막지 않게 문턱을 낮춤 — 사이징 경로만 검증
    acct = PaperAccount(cash={"KR": 1_000_000}, fee_rate={"KR": 0.0},
                        slippage_bps={"KR": 0.0}, state_path=tmp_path / "a.json")
    gate = RiskGate({"capital": {"KR": 1_000_000}, "max_position_pct": 0.2,
                     "max_positions": 5, "daily_loss_limit_pct": 0.05,
                     "max_order_notional": {"KR": 200_000},
                     "allow_min_lot": True,
                     "kill_switch_file": str(tmp_path / "HALT")})
    broker = Broker(account=acct, gate=gate, client=None, mode="paper")
    risk = RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.2)
    res = run_cycle(
        context_json="{}", decision_agent=DecisionAgent(llm),
        validation_agent=ValidationAgent(llm, min_conviction=0.5),
        broker=broker, risk=risk, price_lookup={"004370": 357_000},
        journal_path=tmp_path / "d.jsonl",
        conviction_sizing=True, min_lot_conviction=0.6)
    assert res.executed[0]["status"] == "gate_rejected"
    assert broker.account.position("004370").qty == 0
    # qty=0 은 RiskGate 비정상 수량 — thesis 가 아니라 게이트 사유가 저널에 남는다
    assert "수량" in res.executed[0]["reason"] or "qty" in res.executed[0]["reason"].lower() \
        or res.executed[0]["reason"] == broker.last_reject_reason
    assert broker.last_reject_reason
    assert res.executed[0]["reason"] == broker.last_reject_reason

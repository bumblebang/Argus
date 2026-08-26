"""사이징 정책: equity 분모 · base_position_pct · 확신 소폭 ± · LLM tw 무시."""
import json
from pathlib import Path

from src.agents import (DecisionAgent, ValidationAgent, MockLLM, DecisionOutput,
                        Proposal, ValidationOutput, ValidationVerdict)
from src.agents.cycle import run_cycle
from src.broker import Broker
from src.paper_account import PaperAccount
from src.risk import RiskManager, risk_manager_from_cfg
from src.risk_gate import RiskGate
from src.agents.conviction import size_weight


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


def test_risk_manager_from_cfg_reads_tunables():
    rm = risk_manager_from_cfg({
        "capital": {"KR": 1_000_000},
        "base_position_pct": 0.15,
        "max_position_pct": 0.30,
        "sizing_base": "equity",
        "conviction_size_floor": 0.8,
        "conviction_size_span": 0.2,
    })
    assert rm.base_position_pct == 0.15
    assert rm.max_position_pct == 0.30
    assert rm.conviction_size_floor == 0.8
    assert abs(size_weight(0.15, 1.0, floor=0.8, span=0.2) - 0.15) < 1e-12


def test_size_buy_uses_base_equity_not_capital():
    risk = RiskManager(capital={"KR": 1_000_000}, base_position_pct=0.20,
                       max_position_pct=0.25)
    # capital 기준이면 200주, equity 120만·weight 0.2 → 240주
    assert risk.size_buy("KR", 1000, 0.20) == 200
    assert risk.size_buy("KR", 1000, 0.20, base_equity=1_200_000) == 240


def test_size_buy_notional_cap_and_conviction_band():
    risk = RiskManager(capital={"KR": 1_000_000}, base_position_pct=0.20,
                       max_position_pct=0.25)
    # c=1 → weight 0.20, equity 1.2M → 24만 but cap 20만 → 200주
    w = size_weight(0.20, 1.0, cap=0.25)
    assert abs(w - 0.20) < 1e-12
    assert risk.size_buy("KR", 1000, w, base_equity=1_200_000,
                         notional_cap=200_000) == 200
    # c=0.5 → 0.20×0.875=0.175 → 1.2M → 210_000 → 210주
    w2 = size_weight(0.20, 0.5, cap=0.25)
    assert abs(w2 - 0.175) < 1e-12
    assert risk.size_buy("KR", 1000, w2, base_equity=1_200_000) == 210


def test_cycle_ignores_llm_target_weight_uses_base(tmp_path):
    """LLM 이 tw=0.05 를 내도 코드 base 0.20 으로 산다."""
    decision = DecisionOutput(market_view="x", proposals=[Proposal(
        symbol="005930", market="KR", side="BUY", conviction=1.0,
        horizon="swing", target_weight=0.05, thesis="작다", key_risks=[])])
    llm = MockLLM(_responder(decision))
    acct = PaperAccount(cash={"KR": 1_000_000}, fee_rate={"KR": 0.0},
                        slippage_bps={"KR": 0.0}, state_path=tmp_path / "a.json")
    gate = RiskGate({"capital": {"KR": 1_000_000}, "max_position_pct": 0.25,
                     "max_positions": 5, "daily_loss_limit_pct": 0.05,
                     "max_order_notional": {},
                     "exposure_base": "equity",
                     "kill_switch_file": str(tmp_path / "HALT")})
    broker = Broker(account=acct, gate=gate, client=None, mode="paper")
    risk = RiskManager(capital={"KR": 1_000_000}, base_position_pct=0.20,
                       max_position_pct=0.25, sizing_base="equity")
    res = run_cycle(
        context_json="{}", decision_agent=DecisionAgent(llm),
        validation_agent=ValidationAgent(llm, min_conviction=0.0),
        broker=broker, risk=risk, price_lookup={"005930": 1000.0},
        journal_path=tmp_path / "d.jsonl",
        conviction_sizing=True)
    assert res.executed[0]["status"] == "filled"
    # c=1 → base 0.20 전량 → 200주 (LLM 0.05 무시)
    assert broker.account.position("005930").qty == 200
    assert abs(decision.proposals[0].target_weight - 0.20) < 1e-9


def test_cycle_budget_cap_clips_sleeve_room(tmp_path):
    decision = DecisionOutput(market_view="x", proposals=[Proposal(
        symbol="005930", market="KR", side="BUY", conviction=1.0,
        horizon="position", target_weight=0.99, thesis="v", key_risks=[])])
    llm = MockLLM(_responder(decision))
    acct = PaperAccount(cash={"KR": 1_000_000}, fee_rate={"KR": 0.0},
                        slippage_bps={"KR": 0.0}, state_path=tmp_path / "a.json")
    gate = RiskGate({"capital": {"KR": 1_000_000}, "max_position_pct": 0.25,
                     "max_positions": 5, "daily_loss_limit_pct": 0.05,
                     "max_order_notional": {},
                     "kill_switch_file": str(tmp_path / "HALT")})
    broker = Broker(account=acct, gate=gate, client=None, mode="paper")
    risk = RiskManager(capital={"KR": 1_000_000}, base_position_pct=0.20,
                       max_position_pct=0.25)
    res = run_cycle(
        context_json="{}", decision_agent=DecisionAgent(llm),
        validation_agent=ValidationAgent(llm, min_conviction=0.0),
        broker=broker, risk=risk, price_lookup={"005930": 1000.0},
        journal_path=tmp_path / "d.jsonl",
        conviction_sizing=True,
        budget_caps={"005930": 50_000})
    assert res.executed[0]["status"] == "filled"
    assert broker.account.position("005930").qty == 50  # room 5만


def test_cycle_tranche_weight_scales(tmp_path):
    decision = DecisionOutput(market_view="x", proposals=[Proposal(
        symbol="005930", market="KR", side="BUY", conviction=1.0,
        horizon="position", target_weight=0.99, thesis="t", key_risks=[])])
    llm = MockLLM(_responder(decision))
    acct = PaperAccount(cash={"KR": 1_000_000}, fee_rate={"KR": 0.0},
                        slippage_bps={"KR": 0.0}, state_path=tmp_path / "a.json")
    gate = RiskGate({"capital": {"KR": 1_000_000}, "max_position_pct": 0.25,
                     "max_positions": 5, "daily_loss_limit_pct": 0.05,
                     "max_order_notional": {},
                     "kill_switch_file": str(tmp_path / "HALT")})
    broker = Broker(account=acct, gate=gate, client=None, mode="paper")
    risk = RiskManager(capital={"KR": 1_000_000}, base_position_pct=0.20,
                       max_position_pct=0.25)
    res = run_cycle(
        context_json="{}", decision_agent=DecisionAgent(llm),
        validation_agent=ValidationAgent(llm, min_conviction=0.0),
        broker=broker, risk=risk, price_lookup={"005930": 1000.0},
        journal_path=tmp_path / "d.jsonl",
        conviction_sizing=True, allow_add=True,
        tranche_weights={"005930": 0.5})
    assert res.executed[0]["status"] == "filled"
    # 0.20 × 0.5 = 0.10 → 100주
    assert broker.account.position("005930").qty == 100

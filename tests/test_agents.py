import json
import sys
from pathlib import Path

import pytest

from src.agents import (DecisionAgent, ValidationAgent, MockLLM, ClaudeCLIClient, DecisionOutput,
                        ValidationOutput, Proposal, ValidationVerdict)
from src.agents.llm import _extract_json
from src.agents.cycle import run_cycle
from src.agents.context import build_context
from src.paper_account import PaperAccount
from src.risk_gate import RiskGate
from src.risk import RiskManager
from src.broker import Broker


def _broker(tmp_path):
    acct = PaperAccount(cash={"KR": 1_000_000}, fee_rate={"KR": 0.0}, slippage_bps={"KR": 0.0},
                        state_path=tmp_path / "acct.json")
    gate = RiskGate({"capital": {"KR": 1_000_000}, "max_position_pct": 0.2, "max_positions": 5,
                     "daily_loss_limit_pct": 0.05, "max_order_notional": {"KR": 500_000},
                     "kill_switch_file": str(tmp_path / "HALT")})
    return Broker(account=acct, gate=gate, client=None, mode="paper")


def _decision_buy(conviction=0.8):
    return DecisionOutput(market_view="중립", proposals=[Proposal(
        symbol="005930", market="KR", side="BUY", conviction=conviction,
        horizon="swing", target_weight=0.2, thesis="수급+모멘텀 정렬", key_risks=["변동성"])])


def _responder(decision, approve):
    def r(schema, system, user):
        if schema is DecisionOutput:
            return decision
        if schema is ValidationOutput:
            syms = [p["symbol"] for p in json.loads(user)["proposals"]]
            return ValidationOutput(verdicts=[ValidationVerdict(
                symbol=s, approved=approve, reason="ok" if approve else "thesis 모순",
                concerns=[] if approve else ["rule1"]) for s in syms])
        raise AssertionError(schema)
    return r


def test_extract_json_from_noisy_text():
    assert _extract_json('어쩌고 {"a": 1} 저쩌고') == {"a": 1}
    assert _extract_json('```json\n{"b": 2}\n```') == {"b": 2}
    assert _extract_json("그냥 텍스트") is None


def test_claude_cli_backend_via_fake():
    """가짜 claude CLI 로 subprocess/stdin/stdout/파싱 경로 검증 (실제 claude 불필요)."""
    fake = Path(__file__).parent / "fake_claude.py"
    cli = ClaudeCLIClient(command=sys.executable, base_args=[str(fake)])
    dec = cli.structured("system", '{"candidates":[]}', DecisionOutput)
    assert dec.market_view == "fake-cli"
    val = cli.structured("system", '{"proposals":[{"symbol":"A"}]}', ValidationOutput)
    assert val.verdicts == []


def test_claude_cli_fallback_on_primary_failure():
    """주모델(opus) rc=1 실패 시 폴백 모델로 자동 재시도해 사이클을 살린다."""
    fake = Path(__file__).parent / "fake_claude_fallback.py"
    cli = ClaudeCLIClient(command=sys.executable, base_args=[str(fake)],
                          model="opus", fallback_model="sonnet", error_dump_path=None)
    dec = cli.structured("system", '{"candidates":[]}', DecisionOutput)
    assert dec.market_view == "fallback-sonnet"


def test_claude_cli_raises_without_fallback():
    """폴백 미설정이면 주모델 실패가 그대로 표면화된다(ClaudeCLIError)."""
    from src.agents.llm import ClaudeCLIError
    fake = Path(__file__).parent / "fake_claude_fallback.py"
    cli = ClaudeCLIClient(command=sys.executable, base_args=[str(fake)],
                          model="opus", error_dump_path=None)
    with pytest.raises(ClaudeCLIError):
        cli.structured("system", '{"candidates":[]}', DecisionOutput)


def test_proposal_strategy_fields_optional():
    p = Proposal(symbol="A", market="KR", side="BUY", conviction=0.7,
                 target_weight=0.2, thesis="t")
    assert p.strategy is None and p.params is None          # 하위호환: 없어도 됨
    p2 = Proposal(symbol="A", market="KR", side="BUY", conviction=0.7, target_weight=0.2,
                  thesis="t", strategy="rsi_reversion", params={"period": 10})
    assert p2.strategy == "rsi_reversion" and p2.params == {"period": 10.0}


def test_약세_스틸맨_필드_선택적이고_저널에_남는다():
    """bear_case/bear_rebuttal 은 스키마상 선택 — 강제는 프롬프트·검증 에이전트가 한다.

    코드 하드게이트를 두지 않은 건 의도다(라이브 봇에 새 강제 거부를 걸면 LLM 이
    필드를 안 채우는 순간 매수가 통째로 멎는다). 대신 model_dump 에 실려
    decisions.jsonl 로 남아야 나중에 스틸맨 품질을 사후 검증할 수 있다.
    """
    hold = Proposal(symbol="A", market="KR", side="HOLD", conviction=0.5,
                    target_weight=0.0, thesis="t")
    assert hold.bear_case is None and hold.bear_rebuttal is None   # 하위호환

    buy = Proposal(symbol="A", market="KR", side="BUY", conviction=0.7, target_weight=0.2,
                   thesis="t", key_risks=["환율"],
                   bear_case="외국인 5일 순매도 -420억, 영업이익률 3.1%로 전분기 대비 하락",
                   bear_rebuttal="순매도는 배당락 기술적 요인이고 이익률 하락은 일회성 충당금")
    d = buy.model_dump()
    assert d["bear_case"].startswith("외국인") and "일회성" in d["bear_rebuttal"]
    assert d["key_risks"] == ["환율"]          # key_risks 와 별개 필드


def test_약세_스틸맨_지침이_프롬프트에_있다():
    """프롬프트가 이 절차의 유일한 강제 수단이라 실수로 지워지면 조용히 무력화된다."""
    from src.agents.decision_agent import SYSTEM
    from src.agents.validation_agent import SYSTEM as VAL_SYSTEM
    from src.agents.value_trade import VALUE_TRADE_SYSTEM
    for src_text in (SYSTEM, VALUE_TRADE_SYSTEM):
        assert "bear_case" in src_text and "bear_rebuttal" in src_text
    assert "스틸맨" in SYSTEM and "HOLD" in SYSTEM
    assert "스틸맨" in VAL_SYSTEM        # 검증 규칙 11(형식적 스틸맨 거부)
    assert "밸류트랩 반증" in VALUE_TRADE_SYSTEM


def test_context_builds_json():
    ctx = build_context({"asof": "t", "regime": {"KR": {"label": "risk_on"}}},
                        candidates=[{"symbol": "005930", "price": 1000}],
                        portfolio={"cash": {"KR": 1_000_000}}, constraints={"max_positions": 5})
    d = json.loads(ctx)
    assert d["candidates"][0]["symbol"] == "005930"
    assert d["market"]["regime"]["KR"]["label"] == "risk_on"


def test_context_includes_wake_when_provided():
    ctx = json.loads(build_context(
        {}, [], {}, {},
        wake={"reason": "wake_triggers", "n": 1,
              "triggers": [{"kind": "vol_spike", "symbol": "005930"}]}))
    assert ctx["wake"]["reason"] == "wake_triggers"
    assert ctx["wake"]["triggers"][0]["kind"] == "vol_spike"
    assert "wake" not in json.loads(build_context({}, [], {}, {}))


def test_decision_prompt_mentions_wake():
    from src.agents.decision_agent import SYSTEM
    assert "wake.reason" in SYSTEM and "vol_spike" in SYSTEM


def test_cycle_approved_fills(tmp_path):
    llm = MockLLM(_responder(_decision_buy(0.8), approve=True))
    broker = _broker(tmp_path)
    res = run_cycle(context_json="{}", decision_agent=DecisionAgent(llm),
                    validation_agent=ValidationAgent(llm, min_conviction=0.6), broker=broker,
                    risk=RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.2),
                    price_lookup={"005930": 1000}, journal_path=tmp_path / "d.jsonl")
    assert res.executed[0]["status"] == "filled"
    assert broker.position("005930").qty > 0


def test_cycle_vetoed_blocks(tmp_path):
    llm = MockLLM(_responder(_decision_buy(0.8), approve=False))
    broker = _broker(tmp_path)
    res = run_cycle(context_json="{}", decision_agent=DecisionAgent(llm),
                    validation_agent=ValidationAgent(llm, min_conviction=0.6), broker=broker,
                    risk=RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.2),
                    price_lookup={"005930": 1000}, journal_path=tmp_path / "d.jsonl")
    assert res.executed[0]["status"] == "vetoed"
    assert broker.position("005930").qty == 0


def test_low_conviction_pre_rejected(tmp_path):
    llm = MockLLM(_responder(_decision_buy(0.4), approve=True))  # LLM이 승인해도 사전거부돼야
    broker = _broker(tmp_path)
    res = run_cycle(context_json="{}", decision_agent=DecisionAgent(llm),
                    validation_agent=ValidationAgent(llm, min_conviction=0.6), broker=broker,
                    risk=RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.2),
                    price_lookup={"005930": 1000}, journal_path=tmp_path / "d.jsonl")
    assert res.executed[0]["status"] == "vetoed"
    assert broker.position("005930").qty == 0


def _decision_buy_day(conviction=0.8):
    return DecisionOutput(market_view="중립", proposals=[Proposal(
        symbol="005930", market="KR", side="BUY", conviction=conviction,
        horizon="day", target_weight=0.2, thesis="데이트레 적합", key_risks=["변동성"])])


def test_cycle_day_buy_routes_to_arm(tmp_path):
    """데이트레(day) BUY 는 즉시 체결하지 않고 arm_fn 으로 진입대기 등록."""
    llm = MockLLM(_responder(_decision_buy_day(0.8), approve=True))
    broker = _broker(tmp_path)
    armed = []
    res = run_cycle(context_json="{}", decision_agent=DecisionAgent(llm),
                    validation_agent=ValidationAgent(llm, min_conviction=0.6), broker=broker,
                    risk=RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.2),
                    price_lookup={"005930": 1000}, journal_path=tmp_path / "d.jsonl",
                    arm_fn=lambda p, price: armed.append((p.symbol, price)) or True)
    assert res.executed[0]["status"] == "armed"
    assert armed == [("005930", 1000)]
    assert broker.position("005930").qty == 0          # 즉시 체결 안 함


def _decision_sell(conviction=0.3):
    return DecisionOutput(market_view="risk_off", proposals=[Proposal(
        symbol="005930", market="KR", side="SELL", conviction=conviction,
        horizon="swing", target_weight=0.0, thesis="진입 thesis 깨짐: 모멘텀 반전", key_risks=[])])


def test_thesis_break_sell_exempt_from_conviction_floor(tmp_path):
    """thesis 깨짐 SELL 은 확신도 미달이어도 사전거부되지 않고, 승인 시 청산된다."""
    broker = _broker(tmp_path)
    broker.account.fill("005930", "KR", "BUY", 5, 1000)        # 기보유
    llm = MockLLM(_responder(_decision_sell(0.3), approve=True))  # 0.3 < 0.6 임계
    res = run_cycle(context_json="{}", decision_agent=DecisionAgent(llm),
                    validation_agent=ValidationAgent(llm, min_conviction=0.6), broker=broker,
                    risk=RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.2),
                    price_lookup={"005930": 1000}, journal_path=tmp_path / "d.jsonl")
    assert res.executed[0]["status"] == "filled"               # 사전거부 안 됨 -> 청산 체결
    assert broker.position("005930").qty == 0                  # 전량 청산


def test_buy_still_pre_rejected_by_conviction_floor(tmp_path):
    """대조군: min_conviction>0 이면 BUY 는 확신도 미달 시 사전거부."""
    broker = _broker(tmp_path)
    llm = MockLLM(_responder(_decision_buy(0.3), approve=True))
    res = run_cycle(context_json="{}", decision_agent=DecisionAgent(llm),
                    validation_agent=ValidationAgent(llm, min_conviction=0.6), broker=broker,
                    risk=RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.2),
                    price_lookup={"005930": 1000}, journal_path=tmp_path / "d.jsonl")
    assert res.executed[0]["status"] == "vetoed"
    assert broker.position("005930").qty == 0


def test_buy_not_pre_rejected_when_brain_floor_zero(tmp_path):
    """뇌 스윙: min_conviction=0 이면 낮은 LLM 확신도여도 검증 LLM 까지 간다."""
    broker = _broker(tmp_path)
    llm = MockLLM(_responder(_decision_buy(0.3), approve=True))
    res = run_cycle(context_json="{}", decision_agent=DecisionAgent(llm),
                    validation_agent=ValidationAgent(llm, min_conviction=0.0), broker=broker,
                    risk=RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.2),
                    price_lookup={"005930": 1000}, journal_path=tmp_path / "d.jsonl")
    assert res.executed[0]["status"] == "filled"
    assert "확신도 미달" not in (res.executed[0].get("reason") or "")


def test_cycle_code_conviction_overwrites_llm(tmp_path):
    """apply_code_conviction 이면 저널·사이징에 쓰는 숫자는 코드 점수."""
    broker = _broker(tmp_path)
    llm = MockLLM(_responder(_decision_buy(0.9), approve=True))
    res = run_cycle(context_json="{}", decision_agent=DecisionAgent(llm),
                    validation_agent=ValidationAgent(llm, min_conviction=0.0), broker=broker,
                    risk=RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.2),
                    price_lookup={"005930": 1000}, journal_path=tmp_path / "d.jsonl",
                    apply_code_conviction=True)
    assert res.decision.proposals[0].conviction == 0.38  # 도시에 없음 = base-계획없음
    rec = json.loads((tmp_path / "d.jsonl").read_text(encoding="utf-8").strip())
    assert rec["conviction_code"]["005930"]["llm"] == 0.9
    assert rec["conviction_code"]["005930"]["code"] == 0.38
    assert rec["conviction_code"]["005930"]["snap"]["price"] == 1000
    assert rec["conviction_code"]["005930"]["snap"]["stance"] is None


def test_cycle_swing_buy_ignores_arm_fn(tmp_path):
    """스윙/장투 BUY 는 arm_fn 이 있어도 기존대로 즉시 체결."""
    llm = MockLLM(_responder(_decision_buy(0.8), approve=True))   # horizon=swing
    broker = _broker(tmp_path)
    called = []
    res = run_cycle(context_json="{}", decision_agent=DecisionAgent(llm),
                    validation_agent=ValidationAgent(llm, min_conviction=0.6), broker=broker,
                    risk=RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.2),
                    price_lookup={"005930": 1000}, journal_path=tmp_path / "d.jsonl",
                    arm_fn=lambda p, price: called.append(p.symbol) or True)
    assert res.executed[0]["status"] == "filled"
    assert called == []                                # swing 은 즉시 체결(arm 미사용)
    assert broker.position("005930").qty > 0

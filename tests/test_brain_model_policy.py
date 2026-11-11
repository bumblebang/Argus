"""brain_model_policy — wake별 결정 LLM opus/sonnet 라우팅."""
from src.agents.brain_model_policy import decision_tier, pick_decision_llm


def test_opus_athena_and_gap_rebound():
    assert decision_tier({"reason": "athena_done"}) == "opus"
    assert decision_tier({"reason": "gap_rebound_scan"}) == "opus"
    assert decision_tier({"reason": "nxt_gap_scan"}) == "opus"


def test_sonnet_events_and_other_extra():
    assert decision_tier({"reason": "disclosure"}) == "sonnet"
    assert decision_tier({"reason": "wake_triggers"}) == "sonnet"
    assert decision_tier({"reason": "extra", "at": "11:00", "market": "KR"}) == "sonnet"
    assert decision_tier({"reason": "periodic"}) == "sonnet"


def test_opus_market_open_extra_slots():
    assert decision_tier({"reason": "extra", "at": "08:00", "market": "KR"}) == "opus"
    assert decision_tier({"reason": "extra", "at": "09:00", "market": "KR"}) == "opus"
    # US 슬롯은 extra_wakes 와 같은 ET 벽시계 — 정규장 개장 09:30.
    assert decision_tier({"reason": "extra", "at": "09:30", "market": "US"}) == "opus"
    # 구 KST 표기(22:30)는 ET extra_wakes 와 매칭될 일이 없다.
    assert decision_tier({"reason": "extra", "at": "22:30", "market": "US"}) == "sonnet"


def test_opus_slot_survives_trigger_coalesce():
    """슬롯 시각에 트리거가 합쳐져도(reason 'wake_triggers+extra') opus 슬롯 유지."""
    assert decision_tier({"reason": "wake_triggers+extra", "at": "09:30",
                          "market": "US"}) == "opus"
    assert decision_tier({"reason": "wake_triggers+extra", "at": "08:00",
                          "market": "KR"}) == "opus"
    assert decision_tier({"reason": "wake_triggers+extra", "at": "11:30",
                          "market": "US"}) == "sonnet"


def test_pick_decision_llm():
    opus, sonnet = object(), object()
    assert pick_decision_llm({"reason": "athena_done"}, opus, sonnet) is opus
    assert pick_decision_llm({"reason": "disclosure"}, opus, sonnet) is sonnet

"""J7: Athena invalidation 이 코드 손절을 덮어쓰던 경로 + RR 순환 차단."""
from src.agents.conviction import score_buy, BASE, W_RR_HI, W_RR_LO
from src.agents.manager_id import SCORING_REV, manager_snapshot
from src.agents.schemas import Proposal
from src.agents.wiring import (MAX_STOP_PCT, MIN_STOP_PCT, combine_stop_target,
                               entry_stop_target)


def _buy(**kw):
    base = dict(symbol="005930", market="KR", side="BUY", conviction=0.9,
                horizon="swing", target_weight=0.2, thesis="t", key_risks=[])
    base.update(kw)
    return Proposal(**base)


def test_combine_in_band_keeps_invalidation():
    stop, target, note = combine_stop_target(
        1000, "swing", None, invalidation=900, target=1200)
    assert stop == 900 and target == 1200 and note is None


def test_combine_too_thin_falls_back_to_code_stop():
    """0.1% 손절은 밴드 밖 → 코드 손절(스윙 5%)."""
    code_stop, code_tgt = entry_stop_target(1000, "swing", None)
    stop, target, note = combine_stop_target(
        1000, "swing", None, invalidation=999, target=1200)
    assert stop == code_stop
    assert target == 1200
    assert note and "밴드 밖" in note


def test_combine_too_wide_falls_back_to_code_stop():
    code_stop, _ = entry_stop_target(1000, "swing", None)
    stop, _, note = combine_stop_target(
        1000, "swing", None, invalidation=100, target=1200)
    assert stop == code_stop
    assert note and "밴드 밖" in note
    # 밴드 자체: 가장 넓은 손절 = 30%, 가장 얇은 = 0.5%
    lo = 1000 * (1 - MAX_STOP_PCT)
    hi = 1000 * (1 - MIN_STOP_PCT)
    assert lo == 700 and abs(hi - 995) < 1e-9


def test_combine_phantom_target_falls_back_to_code():
    _, code_tgt = entry_stop_target(1000, "swing", None)
    stop, target, note = combine_stop_target(
        1000, "swing", None, invalidation=950, target=5000)
    assert stop == 950
    assert target == code_tgt
    assert note and "target" in note


def test_score_buy_ignores_llm_target():
    """허상 목표가 사이징 가산을 키우지 못한다."""
    tight = {"stance": "bullish", "entry_low": 90, "entry_high": 110,
             "invalidation": 95, "target": 110}
    phantom = dict(tight, target=5000)
    a = score_buy(_buy(), price=100, dossier=tight)
    b = score_buy(_buy(), price=100, dossier=phantom)
    assert a.value == b.value == round(BASE + W_RR_HI, 2)


def test_score_buy_wide_stop_is_rr_penalty():
    """넓은 손절(밴드 안 20%)은 코드 목표 10% 대비 RR<1.5 → 감점."""
    wide = {"stance": "bullish", "entry_low": 90, "entry_high": 110,
            "invalidation": 80, "target": 140}
    sc = score_buy(_buy(), price=100, dossier=wide)
    assert sc.value == round(BASE + W_RR_LO, 2)
    assert "손익비" in " ".join(sc.parts)


def test_scoring_rev_in_epoch():
    class L:
        model = "opus"
        last_model = "opus"
        used_fallback = False
        last_source = "cli"
    snap = manager_snapshot(decision_llm=L(), decision_prompt="D",
                            validation_prompt="V")
    assert SCORING_REV == "j7"
    assert f":{SCORING_REV}" in snap["epoch"]

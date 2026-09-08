"""뇌 BUY 확신도 코드 루브릭."""
from src.agents.conviction import (
    score_buy, apply_buy_conviction, size_weight, min_lot_adjust, unit_intensity,
    BASE, FLOOR, CAP,
    W_RR_HI, W_STAB, W_STAB_BAD, W_FLOW, W_FLOW_BAD, W_SETUP, W_FUND_RED,
    W_NO_PLAN, W_DAY_OS, W_EARN_MISS,
    STAB_RET_SCALE, STAB_DD_SCALE, FLOW_PART_SCALE, EARN_MISS_SCALE,
    RSI_OS, RSI_OS_SPAN, SETUP_WR_MID, SETUP_WR_SPAN,
)
from src.agents.schemas import Proposal, DecisionOutput


def _buy(**kw):
    base = dict(symbol="005930", market="KR", side="BUY", conviction=0.9,
                horizon="swing", target_weight=0.2, thesis="t", key_risks=[])
    base.update(kw)
    return Proposal(**base)


def _zone(**extra):
    d = {"stance": "bullish", "entry_low": 90, "entry_high": 110,
         "invalidation": 95, "target": 140, "rr": 2.0}
    d.update(extra)
    return d


def _total(*parts):
    return round(min(CAP, max(FLOOR, sum(parts))), 2)


def test_no_plan_haircut():
    sc = score_buy(_buy(), price=100, dossier=None)
    assert sc.value == round(BASE - 0.10, 2)
    assert sc.llm == 0.9


def test_plan_rr_only_no_feature_stamps():
    sc = score_buy(_buy(), price=100, dossier=_zone())
    assert sc.value == round(BASE + 0.08, 2)  # rr>=2


def test_evidence_bullet_count_does_not_add():
    p = _buy()
    a = score_buy(p, price=100, dossier=_zone(evidence_n=1))
    b = score_buy(p, price=100, dossier=_zone(evidence_n=8,
                                             evidence=["a", "b", "c", "d", "e"]))
    assert a.value == b.value


def test_zone_location_does_not_change_score():
    """존 안/위는 체결 경로가 처리. 사이징은 같은 계획이면 같다."""
    a = score_buy(_buy(), price=100, dossier=_zone())
    b = score_buy(_buy(), price=130, dossier=_zone())
    assert a.value == b.value


def test_aligned_signed_features():
    feat = {
        "stabilizing": {"ok": True, "above_ma20": True, "ret_20d_pct": 3.0},
        "flows": {"foreign_net": 12000},
        "fundamentals": {"net_margin": 0.08},
        "base_rates": {"breakout_pullback": {
            "n": 40, "win_rate": 0.62, "avg_ret_pct": 1.2, "small_sample": False}},
    }
    sc = score_buy(_buy(), price=100, dossier=_zone(), features=feat)
    stab = W_STAB * unit_intensity(3.0, STAB_RET_SCALE)
    setup_t = min(1.0, (0.62 - SETUP_WR_MID) / SETUP_WR_SPAN)
    assert sc.value == _total(BASE, W_RR_HI, stab, W_FLOW, W_SETUP * setup_t)
    blob = " ".join(sc.parts)
    assert "안정화" in blob and "순매수" in blob and "셋업" in blob
    assert "흑자" not in blob  # 흑자는 가산하지 않음


def test_clear_alignment_still_caps():
    feat = {
        "stabilizing": {"ok": True, "above_ma20": True, "ret_20d_pct": 12.0},
        "flows": {"foreign_net": 12000},
        "base_rates": {"breakout_pullback": {
            "n": 40, "win_rate": 0.70, "avg_ret_pct": 1.2, "small_sample": False}},
    }
    sc = score_buy(_buy(), price=100, dossier=_zone(), features=feat)
    assert sc.value == CAP


def test_hostile_features_subtract():
    feat = {
        "stabilizing": {"ok": False, "above_ma20": False, "ret_20d_pct": -8.0},
        "flows": {"foreign_net": -5000},
        "fundamentals": {"net_margin": -0.12},
    }
    sc = score_buy(_buy(), price=100, dossier=_zone(), features=feat)
    stab = W_STAB_BAD * unit_intensity(8.0, STAB_DD_SCALE)
    assert sc.value == _total(BASE, W_RR_HI, stab, W_FLOW_BAD, W_FUND_RED)


def test_weak_stab_below_min_lot_cut():
    """부호만 있는 약한 안정화는 0.6 을 넘기지 않는다(고단가 1주 컷)."""
    feat = {"stabilizing": {"ok": True, "above_ma20": True, "ret_20d_pct": 0.3}}
    sc = score_buy(_buy(), price=100, dossier=_zone(), features=feat)
    assert sc.value < 0.6
    strong = score_buy(_buy(), price=100, dossier=_zone(), features={
        "stabilizing": {"ok": True, "above_ma20": True, "ret_20d_pct": 5.0}})
    assert strong.value >= 0.6
    assert strong.value > sc.value


def test_flow_intensity_uses_volume_when_present():
    sign = score_buy(_buy(), price=100, dossier=_zone(),
                     features={"flows": {"foreign_net": 1000}})
    weak = score_buy(_buy(), price=100, dossier=_zone(),
                     features={"flows": {"foreign_net": 1000}, "volume": 100_000})
    strong = score_buy(_buy(), price=100, dossier=_zone(),
                      features={"flows": {"foreign_net": 8000}, "volume": 100_000})
    mag = unit_intensity(1000 / 100_000, FLOW_PART_SCALE)
    assert sign.value == _total(BASE, W_RR_HI, W_FLOW)
    assert weak.value == _total(BASE, W_RR_HI, W_FLOW * mag)
    assert weak.value < strong.value <= sign.value
    assert "참여" in " ".join(weak.parts)


def test_setup_win_rate_is_graded():
    def feat(wr):
        return {"base_rates": {"breakout_pullback": {
            "n": 40, "win_rate": wr, "avg_ret_pct": 1.0, "small_sample": False}}}
    mid = score_buy(_buy(), price=100, dossier=_zone(), features=feat(0.50))
    hi = score_buy(_buy(), price=100, dossier=_zone(), features=feat(0.62))
    lo = score_buy(_buy(), price=100, dossier=_zone(), features=feat(0.38))
    assert mid.value == _total(BASE, W_RR_HI)
    assert hi.value == _total(BASE, W_RR_HI, W_SETUP)
    assert lo.value < mid.value < hi.value


def test_below_invalidation_penalized():
    sc = score_buy(_buy(), price=70, dossier=_zone())
    assert sc.value == round(BASE + 0.08 - 0.20, 2)
    assert "무효화가 하회" in " ".join(sc.parts)


def test_day_oversold_only_with_rsi():
    p = _buy(horizon="day", strategy="volatility_breakout")
    a = score_buy(p, price=100, dossier=None)
    b = score_buy(p, price=100, dossier=None, features={"rsi": 36})
    c = score_buy(p, price=100, dossier=None, features={"rsi": 30})
    dlt = W_DAY_OS * unit_intensity(RSI_OS - 36, RSI_OS_SPAN)
    assert a.value == round(BASE + W_NO_PLAN, 2)
    assert b.value == _total(BASE, W_NO_PLAN, dlt)
    assert a.value < b.value < c.value


def test_floor_and_cap():
    sc = score_buy(_buy(), price=None, dossier=None)
    assert FLOOR <= sc.value <= CAP


def test_size_weight_matches_cycle_formula():
    # base × (0.75 + 0.25×c)
    assert size_weight(0.2, 0.6) == 0.2 * (0.75 + 0.25 * 0.6)
    assert size_weight(0.2, None) == 0.2
    assert size_weight(0.2, 0.6, enabled=False) == 0.2
    assert size_weight(0.2, 1.0, cap=0.18) == 0.18
    assert abs(size_weight(0.2, 0.0) - 0.15) < 1e-12  # floor only


def test_min_lot_adjust_bumps_only_when_cut_clears():
    w, q = min_lot_adjust(0.12, price=357_000, capital=1_000_000,
                          conviction=0.62, min_lot_conviction=0.6)
    assert q == 1.0 and w == 357_000 / 1_000_000
    w2, q2 = min_lot_adjust(0.12, price=357_000, capital=1_000_000,
                            conviction=0.55, min_lot_conviction=0.6)
    assert q2 == 0.0 and w2 == 0.12
    w3, q3 = min_lot_adjust(0.12, price=357_000, capital=1_000_000,
                            conviction=0.80, min_lot_conviction=0.6, enabled=False)
    assert q3 == 0.0 and w3 == 0.12


def test_apply_overwrites_llm_score():
    dec = DecisionOutput(market_view="x", proposals=[_buy(conviction=0.42)])
    audit = apply_buy_conviction(dec, {"005930": 100}, brief_fn=lambda s: None)
    assert dec.proposals[0].conviction == round(BASE - 0.10, 2)
    assert audit["005930"]["llm"] == 0.42
    snap = audit["005930"]["snap"]
    assert snap["price"] == 100
    assert snap["horizon"] == "swing"
    assert snap["stance"] is None
    assert snap["foreign_net"] is None
    assert snap["zone"] is None


def test_snap_freezes_signed_inputs_not_whole_candidate():
    feat = {
        "stabilizing": {"ok": True, "above_ma20": True, "ret_20d_pct": 3.0},
        "flows": {"foreign_net": 12000},
        "fundamentals": {"net_margin": 0.08, "revenue": 1e12},
        "rsi": 55.0,
        "volume": 50000,
        "drawdown_pct": -4.2,
        "news": [{"title": "호재 제목은 남기지 않는다"}],
        "base_rates": {"breakout_pullback": {
            "n": 40, "win_rate": 0.62, "avg_ret_pct": 1.2, "small_sample": False}},
        "disclosures": [{"keyword": "소송", "report_nm": "소송등의제기"}],
        "earnings_results": [{"op_profit_surprise_pct": -18.4}],
        "past_trades": {"n": 9},
    }
    audit = apply_buy_conviction(
        DecisionOutput(market_view="x", proposals=[_buy()]),
        {"005930": 100}, brief_fn=lambda s: _zone(),
        features_by_sym={"005930": feat})
    snap = audit["005930"]["snap"]
    assert snap["zone"] == "in"
    assert snap["stab_ok"] is True
    assert snap["foreign_net"] == 12000
    assert snap["net_margin"] == 0.08
    assert snap["volume"] == 50000
    assert snap["drawdown_pct"] == -4.2
    assert snap["setup"]["name"] == "breakout_pullback"
    assert snap["disclosures"][0]["keyword"] == "소송"
    assert snap["earn_surprise_pct"] == -18.4
    assert "news" not in snap and "past_trades" not in snap and "revenue" not in snap
    import json
    json.dumps(snap)  # 저널에 그대로 들어가므로 직렬화 가능해야 한다


def test_dilution_filing_haircut():
    feat = {"news": [{"title": "[삼성전자] 유상증자결정"}]}
    sc = score_buy(_buy(), price=100, dossier=_zone(), features=feat)
    assert sc.value == round(BASE + 0.08 - 0.10, 2)
    assert "유상증자" in " ".join(sc.parts)


def test_legal_filing_worse_than_dilution():
    feat = {"disclosures": [{"symbol": "005930", "keyword": "소송",
                             "report_nm": "소송등의제기"}]}
    sc = score_buy(_buy(), price=100, dossier=_zone(), features=feat)
    assert sc.value == round(BASE + 0.08 - 0.12, 2)


def test_supply_contract_and_bullish_headline_do_not_add():
    feat = {"news": [
        {"title": "[HD현대중공업] 대규모 공급계약체결"},
        {"title": "실적 호조·목표가 상향"},
    ]}
    a = score_buy(_buy(), price=100, dossier=_zone())
    b = score_buy(_buy(), price=100, dossier=_zone(), features=feat)
    assert a.value == b.value
    blob = " ".join(b.parts)
    assert "공급계약" not in blob and "호조" not in blob


def test_earnings_miss_haircut_not_beat():
    miss = score_buy(_buy(), price=100, dossier=_zone(), features={
        "earnings_results": [{"op_profit_surprise_pct": -18.4, "parse_ok": True}]})
    worse = score_buy(_buy(), price=100, dossier=_zone(), features={
        "earnings_results": [{"op_profit_surprise_pct": -40.0, "parse_ok": True}]})
    beat = score_buy(_buy(), price=100, dossier=_zone(), features={
        "earnings_results": [{"op_profit_surprise_pct": 22.0, "parse_ok": True}]})
    dlt = W_EARN_MISS * unit_intensity(18.4, EARN_MISS_SCALE)
    assert miss.value == _total(BASE, W_RR_HI, dlt)
    assert beat.value == round(BASE + 0.08, 2)
    assert worse.value < miss.value
    assert "실적 하회" in " ".join(miss.parts)


def test_profit_margin_still_does_not_add():
    sc = score_buy(_buy(), price=100, dossier=_zone(),
                   features={"fundamentals": {"net_margin": 0.15}})
    assert sc.value == round(BASE + 0.08, 2)
    assert "흑자" not in " ".join(sc.parts)


def test_attach_event_features_by_symbol():
    from src.agents.conviction import attach_event_features
    feats = {"005930": {"symbol": "005930"}, "000660": {"symbol": "000660"}}
    attach_event_features(
        feats,
        disclosures=[{"symbol": "005930", "keyword": "유상증자", "report_nm": "유상증자"}],
        earnings_results=[{"symbol": "000660", "op_profit_surprise_pct": -12.0}])
    assert feats["005930"]["disclosures"][0]["keyword"] == "유상증자"
    assert feats["000660"]["earnings_results"][0]["op_profit_surprise_pct"] == -12.0
    assert "earnings_results" not in feats["005930"]


# ── 밸류 트랙 루브릭 ──────────────────────────────────────────────
# 스윙과 축이 다르다: 가산은 고유(저평가도·안전마진), 감점은 공유(_event_parts).
from src.agents.conviction import (  # noqa: E402
    score_value_buy, freeze_value_snap, value_margin,
    VALUE_BASE, VALUE_FLOOR, VALUE_CAP,
    W_VALUE_CHEAP, W_VALUE_MARGIN, CV_MID, CV_SPAN, MARGIN_SCALE,
    W_DISC_LEGAL, W_DISC_DILUTE,
)


def _vbuy(**kw):
    base = dict(symbol="005930", market="KR", side="BUY", conviction=0.62,
                horizon="position", target_weight=0.0, thesis="t", key_risks=[])
    base.update(kw)
    return Proposal(**base)


def _cand(**extra):
    """value_watchlist 항목 + 셀렉터가 붙이는 _fair_low."""
    d = {"stance": "undervalued", "conviction": 0.5,
         "composite_value": CV_MID, "_fair_low": None,
         "fair_low_pct": 12.0, "metrics": {"price": 100.0},
         "fundamentals": {"pb": 0.6, "pe_trailing": 5.0}}
    d.update(extra)
    return d


def test_value_base_when_all_missing():
    """결측은 0 기여 — 스윙과 같은 규약."""
    sc = score_value_buy(_vbuy(), price=100, dossier=None)
    assert sc.value == round(VALUE_BASE, 2)
    assert sc.llm == 0.62


def test_value_cheapness_axis_signed_and_saturates():
    lo = score_value_buy(_vbuy(), price=100,
                         dossier=_cand(composite_value=CV_MID - CV_SPAN))
    mid = score_value_buy(_vbuy(), price=100, dossier=_cand())
    hi = score_value_buy(_vbuy(), price=100,
                         dossier=_cand(composite_value=CV_MID + CV_SPAN))
    assert lo.value == round(VALUE_BASE - W_VALUE_CHEAP, 2)
    assert mid.value == round(VALUE_BASE, 2)
    assert hi.value == round(VALUE_BASE + W_VALUE_CHEAP, 2)
    # 포화: span 밖으로 더 나가도 한도를 넘지 않는다
    way_hi = score_value_buy(_vbuy(), price=100,
                             dossier=_cand(composite_value=1.0))
    assert way_hi.value == hi.value


def test_value_margin_axis_is_one_sided():
    """안전마진은 편도(+) — 음수 구간은 passes_margin_guard 가 이미 자른다."""
    none_m = score_value_buy(_vbuy(), price=100, dossier=_cand(_fair_low=None))
    thin = score_value_buy(_vbuy(), price=100, dossier=_cand(_fair_low=105.0))
    fat = score_value_buy(_vbuy(), price=100, dossier=_cand(_fair_low=130.0))
    assert none_m.value == round(VALUE_BASE, 2)
    assert thin.value > none_m.value
    assert fat.value > thin.value
    assert fat.value <= round(VALUE_BASE + W_VALUE_MARGIN, 2)
    # 이미 적정가 위 → 가산 없음(게이트가 거른 케이스라도 점수는 중립)
    over = score_value_buy(_vbuy(), price=140, dossier=_cand(_fair_low=130.0))
    assert over.value == round(VALUE_BASE, 2)


def test_value_margin_matches_scale():
    d = _cand(_fair_low=115.0)
    sc = score_value_buy(_vbuy(), price=100, dossier=d)
    expected = VALUE_BASE + W_VALUE_MARGIN * unit_intensity(0.15, MARGIN_SCALE)
    assert sc.value == round(expected, 2)


def test_value_shares_event_haircuts():
    """감점항은 스윙과 공유 — 법적 공시 > 희석."""
    legal = score_value_buy(_vbuy(), price=100, dossier=_cand(),
                            features={"news": [{"title": "대표 횡령 혐의 피소"}]})
    dilute = score_value_buy(_vbuy(), price=100, dossier=_cand(),
                             features={"news": [{"title": "1500억 유상증자 결정"}]})
    assert legal.value == round(VALUE_BASE + W_DISC_LEGAL, 2)
    assert dilute.value == round(VALUE_BASE + W_DISC_DILUTE, 2)
    assert legal.value < dilute.value


def test_value_ignores_swing_axes():
    """스윙 축이 밸류로 새지 않는다 — 두 루브릭 분리의 계약.

    안정화는 timing_gate 가 하드 요구라 전원 통과(변별력 0)이고, 일간 수급·셋업
    승률은 넉 달 보유엔 노이즈다. 재무는 composite_value 의 quality_tilt 가 이미 본다.
    """
    plain = score_value_buy(_vbuy(), price=100, dossier=_cand())
    noisy = score_value_buy(_vbuy(), price=100, dossier=_cand(), features={
        "stabilizing": {"ok": True, "above_ma20": True, "ret_20d_pct": 9.0},
        "flows": {"foreign_net": 5_000_000}, "volume": 10_000_000,
        "base_rates": {"s": {"win_rate": 0.9, "n": 50, "avg_ret_pct": 5.0}},
        "fundamentals": {"net_margin": -0.40},
        "rsi": 20.0,
    })
    assert noisy.value == plain.value


def test_value_rr_and_invalidation_absent():
    """ValueDossier 에 레벨이 없다 — 스윙의 손익비·무효화 항이 발화하면 안 된다."""
    sc = score_value_buy(_vbuy(), price=100,
                         dossier=_cand(entry_low=90, entry_high=110,
                                       invalidation=120, target=200, rr=3.0))
    assert sc.value == round(VALUE_BASE, 2)


def test_value_clamped_both_ends():
    floor_hit = score_value_buy(_vbuy(), price=100, dossier=_cand(
        composite_value=0.0), features={"news": [{"title": "상장폐지 심사 착수"}]})
    assert floor_hit.value == VALUE_FLOOR
    cap_hit = score_value_buy(_vbuy(), price=1, dossier=_cand(
        composite_value=1.0, _fair_low=10.0))
    assert cap_hit.value <= VALUE_CAP


def test_value_margin_helper():
    assert value_margin(100, 120) == 0.2
    assert value_margin(100, None) is None
    assert value_margin(0, 120) is None
    assert value_margin(None, 120) is None


def test_value_snap_freezes_value_axes():
    snap = freeze_value_snap(_vbuy(), price=100,
                             dossier=_cand(_fair_low=112.0),
                             features={"news": [{"title": "실적 발표"}]})
    assert snap["composite_value"] == CV_MID
    assert snap["fair_low"] == 112.0
    assert snap["margin"] == 0.12
    assert snap["horizon"] == "position"
    assert snap["news"] == ["실적 발표"]
    # 스윙 전용 필드는 밸류 snap 에 없다
    assert "zone" not in snap and "rr" not in snap


def test_apply_buy_conviction_accepts_track_rubric():
    """score_fn/snap_fn 주입 — LLM 자가채점은 audit.llm 으로만 남는다."""
    p = _vbuy(conviction=0.62)
    dec = DecisionOutput(market_view="v", proposals=[p])
    audit = apply_buy_conviction(
        dec, {"005930": 100}, lambda s: _cand(_fair_low=130.0),
        score_fn=score_value_buy, snap_fn=freeze_value_snap)
    assert p.conviction == audit["005930"]["code"]
    assert p.conviction != 0.62
    assert audit["005930"]["llm"] == 0.62
    assert "composite_value" in audit["005930"]["snap"]


def test_apply_buy_conviction_defaults_to_swing():
    p = _buy()
    dec = DecisionOutput(market_view="v", proposals=[p])
    audit = apply_buy_conviction(dec, {"005930": 100}, lambda s: _zone())
    assert p.conviction == round(BASE + W_RR_HI, 2)
    assert "zone" in audit["005930"]["snap"]

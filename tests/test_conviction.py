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
         "invalidation": 80, "target": 140, "rr": 2.0}
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
    assert size_weight(0.2, 0.6) == 0.2 * (0.5 + 0.5 * 0.6)
    assert size_weight(0.2, None) == 0.2
    assert size_weight(0.2, 0.6, enabled=False) == 0.2


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

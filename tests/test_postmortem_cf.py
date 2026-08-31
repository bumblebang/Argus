"""J10 postmortem CF — 손절·비용 반사실."""
from src.eval.postmortem_cf import cf_stop_pct, forward_ret_pct_with_stop


def test_cf_stop_pct_by_horizon():
    assert cf_stop_pct(sleeve="brain", horizon="day", cfg={}) == 0.05
    assert cf_stop_pct(sleeve="brain", horizon="swing", cfg={}) == 0.08
    assert cf_stop_pct(sleeve="value", horizon="swing",
                       cfg={"value_trade": {"hard_stop_pct": 0.25}}) == 0.25


def test_forward_ret_stop_hit_before_horizon():
    entry = 100.0
    bars = [(99.0, 91.0), (98.0, 97.0), (102.0, 101.0)]  # d1 low hits 8% stop
    ret = forward_ret_pct_with_stop(
        entry=entry, bars=bars, days=3, stop_pct=0.08, cost_pct=0.0028)
    assert ret is not None
    assert ret < -7.0  # ~-8% minus cost


def test_forward_ret_hold_to_horizon():
    entry = 100.0
    bars = [(101.0, 100.5), (102.0, 101.0), (110.0, 109.0)]
    ret = forward_ret_pct_with_stop(
        entry=entry, bars=bars, days=3, stop_pct=0.08, cost_pct=0.0)
    assert ret == 10.0

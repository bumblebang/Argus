"""engine.triggers — 손절/익절/변동성/관심가 트리거 순수함수 검증."""
from src.engine import triggers as T


def _pos(**kw):
    base = {"symbol": "005930", "qty": 10, "avg_price": 70000,
            "stop_price": None, "target_price": None}
    base.update(kw)
    return base


def test_stop_hit_when_price_below_stop():
    out = T.position_triggers(_pos(stop_price=69000), price=68500)
    kinds = {t.kind for t in out}
    assert "stop_hit" in kinds
    assert out[0].urgency == "act"


def test_stop_approach_within_buffer():
    # 손절가 69000, 버퍼 0.5% -> 69345 이하에서 근접
    out = T.position_triggers(_pos(stop_price=69000), price=69300, stop_buffer_pct=0.005)
    assert [t.kind for t in out] == ["stop_approach"]
    assert out[0].urgency == "watch"


def test_no_stop_trigger_above_buffer():
    out = T.position_triggers(_pos(stop_price=69000), price=72000)
    assert out == []


def test_target_hit():
    out = T.position_triggers(_pos(target_price=75000), price=75500)
    assert [t.kind for t in out] == ["target_hit"]
    assert out[0].urgency == "act"


def test_position_triggers_no_price():
    assert T.position_triggers(_pos(stop_price=69000), price=None) == []


def test_volatility_spike_up():
    t = T.volatility_trigger("AAPL", [100, 101, 104.0], spike_pct=0.03)
    assert t and t.kind == "vol_spike" and t.payload["change"] > 0


def test_volatility_spike_down():
    t = T.volatility_trigger("AAPL", [100, 99, 96.0], spike_pct=0.03)
    assert t and t.payload["change"] < 0


def test_volatility_no_spike_small_move():
    assert T.volatility_trigger("AAPL", [100, 100.5, 101], spike_pct=0.03) is None


def test_volatility_needs_two_points():
    assert T.volatility_trigger("AAPL", [100], spike_pct=0.03) is None
    assert T.volatility_trigger("AAPL", [], spike_pct=0.03) is None


def test_limit_trigger_below():
    t = T.limit_trigger("005930", price=68000, watch_level=68500, direction="below")
    assert t and t.kind == "limit_reached" and t.urgency == "act"


def test_limit_trigger_above():
    t = T.limit_trigger("005930", price=80000, watch_level=79000, direction="above")
    assert t and t.kind == "limit_reached"


def test_limit_trigger_no_level_or_no_hit():
    assert T.limit_trigger("005930", price=68000, watch_level=None) is None
    assert T.limit_trigger("005930", price=70000, watch_level=68500, direction="below") is None


def test_regime_flip_fires_on_opposite():
    t = T.regime_flip_trigger("005930", "risk_on", "risk_off")
    assert t and t.kind == "regime_flip" and t.urgency == "act"
    assert t.payload == {"entry_regime": "risk_on", "current_regime": "risk_off"}
    t2 = T.regime_flip_trigger("005930", "risk_off", "risk_on")
    assert t2 and t2.kind == "regime_flip"


def test_regime_flip_none_when_same_or_neutral():
    assert T.regime_flip_trigger("005930", "risk_on", "risk_on") is None
    assert T.regime_flip_trigger("005930", "risk_on", "neutral") is None
    assert T.regime_flip_trigger("005930", "neutral", "risk_off") is None


def test_regime_flip_none_when_missing():
    assert T.regime_flip_trigger("005930", None, "risk_off") is None
    assert T.regime_flip_trigger("005930", "risk_on", None) is None


def test_max_urgency():
    a = T.Trigger("vol_spike", "X", "watch", "")
    b = T.Trigger("stop_hit", "X", "act", "")
    assert T.max_urgency([a, b]) == "act"
    assert T.max_urgency([a]) == "watch"
    assert T.max_urgency([]) is None

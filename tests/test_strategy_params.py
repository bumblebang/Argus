"""strategies — 파라미터 스키마/하드가드(클램프)/카탈로그 검증."""
from src.strategies import (build_strategy, validate_params, strategy_catalog, REGISTRY)


def test_catalog_lists_all_with_param_ranges():
    cat = {c["name"]: c for c in strategy_catalog()}
    assert set(cat) == set(REGISTRY)
    vb = cat["volatility_breakout"]
    names = {p["name"] for p in vb["params"]}
    assert {"k", "target_profit_pct", "stop_loss_pct"} <= names
    for p in vb["params"]:                      # 모든 파라미터에 범위 명시
        assert p["min"] <= p["default"] <= p["max"]


def test_clamp_over_and_under_bounds():
    # k 범위 0~2: 과대 5.0 -> 2.0, 과소 -1 -> 0.0
    params, viol = validate_params("volatility_breakout",
                                   {"k": 5.0, "stop_loss_pct": 0.001})
    assert params["k"] == 2.0
    assert params["stop_loss_pct"] == 0.005     # 최소로 클램프
    assert len(viol) == 2


def test_int_params_rounded():
    params, _ = validate_params("rsi_reversion", {"period": 13.6})
    assert params["period"] == 14 and isinstance(params["period"], int)


def test_bad_value_falls_back_to_default():
    params, viol = validate_params("volatility_breakout", {"k": "abc"})
    assert params["k"] == 0.5 and viol          # 비정상값 -> 기본 + 위반기록


def test_ma_cross_check_short_lt_long():
    params, viol = validate_params("ma_crossover", {"short": 30, "long": 10})
    assert params["short"] < params["long"]     # short >= long 교정됨
    assert any("short" in v for v in viol)


def test_passthrough_non_schema_keys():
    params, _ = validate_params("volatility_breakout",
                                {"candle_interval": "1m", "exit_at_session_end": True})
    assert params["candle_interval"] == "1m" and params["exit_at_session_end"] is True


def test_build_strategy_clamps_and_records():
    strat = build_strategy("volatility_breakout", {"k": 99})
    assert strat.params["k"] == 2.0
    assert strat.param_violations                # 클램프 기록 남음

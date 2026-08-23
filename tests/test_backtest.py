"""backtest 엔진 — 단일 백테스트 지표 + 파라미터 스윕/그리드 + 리서치 매트릭스."""
from src.backtest import (backtest, sweep, param_grid, synthetic, BacktestResult,
                          buy_and_hold, best_per_strategy)
from src.strategies import build_strategy, REGISTRY


def test_backtest_returns_metrics():
    df = synthetic(300, seed=1, drift=0.001, vol=0.02)
    r = backtest(build_strategy("ma_crossover", {"short": 5, "long": 20}), df)
    assert isinstance(r, BacktestResult)
    assert r.n_trades >= 0
    assert 0.0 <= r.win_rate <= 1.0
    assert r.mdd <= 0.0                       # 낙폭은 0 이하
    assert isinstance(r.return_pct, float)


def test_backtest_deterministic():
    df = synthetic(200, seed=42)
    a = backtest(build_strategy("rsi_reversion", {}), df)
    b = backtest(build_strategy("rsi_reversion", {}), df)
    assert a.pnl == b.pnl and a.n_trades == b.n_trades


def test_param_grid_within_bounds_and_deduped():
    grid = param_grid("rsi_reversion", steps=3)
    assert len(grid) > 1
    specs = {s.name: s for s in REGISTRY["rsi_reversion"].PARAMS}
    seen = set()
    for combo in grid:
        for name, val in combo.items():
            assert specs[name].min <= val <= specs[name].max
        key = tuple(sorted(combo.items()))
        assert key not in seen                # 중복 없음
        seen.add(key)


def test_param_grid_respects_cross_check():
    # ma_crossover: short < long 가 항상 성립해야(cross_check 반영)
    for combo in param_grid("ma_crossover", steps=4):
        assert combo["short"] < combo["long"]


def test_sweep_sorted_by_metric():
    df = synthetic(400, seed=3, drift=0.0008, vol=0.025)
    ranked = sweep("volatility_breakout", df, steps=3)
    assert len(ranked) > 1
    rets = [r.return_pct for r in ranked]
    assert rets == sorted(rets, reverse=True)     # 내림차순 정렬(첫=최적)
    assert ranked[0].strategy == "volatility_breakout"


def test_buy_and_hold():
    df = synthetic(50, seed=5)
    bnh = buy_and_hold(df)
    expected = df["close"].iloc[-1] / df["close"].iloc[0] - 1
    assert abs(bnh - expected) < 1e-9
    assert buy_and_hold(df.iloc[:1]) == 0.0        # 1봉 이하 -> 0


def test_best_per_strategy_covers_all():
    df = synthetic(300, seed=2, drift=0.001, vol=0.02)
    best = best_per_strategy(df, steps=3)
    assert set(best.keys()) == set(REGISTRY.keys())   # 전 전략 커버
    for name, res in best.items():
        assert res.strategy == name and isinstance(res, BacktestResult)

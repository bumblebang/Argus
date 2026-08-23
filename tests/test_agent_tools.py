"""agents.tools — 브레인 분석 도구(지표 스냅샷·전략 추천·툴킷)."""
from src.agents.tools import (indicator_snapshot, recommend_strategy, toolkit_manifest, TOOLKIT)
from src.backtest import synthetic
from src.strategies import REGISTRY


def test_indicator_snapshot_fields():
    df = synthetic(80, seed=2)
    snap = indicator_snapshot(df)
    assert "price" in snap and "ma20" in snap and "rsi14" in snap
    assert "ma60" in snap and "volatility" in snap


def test_indicator_snapshot_short_series():
    df = synthetic(8, seed=2)
    snap = indicator_snapshot(df)
    assert "price" in snap and "ma5" in snap
    assert "ma20" not in snap                  # 캔들 부족 → 생략


def test_recommend_strategy_ranks_all_applicable():
    df = synthetic(120, seed=5, drift=0.001, vol=0.025)
    rec = recommend_strategy(df)
    assert rec["best"] in REGISTRY
    assert {r["strategy"] for r in rec["ranking"]} == set(REGISTRY)   # 3종 모두 적용
    rets = [r["return_pct"] for r in rec["ranking"]]
    assert rets == sorted(rets, reverse=True)                          # return_pct 내림차순
    assert rec["best"] == rec["ranking"][0]["strategy"]


def test_recommend_strategy_short_series_excludes():
    df = synthetic(10, seed=5)              # ma_crossover(min 22)·rsi(min 16) 제외, vol(min 2)만
    rec = recommend_strategy(df)
    names = {r["strategy"] for r in rec["ranking"]}
    assert "ma_crossover" not in names and "rsi_reversion" not in names


def test_toolkit_manifest():
    man = {m["name"] for m in toolkit_manifest()}
    assert man == set(TOOLKIT)
    assert "recommend_strategy" in man

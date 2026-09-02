"""strategy_scores — core_refresh 배치·scan pad 정렬."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src import strategy_scores as ss


def _synth_df(n=80, base=50_000.0):
    rng = np.random.default_rng(42)
    close = base * np.cumprod(1 + rng.normal(0.001, 0.02, n))
    return pd.DataFrame({
        "open": np.concatenate([[close[0]], close[:-1]]),
        "high": close * 1.01, "low": close * 0.99, "close": close,
        "volume": np.full(n, 5_000_000),
    })


def test_pad_score_uses_ranking_return():
    scores = {"A": {"ranking": [{"return_pct": 0.12}]},
              "B": {"ranking": [{"return_pct": 0.05}]}}
    assert ss.pad_score(scores, "A") > ss.pad_score(scores, "B")
    assert ss.pad_score(scores, "MISSING") == float("-inf")


def test_load_strategy_scores_missing(tmp_path):
    assert ss.load_strategy_scores(tmp_path / "nope.json") == {}


def test_refresh_and_load_roundtrip(tmp_path, monkeypatch):
    out = tmp_path / "strategy_scores.json"
    uni = {"KR": [{"symbol": "005930", "market": "KR", "name": "삼성"}]}
    monkeypatch.setattr(ss, "OUT", out)

    monkeypatch.setattr(
        ss, "recommend_strategy",
        lambda df: {"best": "ma_crossover",
                    "ranking": [{"strategy": "ma_crossover", "return_pct": 0.1}]})

    got = ss.refresh_strategy_scores(
        uni, lambda s, m: _synth_df().to_dict("records"), path=out)
    assert "005930" in got
    assert got["005930"]["best"] == "ma_crossover"
    loaded = ss.load_strategy_scores(out)
    assert loaded["005930"]["best"] == "ma_crossover"


def test_refresh_accepts_dataframe_fetch(tmp_path, monkeypatch):
    out = tmp_path / "strategy_scores.json"
    uni = {"KR": [{"symbol": "005930", "market": "KR"}]}
    monkeypatch.setattr(
        ss, "recommend_strategy",
        lambda df: {"best": "ma_crossover",
                    "ranking": [{"strategy": "ma_crossover", "return_pct": 0.1}]})
    got = ss.refresh_strategy_scores(uni, lambda s, m: _synth_df(), path=out)
    assert got["005930"]["best"] == "ma_crossover"


def test_refresh_dry_skips(tmp_path):
    out = tmp_path / "strategy_scores.json"
    uni = {"KR": [{"symbol": "005930", "market": "KR"}]}
    got = ss.refresh_strategy_scores(uni, lambda s, m: None, dry=True, path=out)
    assert got == {}
    assert not out.exists()


def test_refresh_only_processes_passed_markets(tmp_path, monkeypatch):
    out = tmp_path / "strategy_scores.json"
    uni = {
        "KR": [{"symbol": "005930", "market": "KR"}],
        "US": [{"symbol": "AAPL", "market": "US"}],
    }
    monkeypatch.setattr(
        ss, "recommend_strategy",
        lambda df: {"best": "ma_crossover",
                    "ranking": [{"strategy": "ma_crossover", "return_pct": 0.1}]})
    seen: list[str] = []

    def fetch(sym, mkt):
        seen.append(sym)
        return _synth_df()

    ss.refresh_strategy_scores({"KR": uni["KR"]}, fetch, path=out)
    assert seen == ["005930"]
    ss.refresh_strategy_scores({"US": uni["US"]}, fetch, path=out)
    assert seen == ["005930", "AAPL"]
    loaded = ss.load_strategy_scores(out)
    assert "005930" in loaded and "AAPL" in loaded


def test_refresh_prunes_symbols_outside_universe(tmp_path, monkeypatch):
    out = tmp_path / "strategy_scores.json"
    out.write_text(
        '{"asof": 1, "symbols": {"005930": {"best": "x", "ranking": []}, '
        '"OLD": {"best": "y", "ranking": []}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ss, "recommend_strategy",
        lambda df: {"best": "ma_crossover",
                    "ranking": [{"strategy": "ma_crossover", "return_pct": 0.1}]})
    uni = {"KR": [{"symbol": "005930", "market": "KR"}]}
    ss.refresh_strategy_scores(
        uni, lambda s, m: _synth_df(), path=out, prune_universe=uni)
    loaded = ss.load_strategy_scores(out)
    assert "005930" in loaded and "OLD" not in loaded

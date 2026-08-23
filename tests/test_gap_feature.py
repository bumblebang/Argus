"""시가 갭 피처 — 후보 피처·Athena 기술요약 공용 계산과 미장 배경 프롬프트 회귀.

갭은 "추격 금지" 하드룰이 아니라 간밤 미장 흐름이 시가에 얼마나 반영됐는지 보는
로그형 데이터다. 계산이 조용히 빠지거나(필드 소실) 프롬프트 절이 지워지면 뇌는
그 배경을 아예 못 보므로 둘 다 고정한다.
"""
import json

import numpy as np
import pandas as pd

from src.agents.athena import build_research_context, technical_summary
from src.agents.features import _gap_fields, assemble


def _candles(bars):
    """bars: [(open, close), ...] — 일봉 리스트."""
    return [{"time": i, "open": o, "high": max(o, c) * 1.01,
             "low": min(o, c) * 0.99, "close": c, "volume": 1000}
            for i, (o, c) in enumerate(bars)]


def _assemble_one(bars):
    items = [{"symbol": "005930", "name": "삼성전자", "market": "KR"}]
    cands, _ = assemble(items, {}, lambda s, m: _candles(bars))
    return cands[0]


# ── 갭 계산 ────────────────────────────────────────────────────────
def test_gap_up_three_percent():
    feat = _assemble_one([(100.0, 100.0)] * 30 + [(103.0, 104.0)])
    assert feat["gap_pct"] == 3.0
    assert feat["open"] == 103.0 and feat["prev_close"] == 100.0


def test_gap_down_three_percent():
    feat = _assemble_one([(100.0, 100.0)] * 30 + [(97.0, 96.0)])
    assert feat["gap_pct"] == -3.0
    assert feat["open"] == 97.0 and feat["prev_close"] == 100.0


def test_gap_absent_with_single_bar():
    feat = _assemble_one([(100.0, 101.0)])
    assert feat.get("gap_pct") is None            # 봉 1개 → 전일 종가가 없다
    assert "prev_close" not in feat


def test_gap_fields_guard_missing_and_zero():
    assert _gap_fields(None) == {}
    assert _gap_fields(pd.DataFrame(columns=["open", "close"])) == {}
    assert _gap_fields(pd.DataFrame({"close": [100.0, 101.0]})) == {}   # open 결측
    assert _gap_fields(pd.DataFrame({"open": [100.0, 103.0],
                                     "close": [0.0, 104.0]})) == {}    # 전일종가 0
    assert _gap_fields(pd.DataFrame({"open": [100.0, np.nan],
                                     "close": [100.0, 104.0]})) == {}  # 시가 NaN


def test_gap_is_json_serializable():
    feat = _assemble_one([(100.0, 100.0)] * 30 + [(103.0, 104.0)])
    assert '"gap_pct": 3.0' in json.dumps(feat, ensure_ascii=False)


# ── Athena 쪽 재사용 ───────────────────────────────────────────────
def _df(n=120, base=100.0):
    rng = np.random.default_rng(3)
    close = base * np.cumprod(1 + rng.normal(0.0005, 0.015, n))
    open_ = close * 1.005                          # 매일 소폭 갭업
    return pd.DataFrame({"time": pd.date_range("2024-01-01", periods=n),
                         "open": open_, "high": close * 1.02, "low": close * 0.98,
                         "close": close, "volume": rng.integers(1e5, 1e6, n)})


def test_technical_summary_has_gap():
    t = technical_summary(_df())
    assert {"gap_pct", "open", "prev_close"} <= set(t)
    df = _df()
    expect = round((float(df["open"].iloc[-1]) / float(df["close"].iloc[-2]) - 1) * 100, 2)
    assert t["gap_pct"] == expect


def test_research_context_carries_markets():
    ms = {"markets": {"SP500": {"last": 5500.0, "chg_1d": 0.012},
                      "NASDAQ": {"last": 18000.0, "chg_1d": 0.019},
                      "USDKRW": {"last": 1380.5, "chg_1d": -0.003}},
          "sentiment": {"vix": 17.1}}
    ctx = build_research_context("005930", "삼성전자", "KR",
                                 history_df=_df(), market_state=ms)
    assert ctx["markets"]["SP500"]["chg_1d"] == 0.012
    assert ctx["technical"]["gap_pct"] is not None


# ── 프롬프트 회귀: 미장 배경 절이 살아 있는지 ──────────────────────
def test_decision_prompt_has_overnight_us_background():
    from src.agents.decision_agent import SYSTEM as DEC

    assert "미장 → 한국장 배경" in DEC
    assert "gap_pct" in DEC and "prev_close" in DEC
    assert "SP500" in DEC and "USDKRW" in DEC
    assert "기계적으로 '미장이 올랐으니 BUY' 로 가지 마라" in DEC


def test_athena_prompt_has_overnight_us_background():
    from src.agents.athena import ATHENA_SYSTEM as ATH

    assert "미장 → 한국장 배경" in ATH
    assert "technical.gap_pct" in ATH
    assert "기계적 추종은 근거가 아니다" in ATH

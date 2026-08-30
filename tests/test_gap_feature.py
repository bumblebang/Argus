"""시가 갭 피처 — 후보 피처·Athena 기술요약 공용 계산과 미장 배경 프롬프트 회귀.

갭은 "추격 금지" 하드룰이 아니라 간밤 미장 흐름이 시가에 얼마나 반영됐는지 보는
로그형 데이터다. 계산이 조용히 빠지거나(필드 소실) 프롬프트 절이 지워지면 뇌는
그 배경을 아예 못 보므로 둘 다 고정한다.
"""
import json
from zoneinfo import ZoneInfo

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


def test_intraday_ret_down_five_percent():
    """당일 시가 100 → 종가 95 → intraday -5%."""
    feat = _assemble_one([(100.0, 100.0)] * 30 + [(100.0, 95.0)])
    assert feat["intraday_ret_pct"] == -5.0
    assert feat["intraday_open"] == 100.0


def test_live_price_overrides_stale_close():
    """Yahoo 캐시 종가(100) 대신 live_prices(90) → intraday·price_lookup 반영."""
    from src.agents.features import assemble
    from src.market_hours import trading_date

    today = trading_date("KR")
    tz = ZoneInfo("Asia/Seoul")
    rows = []
    for i in range(31):
        d = (pd.Timestamp(today, tz=tz) - pd.Timedelta(days=30 - i)).date().isoformat()
        rows.append({"time": pd.Timestamp(d, tz=tz), "open": 100.0, "high": 101.0,
                     "low": 99.0, "close": 100.0, "volume": 1000})
    items = [{"symbol": "005930", "name": "삼성전자", "market": "KR"}]
    cands, px = assemble(items, {}, lambda s, m: rows,
                         live_prices={"005930": 90.0})
    assert cands[0]["intraday_ret_pct"] == -10.0
    assert cands[0]["price"] == 90.0
    assert px["005930"] == 90.0


def test_assemble_refreshes_stale_daily_before_patch(monkeypatch):
    """20h TTL 어제 봉 캐시 — 패치 전 fresh 일봉으로 당일 시가 확보."""
    from src.agents.features import assemble
    from src.market_hours import trading_date

    today = trading_date("KR")
    tz = ZoneInfo("Asia/Seoul")
    yday = (pd.Timestamp(today, tz=tz) - pd.Timedelta(days=1)).date().isoformat()

    stale_rows = [{"time": pd.Timestamp(yday, tz=tz), "open": 100.0, "high": 100.0,
                   "low": 90.0, "close": 95.0, "volume": 1000}]
    fresh_rows = stale_rows + [{"time": pd.Timestamp(today, tz=tz), "open": 98.0,
                                "high": 99.0, "low": 97.0, "close": 97.5, "volume": 500}]

    monkeypatch.setattr(
        "src.agents.wiring.history_candles_1y",
        lambda sym, mkt, fresh=False: fresh_rows if fresh else stale_rows,
    )
    items = [{"symbol": "005930", "name": "삼성", "market": "KR"}]
    cands, _ = assemble(items, {}, lambda s, m: stale_rows, live_prices={"005930": 92.0})
    assert cands[0]["intraday_ret_pct"] == round((92.0 / 98.0 - 1) * 100, 2)
    gs = cands[0].get("gap_shape") or {}
    assert gs.get("gap_pct") == round((98.0 / 95.0 - 1) * 100, 2)


def test_filter_gap_rebound_keeps_held():
    from src.agents.features import filter_gap_rebound_candidates
    cands = [
        {"symbol": "A", "intraday_ret_pct": -2.0},
        {"symbol": "B", "intraday_ret_pct": -6.0},
        {"symbol": "C", "intraday_ret_pct": -7.0},
    ]
    out = filter_gap_rebound_candidates(cands, held=["A"])
    syms = {c["symbol"] for c in out}
    assert syms == {"A", "B", "C"}


def test_filter_gap_rebound_fluctuation_fallback():
    """캔들 실패로 intraday 없을 때 풀 fluctuation 으로 -5% 컷."""
    from src.agents.features import filter_gap_rebound_candidates
    cands = [
        {"symbol": "A", "fluctuation": -6.0},
        {"symbol": "B", "fluctuation": -3.0},
    ]
    out = filter_gap_rebound_candidates(cands)
    assert [c["symbol"] for c in out] == ["A"]


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
    assert "intraday_ret_pct" in DEC
    assert "gap_rebound_scan" in DEC
    assert "horizon=close_scan" in DEC
    assert "SP500" in DEC and "USDKRW" in DEC
    assert "기계적으로 '미장이 올랐으니 BUY' 로 가지 마라" in DEC
    assert "gap_shape" in DEC


def test_gap_shape_intraday_after_open_is_open_to_price():
    """intraday_after_open_pct = px/open-1 (gap_pct 이중 차감 금지)."""
    from src.gap_rebound_features import gap_shape_fields

    df = pd.DataFrame({
        "open": [100.0, 97.0],
        "high": [100.0, 97.2],
        "low": [99.0, 96.0],
        "close": [100.0, 96.1],
        "volume": [1000, 1000],
    })
    gs = gap_shape_fields(df)["gap_shape"]
    assert gs["gap_pct"] == -3.0
    assert gs["intraday_after_open_pct"] == round((96.1 / 97.0 - 1) * 100, 2)


def test_assemble_gap_shape_flags():
    raw = [{"time": i, "open": 100.0, "high": 101.0, "low": 99.5,
            "close": 100.0, "volume": 1000} for i in range(29)]
    raw.append({"time": 29, "open": 97.0, "high": 97.2, "low": 96.0,
                "close": 96.1, "volume": 1000})
    items = [{"symbol": "005930", "name": "삼성", "market": "KR"}]
    cands, _ = assemble(items, {}, lambda s, m: raw)
    gs = cands[0].get("gap_shape") or {}
    assert gs.get("gap_down_deep") is True
    assert gs.get("close_near_day_low") is True


def test_gap_shape_intraday_rebound_not_near_low():
    # 장중 급락 후 종가 반등 — close_loc 높음
    raw = [{"time": i, "open": 100.0, "high": 100.0, "low": 90.0,
            "close": 95.0, "volume": 1000} for i in range(30)]
    raw[-1] = {"time": 30, "open": 100.0, "high": 100.0, "low": 90.0,
               "close": 98.0, "volume": 1000}
    items = [{"symbol": "005930", "name": "삼성", "market": "KR"}]
    cands, _ = assemble(items, {}, lambda s, m: raw)
    gs = cands[0].get("gap_shape") or {}
    assert gs.get("close_near_day_low") is False


def test_athena_prompt_has_overnight_us_background():
    from src.agents.athena import ATHENA_SYSTEM as ATH

    assert "미장 → 한국장 배경" in ATH
    assert "technical.gap_pct" in ATH
    assert "기계적 추종은 근거가 아니다" in ATH

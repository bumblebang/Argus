"""gap_rebound_backtest — 버킷·이벤트 순수 로직."""
from __future__ import annotations

import pandas as pd

from src.gap_rebound_backtest import (
    assign_bucket,
    bucket_edges,
    build_prior,
    events_for_day,
    summarize_by_bucket,
    summarize_conditional,
)


def _day_frame() -> pd.DataFrame:
    rows = []
    # 거래대금 상위권 + 다양한 intraday
    specs = [
        ("A", 100, 100, 95, 94, 1_000_000),   # intraday -5.26%, daily -6%
        ("B", 100, 100, 92, 90, 900_000),     # -8%, daily -10%
        ("C", 100, 100, 88, 85, 800_000),     # -12%
        ("D", 100, 100, 97, 96, 50_000),      # -3% — 필터 아웃
        ("E", 100, 100, 93, 92, 700_000),     # -7%
    ]
    for sym, o, h, l, c, vol in specs:
        rows.append({
            "symbol": sym, "open": o, "high": h, "low": l, "close": c,
            "volume": vol, "prev_close": 100.0,
            "next_open": c + 2, "next_close": c + 1,
            "intraday_ret_pct": (c / o - 1) * 100,
            "daily_ret_pct": (c / 100 - 1) * 100,
            "trading_value": c * vol,
            "date": pd.Timestamp("2026-01-02"),
        })
    return pd.DataFrame(rows)


def test_events_respect_floor_and_decline_pool():
    ev = events_for_day(_day_frame(), liq_top=10, decline_top=3,
                        intraday_floor=-5.0, eligible={"A", "B", "C", "D", "E"})
    assert set(ev["symbol"]) == {"B", "C", "E"}   # 하락 top3 중 floor 통과
    assert all(ev["intraday_ret_pct"] <= -5)


def test_bucket_assignment():
    edges = bucket_edges(floor=-5.0, step=1.0, tail=-15.0)
    assert assign_bucket(-5.5, edges) == "(-6%, -5%]"
    assert assign_bucket(-5.0, edges) == "(-6%, -5%]"
    assert assign_bucket(-16.0, edges) == "<=-15%"


def test_assign_bucket_exact_boundaries():
    edges = bucket_edges(floor=-5.0, step=1.0, tail=-15.0)
    assert assign_bucket(-5.5, edges) == "(-6%, -5%]"
    assert assign_bucket(-8.0, edges) == "(-9%, -8%]"
    assert assign_bucket(-12.0, edges) == "(-13%, -12%]"
    assert assign_bucket(-16.0, edges) == "<=-15%"


def test_summarize_by_bucket_counts():
    ev = events_for_day(_day_frame(), liq_top=10, decline_top=5,
                        intraday_floor=-6.0,
                        eligible={"A", "B", "C", "D", "E"})
    assert len(ev) == 4   # D(-3%%) 제외
    summ = summarize_by_bucket(ev, floor=-5.0, step=1.0, tail=-15.0)
    assert not summ.empty
    assert summ["n"].sum() == len(ev)
    assert "win_open_pct" in summ.columns


def test_summarize_conditional_on_synthetic():
    ev = events_for_day(_day_frame(), liq_top=10, decline_top=5,
                        intraday_floor=-20.0,
                        eligible={"A", "B", "C", "D", "E"})
    ev["gap_pct"] = [-1, -3, -4, 0, -2]
    ev["close_loc"] = [0.1, 0.2, 0.1, 0.5, 0.3]
    ev["vol_ratio_20d"] = [1.0, 2.5, 1.2, 1.0, 1.8]
    cond = summarize_conditional(ev, min_n=1)
    assert not cond.empty
    assert "gap_down_deep" in set(cond["id"])
    prior = build_prior(ev, overall={"n_events": len(ev)})
    assert prior.get("winner_loser")
    assert prior.get("conditions") == []   # n<200 이라 prior 조건 제외

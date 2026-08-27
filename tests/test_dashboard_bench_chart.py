# -*- coding: utf-8 -*-
"""메인 Argus vs 코스피·S&P 수익률 차트 단위 테스트."""
from datetime import datetime, timedelta

from scripts.dashboard import (
    _bench_chart_html, _equity_vs_kospi, _kr_journal_fills, _ret_chart_svg, _parse_jts,
)


def test_parse_jts_aware():
    dt = _parse_jts("2026-08-07T01:03:18.775107+00:00")
    assert dt is not None
    assert dt.tzinfo is None


def test_kr_journal_fills_backfills_missing_buy():
    paper = {
        "start_cash": {"KR": 1_000_000},
        "positions": {"005930": {"qty": 1.0, "avg_price": 267500.0}},
        "journal": [{
            "ts": "2026-08-07T01:03:18+00:00", "symbol": "257720", "market": "KR",
            "side": "BUY", "qty": 3.0, "price": 39000.0, "fee": 0,
        }],
    }
    store = [{"symbol": "005930", "market": "KR", "state": "open",
              "qty": 1.0, "avg_price": 267500.0,
              "opened_at": datetime(2026, 8, 8).timestamp()}]
    fills = _kr_journal_fills(paper, store)
    syms = {(f["side"], f["symbol"]) for f in fills}
    assert ("BUY", "257720") in syms
    assert ("BUY", "005930") in syms


def test_equity_vs_kospi_builds_series(monkeypatch):
    # 고정 일봉: 벤치·종목·S&P(SPY)
    base = datetime(2026, 8, 1)
    days = [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(10)]

    def fake_closes(symbol, market="KR", *, refresh=False, max_age_hours=None):
        if symbol == "^KS11":
            return {d: 3000 + i * 10 for i, d in enumerate(days)}
        if symbol == "SPY":
            return {d: 500 + i * 2 for i, d in enumerate(days)}
        return {d: 100 + i for i, d in enumerate(days)}

    monkeypatch.setattr("scripts.dashboard._hist_closes", fake_closes)
    monkeypatch.setattr("scripts.dashboard._chart_cache", {})

    paper = {
        "start_cash": {"KR": 1_000_000},
        "cash": {"KR": 900_000},
        "positions": {"AAA": {"qty": 10.0, "avg_price": 100.0}},
        "journal": [{
            "ts": "2026-08-03T01:00:00+00:00", "symbol": "AAA", "market": "KR",
            "side": "BUY", "qty": 10.0, "price": 100.0, "fee": 0,
        }],
    }
    snap = {"ts": 1.0, "cash": {"KR": 900_000}, "market_value": {"KR": 1200.0}}
    out = _equity_vs_kospi(paper, snap, store_rows=[], latest_px={"AAA": 120})
    assert out is not None
    assert len(out["dates"]) >= 2
    assert len(out["port"]) == len(out["dates"])
    assert "alpha_now" in out
    assert out.get("bench2_name") == "S&P500"
    assert len(out["bench2"]) == len(out["dates"])
    svg = _ret_chart_svg(out)
    assert "polyline" in svg
    assert "bc-overlay" in svg
    assert "bc-dot-bench2" in svg
    assert "max-width:none" in svg
    assert "width:100%" in svg
    assert "Argus" not in svg  # svg itself is geometry only


def test_equity_vs_kospi_refreshes_stale_spy(monkeypatch):
    """SPY 캐시가 since 이전이면 refresh 해서 평평한 0% 선을 막는다."""
    base = datetime(2026, 8, 1)
    days = [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(10)]
    stale = {f"2026-07-{10+i:02d}": 700.0 + i for i in range(5)}
    fresh = {d: 750 + i * 3 for i, d in enumerate(days)}
    calls = {"spy_refresh": 0}

    def fake_closes(symbol, market="KR", *, refresh=False, max_age_hours=None):
        if symbol == "^KS11":
            return {d: 3000 + i * 10 for i, d in enumerate(days)}
        if symbol == "SPY":
            if refresh:
                calls["spy_refresh"] += 1
                return fresh
            return stale
        return {d: 100 + i for i, d in enumerate(days)}

    monkeypatch.setattr("scripts.dashboard._hist_closes", fake_closes)
    monkeypatch.setattr("scripts.dashboard._chart_cache", {})
    paper = {
        "start_cash": {"KR": 1_000_000},
        "cash": {"KR": 900_000},
        "positions": {"AAA": {"qty": 10.0, "avg_price": 100.0}},
        "journal": [{
            "ts": "2026-08-03T01:00:00+00:00", "symbol": "AAA", "market": "KR",
            "side": "BUY", "qty": 10.0, "price": 100.0, "fee": 0,
        }],
    }
    out = _equity_vs_kospi(paper, {"ts": 2.0}, store_rows=[])
    assert out is not None
    assert calls["spy_refresh"] >= 1
    assert len(set(round(x, 4) for x in out["bench2"])) > 1
    assert abs(out["bench2_now"]) > 1e-6

    series = {
        "dates": ["2026-08-01", "2026-08-02", "2026-08-03"],
        "port": [0.0, 1.2, 2.5],
        "bench": [0.0, 0.8, 1.0],
        "bench2": [0.0, 0.5, 0.9],
        "port_now": 2.5, "bench_now": 1.0, "bench2_now": 0.9,
        "alpha_now": 1.5, "alpha2_now": 1.6,
        "since": "2026-08-01", "bench_name": "코스피", "bench2_name": "S&P500",
    }
    html = _bench_chart_html({"bench_chart": series})
    assert "bc-wrap" in html
    assert "bc-tip" in html
    assert "bc-data" in html
    assert "2026-08-02" in html
    assert "S&P500" in html
    assert "코스피" in html

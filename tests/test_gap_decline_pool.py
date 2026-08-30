"""gap_decline_pool — merge·refresh·신선도 순수 로직."""
from __future__ import annotations

from src.gap_decline_pool import (
    fresh_gap_symbols,
    gap_pool_date,
    is_gap_pool_fresh,
    items_for_gap_scan,
    load_gap_decline_pool,
    merge_all_pools,
    refresh_gap_decline_pool,
)


def test_items_for_gap_scan_gap_plus_held():
    import src.gap_decline_pool as gdp
    orig = gdp.trading_date
    gdp.trading_date = lambda m, ts=None: "2026-08-29"
    try:
        universe = [
            {"symbol": "005930", "name": "삼성", "market": "KR", "pool": "swing"},
            {"symbol": "000660", "name": "하이닉스", "market": "KR", "pool": "day"},
        ]
        gap = {
            "_meta": {"pool_date": "2026-08-29"},
            "KR": [
                {"symbol": "085620", "name": "미래에셋생명", "pool": "gap_decline",
                 "pool_date": "2026-08-29", "fluctuation": -10.0},
            ],
        }
        out = items_for_gap_scan("gap_rebound_scan", universe_items=universe,
                                 held=["005930"], gap_data=gap)
        syms = [it["symbol"] for it in out]
        assert syms == ["085620", "005930"]
        assert "000660" not in syms
        assert out[1].get("force_include")
    finally:
        gdp.trading_date = orig


def test_items_for_gap_scan_stale_pool_held_only():
    import src.gap_decline_pool as gdp
    orig = gdp.trading_date
    gdp.trading_date = lambda m, ts=None: "2026-08-29"
    try:
        gap = {"_meta": {"pool_date": "2026-08-28"},
               "KR": [{"symbol": "085620", "pool_date": "2026-08-28"}]}
        out = items_for_gap_scan("gap_rebound_scan", universe_items=[],
                                 held=["005930"], gap_data=gap)
        assert [it["symbol"] for it in out] == ["005930"]
    finally:
        gdp.trading_date = orig


def test_merge_all_pools_adds_gap_only():
    swing = {"KR": [{"symbol": "005930", "name": "삼성", "pool": "swing"}]}
    day = {"KR": [{"symbol": "000660", "name": "하이닉스", "pool": "day"}]}
    gap = {
        "_meta": {"pool_date": "2026-08-29"},
        "KR": [{"symbol": "085620", "name": "미래에셋생명", "pool": "gap_decline",
                "pool_date": "2026-08-29", "fluctuation": -10.0}],
    }
    import src.gap_decline_pool as gdp
    orig = gdp.trading_date
    gdp.trading_date = lambda m, ts=None: "2026-08-29"
    try:
        out = merge_all_pools(swing, day, gap, now_fn=lambda: 1.0)
    finally:
        gdp.trading_date = orig
    syms = [it["symbol"] for it in out["KR"]]
    assert syms == ["005930", "000660", "085620"]
    assert {it["symbol"]: it["pool"] for it in out["KR"]}["085620"] == "gap_decline"


def test_merge_skips_stale_gap_pool():
    swing = {"KR": [{"symbol": "005930", "name": "삼성", "pool": "swing"}]}
    gap = {
        "_meta": {"pool_date": "2026-08-28"},
        "KR": [{"symbol": "085620", "name": "X", "pool": "gap_decline",
                "pool_date": "2026-08-28"}],
    }
    import src.gap_decline_pool as gdp
    orig = gdp.trading_date
    gdp.trading_date = lambda m, ts=None: "2026-08-29"
    try:
        out = merge_all_pools(swing, {}, gap, now_fn=lambda: 1.0)
    finally:
        gdp.trading_date = orig
    assert [it["symbol"] for it in out["KR"]] == ["005930"]


def test_is_gap_pool_fresh_requires_pool_date():
    import src.gap_decline_pool as gdp
    orig = gdp.trading_date
    gdp.trading_date = lambda m, ts=None: "2026-08-29"
    try:
        assert not is_gap_pool_fresh({}, "KR")
        data = {"_meta": {"pool_date": "2026-08-29"},
                "KR": [{"symbol": "085620", "pool_date": "2026-08-29"}]}
        assert is_gap_pool_fresh(data, "KR")
        assert fresh_gap_symbols(data, "KR") == {"085620": "2026-08-29"}
        assert not is_gap_pool_fresh({"_meta": {"pool_date": "2026-08-28"},
                                      "KR": []}, "KR")
        assert fresh_gap_symbols({"_meta": {"pool_date": "2026-08-28"},
                                  "KR": [{"symbol": "X"}]}, "KR") == {}
    finally:
        gdp.trading_date = orig


def test_refresh_sorts_by_fluctuation(tmp_path, monkeypatch):
    rows = [
        {"symbol": "005930", "name": "A", "trading_value": 100, "fluctuation": -2},
        {"symbol": "000660", "name": "B", "trading_value": 200, "fluctuation": -8},
        {"symbol": "035720", "name": "C", "trading_value": 150, "fluctuation": -5},
        {"symbol": "051910", "name": "D", "trading_value": 140, "fluctuation": -4},
        {"symbol": "006400", "name": "E", "trading_value": 130, "fluctuation": -3},
        {"symbol": "028260", "name": "F", "trading_value": 120, "fluctuation": -2.5},
        {"symbol": "105560", "name": "G", "trading_value": 110, "fluctuation": -2.2},
        {"symbol": "055550", "name": "H", "trading_value": 105, "fluctuation": -2.1},
        {"symbol": "012330", "name": "I", "trading_value": 102, "fluctuation": -2.0},
        {"symbol": "034020", "name": "J", "trading_value": 101, "fluctuation": -1.9},
    ]

    def fetch_top(market, count=100, pool=100, **_kw):
        return list(rows)

    monkeypatch.setattr("src.gap_decline_pool.trading_date", lambda m, ts=None: "2026-08-29")
    cfg = {"gap_decline_pool": {"enabled": True, "liquidity_top": 10, "decline_top": 10}}
    out = refresh_gap_decline_pool(fetch_top, cfg, "KR", path=tmp_path / "gap.yaml")
    syms = [it["symbol"] for it in out["KR"]]
    assert syms[0] == "000660"
    assert len(syms) == 10
    assert all(it["pool_date"] == "2026-08-29" for it in out["KR"])
    loaded = load_gap_decline_pool(tmp_path / "gap.yaml")
    assert loaded["KR"][0]["pool"] == "gap_decline"
    assert loaded["KR"][0]["source"] == "gap_rebound"
    assert loaded["KR"][0]["pool_date"] == "2026-08-29"
    assert gap_pool_date(loaded, "KR") == "2026-08-29"
    assert loaded["_meta"]["pool_date"] == "2026-08-29"


def test_refresh_rejects_insufficient_live(tmp_path, monkeypatch):
    def fetch_top(market, count=100, pool=100, **_kw):
        return [{"symbol": "005930", "name": "A", "trading_value": 100, "fluctuation": -2}]

    monkeypatch.setattr("src.gap_decline_pool.trading_date", lambda m, ts=None: "2026-08-29")
    cfg = {"gap_decline_pool": {"enabled": True, "liquidity_top": 10, "decline_top": 10}}
    assert refresh_gap_decline_pool(fetch_top, cfg, "KR", path=tmp_path / "gap.yaml") is None

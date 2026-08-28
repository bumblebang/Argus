"""gap_decline_pool — merge·refresh 순수 로직."""
from src.gap_decline_pool import merge_all_pools, refresh_gap_decline_pool, load_gap_decline_pool


def test_merge_all_pools_adds_gap_only():
    swing = {"KR": [{"symbol": "005930", "name": "삼성", "pool": "swing"}]}
    day = {"KR": [{"symbol": "000660", "name": "하이닉스", "pool": "day"}]}
    gap = {"KR": [{"symbol": "085620", "name": "미래에셋생명", "pool": "gap_decline",
                   "fluctuation": -10.0}]}
    out = merge_all_pools(swing, day, gap)
    syms = [it["symbol"] for it in out["KR"]]
    assert syms == ["005930", "000660", "085620"]
    assert {it["symbol"]: it["pool"] for it in out["KR"]}["085620"] == "gap_decline"


def test_refresh_sorts_by_fluctuation(tmp_path):
    def fetch_top(market, count=100, pool=100, **_kw):
        return [
            {"symbol": "005930", "name": "A", "trading_value": 100, "fluctuation": -2},
            {"symbol": "000660", "name": "B", "trading_value": 200, "fluctuation": -8},
            {"symbol": "035720", "name": "C", "trading_value": 150, "fluctuation": -5},
        ]

    cfg = {"gap_decline_pool": {"enabled": True, "liquidity_top": 10, "decline_top": 3}}
    out = refresh_gap_decline_pool(fetch_top, cfg, "KR", path=tmp_path / "gap.yaml")
    assert [it["symbol"] for it in out["KR"]] == ["000660", "035720", "005930"]
    loaded = load_gap_decline_pool(tmp_path / "gap.yaml")
    assert loaded["KR"][0]["pool"] == "gap_decline"
    assert loaded["KR"][0]["source"] == "gap_rebound"
    assert loaded["KR"][0]["decline_pct"] == loaded["KR"][0]["fluctuation"]

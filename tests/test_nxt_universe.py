"""NXT 유니버스 split — 갭반등 15:20 vs 19:50."""
from src.datasources.nxt_universe import filter_items_for_gap_scan, nxt_supported_for


def test_nxt_split_1520_excludes_nxt_true():
    items = [{"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"}]
    nxt = {"A": False, "B": True, "C": None}
    out = filter_items_for_gap_scan(items, "gap_rebound_scan", nxt)
    assert [i["symbol"] for i in out] == ["A", "C"]


def test_nxt_split_1950_only_nxt_true():
    items = [{"symbol": "A"}, {"symbol": "B"}, {"symbol": "C"}]
    nxt = {"A": False, "B": True, "C": None}
    out = filter_items_for_gap_scan(items, "nxt_gap_scan", nxt)
    assert [i["symbol"] for i in out] == ["B"]


def test_nxt_supported_from_cache_dict():
    cache = {"005930": {"info": {"nxtSupported": True}}}
    assert nxt_supported_for("005930", cache) is True
    assert nxt_supported_for("999999", cache) is None

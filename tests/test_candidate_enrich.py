"""candidate_enrich — 결측 fundamentals 패치."""
from src.candidate_enrich import enrich_candidates, patch_fundamentals


def test_patch_fundamentals():
    cands = [{"symbol": "085620", "market": "KR"}]
    n = patch_fundamentals(cands, {"085620": {"net_margin": 0.1}})
    assert n == 1
    assert cands[0]["fundamentals"]["net_margin"] == 0.1


def test_enrich_from_market_state_without_fetch(monkeypatch):
    def no_fetch(*a, **k):
        raise AssertionError("should not fetch")

    monkeypatch.setattr("src.candidate_enrich.fetch_fundamentals_kr", no_fetch)
    ms = {"fundamentals": {"005930": {"net_margin": 0.13}}}
    cands = [{"symbol": "005930", "market": "KR"}]
    stats = enrich_candidates(cands, ms, enrich_flows=False)
    assert stats["fundamentals"] == 1
    assert cands[0]["fundamentals"]["net_margin"] == 0.13


def test_gap_scan_fetches_missing(monkeypatch):
    fetched = []

    def fake_fetch(symbols, **kw):
        fetched.extend(symbols)
        return {s: {"net_margin": -0.1} for s in symbols}

    monkeypatch.setattr("src.candidate_enrich.fetch_fundamentals_kr", fake_fetch)
    monkeypatch.setattr(
        "src.agents.serve_policy.fetch_ondemand_flows",
        lambda syms, **kw: {},
    )
    cands = [
        {"symbol": "085620", "market": "KR", "pool": "gap_decline"},
        {"symbol": "000660", "market": "KR", "pool": "swing"},
    ]
    stats = enrich_candidates(cands, {}, gap_scan=True, enrich_flows=False)
    assert stats["fundamentals"] == 2
    assert set(fetched) == {"085620", "000660"}

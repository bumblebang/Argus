"""US Finnhub 품질 miss-only 백필 — 네트워크 무접촉."""
import json
from types import SimpleNamespace

from src.value_us_quality_backfill import (
    backfill_us_quality,
    last_backfill_ts,
    list_us_quality_misses,
    maybe_us_quality_backfill,
    needs_us_quality,
)

_NOW = 1_800_000_000.0


def test_needs_us_quality():
    assert needs_us_quality({"market": "KR"}) is False
    assert needs_us_quality({"market": "US", "fundamentals": {"pb": 1}}) is True
    assert needs_us_quality({
        "market": "US",
        "fundamentals": {"quality_source": "finnhub", "roe": 0.1},
    }) is False
    assert needs_us_quality({
        "market": "US",
        "fundamentals": {"quality_source": "finnhub"},
    }, force=True) is True


def test_list_misses_limit():
    wl = {
        "A": {"market": "US", "fundamentals": {"pb": 1}},
        "B": {"market": "KR"},
        "C": {"market": "US", "fundamentals": {"quality_source": "finnhub"}},
        "D": {"market": "US", "fundamentals": {"pe_trailing": 10}},
    }
    assert list_us_quality_misses(wl) == ["A", "D"]
    assert list_us_quality_misses(wl, limit=1) == ["A"]


def test_backfill_merges_and_skips_complete():
    wl = {
        "AAA": {"market": "US", "fundamentals": {"pb": 1.2, "pe_trailing": 8}},
        "BBB": {"market": "US",
                "fundamentals": {"quality_source": "finnhub", "roe": 0.1}},
        "CCC": {"market": "KR", "fundamentals": {"roe": 0.2}},
    }
    calls = []

    def fetch(key, sym):
        calls.append(sym)
        return {"roe": 0.15, "debt_ratio": 0.5, "quality_source": "finnhub"}

    annotated = []

    def annotate(watchlist, *, now):
        annotated.append(now)
        return watchlist

    summary = backfill_us_quality(
        "k", wl, sleep_s=0, fetch_fn=fetch, now_fn=lambda: _NOW,
        annotate_fn=annotate)
    assert calls == ["AAA"]
    assert wl["AAA"]["fundamentals"]["roe"] == 0.15
    assert wl["AAA"]["fundamentals"]["pb"] == 1.2  # 기존 유지
    assert wl["BBB"]["fundamentals"]["roe"] == 0.1
    assert summary["fetched"] == 1
    assert summary["targets"] == 1
    assert annotated == [_NOW]


def test_backfill_force_and_empty():
    wl = {"ZZ": {"market": "US", "fundamentals": {"quality_source": "finnhub"}}}

    def fetch(key, sym):
        return None

    summary = backfill_us_quality(
        "k", wl, force=True, sleep_s=0, fetch_fn=fetch,
        now_fn=lambda: _NOW, annotate=False)
    assert summary["empty"] == 1
    assert summary["fetched"] == 0


def _cfg(**vs):
    raw = {"value_scan": {"markets": ["US", "KR"], **vs}}
    return SimpleNamespace(raw=raw)


def test_maybe_게이트(tmp_path):
    wl = {"X": {"market": "US", "fundamentals": {"pb": 1}}}
    saved = []

    def save_fn(data, path):
        saved.append((dict(data), path))

    def backfill_fn(api_key, watchlist, **kw):
        watchlist["X"]["fundamentals"]["quality_source"] = "finnhub"
        return {"ts": _NOW, "finnhub_calls": 1, "fetched": 1,
                "coverage_pct": 100.0}

    out = maybe_us_quality_backfill(
        _cfg(us_quality_backfill_days=7, us_quality_backfill_max=80),
        watchlist=wl, watchlist_path=tmp_path / "wl.json",
        now=_NOW, report_path=tmp_path / "r.json", api_key="k",
        backfill_fn=backfill_fn, save_fn=save_fn)
    assert out["ran"] is True
    assert saved
    assert last_backfill_ts(tmp_path / "r.json") == _NOW

    out2 = maybe_us_quality_backfill(
        _cfg(us_quality_backfill_days=7),
        watchlist=wl, watchlist_path=tmp_path / "wl.json",
        now=_NOW + 3 * 86400, report_path=tmp_path / "r.json", api_key="k",
        backfill_fn=backfill_fn, save_fn=save_fn)
    assert out2["ran"] is False
    assert out2["why"] == "fresh"


def test_maybe_스킵_조건(tmp_path):
    off = maybe_us_quality_backfill(
        _cfg(us_quality_backfill_days=0), now=_NOW,
        report_path=tmp_path / "r.json", api_key="k")
    assert off["why"] == "disabled"
    nous = maybe_us_quality_backfill(
        SimpleNamespace(raw={"value_scan": {"markets": ["KR"],
                                            "us_quality_backfill_days": 7}}),
        now=_NOW, report_path=tmp_path / "r2.json", api_key="k")
    assert nous["why"] == "no_us"
    nokey = maybe_us_quality_backfill(
        _cfg(us_quality_backfill_days=7), now=_NOW,
        report_path=tmp_path / "r3.json", api_key="")
    assert nokey["why"] == "no_finnhub_key"

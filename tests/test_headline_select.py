"""headlines select_headlines — tier·시장·TTL."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from src.agents import context as ctx
from src.agents.context import build_context, select_headlines


def _iso_utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00")


def _rfc_gmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S GMT")


def _yyyymmdd(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y%m%d")


def _sample_pool(now_ts: float | None = None) -> list[dict]:
    """배치 순서를 흉내: RSS → Finnhub general → US sym → DART KR.

    published 는 now 기준 최근(기본 1h 전) — 고정 날짜는 TTL(24h)에 걸려 깨진다.
    """
    now = time.time() if now_ts is None else float(now_ts)
    pub = now - 3600.0
    out: list[dict] = []
    for i in range(32):
        out.append({"source": "연합뉴스", "title": f"kr-macro-{i}",
                    "published": _rfc_gmt(pub)})
    for i in range(15):
        out.append({"source": "Finnhub", "title": f"us-macro-{i}",
                    "published": _iso_utc(pub)})
    for sym in ("NVDA", "AAPL"):
        for i in range(3):
            out.append({"source": f"Finnhub/{sym}", "symbol": sym,
                        "title": f"{sym}-news-{i}",
                        "published": _iso_utc(pub)})
    for i in range(5):
        out.append({"source": "DART공시", "symbol": "196170",
                    "title": f"dart-{i}", "published": _yyyymmdd(pub)})
    return out


def test_classify_news_item():
    assert ctx.classify_news_item({"source": "연합", "title": "x"}) == "macro_kr"
    assert ctx.classify_news_item({"source": "Finnhub", "title": "x"}) == "macro_us"
    assert ctx.classify_news_item({"source": "Finnhub/NVDA", "symbol": "NVDA",
                                    "title": "x"}) == "US"
    assert ctx.classify_news_item({"source": "DART공시", "symbol": "196170",
                                    "title": "x"}) == "KR"


def test_focus_kr_excludes_us_stock_headlines():
    now = time.time()
    pool = _sample_pool(now)
    out = select_headlines(
        pool, tier="focus", limit=12, focus_macro_pad=8,
        wake={"reason": "wake_triggers", "market": "KR",
              "triggers": [{"symbol": "196170", "kind": "vol_spike"}]},
        candidates=[{"symbol": "196170", "market": "KR"}],
        now_ts=now,
    )
    assert len(out) <= 12
    assert all("NVDA" not in (h.get("title") or "") for h in out)
    assert all("AAPL" not in (h.get("title") or "") for h in out)
    assert any("kr-macro" in (h.get("title") or "") for h in out)


def test_focus_limit_zero_skips_global():
    raw = json.loads(build_context(
        {"news": _sample_pool()}, [{"symbol": "196170", "market": "KR"}],
        {}, {}, tier="focus", headline_limit=0))
    assert raw["headlines"] == []


def test_focus_no_trim_notify_by_default(monkeypatch):
    called = []
    monkeypatch.setattr(ctx, "_notify_headline_trim", lambda *a: called.append(a))
    select_headlines(_sample_pool(), tier="focus", limit=12, notify_trim=False)
    assert called == []


def test_scan_ttl_drops_stale(monkeypatch):
    called = []
    monkeypatch.setattr(ctx, "_notify_headline_trim", lambda *a: called.append(a))
    now = time.time()
    fresh = [{"source": "t", "title": "new", "published": _iso_utc(now - 3600)}]
    stale = [{"source": "t", "title": "old", "published": "2020-01-01T12:00:00+00:00"}]
    out = select_headlines(fresh + stale, tier="scan", limit=200, ttl_hours=24,
                           now_ts=now, notify_trim=False)
    titles = [h["title"] for h in out]
    assert "new" in titles and "old" not in titles


def test_infer_wake_market_from_triggers():
    assert ctx.infer_wake_market(
        {"triggers": [{"symbol": "196170"}]},
        [{"symbol": "196170", "market": "KR"}]) == "KR"
    assert ctx.infer_wake_market(
        {"triggers": [{"symbol": "NVDA"}]},
        [{"symbol": "NVDA", "market": "US"}]) == "US"


def test_validation_rule8_focus_headlines_wording():
    from src.agents.validation_agent import SYSTEM
    assert "candidates[].news" in SYSTEM
    assert "macro 위주" in SYSTEM


def test_athena_live_news_fallback(monkeypatch):
    from src.agents.athena import build_research_context

    monkeypatch.setattr(
        "src.agents.athena.fetch_symbol_news",
        lambda sym, mkt, per=5: [{"source": "naver", "title": "live"}])
    ctx_out = build_research_context(
        "196170", "알테오", "KR", history_df=None, market_state={"news": []},
        live_news=True)
    assert ctx_out["news"] == [{"source": "naver", "title": "live"}]

    ctx_batch = build_research_context(
        "196170", "알테오", "KR", history_df=None,
        market_state={"news": [{"symbol": "196170", "source": "DART", "title": "batch"}]},
        live_news=False)
    assert ctx_batch["news"][0]["title"] == "batch"

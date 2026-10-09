"""뇌 headlines 한도(200) + 초과 시 알림(옵트인)."""
from __future__ import annotations

import json

from src.agents import context as ctx


def _news(n: int) -> list[dict]:
    return [{"source": "t", "title": f"제목{i}", "url": "u", "symbol": None}
            for i in range(n)]


def test_trim_under_limit_no_notify(monkeypatch):
    called = []
    monkeypatch.setattr(ctx, "_notify_headline_trim", lambda *a: called.append(a))
    out = ctx.select_headlines(_news(50), tier="scan", limit=200, notify_trim=True)
    assert len(out) == 50 and called == []
    assert set(out[0]) == {"source", "title", "symbol"}


def test_trim_at_limit_no_notify(monkeypatch):
    called = []
    monkeypatch.setattr(ctx, "_notify_headline_trim", lambda *a: called.append(a))
    out = ctx.select_headlines(_news(200), tier="scan", limit=200, notify_trim=True)
    assert len(out) == 200 and called == []


def test_trim_over_limit_notifies_and_cuts(monkeypatch):
    called = []
    monkeypatch.setattr(ctx, "_notify_headline_trim", lambda *a: called.append(a))
    out = ctx.select_headlines(_news(220), tier="scan", limit=200, notify_trim=True)
    assert len(out) == 200
    assert called == [(220, 200)]
    assert out[-1]["title"] == "제목199"


def test_trim_over_limit_default_no_notify(monkeypatch):
    called = []
    monkeypatch.setattr(ctx, "_notify_headline_trim", lambda *a: called.append(a))
    out = ctx.select_headlines(_news(220), tier="scan", limit=200)
    assert len(out) == 200 and called == []


def test_build_context_uses_headline_limit(monkeypatch):
    monkeypatch.setattr(ctx, "_notify_headline_trim", lambda *a: None)
    raw = json.loads(ctx.build_context(
        {"news": _news(210)}, [], {}, {}))
    assert len(raw["headlines"]) == ctx.HEADLINE_LIMIT == 200

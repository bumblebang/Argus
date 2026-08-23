"""뇌 headlines 한도(100) + 초과 시 알림."""
from __future__ import annotations

import json

from src.agents import context as ctx


def _news(n: int) -> list[dict]:
    return [{"source": "t", "title": f"제목{i}", "url": "u", "symbol": None}
            for i in range(n)]


def test_trim_under_limit_no_notify(monkeypatch):
    called = []
    monkeypatch.setattr(ctx, "_notify_headline_trim", lambda *a: called.append(a))
    out = ctx._trim_news(_news(50), limit=100)
    assert len(out) == 50 and called == []
    assert set(out[0]) == {"source", "title", "symbol"}


def test_trim_at_limit_no_notify(monkeypatch):
    called = []
    monkeypatch.setattr(ctx, "_notify_headline_trim", lambda *a: called.append(a))
    out = ctx._trim_news(_news(100), limit=100)
    assert len(out) == 100 and called == []


def test_trim_over_limit_notifies_and_cuts(monkeypatch):
    called = []
    monkeypatch.setattr(ctx, "_notify_headline_trim", lambda *a: called.append(a))
    out = ctx._trim_news(_news(120), limit=100)
    assert len(out) == 100
    assert called == [(120, 100)]
    assert out[-1]["title"] == "제목99"


def test_build_context_uses_headline_limit(monkeypatch):
    monkeypatch.setattr(ctx, "_notify_headline_trim", lambda *a: None)
    raw = json.loads(ctx.build_context(
        {"news": _news(110)}, [], {}, {}))
    assert len(raw["headlines"]) == ctx.HEADLINE_LIMIT == 100

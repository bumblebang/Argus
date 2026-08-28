"""EDGAR since_date · loop interleave · http sanitize · dashboard FX 거부."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_edgar_fetch_skips_old_filings_by_since_date(monkeypatch):
    from src.datasources import edgar as emod

    monkeypatch.setattr(emod, "load_cik_map", lambda *a, **k: {"AAPL": 320193})

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "name": "Apple",
                "filings": {"recent": {
                    "form": ["8-K", "8-K", "10-K"],
                    "accessionNumber": ["A1", "A2", "A3"],
                    "filingDate": ["2026-08-28", "2026-08-20", "2026-01-01"],
                    "items": ["1.01", "2.01", ""],
                }},
            }

    monkeypatch.setattr(emod.requests, "get", lambda *a, **k: _Resp())
    out = emod.fetch_recent_filings(["AAPL"], "ua", since_date="2026-08-25")
    assert [x["accession"] for x in out] == ["A1"]
    assert all(x["filing_date"] >= "2026-08-25" for x in out)


def test_interleave_by_market_fairness():
    from src.engine.loop import _interleave_by_market

    items = [("KR", "a", []), ("KR", "b", []), ("KR", "c", []),
             ("US", "x", []), ("US", "y", [])]
    picked = _interleave_by_market(items, ("KR", "US"), 4)
    assert [p[0] for p in picked] == ["KR", "US", "KR", "US"]
    assert [p[1] for p in picked] == ["a", "x", "b", "y"]


def test_redact_secrets_strips_query_tokens():
    from src.http_sanitize import redact_secrets

    raw = "403 https://finnhub.io/api/v1/news?token=SECRET123&category=general"
    assert "SECRET123" not in redact_secrets(raw)
    assert "token=***" in redact_secrets(raw)


def test_store_trade_stats_no_fx_us_refuses_total():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import dashboard as dash

    st = dash._store_trade_stats(
        [{"symbol": "AAPL", "market": "US", "qty": 1, "avg_price": 100,
          "exit_price": 110, "pnl": 10, "closed_at": 1}],
        paper={"start_cash": {"KR": 1_000_000, "US": 10_000}},
        fx=None,
    )
    assert st["ret_total"] is None
    assert st["realized_krw"] is None

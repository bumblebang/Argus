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


def test_redact_ecos_path_key():
    from src.http_sanitize import redact_secrets

    raw = "ECOS 실패: 403 https://ecos.bok.or.kr/api/KeyStatisticList/ABCD1234EFGH/json/kr/1/101"
    out = redact_secrets(raw)
    assert "ABCD1234EFGH" not in out
    assert "KeyStatisticList/***" in out


def test_redact_fred_api_key_in_query():
    from src.http_sanitize import redact_secrets

    raw = ("HTTP 403 for url: https://api.stlouisfed.org/fred/series/observations"
           "?series_id=UNRATE&api_key=FRED_SECRET_KEY_XYZ&file_type=json")
    out = redact_secrets(raw)
    assert "FRED_SECRET_KEY_XYZ" not in out
    assert "api_key=***" in out


def test_redact_dart_crtfc_key():
    from src.http_sanitize import redact_secrets

    raw = "https://opendart.fss.or.kr/api/list.json?crtfc_key=DARTKEY999&bgn_de=20260828"
    out = redact_secrets(raw)
    assert "DARTKEY999" not in out
    assert "crtfc_key=***" in out


def test_redact_env_secret_anywhere(monkeypatch):
    from src.http_sanitize import invalidate_env_secret_cache, redact_secrets

    monkeypatch.setenv("ECOS_API_KEY", "ecos-path-key-9999")
    invalidate_env_secret_cache()
    raw = "요청 실패 https://ecos.bok.or.kr/api/StatisticSearch/ecos-path-key-9999/json"
    out = redact_secrets(raw)
    assert "ecos-path-key-9999" not in out
    assert "***" in out


def test_redacting_formatter_masks_log_record(monkeypatch):
    import logging

    from src.http_sanitize import invalidate_env_secret_cache
    from src.logging_setup import RedactingFormatter

    monkeypatch.setenv("FRED_API_KEY", "fred-log-test-key-88")
    invalidate_env_secret_cache()
    rec = logging.LogRecord(
        name="test", level=logging.WARNING, pathname="", lineno=0,
        msg="FRED 실패 %s", args=("403 api_key=fred-log-test-key-88",),
        exc_info=None, func=None)
    out = RedactingFormatter("%(message)s").format(rec)
    assert "fred-log-test-key-88" not in out
    assert "***" in out


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

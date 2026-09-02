"""market_status_crosscheck — 정규장 교차검증."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.market_status_crosscheck as msc


def test_crosscheck_no_alert_when_agree(monkeypatch):
    monkeypatch.setattr(msc, "argus_regular_open", lambda m, now=None: True)
    monkeypatch.setattr(msc, "external_regular_open", lambda m, now=None: True)
    monkeypatch.setattr(msc, "kr_cache_date_mismatch", lambda now=None: None)
    assert msc.crosscheck_reasons(1_000_000.0) == []


def test_crosscheck_us_mismatch(monkeypatch):
    monkeypatch.setattr(msc, "argus_regular_open",
                        lambda m, now=None: m == "US")
    monkeypatch.setattr(msc, "external_regular_open",
                        lambda m, now=None: False if m == "US" else True)
    monkeypatch.setattr(msc, "kr_cache_date_mismatch", lambda now=None: None)
    r = msc.crosscheck_reasons(1_000_000.0)
    assert any("불일치 US" in x for x in r)


def test_crosscheck_fail_open_when_external_none(monkeypatch):
    monkeypatch.setattr(msc, "argus_regular_open", lambda m, now=None: True)
    monkeypatch.setattr(msc, "external_regular_open", lambda m, now=None: None)
    monkeypatch.setattr(msc, "kr_cache_date_mismatch", lambda now=None: None)
    assert msc.crosscheck_reasons(1_000_000.0) == []


def test_us_regular_open_finnhub_regular(monkeypatch):
    monkeypatch.setattr(msc, "fetch_us_market_status_finnhub",
                        lambda key, timeout=8: {"session": "regular", "isOpen": True})
    assert msc.us_regular_open_finnhub("k") is True


def test_us_regular_open_finnhub_premarket_not_regular(monkeypatch):
    monkeypatch.setattr(msc, "fetch_us_market_status_finnhub",
                        lambda key, timeout=8: {"session": "pre-market", "isOpen": True})
    assert msc.us_regular_open_finnhub("k") is False


def test_us_regular_open_yahoo_regular(monkeypatch):
    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"chart": {"result": [{"meta": {"marketState": "REGULAR"}}]}}

    monkeypatch.setattr(msc.requests, "get", lambda *a, **k: _Resp())
    assert msc.us_regular_open_yahoo() is True


def test_kr_regular_open_yahoo_regular(monkeypatch):
    monkeypatch.setattr(msc, "_yahoo_regular_open",
                        lambda url, timeout=8: True if "KS11" in url else None)
    assert msc.kr_regular_open_yahoo() is True


def test_external_kr_uses_yahoo_not_is_open(monkeypatch):
    monkeypatch.setattr(msc, "kr_regular_open_yahoo", lambda timeout=8: False)
    monkeypatch.setattr(msc, "is_open", lambda m, now=None: True)
    assert msc.external_regular_open("KR") is False


def test_kr_cache_date_mismatch_on_stale(tmp_path, monkeypatch):
    path = tmp_path / "market_sessions.json"
    path.write_text(json.dumps({
        "KR": {"market": "KR", "date": "2026-01-01", "sessions": [], "fetched": 0},
    }), encoding="utf-8")
    monkeypatch.setattr(msc, "_SESSIONS_CACHE", path)
    monkeypatch.setattr(msc, "is_open", lambda m, now=None: True)
    monkeypatch.setattr(msc, "trading_date", lambda m, ts: "2026-08-28")
    monkeypatch.setattr(msc, "_cache_valid", lambda m, entry, ts: False)
    msg = msc.kr_cache_date_mismatch(1_000_000.0)
    assert msg and "2026-01-01" in msg and "2026-08-28" in msg


def test_kr_cache_date_mismatch_skips_when_closed(monkeypatch):
    monkeypatch.setattr(msc, "is_open", lambda m, now=None: False)
    assert msc.kr_cache_date_mismatch(1_000_000.0) is None


def test_alert_check_includes_crosscheck(monkeypatch, tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import alert_check as ac

    monkeypatch.setattr(ac, "_read_heartbeat", lambda now: (2.0, {"ok": True, "polled": 1,
                                                                  "should_be_open": ["KR"],
                                                                  "markets_open": ["KR"]}))
    monkeypatch.setattr(ac, "_load_brain_mode", lambda: {"mode": "ok"})
    monkeypatch.setattr(ac, "_auth_expired_recent", lambda now, window=3600: False)
    monkeypatch.setattr(ac, "_bridge_inbox_and_max_age", lambda: (tmp_path / "inbox", 90.0))
    monkeypatch.setattr(ac, "is_bridge_armed", lambda *a, **k: True)
    monkeypatch.setattr(ac, "crosscheck_reasons",
                        lambda now=None: ["장 상태 불일치 US: Argus 정규장=True 외부=False — 세션 캐시 stale 의심"])
    monkeypatch.setattr(ac, "ALERT", tmp_path / "ALERT.json")
    monkeypatch.setattr(ac, "ALERTS_LOG", tmp_path / "alerts.jsonl")
    monkeypatch.setattr(ac, "_ntfy_topic", lambda: "")
    monkeypatch.setattr(ac, "_push_live_orders", lambda now: None)
    monkeypatch.setattr(
        "src.ops_budget.budget_gauge",
        lambda *a, **k: {"line": ""},
    )

    ac.main()
    payload = json.loads((tmp_path / "ALERT.json").read_text(encoding="utf-8"))
    assert payload["active"] is True
    assert any("불일치 US" in r for r in payload["reasons"])

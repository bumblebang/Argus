"""시장 전체 수급(flows_market) — 파싱·히스토리·소스."""
from __future__ import annotations

from src.datasources.base import SourceContext
from src.datasources.flows_market import (FlowsMarketSource, enrich_from_history,
                                          parse_trend_row, update_history)
from src.market_state import MarketState


def test_parse_trend_row():
    row = parse_trend_row({
        "bizdate": "20260731",
        "personalValue": "-82,840",
        "foreignValue": "+72,410",
        "institutionalValue": "+11,503",
    })
    assert row == {"date": "20260731", "foreign_net": 72410.0,
                   "inst_net": 11503.0, "indiv_net": -82840.0}


def test_parse_bad_row():
    assert parse_trend_row({}) is None
    assert parse_trend_row({"bizdate": "x"}) is None


def test_history_enrich_3d_and_p90():
    days = [
        {"date": "20260701", "foreign_net": 100},
        {"date": "20260702", "foreign_net": -200},
        {"date": "20260703", "foreign_net": 300},
        {"date": "20260704", "foreign_net": -400},
        {"date": "20260705", "foreign_net": 500},
    ]
    out = enrich_from_history(days[-1], days)
    assert out["foreign_net_3d"] == 300 - 400 + 500
    assert "foreign_net_p90" in out


def test_history_p90_requires_min_samples():
    days = [{"date": "20260701", "foreign_net": 100},
            {"date": "20260702", "foreign_net": 200}]
    out = enrich_from_history(days[-1], days)
    assert "foreign_net_p90" not in out
    assert out["foreign_net_3d"] == 300


def test_update_history_upsert(tmp_path):
    hist: dict = {}
    update_history(hist, "KOSPI", {"date": "20260701", "foreign_net": 1})
    update_history(hist, "KOSPI", {"date": "20260701", "foreign_net": 9})
    assert len(hist["KOSPI"]) == 1
    assert hist["KOSPI"][0]["foreign_net"] == 9


def test_dry_fetch():
    out = FlowsMarketSource().fetch(SourceContext(dry=True))
    assert out["flows_market"]["KOSPI"]["foreign_net"] == 5000.0
    assert out["flows_market"]["source"] == "naver_index"


def test_fetch_parses_live_shape(monkeypatch, tmp_path):
    import src.datasources.flows_market as fm

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"bizdate": "20260731", "personalValue": "-1",
                    "foreignValue": "+10", "institutionalValue": "+2"}

    monkeypatch.setattr(fm.requests, "get", lambda *a, **kw: _Resp())
    src = FlowsMarketSource(history_path=tmp_path / "h.json")
    out = src.fetch(SourceContext())["flows_market"]
    assert out["KOSPI"]["foreign_net"] == 10.0
    assert out["KOSDAQ"]["foreign_net"] == 10.0
    assert out["source"] == "naver_index"


def test_market_state_slot():
    ms = MarketState()
    ms.merge(FlowsMarketSource().fetch(SourceContext(dry=True)))
    assert ms.flows_market["KOSPI"]["foreign_net"] == 5000.0


def test_network_failure_returns_empty(monkeypatch, tmp_path):
    import src.datasources.flows_market as fm

    def boom(*a, **kw):
        raise RuntimeError("down")

    monkeypatch.setattr(fm.requests, "get", boom)
    out = FlowsMarketSource(history_path=tmp_path / "h.json").fetch(SourceContext())
    assert out == {"flows_market": {}}

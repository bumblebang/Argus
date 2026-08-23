"""신용·공매도 positioning — 파싱·급변·소스."""
from __future__ import annotations

from src.datasources.base import SourceContext
from src.datasources.positioning import (PositioningSource, detect_spike,
                                         parse_krx_short_row)
from src.focus import build_focus


def test_parse_krx_short_row():
    out = parse_krx_short_row({
        "TRD_DD": "20260731",
        "STR_CONST_VAL1": "1,234,567",
        "STR_CONST_VAL2": "9,000",
        "SRTSLL_NTPOS_RT": "2.5",
    })
    assert out["short_balance"] == 1234567.0
    assert out["short_ratio"] == 2.5
    assert out["asof"] == "2026-07-31"


def test_detect_spike():
    assert detect_spike({"short_balance": 115}, {"short_balance": 100})
    assert not detect_spike({"short_balance": 105}, {"short_balance": 100})
    assert not detect_spike({"short_balance": 100}, None)


def test_dry_fetch():
    out = PositioningSource(["005930"]).fetch(SourceContext(dry=True))
    assert "005930" in out["positioning"]


def test_no_creds_returns_empty(monkeypatch):
    src = PositioningSource(["005930"], user="", password="")
    out = src.fetch(SourceContext())
    assert out["positioning"] == {}
    assert out.get("short_market") == {}


def test_focus_positioning_lens():
    out = build_focus({}, candidates=[
        {"symbol": "005930", "positioning": {"spike": True, "short_balance": 1}},
    ], macro_events=[])
    assert any(ln["id"] == "positioning" for ln in out["lenses"])

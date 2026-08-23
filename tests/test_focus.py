"""주의층(focus) — 렌즈 생성·컨텍스트·프롬프트 회귀."""
from __future__ import annotations

import json
from datetime import date, timedelta

from src.focus import (MACRO_DDAY_MAX, MACRO_DDAY_MIN, attach_macro_tags,
                       build_focus, macro_tags_for_sector)


def test_build_focus_empty_without_signals():
    out = build_focus({}, candidates=[], positions=[], macro_events=[])
    assert out["lenses"] == []
    assert out["summary"] == ""
    assert "asof" in out


def test_macro_lens_in_window():
    today = date(2026, 8, 2)
    events = [
        {"id": "fomc", "label": "FOMC", "date": "2026-08-03", "market": "US"},  # D+1
        {"id": "bok_mpc", "label": "금통위", "date": "2026-07-20", "market": "KR"},  # 밖
    ]
    out = build_focus({}, macro_events=events, today=today)
    ids = [ln["id"] for ln in out["lenses"]]
    assert "fomc" in ids
    assert "bok_mpc" not in ids
    fomc = next(ln for ln in out["lenses"] if ln["id"] == "fomc")
    assert fomc["dday"] == 1
    assert fomc["priority"] == "high"
    assert "macro" in fomc["read"]
    assert "FOMC" in out["summary"]


def test_macro_lens_high_near_day():
    today = date(2026, 8, 2)
    out = build_focus({}, macro_events=[
        {"id": "fomc", "label": "FOMC", "date": (today + timedelta(days=1)).isoformat()},
    ], today=today)
    assert out["lenses"][0]["priority"] == "high"
    assert out["lenses"][0]["dday"] == 1


def test_macro_window_bounds():
    today = date(2026, 8, 10)
    inside = (today + timedelta(days=MACRO_DDAY_MIN)).isoformat()
    outside = (today + timedelta(days=MACRO_DDAY_MIN - 1)).isoformat()
    out = build_focus({}, macro_events=[
        {"id": "fomc", "label": "FOMC", "date": inside},
        {"id": "cpi_us", "label": "US CPI", "date": outside},
    ], today=today)
    ids = {ln["id"] for ln in out["lenses"]}
    assert ids == {"fomc"}
    assert MACRO_DDAY_MAX == 1


def test_flows_regime_lens_p90():
    ms = {"flows_market": {
        "KOSPI": {"foreign_net": -5000, "foreign_net_p90": 4000,
                  "foreign_net_3d": -12000},
        "KOSDAQ": {"foreign_net": 100, "foreign_net_p90": 2000},
    }}
    out = build_focus(ms, macro_events=[])
    assert any(ln["id"] == "flows_regime" for ln in out["lenses"])
    assert "수급" in out["summary"]


def test_flows_regime_no_false_positive():
    ms = {"flows_market": {
        "KOSPI": {"foreign_net": 100, "foreign_net_p90": 4000, "foreign_net_3d": -50},
    }}
    out = build_focus(ms, macro_events=[])
    assert not any(ln["id"] == "flows_regime" for ln in out["lenses"])


def test_positioning_lens():
    cands = [{"symbol": "005930", "positioning": {"spike": True, "short_ratio": 5.0}}]
    out = build_focus({}, candidates=cands, macro_events=[])
    assert out["lenses"][0]["id"] == "positioning"
    assert "005930" in out["lenses"][0]["hint"]


def test_macro_tags():
    assert "rate_sensitive" in macro_tags_for_sector("은행")
    assert "export" in macro_tags_for_sector("반도체")
    cands = [{"symbol": "X", "sector": "증권"}]
    attach_macro_tags(cands)
    assert "rate_sensitive" in cands[0]["macro_tags"]


def test_build_context_includes_focus_and_flows_market():
    from src.agents.context import build_context
    ctx = json.loads(build_context(
        {"flows_market": {"KOSPI": {"foreign_net": 1}}, "macro": {}},
        [], {}, {},
        focus={"asof": "t", "lenses": [{"id": "fomc"}], "summary": "FOMC D-1"},
    ))
    assert ctx["focus"]["summary"] == "FOMC D-1"
    assert ctx["market"]["flows_market"]["KOSPI"]["foreign_net"] == 1
    # focus 없으면 키 자체 없음(소음 방지)
    ctx2 = json.loads(build_context({}, [], {}, {}))
    assert "focus" not in ctx2


def test_prompts_mention_focus():
    from src.agents.decision_agent import SYSTEM
    from src.agents.athena import ATHENA_SYSTEM
    assert "focus" in SYSTEM and "오늘의 렌즈" in SYSTEM
    assert "focus" in ATHENA_SYSTEM and "오늘의 렌즈" in ATHENA_SYSTEM


def test_lens_priority_sorts_high_first():
    today = date(2026, 8, 2)
    out = build_focus(
        {"flows_market": {"KOSPI": {"foreign_net": -9000, "foreign_net_p90": 1000}}},
        macro_events=[{"id": "fomc", "label": "FOMC",
                       "date": (today + timedelta(days=1)).isoformat()}],
        today=today,
    )
    assert out["lenses"][0]["id"] == "fomc"
    assert out["lenses"][0]["priority"] == "high"

"""진입대기 패널 — 카드 UI·돌파선·존 표시."""
import json

from scripts.dashboard import (
    _armed_panel_html, _condense_armed_thesis, _today_html, build_armed_plan,
)


def test_build_armed_plan_breakout():
    pos = {
        "symbol": "066570", "market": "KR", "strategy": "volatility_breakout",
        "state": "armed",
        "meta": json.dumps({
            "horizon": "day",
            "params": {"k": 0.6},
        }),
        "thesis": "데이 풀 후보",
    }
    plan = build_armed_plan(pos, price=200_500.0)
    assert plan["track"] == "데이"
    assert plan["mode"] == "돌파"
    assert plan["entry_kind"] == "breakout"
    assert plan["entry_rows"][0]["label"] == "돌파선"
    assert "돌파선" in plan["entry_line"]
    assert plan["current_line"] == "200,500"
    assert plan["status_label"] in ("돌파 대기", "돌파 충족")


def test_build_armed_plan_zone():
    pos = {
        "symbol": "005930", "market": "KR", "strategy": "breakout_pullback",
        "state": "armed",
        "meta": {
            "horizon": "swing",
            "entry_zone": {"low": 75000, "high": 78000, "invalidation": 72000,
                           "target": 85000},
        },
    }
    plan = build_armed_plan(pos, price=79000.0)
    assert plan["track"] == "스윙"
    assert plan["mode"] == "존 재진입"
    assert plan["entry_kind"] == "zone"
    assert plan["entry_rows"][0]["label"] == "진입"
    assert "75,000" in plan["entry_rows"][0]["value"]
    assert plan["status_label"] == "존 위"


def test_condense_armed_thesis_first_sentence():
    long = ("시장이 갭다운으로 연 아침에 이 종목만 갭 +0.35%로 열려 상대강도가 확인된다. "
            "현재가 86,600은 ma20 대비 +11.1%로 과열이다.")
    short, full = _condense_armed_thesis(long)
    assert short == (
        "시장이 갭다운으로 연 아침에 이 종목만 갭 +0.35%로 열려 상대강도가 확인된다.")
    assert full == long


def test_condense_armed_thesis_strips_dossier_boilerplate():
    long = ("신선한 bullish 도시에(id 1252, age 6.1h, rr 3.35)가 진입존 248,000~255,000을 "
            "제시. 지수 하락 속 상대강도가 확인된다.")
    short, _ = _condense_armed_thesis(long)
    assert "도시에" not in short
    assert "248,000" not in short
    assert "상대강도" in short or "지수" in short


def test_armed_panel_renders_cards():
    d = {
        "names": {"066570": "LG전자"},
        "pos_px": {"066570": 200500},
        "dossiers": [],
        "positions": [{
            "symbol": "066570", "market": "KR", "state": "armed",
            "strategy": "volatility_breakout", "opened_at": 1787837405.0,
            "meta": json.dumps({"horizon": "day", "params": {"k": 0.6}}),
            "thesis": "데이 후보 테스트",
        }],
    }
    h = _armed_panel_html(d)
    assert "진입대기" in h
    assert "armed-list" in h and "armed-card" in h
    assert "armed-chip" in h
    assert "LG전자" in h
    assert "데이" in h and "돌파" in h
    assert "근거" in h
    assert "데이 후보 테스트" in h
    assert "<table>" not in h


def test_today_tab_armed_above_brain():
    d = {
        "now": 1785500000.0, "names": {}, "positions": [], "pos_px": {},
        "live_trades": [], "sentiment": {}, "fear_history": {},
        "hb": None, "hb_age": None, "kr_session": "closed", "us_session": "closed",
        "last_cycle": None, "brain_summary": {},
    }
    h = _today_html(d)
    assert h.index("진입대기") < h.index("마지막 브레인 판단")

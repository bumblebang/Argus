"""리서치 탭: 손익비 라벨·존 위치·도씨에 이후 매수 플로우."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import dashboard as dash  # noqa: E402


def test_zone_status_above_below_in_invalidated():
    assert dash.zone_status(110, 100, 105, 95) == "above"
    assert dash.zone_status(102, 100, 105, 95) == "in"
    assert dash.zone_status(98, 100, 105, 95) == "below"
    assert dash.zone_status(90, 100, 105, 95) == "invalidated"
    assert dash.zone_status(None, 100, 105, 95) is None


def test_build_flow_prefers_exec_over_zone():
    d = {"symbol": "X", "stance": "bullish", "created_at": 100.0,
         "entry_low": 100, "entry_high": 105, "invalidation": 95}
    flow = dash.build_dossier_flow(
        d, price=110,
        last_exec={"ts": 200.0, "status": "vetoed", "reason": "확신도 미달"},
    )
    assert flow["key"] == "vetoed"
    assert "검증 거부" in flow["label"]
    assert "확신도" in flow["detail"]
    assert flow["zone"] == "above"


def test_build_flow_ignores_exec_before_dossier():
    d = {"symbol": "X", "stance": "bullish", "created_at": 500.0,
         "entry_low": 100, "entry_high": 105, "invalidation": 95}
    flow = dash.build_dossier_flow(
        d, price=110,
        last_exec={"ts": 100.0, "status": "vetoed", "reason": "옛 기록"},
    )
    assert flow["key"] == "zone_above"
    assert "추격" in flow["label"]


def test_build_flow_holding_beats_exec():
    d = {"symbol": "X", "stance": "bullish", "created_at": 1.0,
         "entry_low": 100, "entry_high": 105, "invalidation": 95}
    flow = dash.build_dossier_flow(
        d, price=102, position={"state": "open"},
        last_exec={"ts": 10.0, "status": "vetoed", "reason": "x"},
    )
    assert flow["key"] == "holding"


def test_index_latest_exec_keeps_newest_per_symbol():
    cycles = [
        {"ts": 30.0, "payload": json.dumps({
            "executed": [{"symbol": "A", "status": "filled", "reason": "new"}]})},
        {"ts": 20.0, "payload": {
            "executed": [{"symbol": "A", "status": "vetoed", "reason": "old"},
                         {"symbol": "B", "status": "gap_armed", "reason": "wait"}]}},
    ]
    out = dash.index_latest_exec(cycles)
    assert out["A"]["status"] == "filled"
    assert out["A"]["ts"] == 30.0
    assert out["B"]["status"] == "gap_armed"


def test_research_html_shows_rr_help_full_thesis_and_flow():
    thesis = "FULL_THESIS_MARKER " + ("hypothesis-body " * 20)  # >80 chars
    d = {
        "now": 1000.0,
        "names": {"102110": "TIGER200"},
        "dossiers": [{
            "symbol": "102110", "market": "KR", "created_at": 100.0,
            "thesis": thesis, "rr": 2.68, "conviction": 0.55,
            "entry_low": 104500, "entry_high": 107020,
            "invalidation": 102500, "target": 114500,
            "evidence": json.dumps({"stance": "bullish"}),
        }],
        "base_rates": {"symbols": {}},
        "pos_px": {"102110": 107530},
        "positions": [],
        "research_cycles": [{
            "ts": 200.0,
            "payload": json.dumps({"executed": [{
                "symbol": "102110", "action": "BUY", "status": "vetoed",
                "reason": "ZONE_CHASE_REJECT",
            }]}),
        }],
        "research_buys": [],
        "athena_runs": [],
    }
    html = dash._research_html(d)
    assert "손익비" in html
    assert "진입존 중앙" in html  # RR 설명
    assert "FULL_THESIS_MARKER" in html
    assert thesis.strip() in html  # 잘림 없음(표시용 strip 만)
    assert "매수 제안 → 검증 거부" in html
    assert "ZONE_CHASE_REJECT" in html
    assert "dos-card" in html
    assert 'data-sym="102110"' in html
    assert " open>" not in html and ' open"' not in html  # 서버 강제 펼침 없음
    assert html.count("hypothesis-body") >= 20

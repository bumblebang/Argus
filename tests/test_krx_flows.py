"""KRX 파서·소스·focus·risk 게이트 단위 테스트."""
from __future__ import annotations

from pathlib import Path

from src.datasources.base import SourceContext
from src.datasources.foreign_exhaustion import parse_exhaustion_row
from src.datasources.krx_alerts import (HARD_BLOCK, load_blocked_symbols,
                                         parse_alert_row, save_blocked_symbols)
from src.datasources.krx_client import load_catalog, num, rows_of, ymd_dash
from src.datasources.krx_flows import parse_market_investor_row
from src.datasources.krx_market import parse_vkospi_row
from src.datasources.positioning import (PositioningSource, detect_spike,
                                         parse_krx_short_row, parse_short_market_row)
from src.datasources.program_flows import ProgramFlowsSource, parse_program_row
from src.datasources.fear_greed import kr_fear_proxy, vkospi_to_score
from src.focus import build_focus
from src.risk_gate import Order, RiskGate


def test_catalog_has_p0_entries():
    cat = load_catalog()
    ids = {e["id"] for e in cat.get("entries") or []}
    assert "short_종합" in ids
    assert "program_trading" in ids
    assert "vkospi" in ids


def test_attach_krx_fields():
    from src.focus import attach_krx_fields
    cands = [{"symbol": "005930"}]
    attach_krx_fields(cands, {
        "positioning": {"005930": {"spike": True, "short_ratio": 2.0}},
        "foreign_exhaustion": {"005930": {"exhaustion_pct": 90.0, "watch": True,
                                          "source": "krx"}},
    })
    assert cands[0]["positioning"]["spike"] is True
    assert cands[0]["foreign_exhaustion"]["watch"] is True
    assert "source" not in cands[0]["foreign_exhaustion"]


def test_rows_of_and_num():
    assert num("1,234.5") == 1234.5
    assert ymd_dash("20260115") == "2026-01-15"
    assert len(rows_of({"OutBlock_1": [{"a": 1}]})) == 1
    assert rows_of({"x": 1}) == []


def test_parse_credit_fields():
    out = parse_krx_short_row({
        "TRD_DD": "20260731",
        "STR_CONST_VAL1": "100",
        "CRD_BAL": "5,000",
        "CREDIT_RATIO": "2.0",
    })
    assert out["short_balance"] == 100.0
    assert out["credit_balance"] == 5000.0
    assert out["credit_ratio"] == 2.0
    assert detect_spike({"credit_balance": 6000}, {"credit_balance": 5000})


def test_parse_short_market_and_program():
    sm = parse_short_market_row({
        "TRD_DD": "2026/01/15",
        "STR_CONST_VAL1": "1", "STR_CONST_VAL2": "2",
        "STR_CONST_VAL3": "3", "STR_CONST_VAL5": "6",
    })
    assert sm["foreign"] == 3.0 and sm["total"] == 6.0
    pr = parse_program_row({
        "TRD_DD": "20260115",
        "ARB_NETBID_TRDVAL": "10000000000",
        "NONARB_NETBID_TRDVAL": "-2000000000",
    })
    assert pr["total_net"] == 8000000000.0
    from src.datasources.program_flows import parse_program_rows
    modern = parse_program_rows([
        {"ITM_TP_NM": "차익", "NETBID_TRDVAL": "65,712,354,904"},
        {"ITM_TP_NM": "비차익", "NETBID_TRDVAL": "-837,772,798,300"},
        {"ITM_TP_NM": "전체", "NETBID_TRDVAL": "-772,060,443,396"},
    ], asof="2026-08-21")
    assert modern["date"] == "2026-08-21"
    assert modern["arb_net"] == 65712354904.0
    assert modern["total_net"] == -772060443396.0

    bal = parse_krx_short_row({
        "ISU_CD": "005930", "BAL_QTY": "7,387,185",
        "BAL_AMT": "1,828,328,287,500", "BAL_RTO": "0.13",
        "_asof": "2026-08-19",
    })
    assert bal["short_balance"] == 7387185.0
    assert bal["short_ratio"] == 0.13
    assert bal["asof"] == "2026-08-19"


def test_positioning_and_program_dry():
    assert "005930" in PositioningSource(["005930"]).fetch(
        SourceContext(dry=True))["positioning"]
    assert "KOSPI" in ProgramFlowsSource().fetch(
        SourceContext(dry=True))["program_flows"]


def test_focus_program_and_short_lenses():
    ms = {
        "program_flows": {
            "KOSPI": {"total_net": 5e9, "arb_net": 1e9},
        },
        "short_market": {
            "KOSPI": {"total": 2e10},
        },
    }
    out = build_focus(ms, macro_events=[])
    ids = {ln["id"] for ln in out["lenses"]}
    assert "program_flows" in ids
    assert "short_market" in ids


def test_exhaustion_and_alert_parse():
    ex = parse_exhaustion_row({
        "ISU_SRT_CD": "005930", "FORN_LMT_EXHST_RT": "85.0",
        "FORN_SHR_RT": "55",
    })
    assert ex["watch"] is True
    assert parse_alert_row({"ISU_SRT_CD": "000660", "WRN_TP_NM": "투자위험"},
                           "INVESTMENT_WARNING")[1] == "INVESTMENT_RISK"


def test_blocked_symbols_roundtrip(tmp_path):
    p = tmp_path / "blocked.json"
    save_blocked_symbols({"005930", "000660"}, p)
    assert load_blocked_symbols(p) == {"005930", "000660"}


def test_risk_gate_blocks_krx_alert(tmp_path):
    class Acc:
        positions = {}
        symbol_market = {}
        realized_pnl = {"KR": 0.0}
        open_count = 0
        def buying_power(self, m): return 1e12
        def position(self, s):
            class P:
                is_open = False
                qty = 0
                avg_price = 0
            return P()
        def daily_realized_pnl(self, m): return 0.0

    gate = RiskGate({
        "capital": {"KR": 1e8},
        "max_order_notional": {"KR": 1e8},
        "blocked_symbols": ["005930"],
        "kill_switch_file": str(tmp_path / "no_halt"),
    })
    d = gate.check(Order("005930", "KR", "BUY", 1, 70000), Acc())
    assert not d.approved
    assert "경보" in d.reason or "차단" in d.reason


def test_vkospi_fear_axis():
    """VKOSPI 는 inputs 만 — score/components 가중치에 넣지 않는다."""
    assert vkospi_to_score(10) == 100.0
    assert vkospi_to_score(40) == 0.0
    base = kr_fear_proxy({"breadth_above_ma20": 0.5, "n": 20}, -3.0, 0.0)
    with_vk = kr_fear_proxy({"breadth_above_ma20": 0.5, "n": 20}, -3.0, 0.0,
                            vkospi_close=25.0)
    assert with_vk["score"] == base["score"]
    assert "vkospi" not in with_vk["components"]
    assert with_vk["inputs"]["vkospi"] == 25.0
    assert with_vk["source"] == "proxy_kr"
    assert parse_vkospi_row({"TRD_DD": "20260101", "CLSPRC_IDX": "18.2"})["close"] == 18.2


def test_parse_market_investor():
    row = parse_market_investor_row({
        "TRD_DD": "2026/01/15", "TRDVAL1": "1", "TRDVAL3": "2", "TRDVAL4": "3",
    })
    assert row["foreign_net"] == 3.0

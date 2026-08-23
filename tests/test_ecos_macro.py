"""ECOS 한국 매크로(macro_kr) — 파싱·슬롯·프롬프트 회귀."""
from __future__ import annotations

import json

from src.datasources import SourceContext, EcosMacroSource
from src.datasources.ecos import KEY_MAP, parse_key_stat_rows
from src.market_state import MarketState


def _row(name: str, value: str, cycle: str = "20260731", unit: str = "%") -> dict:
    return {"CLASS_NAME": "금리", "KEYSTAT_NAME": name, "DATA_VALUE": value,
            "UNIT_NAME": unit, "CYCLE": cycle}


def test_dry_shape():
    out = EcosMacroSource(api_key="x").fetch(SourceContext(dry=True))
    assert "macro_kr" in out
    kr = out["macro_kr"]
    assert kr["source"] == "ecos" and kr["asof"]
    assert isinstance(kr["bok_base_rate"], float)


def test_parse_maps_names_to_keys():
    rows = [
        _row("한국은행 기준금리", "2.5"),
        _row("국고채수익률(3년)", "2.63"),
        _row("회사채수익률(3년,AA-)", "3.15"),
        _row("원/달러 환율(종가)", "1,384.5", unit="원"),
        _row("소비자물가지수", "117.05", cycle="202606"),
        _row("실업률", "2.9", cycle="202606"),
    ]
    out = parse_key_stat_rows(rows)
    assert out["bok_base_rate"] == 2.5
    assert out["kr_treasury_3y"] == 2.63
    assert out["corp_aa_3y"] == 3.15
    assert out["usdkrw_bok"] == 1384.5   # 천단위 콤마 파싱
    assert out["cpi_index"] == 117.05
    assert out["unemployment"] == 2.9
    assert out["source"] == "ecos"
    assert out["asof"] == "20260731"     # 가장 최근 시점
    assert out["raw_n"] == 6


def test_parse_skips_unknown_and_bad_values():
    rows = [
        _row("한국은행 기준금리", "2.5"),
        _row("KOSPI지수", "3100.5"),        # 매핑 대상 아님(markets 소관)
        _row("국고채수익률(5년)", "-"),      # 결측
        _row("경상수지", None, cycle="202605"),
    ]
    out = parse_key_stat_rows(rows)
    assert set(out) == {"bok_base_rate", "source", "asof", "raw_n"}
    assert out["raw_n"] == 1


def test_parse_requires_exact_name():
    out = parse_key_stat_rows([_row("한국은행 기준금리(연말)", "2.5")])
    assert out["raw_n"] == 0


def test_parse_empty_rows_falls_back_to_utc_asof():
    out = parse_key_stat_rows([])
    assert out["raw_n"] == 0 and out["asof"].startswith("20")


def test_fetch_network_failure_returns_empty(monkeypatch):
    import src.datasources.ecos as ecos

    def boom(*a, **kw):
        raise RuntimeError("timeout")

    monkeypatch.setattr(ecos.requests, "get", boom)
    assert EcosMacroSource(api_key="x").fetch(SourceContext()) == {"macro_kr": {}}


def test_fetch_parses_live_shape(monkeypatch):
    import src.datasources.ecos as ecos

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"KeyStatisticList": {"row": [_row("한국은행 기준금리", "2.5"),
                                                 _row("CD수익률(91일)", "2.71")]}}

    monkeypatch.setattr(ecos.requests, "get", lambda *a, **kw: _Resp())
    out = EcosMacroSource(api_key="x").fetch(SourceContext())
    assert out["macro_kr"]["bok_base_rate"] == 2.5
    assert out["macro_kr"]["cd_91d"] == 2.71


def test_market_state_slot_is_separate_from_fred():
    ms = MarketState()
    ms.merge({"macro": {"fed_funds": 4.5}})
    ms.merge(EcosMacroSource(api_key="x").fetch(SourceContext(dry=True)))
    assert ms.macro == {"fed_funds": 4.5}          # FRED 덮어쓰기 금지
    assert ms.macro_kr["bok_base_rate"] == 2.5


def test_build_context_includes_macro_kr():
    from src.agents.context import build_context
    ctx = json.loads(build_context({"macro": {"fed_funds": 4.5},
                                    "macro_kr": {"bok_base_rate": 2.5}},
                                   [], {}, {}))
    assert ctx["market"]["macro_kr"] == {"bok_base_rate": 2.5}
    assert ctx["market"]["macro"] == {"fed_funds": 4.5}


def test_athena_research_context_includes_macro_kr():
    from src.agents.athena import build_research_context
    out = build_research_context("005930", "삼성전자", "KR", history_df=None,
                                 market_state={"macro_kr": {"bok_base_rate": 2.5}})
    assert out["macro_kr"] == {"bok_base_rate": 2.5}


def test_prompts_mention_macro_kr():
    from src.agents.decision_agent import SYSTEM
    from src.agents.athena import ATHENA_SYSTEM
    assert "macro_kr" in SYSTEM
    assert "macro_kr" in ATHENA_SYSTEM


def test_key_map_covers_required_keys():
    required = {"bok_base_rate", "call_overnight", "koribor_3m", "cd_91d", "msb_364d",
                "kr_treasury_3y", "kr_treasury_5y", "corp_aa_3y", "usdkrw_bok",
                "cpi_index", "cpi_core_index", "ppi_index", "unemployment",
                "employment_rate", "consumer_sentiment", "bsi_all",
                "economic_sentiment", "coincident_cycle", "leading_cycle",
                "fx_reserves", "current_account"}
    assert set(KEY_MAP.values()) == required

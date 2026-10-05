"""Athena 입력 보강(재무·수급·공매도) — 시점 재현과 run_batch 주입."""
import json
from datetime import date

from src.agents.athena_inputs import (AthenaInputs, fill_missing, input_flags,
                                      public_fiscal_years, summarize_flows)


def test_public_fiscal_years():
    assert public_fiscal_years(date(2026, 3, 31)) == (2024, 2023)   # 사업보고서 전
    assert public_fiscal_years(date(2026, 4, 1)) == (2025, 2024)
    assert public_fiscal_years(date(2026, 8, 20)) == (2025, 2024)


def _fin(year, rev, ni, eq):
    return {"revenue": rev, "operating_income": ni * 1.2, "net_income": ni, "equity": eq,
            "total_assets": eq * 2, "total_liabilities": eq, "current_assets": eq,
            "current_liabilities": eq / 2, "fiscal_year": year, "operating_cf": 3e10}


def _inputs(**kw):
    cache = {"C1": {"2025": _fin(2025, 2e11, 2e10, 1e11), "2024": _fin(2024, 1e11, 1e10, 8e10)}}
    return AthenaInputs("x", fin_cache=cache, corp_map={"111111": "C1"},
                        mcap={"111111": (4e11, 10000.0)}, **kw)


def test_kr_fundamentals_point_in_time_and_scaled_mcap():
    inp = _inputs()
    f = inp.kr_fundamentals("111111", price=5000.0, asof=date(2026, 8, 20))
    assert f["fiscal_year"] == 2025
    assert f["market_cap_eok"] == 2000              # 시총 4천억 × 5000/10000
    assert f["pb"] == 2.0 and f["revenue_growth"] == 1.0
    assert f["operating_cf_eok"] == 300.0 and "operating_cf" not in f
    # 3월 시점엔 2025 사업보고서가 없다 → 2024
    assert inp.kr_fundamentals("111111", price=5000.0,
                               asof=date(2026, 3, 2))["fiscal_year"] == 2024
    assert inp.kr_fundamentals("999999", price=1.0, asof=date(2026, 8, 20)) is None


def test_summarize_flows_excludes_asof_day_and_counts_streak():
    rows = [{"date": f"202608{d:02d}", "foreign_net": v, "inst_net": 1e8, "indiv_net": -1e8}
            for d, v in ((20, 9e9), (19, -2e8), (18, -1e8), (14, 5e8), (13, -3e8))]
    s = summarize_flows(rows, asof=date(2026, 8, 20))
    assert s["date"] == "20260819"                  # 판단일 당일 행은 제외
    assert s["foreign_streak_days"] == -2
    assert s["foreign_5d"] == round((-2e8 - 1e8 + 5e8 - 3e8) / 1e8, 1)


class _FakeKrx:
    def __init__(self):
        self.calls = []

    def get_rows(self, bld, **p):
        self.calls.append((bld, p))
        if "isuCd" in p:
            return [{"TRD_DD": "2026/08/19", "TRDVAL1": "100,000,000", "TRDVAL3": "0",
                     "TRDVAL4": "-300,000,000"}]
        if p.get("mktTpCd") == "1" and p.get("trdDd") == "20260817":
            return [{"ISU_CD": "111111", "BAL_RTO": "1.5", "TRD_DD": "2026/08/17"}]
        if p.get("mktTpCd") == "1" and p.get("trdDd") == "20260720":
            return [{"ISU_CD": "111111", "BAL_RTO": "1.0", "TRD_DD": "2026/07/20"}]
        return []


def test_flows_and_short_use_isin_and_past_window():
    krx = _FakeKrx()
    inp = _inputs(krx_client=krx, isin_map={"111111": "KR7111111000"})
    out = inp.for_symbol("111111", "KR", price=5000.0, asof=date(2026, 8, 20))
    assert out["flows"]["foreign_5d"] == -3.0 and out["flows"]["inst_5d"] == 1.0
    flow_call = next(p for _, p in krx.calls if "isuCd" in p)
    assert flow_call["isuCd"] == "KR7111111000" and flow_call["endDd"] == "20260819"
    assert out["positioning"]["short_ratio_pct"] == 1.5
    assert out["positioning"]["short_ratio_chg_4w_pp"] == 0.5
    assert set(out) == {"fundamentals", "flows", "positioning"}


def test_us_skipped_when_not_live():
    inp = _inputs(finnhub_key="k")
    inp.us_fundamentals = lambda s: {"roe": 0.1}
    assert inp.for_symbol("MU", "US", price=1, asof=date(2026, 8, 20), live=False) == {}
    assert inp.for_symbol("MU", "US", price=1, asof=date(2026, 8, 20)) == {
        "fundamentals": {"roe": 0.1}}


def test_fill_missing_keeps_market_state_values():
    ctx = {"fundamentals": {"pe": 1}, "flows": None, "news": [1, 2]}
    filled = fill_missing(ctx, {"fundamentals": {"pe": 9}, "flows": {"x": 1}})
    assert filled == ["flows"] and ctx["fundamentals"] == {"pe": 1}
    assert input_flags(ctx) == {"fundamentals": True, "flows": True,
                                "positioning": False, "news": 2}


def test_run_batch_fills_inputs_and_records_flags(tmp_path, monkeypatch):
    from src.agents import athena
    from src.config import load_config
    from src.engine.store import Store
    from tests.test_athena import _capture_llm, _df

    class _Fake:
        def for_symbol(self, sym, market, *, price, asof):
            return {"fundamentals": {"pb": 1.1}, "flows": {"foreign_5d": -3.0}}

    monkeypatch.setattr(athena, "_athena_inputs", lambda cfg: _Fake())
    cfg = load_config()
    cfg.universe["KR"] = [{"symbol": "AAA", "name": "a"}]
    seen = []
    store = Store(tmp_path / "t.db")
    athena.run_batch(cfg, store, _capture_llm(seen), "KR", fetch_df=lambda s_, m: _df())
    assert seen[0]["fundamentals"] == {"pb": 1.1} and seen[0]["flows"]["foreign_5d"] == -3.0
    ev = json.loads(store.get_fresh_dossier("AAA")["evidence"])
    assert ev["inputs"]["fundamentals"] is True and ev["inputs"]["positioning"] is False

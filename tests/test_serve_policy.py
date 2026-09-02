"""뇌 데이터 서빙 정책 — scan/focus shortlist · wake coalesce · ondemand patch."""
from __future__ import annotations

from types import SimpleNamespace

from src.agents import serve_policy as serve
from src.agents.context import build_context
from src.engine.brain import BrainWorker
from src.engine.store import Store
from src.engine.triggers import Trigger


def test_classify_scan_reasons():
    cfg = serve.serve_cfg({"serve": {"enabled": True}})
    for r in ("periodic", "extra", "athena_done", ""):
        assert serve.classify_tier({"reason": r}, cfg=cfg) == "scan"
    assert serve.classify_tier(None, cfg=cfg) == "scan"


def test_classify_focus_reasons():
    cfg = serve.serve_cfg({"serve": {"enabled": True}})
    for r in ("wake_triggers", "disclosure", "earnings_result", "movers"):
        assert serve.classify_tier({"reason": r}, cfg=cfg) == "focus"


def test_classify_disabled_always_scan():
    cfg = serve.serve_cfg({"serve": {"enabled": False}})
    assert serve.classify_tier({"reason": "disclosure"}, cfg=cfg) == "scan"


def test_select_scan_keeps_all_items_when_disabled():
    items = [{"symbol": f"{i:06d}", "market": "KR", "pool": "day"} for i in range(50)]
    cfg = serve.serve_cfg({"serve": {
        "enabled": True, "focus_cap": 8, "scan_enabled": False}})
    out, tier = serve.select_candidates(
        items, {"reason": "periodic"}, held=["000001"], cfg=cfg)
    assert tier == "scan"
    assert len(out) == 50
    assert out is not items  # 복사본


def test_select_scan_shortlist_cap_and_must():
    items = [{"symbol": f"{i:06d}", "market": "KR"} for i in range(1, 51)]
    scores = {f"{i:06d}": {"ranking": [{"return_pct": i / 100.0}]}
              for i in range(1, 51)}
    cfg = serve.serve_cfg({"serve": {
        "enabled": True, "scan_enabled": True, "scan_cap": 10}})
    out, tier = serve.select_candidates(
        items, {"reason": "periodic"},
        held=["000003"], armed=["000007"], bullish=["000010"],
        scores=scores, cfg=cfg)
    assert tier == "scan"
    syms = {c["symbol"] for c in out}
    assert {"000003", "000007", "000010"}.issubset(syms)
    assert len(out) == 10
    # pad 는 scores 순 — must 3 + pad 7
    must_rows = [c for c in out if c.get("serve_must")]
    assert len(must_rows) == 3


def test_select_scan_must_exceeds_cap():
    items = [{"symbol": f"{i:06d}", "market": "KR"} for i in range(1, 15)]
    held = [f"{i:06d}" for i in range(1, 9)]
    cfg = serve.serve_cfg({"serve": {
        "enabled": True, "scan_enabled": True, "scan_cap": 5}})
    out, _ = serve.select_candidates(
        items, {"reason": "extra"}, held=held, cfg=cfg)
    assert len(out) == 8  # must 8 > cap 5 → must 전원


def test_gap_scan_exempt_from_scan_cap():
    items = [{"symbol": f"{i:06d}", "market": "KR", "decline_pct": -8.0}
             for i in range(1, 51)]
    scores = {f"{i:06d}": {"ranking": [{"return_pct": i / 100.0}]}
              for i in range(1, 51)}
    cfg = serve.serve_cfg({"serve": {
        "enabled": True, "scan_enabled": True, "scan_cap": 10}})
    for reason in ("gap_rebound_scan", "nxt_gap_scan"):
        out, tier = serve.select_candidates(
            items, {"reason": reason}, scores=scores, cfg=cfg)
        assert tier == "scan"
        assert len(out) == 50
        assert serve.scan_shortlist_exempt({"reason": reason})


def test_gap_scan_composite_reason_exempt_from_cap():
    items = [{"symbol": f"{i:06d}", "market": "KR"} for i in range(1, 51)]
    cfg = serve.serve_cfg({"serve": {
        "enabled": True, "scan_enabled": True, "scan_cap": 10}})
    out, tier = serve.select_candidates(
        items, {"reason": "gap_rebound_scan+extra"}, cfg=cfg)
    assert tier == "scan"
    assert len(out) == 50


def test_select_focus_held_and_wake_only():
    items = [{"symbol": f"{i:06d}", "market": "KR", "pool": "day"} for i in range(1, 51)]
    wake = {"reason": "wake_triggers",
            "triggers": [{"kind": "vol_spike", "symbol": "000007"}]}
    cfg = serve.serve_cfg({"serve": {
        "enabled": True, "focus_cap": 16, "focus_pad": 0}})
    out, tier = serve.select_candidates(
        items, wake, held=["000003", "000099"], cfg=cfg)
    assert tier == "focus"
    syms = {c["symbol"] for c in out}
    # 099 는 items 밖 → stub 로라도 포함(플랜: 트리거/보유 항상)
    assert syms == {"000003", "000007", "000099"}
    stub = next(c for c in out if c["symbol"] == "000099")
    assert stub.get("serve_stub") and stub.get("force_include")
    assert "000001" not in syms


def test_select_focus_stub_outside_universe():
    items = [{"symbol": "000001", "market": "KR"}]
    wake = {"reason": "disclosure",
            "triggers": [{"symbol": "005930", "market": "KR", "report_nm": "유증"}]}
    cfg = serve.serve_cfg({"serve": {"enabled": True, "focus_pad": 0}})
    out, _ = serve.select_candidates(items, wake, held=[], cfg=cfg)
    assert {c["symbol"] for c in out} == {"005930"}
    assert out[0].get("force_include")


def test_assemble_force_include_skips_ineligible(monkeypatch):
    from src.agents import features as feat

    def always_bad(sym, market="", name="", **kw):
        return True, "etf"

    monkeypatch.setattr(feat, "is_buy_ineligible", always_bad)
    cands, _ = feat.assemble(
        [{"symbol": "069500", "name": "KODEX", "market": "KR", "force_include": True}],
        {}, None)
    assert len(cands) == 1 and cands[0]["symbol"] == "069500"
    assert cands[0].get("buy_ineligible") == "etf"

    cands2, _ = feat.assemble(
        [{"symbol": "069500", "name": "KODEX", "market": "KR"}],
        {}, None)
    assert cands2 == []


def test_select_focus_pad_and_cap():
    items = [{"symbol": f"{i:06d}", "market": "KR"} for i in range(1, 21)]
    wake = {"reason": "disclosure",
            "triggers": [{"symbol": "000002", "report_nm": "유증"}]}
    cfg = serve.serve_cfg({"serve": {
        "enabled": True, "focus_cap": 4, "focus_pad": 10}})
    out, tier = serve.select_candidates(
        items, wake, held=["000001"], cfg=cfg)
    assert tier == "focus"
    syms = [c["symbol"] for c in out]
    assert "000001" in syms and "000002" in syms
    assert len(out) == 4  # must 2 + pad 2 (cap)


def test_select_focus_must_exceeds_cap():
    items = [{"symbol": f"{i:06d}", "market": "KR"} for i in range(1, 10)]
    held = [f"{i:06d}" for i in range(1, 8)]
    wake = {"reason": "disclosure",
            "triggers": [{"symbol": "000008"}]}
    cfg = serve.serve_cfg({"serve": {
        "enabled": True, "focus_cap": 3, "focus_pad": 0}})
    out, _ = serve.select_candidates(items, wake, held=held, cfg=cfg)
    # must 8 > cap 3 → must 전원 유지
    assert len(out) == 8


def test_held_symbols_from_account_positions():
    open_p = SimpleNamespace(is_open=True)
    closed = SimpleNamespace(is_open=False)
    pos = {"005930": open_p, "000660": closed, "035420": open_p}
    assert serve.held_symbols(pos) == ["005930", "035420"]


def test_merge_wake_keeps_both_symbols():
    a = serve.merge_wake_pending(
        None, "disclosure",
        [{"kind": "disclosure", "symbol": "005930", "report_nm": "실적"}])
    b = serve.merge_wake_pending(
        a, "wake_triggers",
        [{"kind": "vol_spike", "symbol": "000660", "reason": "급변"}])
    syms = {t["symbol"] for t in b["triggers"]}
    assert syms == {"005930", "000660"}
    assert "disclosure" in b["reason"] and "wake_triggers" in b["reason"]


def test_merge_wake_dedupes_same_kind_symbol():
    a = serve.merge_wake_pending(
        None, "wake_triggers",
        [{"kind": "vol_spike", "symbol": "A", "reason": "old"}])
    b = serve.merge_wake_pending(
        a, "wake_triggers",
        [{"kind": "vol_spike", "symbol": "A", "reason": "new"}])
    assert len(b["triggers"]) == 1
    assert b["triggers"][0]["reason"] == "new"


def test_brainworker_wake_coalesce_merges_triggers(tmp_path):
    store = Store(tmp_path / "t.db")
    seen = []

    def cycle(wake=None):
        seen.append(wake)
        return "ok"

    bw = BrainWorker(cycle, store=store)
    bw.wake("disclosure", [{"symbol": "111111", "report_nm": "유증"}])
    bw.wake("wake_triggers", [
        Trigger("vol_spike", "222222", "act", "급변", {"change_pct": 0.03}),
    ])
    assert bw.run_pending() is True
    assert len(seen) == 1
    syms = {t.get("symbol") for t in seen[0]["triggers"]}
    assert syms == {"111111", "222222"}
    assert seen[0]["n"] == 2


def test_patch_flows_does_not_touch_other_fields():
    cands = [
        {"symbol": "005930", "flows": {"foreign_net": 1}, "fundamentals": {"x": 1}},
        {"symbol": "000660", "flows": {"foreign_net": 2}},
    ]
    n = serve.patch_candidate_flows(cands, {"005930": {"foreign_net": 99}})
    assert n == 1
    assert cands[0]["flows"]["foreign_net"] == 99
    assert cands[0]["fundamentals"] == {"x": 1}
    assert cands[1]["flows"]["foreign_net"] == 2


def test_fetch_ondemand_flows_dry_no_http():
    got = serve.fetch_ondemand_flows(["005930", "AAPL"], dry=True)
    assert "005930" in got
    assert "AAPL" not in got  # KR 6자리만


def test_fetch_ondemand_news_kr_and_us(monkeypatch):
    """KR=네이버, US=Finnhub. 네트워크 없이 mock."""
    monkeypatch.setattr(
        "src.datasources.news.fetch_kr_stock_news",
        lambda sym, per=3: [{"title": f"kr-{sym}", "source": "naver"}])
    monkeypatch.setattr(
        "src.datasources.finnhub.fetch_company_news",
        lambda key, sym, per=3: [{"title": f"us-{sym}", "source": "Finnhub",
                                  "date": "2026-01-01"}])
    got = serve.fetch_ondemand_news(
        ["005930", "AAPL", "NVDA"], per=2, finnhub_key="K", spacing_sec=0)
    assert got["005930"][0]["title"] == "kr-005930"
    assert got["AAPL"][0]["title"] == "us-AAPL"
    assert got["NVDA"][0]["symbol"] == "NVDA"


def test_fetch_ondemand_news_skips_us_without_key(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.setattr(
        "src.datasources.news.fetch_kr_stock_news",
        lambda sym, per=3: [{"title": "k", "source": "n"}])
    calls = []

    def boom(*a, **k):
        calls.append(1)
        return []

    monkeypatch.setattr("src.datasources.finnhub.fetch_company_news", boom)
    got = serve.fetch_ondemand_news(
        ["005930", "AAPL"], finnhub_key="", spacing_sec=0)
    assert "005930" in got and "AAPL" not in got
    assert calls == []


def test_build_context_focus_compact_and_headline_limit():
    ms = {"news": [{"source": "t", "title": f"n{i}"} for i in range(80)]}
    wide = build_context(ms, [{"symbol": "1"}], {"cash": 0, "positions": []}, {})
    narrow = build_context(
        ms, [{"symbol": "1"}], {"cash": 0, "positions": []}, {},
        headline_limit=10, compact=True)
    assert len(narrow.encode("utf-8")) < len(wide.encode("utf-8"))
    import json
    assert len(json.loads(narrow)["headlines"]) == 10

"""Athena Phase 2 — 존 근접·이벤트 큐·레벨-only 테스트."""
import json
import time

import numpy as np
import pandas as pd

from src.agents.athena import run_batch, select_symbols
from src.agents.athena_phase2 import (
    enqueue_athena, merge_level_refresh, prices_from_market_state,
    resolve_symbol_prices, scan_athena_triggers, should_level_only,
    sort_covered_by_zone, zone_loc, dossier_ref_price,
)
from src.agents.llm import MockLLM
from src.agents.schemas import DossierLevelOutput, DossierOutput
from src.config import load_config
from src.engine.store import Store


def _df(n=300, base=100.0):
    rng = np.random.default_rng(7)
    close = base * np.cumprod(1 + rng.normal(0.0005, 0.015, n))
    return pd.DataFrame({"time": pd.date_range("2024-01-01", periods=n),
                         "open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": rng.integers(1e5, 1e6, n)})


def test_zone_loc_and_prices():
    assert zone_loc(102, 100, 105) == "in"
    assert zone_loc(95, 100, 105) == "below"
    assert zone_loc(110, 100, 105) == "above"
    ms = {
        "candidates": [{"symbol": "AAA", "price": 50}],
        "fundamentals": {"BBB": {"close": 60}},
        "flows": {"CCC": {"last": 70}},
    }
    px = prices_from_market_state(ms)
    assert px == {"AAA": 50.0, "BBB": 60.0, "CCC": 70.0}


def test_resolve_symbol_prices_cascade(tmp_path):
    """market_state 비어도 history/account 폴백으로 채운다."""
    hist = tmp_path / "history"
    hist.mkdir()
    (hist / "005930.KS_1d_1y.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2026-09-01,1,1,1,100,1\n"
        "2026-09-02,1,1,1,110,1\n",
        encoding="utf-8")
    (hist / "035720.KQ_1d_1y.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2026-09-02,1,1,1,55,1\n",
        encoding="utf-8")
    acct = {"items": [{"symbol": "NVDA", "last": 200.0}]}
    px, meta = resolve_symbol_prices(
        ["005930", "035720", "NVDA", "MISSING"],
        market_state={},
        data_dir=tmp_path,
        account_snapshot=acct,
    )
    assert px["005930"] == 110.0
    assert px["035720"] == 55.0
    assert px["NVDA"] == 200.0
    assert "MISSING" not in px
    assert meta["source_counts"]["history"] >= 2
    assert meta["source_counts"]["account"] == 1
    assert meta["coverage_pct"] == 0.75


def test_sort_covered_by_zone(tmp_path):
    store = Store(tmp_path / "t.db")
    store.save_dossier("IN", "KR", thesis="t", entry_low=98, entry_high=102,
                       invalidation=95, target=110, ttl_hours=60,
                       evidence={"stance": "bullish"})
    store.save_dossier("ABOVE", "KR", thesis="t", entry_low=80, entry_high=85,
                       invalidation=75, target=100, ttl_hours=60,
                       evidence={"stance": "bullish"})
    covered = {"IN": 100.0, "ABOVE": 200.0}
    prices = {"IN": 100.0, "ABOVE": 100.0}
    order = sort_covered_by_zone(["ABOVE", "IN"], store, prices, covered)
    assert order == ["IN", "ABOVE"]


def test_select_symbols_zone_and_queue(tmp_path):
    cfg = load_config()
    cfg.raw.setdefault("athena", {})["min_refresh_hours"] = 0
    cfg.universe["KR"] = [
        {"symbol": "QUE", "name": "q"},
        {"symbol": "INZ", "name": "i"},
        {"symbol": "OLD", "name": "o"},
    ]
    store = Store(tmp_path / "t.db")
    enqueue_athena(store, "QUE", "KR", "gap", gap_pct=3.0)
    store.save_dossier("INZ", "KR", thesis="t", entry_low=98, entry_high=102,
                       invalidation=95, target=110, ttl_hours=60,
                       evidence={"stance": "bullish"})
    store.save_dossier("OLD", "KR", thesis="t", entry_low=50, entry_high=55,
                       invalidation=45, target=70, ttl_hours=60,
                       evidence={"stance": "bullish"})
    ms = {"candidates": [{"symbol": "INZ", "price": 100},
                         {"symbol": "OLD", "price": 100}]}
    order = [t["symbol"] for t in select_symbols(cfg, store, "KR", market_state=ms)]
    assert order.index("QUE") < order.index("INZ")
    assert order.index("INZ") < order.index("OLD")


def test_scan_athena_triggers_gap_and_invalidation(tmp_path):
    store = Store(tmp_path / "t.db")
    p2 = {"enabled": True, "gap_pct": 4.0, "invalidation_near_pct": 0.02,
          "queue_cooldown_hours": 0.01}
    store.save_dossier("GAP", "KR", thesis="t", invalidation=90, target=120,
                       entry_low=95, entry_high=100, ttl_hours=60,
                       evidence={"stance": "bullish"})
    n1 = scan_athena_triggers(store, p2, {"GAP": 105.0},
                              ma20={"GAP": {"close": 100.0}})
    assert n1 == 1
    n2 = scan_athena_triggers(store, p2, {"NEAR": 100.5},
                              ma20={"NEAR": {"close": 100.0}})
    assert n2 == 0
    store.save_dossier("NEAR", "KR", thesis="t", invalidation=99, target=120,
                       entry_low=100, entry_high=105, ttl_hours=60,
                       evidence={"stance": "bullish"})
    n3 = scan_athena_triggers(store, p2, {"NEAR": 100.5},
                              ma20={"NEAR": {"close": 100.0}})
    assert n3 == 1


def test_should_level_only(tmp_path):
    store = Store(tmp_path / "t.db")
    p2 = {"enabled": True, "level_only_max_move_pct": 0.05}
    store.save_dossier("OK", "KR", thesis="old thesis", entry_low=98, entry_high=102,
                       invalidation=95, target=110, ttl_hours=60,
                       evidence={"stance": "bullish", "evidence": ["a"],
                                 "ref_price": 100.0})
    ok, row = should_level_only(store, "OK", 103.0, p2)
    assert ok and row
    ok2, _ = should_level_only(store, "OK", 120.0, p2)
    assert not ok2


def test_should_level_only_above_zone_with_ref_price(tmp_path):
    """진입존 위(above)여도 ref_price 기준이면 level-only 가능."""
    store = Store(tmp_path / "t.db")
    p2 = {"enabled": True, "level_only_max_move_pct": 0.05}
    store.save_dossier("ABOVE", "KR", thesis="t", entry_low=98, entry_high=102,
                       invalidation=95, target=120, ttl_hours=60,
                       evidence={"stance": "bullish", "ref_price": 110.0})
    assert dossier_ref_price({"entry_low": 98, "entry_high": 102,
                              "evidence": {"ref_price": 110.0}}) == 110.0
    ok, _ = should_level_only(store, "ABOVE", 112.0, p2)
    assert ok
    assert not should_level_only(store, "ABOVE", 120.0, p2)[0]


def test_merge_level_refresh():
    prev = {"thesis": "원 thesis", "entry_low": 98, "entry_high": 102,
            "invalidation": 95, "target": 110, "conviction": 0.6,
            "evidence": json.dumps({"stance": "bullish", "horizon": "swing",
                                    "evidence": ["e1"], "key_risks": ["r1"]})}
    lvl = DossierLevelOutput(stance="bullish", entry_low=99, entry_high=103,
                             invalidation=96, target=112, conviction=0.65,
                             level_note="지지 상향")
    out = merge_level_refresh(prev, lvl)
    assert out.thesis.startswith("원 thesis")
    assert "지지 상향" in out.thesis
    assert out.evidence == ["e1"]
    assert out.entry_low == 99


def test_merge_level_refresh_replaces_prior_note():
    prev = {"thesis": "원 thesis [레벨갱신] 이전 메모",
            "evidence": json.dumps({"stance": "bullish", "horizon": "swing",
                                    "evidence": [], "key_risks": []})}
    lvl = DossierLevelOutput(stance="bullish", entry_low=99, entry_high=103,
                             invalidation=96, target=112, conviction=0.65,
                             level_note="신규 메모")
    out = merge_level_refresh(prev, lvl)
    assert out.thesis.count("[레벨갱신]") == 1
    assert "이전 메모" not in out.thesis
    assert "신규 메모" in out.thesis


def test_run_batch_level_only(tmp_path):
    cfg = load_config()
    cfg.universe["KR"] = [{"symbol": "LVL", "name": "l"}]
    store = Store(tmp_path / "t.db")
    df = _df()
    px = float(df["close"].iloc[-1])
    store.save_dossier("LVL", "KR", thesis="keep",
                       entry_low=round(px * 0.99, 2), entry_high=round(px * 1.01, 2),
                       invalidation=round(px * 0.95, 2), target=round(px * 1.10, 2),
                       ttl_hours=60,
                       evidence={"stance": "bullish", "evidence": ["orig"],
                                 "ref_price": round(px, 4)})
    modes: list[str] = []

    def responder(schema, system, user):
        ctx = json.loads(user)
        if schema is DossierLevelOutput:
            modes.append("level")
            return DossierLevelOutput(
                stance="bullish", conviction=0.7,
                entry_low=99, entry_high=103, invalidation=96, target=115,
                level_note="조정")
        modes.append("full")
        px = float((ctx.get("technical") or {}).get("price") or 100)
        return DossierOutput(
            stance="bullish", thesis="full", conviction=0.6,
            entry_low=round(px * 0.98, 2), entry_high=round(px * 1.02, 2),
            invalidation=round(px * 0.95, 2), target=round(px * 1.15, 2))

    s = run_batch(cfg, store, MockLLM(responder), "KR",
                  fetch_df=lambda s_, m: df, only_symbols=["LVL"])
    assert s["done"] == 1 and s["level_only"] == 1
    assert modes == ["level"]
    row = store.get_fresh_dossier("LVL")
    ev = json.loads(row["evidence"])
    assert ev["refresh_mode"] == "level_only"
    assert ev["evidence"] == ["orig"]
    assert ev["ref_price"] == round(px, 4)


def test_dry_llm_level_only_path(tmp_path):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import athena as athena_cli  # noqa: E402

    cfg = load_config()
    cfg.universe["KR"] = [{"symbol": "DRY", "name": "d"}]
    store = Store(tmp_path / "t.db")
    df = _df()
    px = float(df["close"].iloc[-1])
    store.save_dossier("DRY", "KR", thesis="keep",
                       entry_low=round(px * 0.99, 2), entry_high=round(px * 1.01, 2),
                       invalidation=round(px * 0.95, 2), target=round(px * 1.10, 2),
                       ttl_hours=60,
                       evidence={"stance": "bullish", "ref_price": round(px, 4)})
    s = run_batch(cfg, store, athena_cli._dry_llm(), "KR",
                  fetch_df=lambda s_, m: df, only_symbols=["DRY"])
    assert s["done"] == 1 and s["failed"] == 0 and s["level_only"] == 1

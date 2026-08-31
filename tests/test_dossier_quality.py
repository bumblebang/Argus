"""Tier 0 — 도시에 품질 리포트."""
import json
import time

from src.eval.dossier_quality import dossier_stance, summarize_dossiers
from src.engine.store import Store


def test_dossier_stance_from_evidence():
    row = {"evidence": json.dumps({"stance": "neutral"})}
    assert dossier_stance(row) == "neutral"


def test_summarize_empty_store(tmp_path):
    store = Store(tmp_path / "bot.db")
    rep = summarize_dossiers(store, cfg={"universe": {"KR": [{"symbol": "005930"}]}})
    assert rep["fresh_count"] == 0
    assert rep["stance"]["bullish"] == 0


def test_summarize_fresh_dossiers(tmp_path):
    store = Store(tmp_path / "bot.db")
    now = time.time()
    store.save_dossier(
        "005930", "KR", thesis="t",
        entry_low=900, entry_high=950, invalidation=850, target=1100,
        rr=2.0, conviction=0.7,
        evidence={"stance": "bullish", "horizon": "swing"},
        ttl_hours=48)
    store.save_dossier(
        "000660", "KR", thesis="n",
        evidence={"stance": "neutral"},
        ttl_hours=48)
    rep = summarize_dossiers(
        store,
        cfg={"universe": {"KR": [{"symbol": "005930"}, {"symbol": "000660"}]}},
        now=now + 1)
    assert rep["fresh_count"] == 2
    assert rep["stance"]["bullish"] == 1
    assert rep["stance"]["neutral"] == 1
    assert rep["bullish_with_levels"] == 1
    cov = rep["coverage"]["KR"]
    assert cov["fresh"] == 2
    assert cov["universe"] == 2


def test_list_fresh_dossiers_latest_only(tmp_path):
    store = Store(tmp_path / "bot.db")
    store.save_dossier("005930", "KR", thesis="old", ttl_hours=1)
    time.sleep(0.01)
    store.save_dossier("005930", "KR", thesis="new", ttl_hours=48)
    rows = store.list_fresh_dossiers()
    assert len(rows) == 1
    assert rows[0]["thesis"] == "new"

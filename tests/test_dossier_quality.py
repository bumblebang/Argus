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
    # history 폴백으로 존 측정
    hist = tmp_path / "history"
    hist.mkdir()
    (hist / "005930.KS_1d_1y.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n2026-09-01,1,1,1,920,1\n",
        encoding="utf-8")
    rep = summarize_dossiers(
        store,
        cfg={"universe": {"KR": [{"symbol": "005930"}, {"symbol": "000660"}]}},
        data_dir=tmp_path,
        now=now + 1)
    assert rep["fresh_count"] == 2
    assert rep["stance"]["bullish"] == 1
    assert rep["stance"]["neutral"] == 1
    assert rep["bullish_with_levels"] == 1
    assert rep["zone_bullish"]["in"] == 1
    assert rep["zone_unknown_rate"] == 0.0
    assert (rep.get("price_coverage") or {}).get("n") == 1
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


def test_reach_outcomes_buckets_by_target_atr(tmp_path):
    """evidence.reach 없는 과거 도시에도 일봉으로 ATR 을 소급 계산해 구간별 결과."""
    from datetime import datetime, timedelta
    from src.eval.dossier_quality import reach_outcomes

    store = Store(tmp_path / "bot.db")
    hist = tmp_path / "history"
    hist.mkdir()
    lines = ["Date,Open,High,Low,Close,Volume"]
    d0 = datetime(2026, 1, 1)
    for i in range(70):            # 폭 ±1 → ATR 2%. 창(생성+20일)이 다 지나도록 70일
        day = (d0 + timedelta(days=i)).strftime("%Y-%m-%d")
        hi = 105.0 if i == 45 else 101.0                 # 45일째 하루 105 터치
        lines.append(f"{day},100,{hi},99,100,1")
    (hist / "005930.KS_1d_1y.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    created = datetime(2026, 2, 10, 12).timestamp()      # 40일째
    for tgt in (104.0, 120.0):                           # 2 ATR(도달) / 10 ATR(미도달)
        store.save_dossier("005930", "KR", thesis="t", entry_low=99, entry_high=101,
                           invalidation=90, target=tgt, rr=2.0, conviction=0.6,
                           evidence={"stance": "bullish", "horizon": "swing"},
                           ttl_hours=48)
    store.conn.execute("UPDATE dossiers SET created_at=?", (created,))
    store.conn.commit()
    rep = reach_outcomes(store, data_dir=tmp_path, cfg=None,
                         since=created - 1, now=created + 1)
    b = rep["by_bucket"]
    assert b["<3"]["target_first"] == 1 and b["<3"]["n_symbols"] == 1
    assert b["6+"]["neither_by_horizon"] == 1 and b["6+"]["target_rate"] == 0.0

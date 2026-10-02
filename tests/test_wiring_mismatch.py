"""배선 mismatch 카운터 단위 테스트."""
import json
from datetime import datetime, timezone
from pathlib import Path

from src.eval.wiring_mismatch import classify_buy, summarize_wiring


def test_classify_fit_vs_assigned():
    hits = classify_buy(
        {"symbol": "AAA", "side": "BUY", "strategy": "macd", "horizon": "swing"},
        {"strategy_fit": {"best": "rsi_reversion", "thin_sample": False},
         "pool": "swing"},
    )
    kinds = [h["kind"] for h in hits]
    assert "fit_vs_assigned" in kinds


def test_classify_skips_thin_fit():
    hits = classify_buy(
        {"symbol": "AAA", "side": "BUY", "strategy": "macd", "horizon": "swing"},
        {"strategy_fit": {"best": None, "thin_sample": True}, "pool": "swing"},
    )
    assert not any(h["kind"] == "fit_vs_assigned" for h in hits)


def test_classify_horizon_vs_catalog():
    hits = classify_buy(
        {"symbol": "AAA", "side": "BUY", "strategy": "volatility_breakout",
         "horizon": "swing"},
        {"pool": "swing"},
    )
    assert any(h["kind"] == "horizon_vs_catalog" for h in hits)


def test_classify_close_scan_day():
    hits = classify_buy(
        {"symbol": "005930", "side": "BUY", "strategy": "volatility_breakout",
         "horizon": "day"},
        {"pool": "close_scan"},
    )
    assert any(h["kind"] == "close_scan_day_overlap" for h in hits)


def test_summarize_from_journal(tmp_path):
    journal = tmp_path / "decisions.jsonl"
    # minimal cycle without archive → still counts BUY, no fit mismatch
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "proposals": [{
            "symbol": "005930", "market": "KR", "side": "BUY",
            "strategy": "macd", "horizon": "swing",
        }],
        "verdicts": [], "executed": [],
    }
    journal.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    rep = summarize_wiring(journal, window_days=14, threshold=3, data_dir=tmp_path)
    assert rep["buy_n"] == 1
    assert rep["actionable"] is False


def test_classify_fit_cross_horizon_is_info_not_mismatch():
    # fit best 가 day 전략인데 뇌는 swing 을 정함 → horizon 맞춤 규칙 준수(참고용)
    hits = classify_buy(
        {"symbol": "AAA", "side": "BUY", "strategy": "macd", "horizon": "swing"},
        {"strategy_fit": {"best": "volatility_breakout", "thin_sample": False},
         "pool": "swing"},
    )
    kinds = [h["kind"] for h in hits]
    assert "fit_cross_horizon" in kinds
    assert "fit_vs_assigned" not in kinds


def test_classify_fit_same_horizon_still_mismatch():
    hits = classify_buy(
        {"symbol": "AAA", "side": "BUY", "strategy": "macd", "horizon": "swing"},
        {"strategy_fit": {"best": "bollinger_reversion", "thin_sample": False}},
    )
    assert any(h["kind"] == "fit_vs_assigned" for h in hits)


def test_summarize_cross_horizon_not_actionable(tmp_path, monkeypatch):
    import src.eval.wiring_mismatch as wm
    now = datetime.now(timezone.utc)
    rec = {"ts": now.isoformat()}
    cross = {"strategy_fit": {"best": "volatility_breakout", "thin_sample": False}}
    rows = [(rec, {"symbol": f"S{i}", "side": "BUY", "strategy": "macd",
                   "horizon": "swing"}, cross) for i in range(4)]
    rows.append((rec, {"symbol": "R", "side": "BUY", "strategy": "rsi_reversion",
                       "horizon": "swing"}, None))
    monkeypatch.setattr(wm, "iter_journal_buys", lambda *a, **k: iter(rows))
    rep = wm.summarize_wiring(tmp_path / "x.jsonl", threshold=3, now=now,
                              data_dir=tmp_path)
    assert rep["info_by_kind"] == {"fit_cross_horizon": 4}
    assert rep["mismatch_n"] == 0
    assert rep["actionable"] is False
    assert rep["assigned_by_strategy"]["macd/swing"] == 4
    assert rep["top_assigned_share"] == 0.8

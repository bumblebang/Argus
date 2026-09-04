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

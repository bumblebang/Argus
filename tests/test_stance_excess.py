"""판정 라벨별 지수 대비 초과수익(eval.stance_excess) 단위 테스트."""
import json
from datetime import datetime, timedelta

from src.eval.stance_excess import load_value_history, stance_excess


def _write(hist, sym, closes, d0=datetime(2026, 1, 1)):
    lines = ["Date,Open,High,Low,Close,Volume"]
    for i, c in enumerate(closes):
        day = (d0 + timedelta(days=i)).strftime("%Y-%m-%d")
        lines.append(f"{day},{c},{c},{c},{c},1")
    (hist / f"{sym}_1d_1y.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _setup(tmp_path):
    hist = tmp_path / "history"
    hist.mkdir()
    _write(hist, "SPY", [100 + i * 0.1 for i in range(60)])          # 지수 +
    _write(hist, "UP", [100 + i for i in range(60)])                  # 크게 +
    _write(hist, "DOWN", [100 - i * 0.5 for i in range(60)])          # -
    return tmp_path


def test_excess_vs_benchmark_by_label(tmp_path):
    data = _setup(tmp_path)
    obs = [{"symbol": "UP", "ts": "2026-01-05", "label": "bullish"},
           {"symbol": "DOWN", "ts": "2026-01-05", "label": "neutral"}]
    rep = stance_excess(obs, data_dir=data, horizon="swing")
    us = rep["by_market"]["US"]
    assert us["bullish"]["excess_avg_pp"] > 0 > us["neutral"]["excess_avg_pp"]
    assert us["bullish"]["beat_rate"] == 1.0 and us["bullish"]["small_sample"] is True


def test_episode_dedupe_and_immature_skip(tmp_path):
    data = _setup(tmp_path)
    obs = [{"symbol": "UP", "ts": "2026-01-05", "label": "bullish"},
           {"symbol": "UP", "ts": "2026-01-07", "label": "bullish"},   # 같은 창 → 접음
           {"symbol": "UP", "ts": "2026-02-25", "label": "bullish"}]   # 창 끝 > 데이터 → 미성숙
    rep = stance_excess(obs, data_dir=data, horizon="swing")
    assert rep["by_market"]["US"]["bullish"]["n"] == 1
    assert rep["skipped"] == {"dup_episode": 1, "immature_or_no_data": 1}


def test_load_value_history_since(tmp_path):
    p = tmp_path / "h.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [
        {"ts": 100.0, "symbol": "A", "stance": "fair"},
        {"ts": 200.0, "symbol": "B", "stance": "undervalued"},
        "garbage"]) + "\n", encoding="utf-8")
    rows = load_value_history(p, since=150.0)
    assert rows == [{"symbol": "B", "ts": 200.0, "label": "undervalued"}]

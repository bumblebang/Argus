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


def test_stance_track_record_and_prompt_rev_filter(tmp_path):
    from src.engine.store import Store
    from src.eval.stance_excess import dossier_observations, stance_track_record
    data = _setup(tmp_path)
    store = Store(tmp_path / "t.db")
    created = datetime(2026, 1, 5, 12).timestamp()
    for sym, stance, rev in (("UP", "bullish", "aaa"), ("DOWN", "neutral", "bbb")):
        store.save_dossier(sym, "US", thesis="t", entry_low=1, entry_high=2,
                           invalidation=0.5, target=3, rr=2.0, conviction=0.6,
                           evidence={"stance": stance, "prompt_rev": rev}, ttl_hours=48)
    store.conn.execute("UPDATE dossiers SET created_at=?", (created,))
    store.conn.commit()
    now = created + 86400
    rec = stance_track_record(store, data_dir=data, since_days=30, now=now)
    us = rec["by_market"]["US"]
    assert us["bullish"]["excess_avg_pp"] > 0 > us["neutral"]["excess_avg_pp"]
    assert rec["by_symbol"]["UP"][0]["stance"] == "bullish"
    only = dossier_observations(store, since=created - 1, now=now, prompt_rev="aaa")
    assert [o["symbol"] for o in only] == ["UP"]


def test_stance_track_record_basis_switches_to_current_prompt(tmp_path):
    from src.engine.store import Store
    from src.eval.stance_excess import stance_track_record
    data = _setup(tmp_path)
    store = Store(tmp_path / "t.db")
    created = datetime(2026, 1, 5, 12).timestamp()
    for sym, rev in (("UP", "old"), ("DOWN", "new")):
        store.save_dossier(sym, "US", thesis="t", entry_low=1, entry_high=2,
                           invalidation=0.5, target=3, rr=2.0, conviction=0.6,
                           evidence={"stance": "bullish", "prompt_rev": rev}, ttl_hours=48)
    store.conn.execute("UPDATE dossiers SET created_at=?", (created,))
    store.conn.commit()
    now = created + 86400
    # 현재 판본 표본 충분(min_current=1) → 현재 판본만(DOWN, 음수)
    cur = stance_track_record(store, data_dir=data, since_days=30, now=now,
                              prompt_rev="new", min_current=1)
    assert cur["basis"]["US"] == "current_prompt"
    assert cur["by_market"]["US"]["bullish"]["n"] == 1
    assert cur["by_market"]["US"]["bullish"]["excess_avg_pp"] < 0
    # 부족(min_current=5) → 전체 rolling (UP+DOWN)
    roll = stance_track_record(store, data_dir=data, since_days=30, now=now,
                               prompt_rev="new", min_current=5)
    assert roll["basis"]["US"] == "rolling"
    assert roll["by_market"]["US"]["bullish"]["n"] == 2

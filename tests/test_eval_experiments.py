"""등록 실험 자동 채점·bullish 하한 감시(eval.experiments) 테스트."""
import json
from datetime import datetime, timedelta

from src.engine.store import Store
from src.eval.experiments import (alert_lines, bullish_floor_check, compute_metric,
                                  dedupe_daily, evaluate_experiments)
from src.eval_protocol import load_registry, save_registry

METRIC = "dossier.US.bullish_minus_neutral_excess_pp"


def _hist(tmp_path):
    hist = tmp_path / "history"
    hist.mkdir()
    d0 = datetime(2026, 1, 1)

    def w(sym, f):
        lines = ["Date,Open,High,Low,Close,Volume"]
        for i in range(60):
            c = f(i)
            lines.append(f"{(d0 + timedelta(days=i)):%Y-%m-%d},{c},{c},{c},{c},1")
        (hist / f"{sym}_1d_1y.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    w("SPY", lambda i: 100 + i * 0.1)
    w("UP", lambda i: 100 + i)
    w("DOWN", lambda i: 100 - i * 0.5)
    return tmp_path


def _seed(store, rows, created):
    for sym, stance, rev in rows:
        store.save_dossier(sym, "US", thesis="t", entry_low=1, entry_high=2,
                           invalidation=0.5, target=3, rr=2.0, conviction=0.6,
                           evidence={"stance": stance, "prompt_rev": rev}, ttl_hours=48)
    store.conn.execute("UPDATE dossiers SET created_at=?", (created,))
    store.conn.commit()


def _registry(tmp_path, created, min_n=1):
    reg = tmp_path / "eval_registry.json"
    save_registry({"version": 1, "experiments": [{
        "id": "exp_t", "name": "athena_stance_feedback", "metric": METRIC,
        "kill": {"metric": METRIC, "op": "<", "threshold": 0.0, "min_n": min_n},
        "min_n": min_n, "touches": ["athena"], "status": "running",
        "created_at": created - 1, "prompt_rev": "new"}]}, reg)
    return reg


def test_compute_metric_prompt_rev_only(tmp_path):
    data = _hist(tmp_path)
    store = Store(tmp_path / "t.db")
    created = datetime(2026, 1, 5, 12).timestamp()
    # 새 판본: bullish=DOWN, neutral=UP → 차이 음수. 옛 판본 bullish=UP 은 제외돼야 함.
    _seed(store, [("DOWN", "bullish", "new"), ("UP", "neutral", "new"),
                  ("UP", "bullish", "old")], created)
    exp = load_registry(_registry(tmp_path, created))["experiments"][0]
    m = compute_metric(exp, store=store, data_dir=data)
    assert m[METRIC] < 0 and m[f"{METRIC}__n"] == 1
    assert compute_metric({"metric": "shadow.avg_ret_pct"}, store=store, data_dir=data) is None


def test_evaluate_kills_and_alerts_with_rollback_hint(tmp_path):
    data = _hist(tmp_path)
    store = Store(tmp_path / "t.db")
    created = datetime(2026, 1, 5, 12).timestamp()
    _seed(store, [("DOWN", "bullish", "new"), ("UP", "neutral", "new")], created)
    reg = _registry(tmp_path, created, min_n=1)
    defs_before = reg.read_text(encoding="utf-8")
    res = evaluate_experiments(store=store, data_dir=data, registry_path=reg)
    assert res["evaluated"][0]["status"] == "kill"
    lines = alert_lines(res, [])
    assert any("kill" in ln for ln in lines)
    assert any("stance_feedback: false" in ln for ln in lines)
    assert reg.read_text(encoding="utf-8") == defs_before        # 정의 파일 불변
    # 이미 kill → 다음 실행은 채점·알림 없음
    res2 = evaluate_experiments(store=store, data_dir=data, registry_path=reg)
    assert res2["evaluated"] == [] and alert_lines(res2, []) == []


def test_evaluate_small_sample_is_shadow_only(tmp_path):
    data = _hist(tmp_path)
    store = Store(tmp_path / "t.db")
    created = datetime(2026, 1, 5, 12).timestamp()
    _seed(store, [("DOWN", "bullish", "new"), ("UP", "neutral", "new")], created)
    reg = _registry(tmp_path, created, min_n=30)
    res = evaluate_experiments(store=store, data_dir=data, registry_path=reg)
    assert res["evaluated"][0]["status"] == "shadow_only"
    assert load_registry(reg)["experiments"][0]["evidence_n"] == 1


def test_bullish_floor_check(tmp_path):
    store = Store(tmp_path / "t.db")
    for i in range(12):
        stance = "bullish" if i == 0 else "neutral"
        store.save_dossier(f"S{i}", "KR", thesis="t", entry_low=1, entry_high=2,
                           invalidation=0.5, target=3, rr=2.0, conviction=0.6,
                           evidence={"stance": stance}, ttl_hours=48)
    hits = bullish_floor_check(store, cfg={"athena": {"bullish_floor_pct": 0.10}})
    assert hits and hits[0]["market"] == "KR" and hits[0]["bullish"] == 1
    assert bullish_floor_check(store, cfg={"athena": {"bullish_floor_pct": 0.05}}) == []
    assert "과보정" in alert_lines({}, hits)[0]


def test_bullish_floor_only_current_prompt_rev(tmp_path):
    store = Store(tmp_path / "t.db")
    for i in range(12):        # 옛 판본은 bullish 0% — 새 판본 표본이 없으면 위반 아님
        store.save_dossier(f"S{i}", "KR", thesis="t", evidence={"stance": "neutral",
                                                              "prompt_rev": "old"},
                           ttl_hours=48)
    cfg = {"athena": {"bullish_floor_pct": 0.10}}
    assert bullish_floor_check(store, cfg=cfg, prompt_rev="new") == []
    for i in range(10):
        store.save_dossier(f"N{i}", "KR", thesis="t", evidence={"stance": "neutral",
                                                              "prompt_rev": "new"},
                           ttl_hours=48)
    hits = bullish_floor_check(store, cfg=cfg, prompt_rev="new")
    assert hits[0]["fresh"] == 10 and hits[0]["other_share"] == 0.0
    assert "이전 판본 0%" in alert_lines({}, hits)[0]


def test_shadow_only_transition_not_alerted():
    res = {"changed": [{"id": "e", "status": "shadow_only", "status_reason": "n=0"}]}
    assert alert_lines(res, []) == []


def test_dedupe_daily(tmp_path):
    p = tmp_path / "s.json"
    assert dedupe_daily(["a", "b"], p, now=1_790_000_000) == ["a", "b"]
    assert dedupe_daily(["a", "c"], p, now=1_790_000_100) == ["c"]
    assert dedupe_daily(["a"], p, now=1_790_000_000 + 86400 * 2) == ["a"]

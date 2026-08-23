"""베이스레이트 마이닝 + dossiers 저장소 + 피처 주입 테스트."""
import numpy as np
import pandas as pd

from src.baserate import (SETUPS, analyze, brief, breakout_pullback,
                          golden_cross, pullback_reversal, setup_stats)
from src.engine.store import Store


def _df(closes) -> pd.DataFrame:
    c = pd.Series(closes, dtype=float)
    return pd.DataFrame({"time": pd.date_range("2024-01-01", periods=len(c)),
                         "open": c, "high": c * 1.01, "low": c * 0.99,
                         "close": c, "volume": 1000})


def _crash_then_bounce(n_pre=80):
    """횡보 후 급락(-20%/10봉) → pullback_reversal 발동 지점을 만든다."""
    flat = [100.0] * n_pre
    crash = list(np.linspace(100, 78, 12))       # 12봉에 걸쳐 -22%
    bounce = list(np.linspace(78, 92, 15))
    return _df(flat + crash + bounce)


# ── 셋업 검출 ──────────────────────────────────────────────────────
def test_pullback_reversal_fires_on_crash():
    df = _crash_then_bounce()
    fired = pullback_reversal(df).fillna(False)
    assert fired.any()                            # 급락+과매도 구간에서 발동
    flat_only = _df([100.0] * 100)
    assert not pullback_reversal(flat_only).fillna(False).any()   # 횡보에선 미발동


def test_golden_cross_fires_on_turn():
    down = list(np.linspace(120, 100, 40))
    up = list(np.linspace(100, 130, 40))
    fired = golden_cross(_df(down + up)).fillna(False)
    assert fired.any()


def test_breakout_pullback_fires_after_new_high_retrace():
    base = [100.0] * 30
    rally = list(np.linspace(100, 120, 10))       # 20봉 신고가 돌파
    pull = [118, 119.5, 118.5]                    # 돌파 레벨 ±3% 눌림
    fired = breakout_pullback(_df(base + rally + pull)).fillna(False)
    assert fired.iloc[-1] or fired.iloc[-2]       # 눌림 구간에서 발동


# ── 통계 ───────────────────────────────────────────────────────────
def test_setup_stats_dedups_consecutive_and_measures_forward():
    closes = [100.0] * 100
    df = _df(closes)
    fired = pd.Series([False] * 100)
    fired.iloc[50] = fired.iloc[51] = fired.iloc[52] = True   # 연속 3봉 발동
    fired.iloc[90] = True
    df.loc[55:, "close"] = 110.0                  # 첫 에피소드 +10%
    stats = setup_stats(df, fired, horizons=(5,))
    s = stats["5"]
    assert s["n"] == 2                            # 에피소드 2개(연속봉 dedup, 90번은 95<100 OK)
    assert s["win_rate"] == 0.5                   # 50→110(승), 90→110에서 110(보합=패)
    # 지평 미도래(끝부분) 표본 제외 확인: 98번에 발동 추가해도 n 안 늘어남
    fired.iloc[98] = True
    assert setup_stats(df, fired, horizons=(5,))["5"]["n"] == 2


def test_analyze_reports_active_now_and_small_sample():
    df = _crash_then_bounce()
    # 급락 직후 시점으로 자르면 마지막 봉이 셋업 상태
    cut = df[pullback_reversal(df).fillna(False)].index[0] + 1
    res = analyze(df.iloc[:cut].reset_index(drop=True))
    assert "pullback_reversal" in res["active_now"]
    info = res["setups"]["pullback_reversal"]
    assert info["active_now"] and info["small_sample"]        # 표본 1~2건


def test_analyze_short_history_returns_empty():
    assert analyze(_df([100] * 10)) == {"setups": {}, "active_now": []}


def test_brief_only_active_setups_json_roundtrip():
    import json
    df = _crash_then_bounce()
    cut = df[pullback_reversal(df).fillna(False)].index[0] + 1
    res = json.loads(json.dumps(analyze(df.iloc[:cut].reset_index(drop=True))))
    b = brief(res)                                # JSON 왕복 후에도 동작(키 문자열)
    assert "pullback_reversal" in b
    assert set(b["pullback_reversal"]) >= {"n", "win_rate", "avg_ret_pct", "small_sample"}
    assert brief({"active_now": [], "setups": {}}) is None    # 활성 없으면 None


# ── dossiers 저장소 ────────────────────────────────────────────────
def test_dossier_save_and_fresh_lookup(tmp_path):
    store = Store(tmp_path / "t.db")
    store.save_dossier("005930", "KR", thesis="반도체 업사이클 초입", entry_low=68000,
                       entry_high=71000, invalidation=65000, target=82000, rr=2.8,
                       conviction=0.7, evidence={"base_rate": "pullback 62%"},
                       ttl_hours=48)
    row = store.get_fresh_dossier("005930")
    assert row["invalidation"] == 65000 and row["rr"] == 2.8
    assert store.get_fresh_dossier("없는종목") is None


def test_dossier_expiry(tmp_path):
    store = Store(tmp_path / "t.db")
    store.save_dossier("005930", "KR", thesis="x", ttl_hours=1)
    assert store.get_fresh_dossier("005930") is not None
    two_hours_later = __import__("time").time() + 7200
    assert store.get_fresh_dossier("005930", now=two_hours_later) is None   # 만료


def test_dossier_coverage_oldest_first(tmp_path):
    store = Store(tmp_path / "t.db")
    store.save_dossier("AAA", "KR", thesis="old")
    store.save_dossier("BBB", "KR", thesis="new")
    cov = store.dossier_coverage()
    assert [r["symbol"] for r in cov] == ["AAA", "BBB"]       # 오래된 것부터


# ── 피처 주입 ──────────────────────────────────────────────────────
def test_assemble_attaches_base_rates():
    from src.agents.features import assemble
    df = _crash_then_bounce()
    cut = df[pullback_reversal(df).fillna(False)].index[0] + 1
    import json
    br = {"005930": json.loads(json.dumps(analyze(df.iloc[:cut].reset_index(drop=True))))}
    items = [{"symbol": "005930", "name": "삼성전자", "market": "KR"}]
    cands, _ = assemble(items, {}, None, base_rates=br)
    assert "base_rates" in cands[0]
    assert "pullback_reversal" in cands[0]["base_rates"]
    # 활성 셋업 없는 종목은 필드 자체가 없음(토큰 절약)
    quiet = {"005930": analyze(_df([100.0] * 100))}
    cands2, _ = assemble(items, {}, None, base_rates=quiet)
    assert "base_rates" not in cands2[0]

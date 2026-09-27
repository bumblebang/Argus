"""도시에 목표가 도달 가능성(ATR 배수) — 관찰 모드 기본, enforce 스위치."""
import json

import numpy as np
import pandas as pd

from src.agents.athena import run_batch
from src.agents.dossier_reach import (apply_reach, atr_pct_from_bars, atr_pct_from_df,
                                      level_reach, reach_bucket, reach_cfg)
from src.agents.llm import MockLLM
from src.agents.schemas import DossierOutput
from src.config import load_config
from src.engine.store import Store
from src.indicators import atr


def _flat_df(n=40, px=100.0, rng_pct=0.02):
    close = pd.Series([px] * n)
    return pd.DataFrame({"high": close * (1 + rng_pct / 2), "low": close * (1 - rng_pct / 2),
                         "close": close})


def _dossier(**over):
    base = dict(stance="bullish", thesis="t", conviction=0.6,
                entry_low=98, entry_high=102, invalidation=95, target=120)
    base.update(over)
    return DossierOutput(**base)


def test_atr_constant_range():
    df = _flat_df()
    a = atr(df["high"], df["low"], df["close"], 14)
    assert abs(float(a.iloc[-1]) - 2.0) < 1e-9
    assert abs(atr_pct_from_df(df) - 0.02) < 1e-9
    assert atr_pct_from_df(_flat_df(n=10)) is None       # 봉 부족


def test_atr_pct_from_bars_matches_df():
    bars = [(None, 101.0, 99.0, 100.0)] * 40
    assert abs(atr_pct_from_bars(bars) - 0.02) < 1e-9


def test_level_reach_multiples():
    r = level_reach(entry_low=98, entry_high=102, invalidation=96, target=110, atr_pct=0.02)
    assert r == {"atr_pct": 0.02, "tgt_atr": 5.0, "stop_atr": 2.0}
    assert level_reach(entry_low=98, entry_high=102, invalidation=96, target=110,
                       atr_pct=None) is None


def test_reach_bucket_edges():
    assert [reach_bucket(x) for x in (None, 2.9, 3.0, 4.0, 6.0)] == [
        "unknown", "<3", "3-4", "4-6", "6+"]


def test_apply_reach_observe_keeps_stance_enforce_downgrades():
    far = {"tgt_atr": 7.0}
    d, notes = apply_reach(_dossier(), far, reach_cfg({}))
    assert d.stance == "bullish" and "관찰 모드" in notes[0]
    d, notes = apply_reach(_dossier(), far,
                           reach_cfg({"athena": {"reach": {"mode": "enforce"}}}))
    assert d.stance == "neutral" and "강등" in notes[0]
    d, notes = apply_reach(_dossier(), {"tgt_atr": 3.5},
                           reach_cfg({"athena": {"reach": {"mode": "enforce"}}}))
    assert d.stance == "bullish" and notes == []


def test_reach_cfg_defaults_and_bad_mode():
    assert reach_cfg({}) == {"mode": "observe", "max_target_atr": 4.0, "atr_period": 14}
    assert reach_cfg({"athena": {"reach": {"mode": "yolo"}}})["mode"] == "observe"


# ── run_batch 배선 ─────────────────────────────────────────

def _df(n=120):
    rng = np.random.default_rng(3)
    close = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    return pd.DataFrame({"time": pd.date_range("2024-01-01", periods=n),
                         "open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": rng.integers(1e5, 1e6, n)})


def _llm(target_mult):
    def responder(schema, system, user):
        px = float((json.loads(user).get("technical") or {}).get("price") or 100)
        return DossierOutput(stance="bullish", thesis="t", conviction=0.6,
                             entry_low=round(px * 0.99, 2), entry_high=round(px * 1.01, 2),
                             invalidation=round(px * 0.96, 2),
                             target=round(px * target_mult, 2))
    return MockLLM(responder)


def _run(tmp_path, mode, target_mult):
    cfg = load_config()
    cfg.raw.setdefault("athena", {})["reach"] = {"mode": mode}
    cfg.universe["KR"] = [{"symbol": "AAA", "name": "a"}]
    store = Store(tmp_path / f"{mode}.db")
    run_batch(cfg, store, _llm(target_mult), "KR", fetch_df=lambda s, m: _df())
    return json.loads(store.get_fresh_dossier("AAA")["evidence"])


def test_run_batch_observe_stamps_reach_without_downgrade(tmp_path):
    ev = _run(tmp_path, "observe", 1.30)                  # ~2.4% ATR 에 +30% → 10+ ATR
    assert ev["stance"] == "bullish"
    assert ev["reach"]["mode"] == "observe" and ev["reach"]["tgt_atr"] > 4
    assert any("관찰 모드" in n for n in ev["sanitize_notes"])


def test_run_batch_enforce_downgrades_far_target(tmp_path):
    ev = _run(tmp_path, "enforce", 1.30)
    assert ev["stance"] == "neutral"
    (tmp_path / "near").mkdir()
    near = _run(tmp_path / "near", "enforce", 1.05)
    assert near["stance"] == "bullish" and near["reach"]["tgt_atr"] <= 4


def test_run_batch_off_skips_reach(tmp_path):
    ev = _run(tmp_path, "off", 1.30)
    assert "reach" not in ev and ev["stance"] == "bullish"

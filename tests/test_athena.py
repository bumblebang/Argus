"""Athena 딥리서치 — 레벨 하드가드·우선순위·배치 실행·창 판별 테스트."""
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from src.agents.athena import (build_research_context, compute_rr, run_batch,
                               sanitize, select_symbols, technical_summary)
from src.agents.llm import MockLLM
from src.agents.schemas import DossierOutput
from src.config import load_config
from src.engine.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import athena as athena_cli  # noqa: E402

KST = ZoneInfo("Asia/Seoul")


def _df(n=300, base=100.0):
    rng = np.random.default_rng(7)
    close = base * np.cumprod(1 + rng.normal(0.0005, 0.015, n))
    return pd.DataFrame({"time": pd.date_range("2024-01-01", periods=n),
                         "open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": rng.integers(1e5, 1e6, n)})


def _dossier(**over) -> DossierOutput:
    base = dict(stance="bullish", thesis="t", conviction=0.7,
                entry_low=100, entry_high=105, invalidation=95, target=120)
    base.update(over)
    return DossierOutput(**base)


# ── 레벨 하드가드 ──────────────────────────────────────────────────
def test_sanitize_valid_bullish_passes():
    d, notes = sanitize(_dossier())
    assert d.stance == "bullish" and notes == []


def test_sanitize_with_price_in_band_passes():
    d, notes = sanitize(_dossier(), price=102.5)
    assert d.stance == "bullish" and notes == []


def test_sanitize_invalidation_too_close():
    d, notes = sanitize(_dossier(invalidation=99.8, entry_low=100), price=100)
    assert d.stance == "neutral" and any("가까움" in n for n in notes)


def test_sanitize_invalidation_too_far():
    d, notes = sanitize(_dossier(invalidation=50, entry_low=90), price=100)
    assert d.stance == "neutral" and any("멀음" in n for n in notes)


def test_sanitize_zone_too_wide():
    d, notes = sanitize(_dossier(entry_low=80, entry_high=100, invalidation=75,
                                 target=120), price=100)
    assert d.stance == "neutral" and any("넓음" in n for n in notes)


def test_sanitize_target_too_far():
    d, notes = sanitize(_dossier(target=200), price=100)
    assert d.stance == "neutral" and any("목표가" in n for n in notes)


def test_sanitize_missing_levels_downgrades():
    d, notes = sanitize(_dossier(target=None))
    assert d.stance == "neutral" and notes


def test_sanitize_bad_ordering_downgrades():
    d, notes = sanitize(_dossier(invalidation=110))     # 무효화가 진입 위 = 모순
    assert d.stance == "neutral" and "순서" in notes[0]


def test_sanitize_neutral_untouched():
    d, notes = sanitize(_dossier(stance="neutral", entry_low=None, entry_high=None,
                                 invalidation=None, target=None))
    assert d.stance == "neutral" and notes == []


def test_compute_rr():
    assert compute_rr(100, 105, 95, 120) == 2.33        # (120-102.5)/(102.5-95)
    assert compute_rr(100, 105, 110, 120) is None       # risk<=0
    assert compute_rr(None, 105, 95, 120) is None


def test_technical_summary_fields():
    t = technical_summary(_df())
    assert {"price", "rsi14", "pct_from_52w_high", "vs_sma20_pct"} <= set(t)
    assert technical_summary(_df(30)) == {}             # 히스토리 부족


def test_build_research_context_compact():
    ctx = build_research_context("005930", "삼성전자", "KR", history_df=_df(),
                                 market_state={"news": [], "fundamentals": {},
                                               "flows": {}, "regime": {"KR": "neutral"}})
    assert ctx["symbol"] == "005930" and ctx["technical"]
    assert "base_rates" in ctx                          # 히스토리에서 직접 계산됨
    # focus 는 market_state 에 없어도 코드가 계산해 넣는다(예전 dead wire 해소).
    assert ctx["focus"] is not None and "lenses" in ctx["focus"]


def test_build_research_context_uses_injected_focus():
    """run_batch 가 창 단위 focus 를 넘기면 그걸 그대로 쓴다."""
    injected = {"asof": "x", "lenses": [{"id": "fomc", "kind": "macro_event"}],
                "summary": "test"}
    ctx = build_research_context(
        "005930", "삼성전자", "KR", history_df=_df(),
        market_state={"news": [], "fundamentals": {}, "flows": {}},
        focus=injected)
    assert ctx["focus"] is injected


def test_build_research_context_carries_macro_and_earnings():
    """US 거시(FRED) + 실적 슬롯이 Athena 컨텍스트에 실린다."""
    ms = {"macro": {"fed_funds": 3.63, "cpi_yoy": 0.035},
          "macro_kr": {"bok_base_rate": 2.75},
          "news": [], "fundamentals": {}, "flows": {}}
    earn = {"market": "US", "symbol": "NVDA", "date": "2026-08-28", "dday": 1,
            "consensus": {"eps": 1.0}}
    ers = [{"symbol": "NVDA", "eps_surprise_pct": 8.2, "route": "queue",
            "market": "US"}]
    ctx = build_research_context(
        "NVDA", "NVIDIA", "US", history_df=_df(), market_state=ms,
        earnings=earn, earnings_results=ers)
    assert ctx["macro"]["fed_funds"] == 3.63
    assert ctx["macro_kr"]["bok_base_rate"] == 2.75
    assert ctx["earnings"]["dday"] == 1
    assert ctx["earnings_results"][0]["eps_surprise_pct"] == 8.2


def test_athena_prompt_mentions_us_macro_and_earnings():
    from src.agents.athena import ATHENA_SYSTEM as ATH
    assert "macro(FRED" in ATH
    assert "earnings_results" in ATH
    assert "macro_kr 로 US" in ATH


# ── 우선순위 ───────────────────────────────────────────────────────
def test_select_symbols_priority(tmp_path):
    cfg = load_config()
    cfg.universe["KR"] = [{"symbol": "AAA", "name": "a"}, {"symbol": "BBB", "name": "b"},
                          {"symbol": "CCC", "name": "c"}]
    store = Store(tmp_path / "t.db")
    store.open_position("CCC", "KR", 1, 100)            # 보유 → 최우선
    store.save_dossier("AAA", "KR", thesis="old")       # AAA 커버됨 → 맨 뒤
    order = [t["symbol"] for t in select_symbols(cfg, store, "KR")]
    assert order == ["CCC", "BBB", "AAA"]               # 보유 > 미커버 > 기존커버


# ── 배치 실행 ──────────────────────────────────────────────────────
def _mock_llm(stance="bullish"):
    def responder(schema, system, user):
        ctx = json.loads(user)
        px = float((ctx.get("technical") or {}).get("price") or 100)
        return DossierOutput(
            stance=stance, thesis=f"{ctx['symbol']} 테스트", conviction=0.6,
            entry_low=round(px * 0.98, 2), entry_high=round(px * 1.02, 2),
            invalidation=round(px * 0.95, 2), target=round(px * 1.15, 2))
    return MockLLM(responder)


def test_run_batch_saves_dossiers(tmp_path):
    cfg = load_config()
    cfg.universe["KR"] = [{"symbol": "AAA", "name": "a"}, {"symbol": "BBB", "name": "b"}]
    store = Store(tmp_path / "t.db")
    s = run_batch(cfg, store, _mock_llm(), "KR", fetch_df=lambda s_, m: _df())
    assert s["done"] == 2 and s["failed"] == 0
    row = store.get_fresh_dossier("AAA")
    ev = json.loads(row["evidence"])
    assert ev["stance"] == "bullish"
    assert row["rr"] == 3.0 and row["conviction"] == 0.6
    kinds = {r["kind"] for r in store.conn.execute("SELECT kind FROM events").fetchall()}
    assert {"dossier", "athena_done"} <= kinds


def test_run_batch_hard_stop(tmp_path):
    cfg = load_config()
    cfg.universe["KR"] = [{"symbol": f"S{i}", "name": f"s{i}"} for i in range(5)]
    store = Store(tmp_path / "t.db")
    clock = [1000.0]

    def now():
        clock[0] += 100                                 # 종목당 100초 경과 시뮬레이션
        return clock[0]

    s = run_batch(cfg, store, _mock_llm(), "KR", fetch_df=lambda s_, m: _df(),
                  stop_at=1350.0, now_fn=now)           # 3번 확인 후 데드라인
    assert s["stopped_by_deadline"] is True
    assert s["done"] < 5                                # 전부 못 돌고 멈춤


def test_run_batch_isolates_symbol_failure(tmp_path):
    cfg = load_config()
    cfg.universe["KR"] = [{"symbol": "BAD", "name": "x"}, {"symbol": "OK", "name": "y"}]
    store = Store(tmp_path / "t.db")

    def fetch(sym, m):
        if sym == "BAD":
            raise RuntimeError("네트워크 오류")
        return _df()

    s = run_batch(cfg, store, _mock_llm(), "KR", fetch_df=fetch)
    assert s["done"] == 1 and s["failed"] == 1          # 실패 격리, 배치 계속
    assert store.get_fresh_dossier("OK") is not None


# ── 리서치 창 판별 ─────────────────────────────────────────────────
def test_window_detection():
    acfg = {"windows": {"KR": {"start": "05:30", "stop": "08:10"},
                        "US": {"start": "15:40", "stop": "21:50"}}}
    kr = datetime(2026, 7, 2, 6, 0, tzinfo=KST)
    m, stop = athena_cli._window_of(acfg, kr)
    assert m == "KR"
    assert datetime.fromtimestamp(stop, KST).strftime("%H:%M") == "08:10"
    us = datetime(2026, 7, 2, 16, 0, tzinfo=KST)
    assert athena_cli._window_of(acfg, us)[0] == "US"
    off = datetime(2026, 7, 2, 12, 0, tzinfo=KST)
    assert athena_cli._window_of(acfg, off) == (None, None)

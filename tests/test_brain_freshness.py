"""P0/P1 — asof 분리·시계·freshness·strategy_scores stale·portfolio mark."""
from __future__ import annotations

import json
import time

from src.agents.context import build_context
from src.live_slice import apply_fast_slice
from src.market_state import MarketState
from src import strategy_scores as ss


def test_market_state_batch_fast_asof_roundtrip(tmp_path):
    s = MarketState()
    s.batch_asof = "2026-09-01T00:00:00+00:00"
    s.fast_asof = "2026-09-02T01:00:00+00:00"
    s.asof = s.fast_asof
    p = tmp_path / "ms.json"
    s.save(p)
    loaded = MarketState.load(p)
    assert loaded.batch_asof == "2026-09-01T00:00:00+00:00"
    assert loaded.fast_asof == "2026-09-02T01:00:00+00:00"


def test_apply_fast_slice_preserves_batch_asof(tmp_path):
    path = tmp_path / "market_state.json"
    seed = MarketState()
    seed.batch_asof = "2026-09-01T08:00:00+00:00"
    seed.asof = seed.batch_asof
    seed.merge({"fundamentals": {"AAPL": {"net_margin": 0.2}}})
    seed.save(path)

    apply_fast_slice({"regime": {"KR": {"label": "risk_on", "n": 2}},
                      "sentiment": {"vix": 18.0}, "markets": {}}, path)
    st = MarketState.load(path)
    assert st.batch_asof == "2026-09-01T08:00:00+00:00"
    assert st.fast_asof != seed.batch_asof
    assert st.regime["KR"]["label"] == "risk_on"
    assert st.fundamentals["AAPL"]["net_margin"] == 0.2


def test_build_context_includes_now_clock_freshness():
    from datetime import datetime, timezone
    batch = "2026-09-01T08:00:00+00:00"
    now_dt = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc)
    now = now_dt.timestamp()
    ms = {
        "asof": now_dt.isoformat(),
        "batch_asof": batch,
        "fast_asof": now_dt.isoformat(),
        "regime": {"KR": {"label": "neutral", "asof": "2026-09-02T08:00:00+00:00"}},
        "fundamentals": {"005930": {"net_margin": 0.1}},
    }
    ctx = json.loads(build_context(
        ms, [], {"cash": {}}, {},
        strategy_scores_asof=now,
        strategy_scores_stale=True,
        now_ts=now,
    ))
    assert "now" in ctx
    assert "clock" in ctx and "KR" in ctx["clock"]
    assert ctx["freshness"]["batch_asof"] == batch
    assert ctx["freshness"]["fast_asof"] == "2026-09-02T09:00:00+00:00"
    assert ctx["freshness"]["strategy_scores_stale"] is True
    assert ctx["freshness"]["slots"]["regime.KR"] == "2026-09-02T08:00:00+00:00"
    assert ctx["freshness"]["slots"]["fundamentals"] == "2026-09-01T08:00:00+00:00"
    assert ctx["freshness"]["batch_asof_age_sec"] == 25 * 3600.0
    assert ctx["freshness"]["batch_asof_stale"] is True


def test_build_freshness_no_asof_fallback():
    """batch_asof 없을 때 asof 로 masquerade 하지 않는다."""
    from datetime import datetime, timezone
    from src.agents.context import build_freshness

    now = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc).timestamp()
    ms = {
        "asof": "2026-09-02T09:00:00+00:00",
        "fundamentals": {"AAPL": {"pe": 20}},
    }
    fr = build_freshness(ms, now_ts=now)
    assert fr["batch_asof"] is None
    assert fr.get("batch_asof_stale") is True
    assert "fundamentals" not in fr["slots"]


def test_build_freshness_fresh_batch():
    from datetime import datetime, timezone
    from src.agents.context import build_freshness

    now = datetime(2026, 9, 2, 9, 0, tzinfo=timezone.utc).timestamp()
    ms = {
        "batch_asof": "2026-09-02T08:30:00+00:00",
        "fundamentals": {"AAPL": {"pe": 20}},
    }
    fr = build_freshness(ms, now_ts=now)
    assert fr.get("batch_asof_stale") is not True
    assert fr["slots"]["fundamentals"] == "2026-09-02T08:30:00+00:00"


def test_strategy_scores_stale_load(tmp_path):
    p = tmp_path / "strategy_scores.json"
    p.write_text(json.dumps({"asof": 1, "symbols": {"X": {"best": "a", "ranking": []}}}),
                 encoding="utf-8")
    assert ss.strategy_scores_stale(p, max_age_hours=36.0, now_fn=lambda: time.time())
    assert ss.load_strategy_scores(p) == {}


def test_pad_score_rejects_thin_n_trades():
    scores = {"A": {"ranking": [{"return_pct": 0.5, "n_trades": 1}]}}
    assert ss.pad_score(scores, "A") == float("-inf")


def test_strategy_fit_brief_thin_sample():
    brief = ss.strategy_fit_brief(
        {"best": "rsi_reversion",
         "ranking": [{"strategy": "rsi_reversion", "return_pct": 0.3, "n_trades": 1}]})
    assert brief["best"] is None
    assert brief["thin_sample"] is True
    assert len(brief["ranking"]) == 1


def test_strategy_fit_brief_ok_when_enough_trades():
    brief = ss.strategy_fit_brief(
        {"best": "ma_crossover",
         "ranking": [{"strategy": "ma_crossover", "return_pct": 0.1, "n_trades": 5}]})
    assert brief["best"] == "ma_crossover"
    assert "thin_sample" not in brief


def _rec_day_top():
    # 실측 흔한 모양: 1위가 day 전략(volatility_breakout), swing·position 은 아래.
    return {"best": "volatility_breakout", "ranking": [
        {"strategy": "volatility_breakout", "return_pct": 0.33, "n_trades": 17},
        {"strategy": "bollinger_breakout", "return_pct": 0.20, "n_trades": 6},
        {"strategy": "donchian_breakout", "return_pct": 0.15, "n_trades": 2},
        {"strategy": "macd", "return_pct": 0.08, "n_trades": 4},
        {"strategy": "ma_crossover", "return_pct": 0.05, "n_trades": 3},
        {"strategy": "rsi_reversion", "return_pct": 0.02, "n_trades": 5},
    ]}


def test_strategy_fit_brief_excludes_day_when_track_off():
    brief = ss.strategy_fit_brief(_rec_day_top(), exclude=ss.day_strategy_names())
    names = [r["strategy"] for r in brief["ranking"]]
    assert "volatility_breakout" not in names and "bollinger_breakout" not in names
    assert brief["best"] is None and brief["thin_sample"] is True   # donchian n=2
    # horizon 별: 표본 충분한 1위만 (donchian n=2 는 건너뛰고 ma_crossover)
    assert brief["best_by_horizon"]["swing"]["strategy"] == "macd"
    assert brief["best_by_horizon"]["position"]["strategy"] == "ma_crossover"
    assert "day" not in brief["best_by_horizon"]


def test_strategy_fit_brief_keeps_day_when_not_excluded():
    brief = ss.strategy_fit_brief(_rec_day_top())
    assert brief["best"] == "volatility_breakout"
    assert brief["best_by_horizon"]["day"]["strategy"] == "volatility_breakout"
    assert len(brief["ranking"]) == 3


def test_day_strategy_names_from_catalog():
    assert ss.day_strategy_names() == {"volatility_breakout", "bollinger_breakout"}


def test_portfolio_mark_to_market(tmp_path):
    from src.agents.cycle_runner import CycleRunner
    from src.config import load_config
    from src.paper_account import PaperAccount
    from src.risk_gate import RiskGate
    from src.risk import RiskManager
    from src.broker import Broker
    from src.strategies.base import Position

    cfg = load_config()
    acct = PaperAccount(cash={"KR": 10_000_000}, state_path=tmp_path / "pa.json")
    gate = RiskGate({"capital": {"KR": 1_000_000}, "max_position_pct": 0.2,
                     "max_positions": 5, "kill_switch_file": str(tmp_path / "H")})
    broker = Broker(account=acct, gate=gate, mode="paper")
    acct.positions["005930"] = Position(symbol="005930", qty=10, avg_price=100_000)
    acct.symbol_market["005930"] = "KR"

    runner = CycleRunner(
        cfg, llm_factory=lambda c: None,
        fetch_candles=lambda s, m: None, store=None, broker=broker,
        risk=RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.2),
        journal_path=tmp_path / "d.jsonl",
        market_state_path=tmp_path / "ms.json",
        price_fn=lambda syms, mkt: {s: 110_000.0 for s in syms},
    )
    pf = runner._portfolio(live_prices={"005930": 110_000.0})
    held = pf["positions"][0]
    assert held["current_price"] == 110_000.0
    assert held["unrealized_pnl_pct"] == 10.0
    assert held["unrealized_pnl"] == 100_000.0

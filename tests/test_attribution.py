"""성과 귀속(attribution) + store 청산 손익 기록 + 대시보드 알파 계산 테스트."""
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.attribution import (decision_stats, recent_trades, strategy_stats,
                             track_record)
from src.engine.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import dashboard as dash  # noqa: E402


# ── store: 청산 시 손익 확정 기록 ──────────────────────────────────
def test_partial_exit_attribution_in_track_record(tmp_path):
    """부분매도+잔량청산 시 track_record 합계가 맞다."""
    from src.attribution import track_record
    store = Store(tmp_path / "t.db")
    pid = store.open_position("005930", "KR", 10, 70000, strategy="rsi_reversion")
    store.record_partial_exit(pid, 3, 71000, reason="partial")
    store.close_position(pid, exit_price=69000, reason="final")
    closed = store.get_closed_positions()
    assert len(closed) == 2
    total = sum(r["pnl"] for r in closed)
    assert total == (71000 - 70000) * 3 + (69000 - 70000) * 7
    tr = track_record(store)
    rsi = [s for s in tr["strategy_stats"] if s["strategy"] == "rsi_reversion"][0]
    assert rsi["trades"] == 1
    assert rsi["total_pnl"] == total


def test_close_position_records_pnl_with_fee(tmp_path):
    store = Store(tmp_path / "t.db")
    pid = store.open_position("005930", "KR", 10, 1000, strategy="rsi_reversion")
    store.close_position(pid, exit_price=1100, reason="target_hit", fee=15.0)
    row = store.get_closed_positions()[0]
    assert row["pnl"] == (1100 - 1000) * 10 - 15.0


def test_open_position_merges_duplicate_open(tmp_path):
    store = Store(tmp_path / "t.db")
    a = store.open_position("005930", "KR", 5, 1000)
    b = store.open_position("005930", "KR", 8, 1100)
    assert a == b
    assert len(store.get_open_positions()) == 1
    assert store.get_open_positions()[0]["qty"] == 8


def test_nearest_snapshot_price_under_lock(tmp_path):
    store = Store(tmp_path / "t.db")
    ts = 1_700_000_000.0
    store.record_snapshot("005930", price=50000, ts=ts)
    assert store.nearest_snapshot_price("005930", ts + 100) == 50000
    assert store.nearest_snapshot_price("005930", ts + 99999) is None


def test_close_position_records_pnl(tmp_path):
    store = Store(tmp_path / "t.db")
    pid = store.open_position("005930", "KR", 10, 1000, strategy="rsi_reversion")
    store.close_position(pid, exit_price=1100, reason="target_hit")
    row = store.get_closed_positions()[0]
    assert row["pnl"] == (1100 - 1000) * 10
    assert row["exit_price"] == 1100 and row["exit_reason"] == "target_hit"


def test_close_without_price_excluded_from_stats(tmp_path):
    store = Store(tmp_path / "t.db")
    pid = store.open_position("005930", "KR", 10, 1000)
    store.close_position(pid)                        # armed 해제 등 — pnl 미확정
    assert store.get_closed_positions() == []        # 통계에서 제외


def test_migration_adds_columns_to_old_db(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)                        # 구버전 스키마(새 컬럼 없음)
    con.execute("CREATE TABLE positions (id INTEGER PRIMARY KEY, symbol TEXT,"
                " market TEXT, strategy TEXT, state TEXT, qty REAL, avg_price REAL,"
                " thesis TEXT, target_price REAL, stop_price REAL,"
                " opened_at REAL, updated_at REAL, closed_at REAL, meta TEXT)")
    con.commit(); con.close()
    store = Store(db)                                # 열면서 마이그레이션
    pid = store.open_position("005930", "KR", 5, 100)
    store.close_position(pid, exit_price=110, reason="stop_hit")
    assert store.get_closed_positions()[0]["pnl"] == 50


# ── attribution 집계 ───────────────────────────────────────────────
def _seed(store):
    for sym, strat, entry, exit_, reason in [
        ("A", "rsi_reversion", 100, 110, "target_hit"),      # +100
        ("B", "rsi_reversion", 100, 95, "stop_hit"),          # -50
        ("C", "momentum", 100, 130, "strategy:momentum"),     # +300
    ]:
        pid = store.open_position(sym, "KR", 10, entry, strategy=strat)
        store.close_position(pid, exit_price=exit_, reason=reason)


def test_strategy_stats_aggregates(tmp_path):
    store = Store(tmp_path / "t.db")
    _seed(store)
    stats = {s["strategy"]: s for s in strategy_stats(store)}
    rsi = stats["rsi_reversion"]
    assert rsi["trades"] == 2 and rsi["wins"] == 1 and rsi["win_rate"] == 0.5
    assert rsi["total_pnl"] == 100 - 50
    assert rsi["small_sample"] is True                # 표본 5건 미만
    assert stats["momentum"]["avg_ret_pct"] == 30.0


def test_recent_trades_and_track_record(tmp_path):
    store = Store(tmp_path / "t.db")
    _seed(store)
    store.record_decision("A", "BUY", conviction=0.7, verdict="approved")
    store.record_decision("B", "BUY", conviction=0.4, verdict="vetoed")
    trades = recent_trades(store, limit=2)
    assert len(trades) == 2 and trades[0]["symbol"] == "C"   # 최신순
    assert trades[0]["exit_reason"] == "strategy:momentum"
    ds = decision_stats(store)
    assert ds["proposed"] == 2 and ds["approved"] == 1 and ds["vetoed"] == 1
    tr = track_record(store)
    assert tr["strategy_stats"] and tr["recent_trades"] and "note" in tr


def test_track_record_survives_store_failure():
    class Broken:
        def get_closed_positions(self, **kw):
            raise RuntimeError("db down")
    assert track_record(Broken()) == {}               # 사이클을 죽이지 않는다


def test_context_includes_track_record():
    from src.agents.context import build_context
    import json
    ctx = json.loads(build_context({}, [], {}, {}, track_record={"strategy_stats": []}))
    assert "track_record" in ctx
    ctx2 = json.loads(build_context({}, [], {}, {}))
    assert "track_record" not in ctx2                 # 없으면 생략(하위호환)


# ── 대시보드 알파(지수 B&H 대비) ───────────────────────────────────
def _paper():
    return {
        "start_cash": {"KR": 1_000_000},
        "cash": {"KR": 500_000},
        "positions": {"005930": {"qty": 100, "avg_price": 5_000}},
        "symbol_market": {"005930": "KR"},
        "journal": [{"ts": "2026-06-01T00:00:00+00:00", "symbol": "005930",
                     "market": "KR", "side": "BUY", "qty": 100, "price": 5000,
                     "fee": 0, "reason": "t"}],
    }


def _bench_df():
    return pd.DataFrame({
        "time": pd.to_datetime(["2026-05-30", "2026-06-30"]),
        "close": [100.0, 110.0],                      # 지수 +10%
    })


def test_alpha_rows_positive_alpha():
    rows = dash._alpha_rows(_paper(), {"005930": 6_000},   # 평가 500k+600k=1.1M → +10%...
                            bench_fn=lambda m: _bench_df())
    assert len(rows) == 1
    a = rows[0]
    assert a["market"] == "KR" and abs(a["port_ret"] - 10.0) < 1e-6
    assert abs(a["bench_ret"] - 10.0) < 1e-6
    assert abs(a["alpha"] - 0.0) < 1e-6               # 지수와 동률 → 알파 0


def test_alpha_rows_no_trades():
    assert dash._alpha_rows({"journal": []}, {}) == []
    assert dash._alpha_rows(None, {}) == []


def test_alpha_rows_bench_missing():
    rows = dash._alpha_rows(_paper(), {"005930": 6_000}, bench_fn=lambda m: None)
    assert rows[0]["bench_ret"] is None and rows[0]["alpha"] is None

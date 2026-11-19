"""src.funnel — 매수 퍼널·현금 활용률 집계."""
import json
import time
from datetime import datetime, timedelta, timezone

from src import funnel
from src.engine.store import Store

_KST = timezone(timedelta(hours=9))


def _store(tmp_path):
    return Store(tmp_path / "f.db")


def test_brain_funnel_counts_each_stage(tmp_path):
    s = _store(tmp_path)
    now = time.time()
    for _ in range(3):
        s.log_event("wake", None, {"reason": "x"}, ts=now - 100)
    s.log_event("brain_skip", None, {"reason": "cooldown"}, ts=now - 90)
    s.log_event("wake", None, {}, ts=now - 40 * 86400)          # 범위 밖
    s.record_decision("A", "HOLD", verdict="vetoed", ts=now - 80)
    s.record_decision("B", "BUY", verdict="approved", ts=now - 80)
    s.record_decision("C", "BUY", verdict="vetoed", ts=now - 80)
    s.log_event("cycle", None, {"executed": [
        {"symbol": "B", "action": "BUY", "status": "filled"},
        {"symbol": "C", "action": "BUY", "status": "vetoed", "reason": "no"},
        {"symbol": "D", "action": "BUY", "status": "market_unknown"},
        {"symbol": "A", "action": "SELL", "status": "filled"},
    ]}, ts=now - 70)
    b = funnel.brain_funnel(s, now - 30 * 86400, now)
    assert b["wakes"] == 3 and b["skips"] == {"cooldown": 1} and b["cycles"] == 1
    assert b["decisions"] == 3 and b["holds"] == 1
    assert b["buy_proposed"] == 2 and b["buy_approved"] == 1 and b["buy_vetoed"] == 1
    assert b["buy_executed"] == {"filled": 1, "vetoed": 1, "market_unknown": 1}
    assert b["buy_filled"] == 1
    assert {e["symbol"] for e in b["not_filled_recent"]} == {"C", "D"}


def test_value_funnel_merges_jsonl_and_value_done(tmp_path):
    s = _store(tmp_path)
    now = time.time()
    p = tmp_path / "vf.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in [
        {"market": "KR", "ts": now - 50, "selected": 10, "too_expensive": 1,
         "margin_fail": 2, "timing_fail": 5, "gated": 2},
        {"market": "KR", "ts": now - 40 * 86400, "selected": 99},   # 범위 밖
    ]) + "\nnot json\n", encoding="utf-8")
    s.log_event("value_done", None, {"summary": {"result": str(
        {"due": ["KR"], "markets": {"KR": {"candidates": 10, "gated": 2, "proposed": 1,
                                           "filled": 1, "vetoed": 0}}})}}, ts=now - 40)
    v = funnel.value_funnel(s, now - 30 * 86400, now, funnel_path=p)
    assert v["KR"] == {"runs": 1, "selected": 10, "too_expensive": 1, "margin_fail": 2,
                       "timing_fail": 5, "gated": 2, "proposed": 1, "filled": 1, "vetoed": 0}


def test_cash_utilization_uses_cost_basis_and_capital_timeline(tmp_path):
    s = _store(tmp_path)
    now = datetime(2026, 11, 19, 12, 0, tzinfo=_KST).timestamp()
    d1 = now - 2 * 86400
    with s._lock:
        s.conn.execute(
            "INSERT INTO positions(symbol, market, strategy, state, qty, avg_price,"
            " opened_at, updated_at, closed_at) VALUES"
            " ('A','KR','value','open',10,10000,?,?,NULL),"
            " ('B','KR','macd','closed',5,20000,?,?,?)",
            (d1, d1, d1, d1, now - 86400 - 3600))
        s.conn.commit()
    s.log_event("capital_sync", None, {"capital": {"KR": 400000}}, ts=now - 86400 - 7200)
    u = funnel.cash_utilization(s, 3, cfg_capital={"KR": 200000, "US": 500}, now=now)
    assert [r["date"] for r in u] == ["2026-11-17", "2026-11-18", "2026-11-19"]
    assert u[0]["KR"]["cost"] == 200000 and u[0]["KR"]["util"] == 1.0   # 자본 20만
    assert u[1]["KR"]["cost"] == 100000 and u[1]["KR"]["util"] == 0.25  # B 청산·자본 40만
    assert u[2]["KR"]["value_cost"] == 100000 and u[2]["KR"]["positions"] == 1
    assert u[2]["US"]["util"] == 0.0


def test_format_report_renders(tmp_path):
    s = _store(tmp_path)
    data = funnel.build(s, 7, cfg_capital={"KR": 1000}, funnel_path=tmp_path / "none")
    text = funnel.format_report(data["brain"], data["value"], data["util"], 7)
    assert "뇌 트랙" in text and "현금 활용률" in text

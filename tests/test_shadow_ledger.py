"""그림자 장부 v1 — 등록·채점·집계."""
import importlib
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.agents.cycle import CycleResult
from src.agents.schemas import DecisionOutput, Proposal, ValidationOutput, ValidationVerdict
from src.engine.store import Store
from src.shadow_ledger import (book_blocked, horizon_calendar_days, score_open_shadows,
                               shadow_stats)


def test_shadow_ledger_imports_before_eval():
    """프로덕션 경로: score_shadow_ledger.py 는 shadow를 eval보다 먼저 로드한다.

    J11 상단 trade_defs import 는 eval.__init__→labels→shadow 순환을 만들고,
    pytest 알파벳 수집(src.eval 선로드)이 결함을 가린다.
    """
    doomed = [k for k in list(sys.modules)
              if k == "src.shadow_ledger" or k.startswith("src.shadow_ledger.")
              or k == "src.eval" or k.startswith("src.eval.")]
    for k in doomed:
        del sys.modules[k]
    mod = importlib.import_module("src.shadow_ledger")
    assert callable(mod.score_open_shadows)

KST = timezone(timedelta(hours=9))


def _vetoed_cycle(sym="005930", price=1000.0, cycle_ts=None):
    cycle_ts = cycle_ts or time.time()
    prop = Proposal(symbol=sym, market="KR", side="BUY", conviction=0.7,
                    horizon="swing", target_weight=0.1, thesis="테스트 매수")
    verdict = ValidationVerdict(symbol=sym, approved=False, reason="rule3:risk_off",
                                concerns=["rule3"])
    return CycleResult(
        decision=DecisionOutput(market_view="test", proposals=[prop]),
        validation=ValidationOutput(verdicts=[verdict]),
        executed=[{"symbol": sym, "action": "BUY", "status": "vetoed",
                   "reason": "rule3:risk_off"}],
        cycle_ts=cycle_ts,
        cycle_ts_iso=datetime.fromtimestamp(cycle_ts, tz=timezone.utc).isoformat(),
    ), {sym: price}


def test_book_vetoed_buy(tmp_path):
    store = Store(tmp_path / "t.db")
    res, prices = _vetoed_cycle()
    n = book_blocked(store, res, prices, sleeve="brain")
    assert n == 1
    open_rows = store.get_open_shadow_positions()
    assert len(open_rows) == 1
    assert open_rows[0]["entry_price"] == 1000.0
    assert open_rows[0]["block_status"] == "vetoed"
    assert open_rows[0]["block_bucket"] == "검증:규칙거부"


def test_skip_filled(tmp_path):
    store = Store(tmp_path / "t.db")
    prop = Proposal(symbol="005930", market="KR", side="BUY", conviction=0.8,
                    target_weight=0.1, thesis="t")
    res = CycleResult(
        decision=DecisionOutput(market_view="test", proposals=[prop]),
        validation=ValidationOutput(verdicts=[
            ValidationVerdict(symbol="005930", approved=True, reason="ok")]),
        executed=[{"symbol": "005930", "action": "BUY", "status": "filled",
                   "reason": "체결"}],
        cycle_ts=time.time(),
    )
    assert book_blocked(store, res, {"005930": 1000.0}) == 0
    assert store.get_open_shadow_positions() == []


def test_dedup_same_cycle(tmp_path):
    store = Store(tmp_path / "t.db")
    res, prices = _vetoed_cycle()
    assert book_blocked(store, res, prices) == 1
    assert book_blocked(store, res, prices) == 0


def test_score_swing_horizon(tmp_path):
    store = Store(tmp_path / "t.db")
    hist = tmp_path / "history"
    hist.mkdir()
    # 30일 종가: 100 → 110 (10% 상승)
    lines = ["Date,Open,High,Low,Close,Volume"]
    base = datetime(2026, 1, 1, tzinfo=KST)
    for i in range(30):
        d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
        close = 100.0 + i * (10.0 / 29)
        lines.append(f"{d},100,100,100,{close:.2f},1000")
    (hist / "TESTSYM_1d_1y.csv").write_text("\n".join(lines), encoding="utf-8")

    entry_ts = base.timestamp() + 3600
    prop = Proposal(symbol="TESTSYM", market="KR", side="BUY", conviction=0.7,
                    horizon="swing", target_weight=0.1, thesis="t")
    res = CycleResult(
        decision=DecisionOutput(market_view="test", proposals=[prop]),
        validation=ValidationOutput(verdicts=[
            ValidationVerdict(symbol="TESTSYM", approved=False, reason="no")]),
        executed=[{"symbol": "TESTSYM", "action": "BUY", "status": "vetoed"}],
        cycle_ts=entry_ts,
        cycle_ts_iso=datetime.fromtimestamp(entry_ts, tz=timezone.utc).isoformat(),
    )
    book_blocked(store, res, {"TESTSYM": 100.0}, cfg={"exit_policy": {
        "time_stop": {"enabled": True, "by_horizon": {"swing": {"max_days": 20}}}}})

    now = entry_ts + 21 * 86400
    stats = score_open_shadows(store, now=now, data_dir=tmp_path, cfg={
        "exit_policy": {"time_stop": {"enabled": True,
                                       "by_horizon": {"swing": {"max_days": 20}}}}})
    assert stats["scored"] == 1
    scored = store.get_scored_shadow_positions()
    assert len(scored) == 1
    assert scored[0]["ret_pct"] is not None
    assert scored[0]["ret_pct"] > 0


def test_idempotent_score(tmp_path):
    store = Store(tmp_path / "t.db")
    hist = tmp_path / "history"
    hist.mkdir()
    base = datetime(2026, 2, 1, tzinfo=KST)
    (hist / "X_1d_1y.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        f"{base.strftime('%Y-%m-%d')},10,10,10,10,1\n"
        f"{(base + timedelta(days=25)).strftime('%Y-%m-%d')},11,11,11,11,1\n",
        encoding="utf-8",
    )
    entry_ts = base.timestamp()
    res, prices = _vetoed_cycle(sym="X", price=10.0, cycle_ts=entry_ts)
    book_blocked(store, res, prices)
    now = entry_ts + 21 * 86400
    score_open_shadows(store, now=now, data_dir=tmp_path)
    score_open_shadows(store, now=now, data_dir=tmp_path)
    assert len(store.get_scored_shadow_positions()) == 1


def test_shadow_stats_buckets(tmp_path):
    store = Store(tmp_path / "t.db")
    ts = time.time() - 86400
    store.insert_shadow_position(
        cycle_ts=ts, sleeve="brain", symbol="A", market="KR",
        block_status="vetoed", block_bucket="검증:규칙거부",
        entry_price=100, entry_ts=ts, state="scored",
        ret_pct=5.0, scored_at=time.time(), exit_reason="horizon_expired")
    store.insert_shadow_position(
        cycle_ts=ts + 1, sleeve="brain", symbol="B", market="KR",
        block_status="gate_rejected", block_bucket="게이트:자본/한도",
        entry_price=100, entry_ts=ts, state="scored",
        ret_pct=-3.0, scored_at=time.time(), exit_reason="horizon_expired")

    stats = shadow_stats(store, since_days=365)
    assert stats["overall"]["n_scored"] == 2
    assert stats["by_bucket"]["검증:규칙거부"]["n"] == 1
    assert stats["by_bucket"]["검증:규칙거부"]["small_sample"] is True


def test_book_failure_does_not_raise(tmp_path):
    store = Store(tmp_path / "t.db")

    class Broken:
        decision = None

    assert book_blocked(store, Broken(), {}) == 0


def _armed_cycle(sym="005930", price=1000.0, cycle_ts=None):
    from src.agents.schemas import DecisionOutput, Proposal, ValidationOutput, ValidationVerdict
    cycle_ts = cycle_ts or time.time()
    prop = Proposal(symbol=sym, market="KR", side="BUY", conviction=0.7,
                    horizon="swing", target_weight=0.1, thesis="armed test")
    return CycleResult(
        decision=DecisionOutput(market_view="test", proposals=[prop]),
        validation=ValidationOutput(verdicts=[
            ValidationVerdict(symbol=sym, approved=True, reason="ok")]),
        executed=[{"symbol": sym, "action": "BUY", "status": "armed",
                   "reason": "진입대기"}],
        cycle_ts=cycle_ts,
    ), {sym: price}


def test_book_soft_pending(tmp_path):
    from src.shadow_ledger import book_soft_pending
    store = Store(tmp_path / "t.db")
    res, prices = _armed_cycle()
    n = book_soft_pending(store, res, prices, sleeve="brain")
    assert n == 1
    rows = store.get_pending_shadow_positions()
    assert len(rows) == 1
    assert rows[0]["state"] == "pending"
    assert rows[0]["block_status"] == "armed"


def test_cancel_shadow_on_fill(tmp_path):
    from src.shadow_ledger import book_soft_pending, cancel_shadow_on_fill
    store = Store(tmp_path / "t.db")
    res, prices = _armed_cycle()
    book_soft_pending(store, res, prices)
    assert len(store.get_pending_shadow_positions()) == 1
    n = cancel_shadow_on_fill(store, "005930")
    assert n == 1
    assert store.get_pending_shadow_positions() == []


def test_pending_timeout_score(tmp_path):
    from src.shadow_ledger import book_soft_pending
    store = Store(tmp_path / "t.db")
    hist = tmp_path / "history"
    hist.mkdir()
    base = datetime(2026, 3, 1, tzinfo=KST)
    (hist / "ARM_1d_1y.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        f"{base.strftime('%Y-%m-%d')},10,10,10,10,1\n"
        f"{(base + timedelta(days=25)).strftime('%Y-%m-%d')},9,9,9,9,1\n",
        encoding="utf-8",
    )
    entry_ts = base.timestamp()
    res, prices = _armed_cycle(sym="ARM", price=10.0, cycle_ts=entry_ts)
    book_soft_pending(store, res, prices)
    now = entry_ts + 21 * 86400
    stats = score_open_shadows(store, now=now, data_dir=tmp_path)
    assert stats["scored"] == 1
    scored = store.get_scored_shadow_positions()
    assert scored[0]["exit_reason"] == "pending_timeout"


def test_shadow_ret_subtracts_roundtrip_cost(tmp_path):
    """왕복비용을 빼지 않으면 그림자가 실거래보다 유리해 '막아서 손해'로 기울었다."""
    store = Store(tmp_path / "t.db")
    hist = tmp_path / "history"
    hist.mkdir()
    lines = ["Date,Open,High,Low,Close,Volume"]
    base = datetime(2026, 1, 1, tzinfo=KST)
    for i in range(30):
        d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
        close = 100.0 + i * (10.0 / 29)
        lines.append(f"{d},100,100,100,{close:.2f},1000")
    (hist / "COSTSYM_1d_1y.csv").write_text("\n".join(lines), encoding="utf-8")
    entry_ts = base.timestamp() + 3600
    res, prices = _vetoed_cycle(sym="COSTSYM", price=100.0, cycle_ts=entry_ts)
    book_blocked(store, res, prices, sleeve="brain", cfg={"exit_policy": {
        "time_stop": {"enabled": True, "by_horizon": {"swing": {"max_days": 20}}}}})
    now = entry_ts + 21 * 86400
    ep = {"exit_policy": {"time_stop": {"enabled": True,
                                        "by_horizon": {"swing": {"max_days": 20}}}}}
    zero = dict(ep, paper={"fee_rate": {"KR": 0}, "slippage_bps": {"KR": 0},
                           "sell_tax_rate": {"KR": 0}})
    score_open_shadows(store, now=now, data_dir=tmp_path, cfg=zero)
    gross = store.get_scored_shadow_positions()[0]["ret_pct"]
    store2 = Store(tmp_path / "t2.db")
    book_blocked(store2, res, prices, sleeve="brain", cfg=ep)
    score_open_shadows(store2, now=now, data_dir=tmp_path, cfg=ep)
    net = store2.get_scored_shadow_positions()[0]["ret_pct"]
    assert gross > net
    from src.eval.trade_defs import roundtrip_cost_pct
    assert abs((gross - net) - roundtrip_cost_pct("KR") * 100) < 1e-6


def test_rescore_shadow_costs_fixes_zero_cost_era(tmp_path):
    """J11 공백 때 찍힌 scored ret 에 비용을 소급 적용."""
    from src.shadow_ledger import rescore_shadow_costs
    from src.eval.trade_defs import roundtrip_cost_pct

    store = Store(tmp_path / "t.db")
    hist = tmp_path / "history"
    hist.mkdir()
    lines = ["Date,Open,High,Low,Close,Volume"]
    base = datetime(2026, 1, 1, tzinfo=KST)
    for i in range(30):
        d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
        close = 100.0 + i * (10.0 / 29)
        lines.append(f"{d},100,100,100,{close:.2f},1000")
    (hist / "RESCORE_1d_1y.csv").write_text("\n".join(lines), encoding="utf-8")
    entry_ts = base.timestamp() + 3600
    res, prices = _vetoed_cycle(sym="RESCORE", price=100.0, cycle_ts=entry_ts)
    ep = {"exit_policy": {"time_stop": {"enabled": True,
                                        "by_horizon": {"swing": {"max_days": 20}}}}}
    zero = dict(ep, paper={"fee_rate": {"KR": 0}, "slippage_bps": {"KR": 0},
                           "sell_tax_rate": {"KR": 0}})
    book_blocked(store, res, prices, sleeve="brain", cfg=ep)
    score_open_shadows(store, now=entry_ts + 21 * 86400, data_dir=tmp_path, cfg=zero)
    before = store.get_scored_shadow_positions()[0]["ret_pct"]
    out = rescore_shadow_costs(store, cfg=ep)
    assert out["updated"] == 1
    after = store.get_scored_shadow_positions()[0]["ret_pct"]
    assert abs((before - after) - roundtrip_cost_pct("KR") * 100) < 1e-6
    assert rescore_shadow_costs(store, cfg=ep)["unchanged"] == 1


def test_open_shadow_cancelled_if_had_closed_position(tmp_path):
    """hard-block 그림자도 실체결(이미 청산)이 있으면 취소 — 생존편향 제거."""
    store = Store(tmp_path / "t.db")
    ts = time.time() - 86400
    store.insert_shadow_position(
        cycle_ts=ts, sleeve="brain", symbol="HAD", market="KR",
        block_status="gate_rejected", block_bucket="게이트:자본/한도",
        entry_price=100, entry_ts=ts, state="open")
    pid = store.open_position("HAD", "KR", 1, 100)
    store.close_position(pid, exit_price=110, reason="target")
    stats = score_open_shadows(store, now=ts + 30 * 86400, data_dir=tmp_path)
    assert stats["cancelled"] == 1
    assert store.get_scored_shadow_positions() == []


def test_horizon_calendar_days():
    assert horizon_calendar_days("day") == 1
    assert horizon_calendar_days("swing") == 20
    cfg = {"exit_policy": {"time_stop": {"by_horizon": {"swing": {"max_days": 15}}}}}
    assert horizon_calendar_days("swing", cfg) == 15

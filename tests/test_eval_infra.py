"""컨텍스트 아카이브 · 판단 단위 채점 · 널 매니저 · 리플레이."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from src.agents.cycle import run_cycle
from src.agents.decision_agent import DecisionAgent
from src.agents.llm import MockLLM
from src.agents.schemas import DecisionOutput, Proposal, ValidationOutput
from src.agents.validation_agent import ValidationAgent
from src.broker import Broker
from src.eval.archive import load_context, persist_context
from src.eval.consistency import consistency_report, fleiss_kappa
from src.eval.labels import forward_return, policy_return, target_hit_before_stop
from src.eval.labels import brier_score, log_loss
from src.eval.null_manager import eligible_candidates, null_cash, null_random_gated
from src.eval.replay import redecide_record
from src.eval.score import score_journal
from src.eval_protocol import can_promote
from src.paper_account import PaperAccount
from src.risk import RiskManager
from src.risk_gate import RiskGate


def _history(tmp_path: Path, symbol: str = "005930") -> Path:
    hist = tmp_path / "history"
    hist.mkdir(exist_ok=True)
    p = hist / f"{symbol}_1d_1y.csv"
    p.write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2026-01-09,99,99,99,99,1\n"
        "2026-01-10,100,100,100,100,1\n"
        "2026-01-11,101,112,100,110,1\n"
        "2026-01-12,110,110,85,90,1\n",
        encoding="utf-8")
    return tmp_path


def _ctx(**extra) -> dict:
    base = {
        "asof": "2026-01-10T06:00:00+00:00",
        "market": {"regime": "risk_off"},
        "candidates": [
            {"symbol": "005930", "market": "KR", "price": 100.0,
             "dossier": {"entry_low": 95, "entry_high": 105,
                         "invalidation": 90, "target": 111, "stance": "bullish"}},
            {"symbol": "000660", "market": "KR", "price": 50.0,
             "dossier": {"entry_low": 45, "entry_high": 55,
                         "invalidation": 40, "target": 60, "stance": "bullish"}},
        ],
        "constraints": {"max_positions": 2},
        "track_record": {"note": "old-manager"},
    }
    base.update(extra)
    return base


def _cycle(tmp_path: Path, context: dict, side: str = "HOLD"):
    def respond(schema, system, user):
        if schema is DecisionOutput:
            return DecisionOutput(
                market_view="t",
                proposals=[Proposal(symbol="005930", market="KR", side=side,
                                    conviction=0.5, target_weight=0.1, thesis="h")])
        return ValidationOutput(verdicts=[])

    llm = MockLLM(respond, model="test-model")
    acct = PaperAccount(cash={"KR": 1_000_000}, fee_rate={"KR": 0.0},
                        slippage_bps={"KR": 0.0}, state_path=tmp_path / "a.json")
    gate = RiskGate({"capital": {"KR": 1_000_000}, "max_position_pct": 0.2,
                     "max_positions": 5, "kill_switch_file": str(tmp_path / "HALT")})
    broker = Broker(account=acct, gate=gate, client=None, mode="paper")
    risk = RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.2)
    jp = tmp_path / "decisions.jsonl"
    res = run_cycle(context_json=json.dumps(context, ensure_ascii=False),
                    decision_agent=DecisionAgent(llm),
                    validation_agent=ValidationAgent(llm, min_conviction=0),
                    broker=broker, risk=risk, price_lookup={"005930": 100.0},
                    journal_path=jp)
    return res, jp


def test_archive_pointer_in_journal(tmp_path):
    ctx = _ctx()
    res, jp = _cycle(tmp_path, ctx)
    assert res.manager
    rec = json.loads(jp.read_text(encoding="utf-8").strip())
    assert rec.get("context_ref")
    assert rec.get("context_sha256")
    raw = load_context(jp, rec["context_ref"], expected_sha256=rec["context_sha256"])
    loaded = json.loads(raw)
    assert loaded["candidates"][0]["symbol"] == "005930"
    assert loaded["track_record"]["note"] == "old-manager"


def test_archive_failure_does_not_kill_cycle(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr("src.eval.archive.persist_context", boom)
    res, jp = _cycle(tmp_path, _ctx())
    assert res.manager
    rec = json.loads(jp.read_text(encoding="utf-8").strip())
    assert "manager" in rec
    assert rec.get("context_ref") is None


def test_null_random_gated_deterministic():
    ctx = _ctx()
    a = null_random_gated(ctx, cycle_ts=1700000000.0, n_buy=1)
    b = null_random_gated(ctx, cycle_ts=1700000000.0, n_buy=1)
    assert a == b
    assert sum(1 for s in a.values() if s == "BUY") == 1
    cash = null_cash(ctx["candidates"])
    assert set(cash.values()) == {"HOLD"}


def test_eligible_requires_bullish_stance():
    ctx = _ctx()
    ctx["candidates"].append(
        {"symbol": "NEUT", "market": "KR", "price": 10.0,
         "dossier": {"entry_low": 9, "entry_high": 11, "invalidation": 8,
                     "target": 12, "stance": "neutral"}})
    elig = eligible_candidates(ctx)
    assert {c["symbol"] for c in elig} == {"005930", "000660"}


def test_score_journal_decomposes_delta(tmp_path):
    data = _history(tmp_path)
    ctx = _ctx()
    raw = json.dumps(ctx, ensure_ascii=False)
    meta = persist_context(raw, cycle_ts=1_768_032_000.0, journal_path=tmp_path / "decisions.jsonl")
    rec = {
        "ts": "2026-01-10T06:00:00+00:00",
        "proposals": [{"symbol": "005930", "side": "HOLD", "horizon": "day"}],
        **{k: meta[k] for k in ("context_ref", "context_sha256", "context_bytes")},
        "manager": {"epoch": "test@abc"},
    }
    jp = tmp_path / "decisions.jsonl"
    jp.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    out = score_journal(journal_path=jp, data_dir=data, min_n=1)
    assert "delta_decomp" in out
    assert "same_pool" in out["delta_decomp"]
    assert "gate_diff" in out["delta_decomp"]


def test_hold_policy_is_zero(tmp_path):
    data = _history(tmp_path)
    lab = forward_return(data, "005930", "2026-01-10", horizon="day")
    assert lab["fwd_ret"] == pytest.approx(0.10, abs=1e-6)
    assert policy_return("HOLD", lab["fwd_ret"]) == 0.0
    assert policy_return("BUY", lab["fwd_ret"]) == pytest.approx(0.10, abs=1e-6)


def test_target_hit_before_stop_labels(tmp_path):
    data = _history(tmp_path)
    hit = target_hit_before_stop(
        data, "005930", "2026-01-10", target=111, invalidation=90, horizon="swing")
    assert hit["target_hit_before_stop"] is True
    stop = target_hit_before_stop(
        data, "005930", "2026-01-10", target=120, invalidation=99, horizon="swing")
    assert stop["target_hit_before_stop"] is False
    amb = target_hit_before_stop(
        data, "005930", "2026-01-10", target=111, invalidation=99, horizon="swing")
    # 2026-01-11 high 112 and low 100 — target only; 01-12 both would be later
    assert amb["reason"] in ("target_first", "same_bar_ambiguous", "stop_first")


def test_score_journal_hold_matrix_and_min_n(tmp_path):
    data = _history(tmp_path)
    ctx = _ctx()
    raw = json.dumps(ctx, ensure_ascii=False)
    meta = persist_context(raw, cycle_ts=1_768_032_000.0, journal_path=tmp_path / "decisions.jsonl")
    rec = {
        "ts": "2026-01-10T06:00:00+00:00",
        "proposals": [{"symbol": "005930", "side": "HOLD", "horizon": "day"}],
        **{k: meta[k] for k in ("context_ref", "context_sha256", "context_bytes")},
        "manager": {"epoch": "test@abc"},
    }
    jp = tmp_path / "decisions.jsonl"
    jp.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    out = score_journal(journal_path=jp, data_dir=data, min_n=20)
    assert out["n"] >= 1
    assert out["status"] == "shadow_only"
    assert out["can_promote"] is False
    hold_rows = [r for r in out["rows"] if r["symbol"] == "005930"]
    assert hold_rows[0]["live_side"] == "HOLD"
    assert hold_rows[0]["live_policy"] == 0.0


def test_min_date_drops_old_rows(tmp_path):
    data = _history(tmp_path)
    ctx = _ctx()
    raw = json.dumps(ctx, ensure_ascii=False)
    meta = persist_context(raw, cycle_ts=1_700_000_000.0, journal_path=tmp_path / "decisions.jsonl")
    rec = {
        "ts": "2026-01-10T06:00:00+00:00",
        "proposals": [{"symbol": "005930", "side": "BUY", "horizon": "day"}],
        **{k: meta[k] for k in ("context_ref", "context_sha256", "context_bytes")},
    }
    jp = tmp_path / "decisions.jsonl"
    jp.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    skipped = score_journal(journal_path=jp, data_dir=data, min_date="2026-06-01", min_n=1)
    assert skipped["skipped_date"] == 1
    kept = score_journal(journal_path=jp, data_dir=data, min_date="2026-01-01", min_n=1)
    assert kept["n"] >= 1


def test_redecide_does_not_call_broker_execute(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("broker.execute 호출됨")
    monkeypatch.setattr("src.broker.Broker.execute", boom)
    ctx = _ctx()
    raw = json.dumps(ctx, ensure_ascii=False)
    meta = persist_context(raw, cycle_ts=1_768_032_000.0, journal_path=tmp_path / "decisions.jsonl")
    rec = {
        "ts": "2026-01-10T06:00:00+00:00",
        "proposals": [{"symbol": "005930", "side": "BUY"}],
        **{k: meta[k] for k in ("context_ref", "context_sha256", "context_bytes")},
    }
    jp = tmp_path / "decisions.jsonl"
    jp.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    def respond(schema, system, user):
        return DecisionOutput(
            market_view="t",
            proposals=[Proposal(symbol="005930", market="KR", side="HOLD",
                                conviction=0.4, target_weight=0.0, thesis="r")])

    out = redecide_record(rec, jp, DecisionAgent(MockLLM(respond, model="m")))
    assert out["new_sides"]["005930"] == "HOLD"
    assert out["n_changed"] == 1


def test_consistency_offline_agreement():
    ctx = _ctx()
    raw = json.dumps(ctx, ensure_ascii=False)

    def decide(_blob: str):
        return DecisionOutput(
            market_view="t",
            proposals=[Proposal(symbol="005930", market="KR", side="HOLD",
                                conviction=0.5, target_weight=0.0, thesis="c")])

    rep = consistency_report(ctx, decide, n=5, context_json=raw)
    assert rep["exact_agreement"] == 1.0
    assert fleiss_kappa([["HOLD", "HOLD", "HOLD"]]) == 1.0
    assert "오프라인" in rep["note"]


def test_replay_score_cannot_promote():
    ok, why = can_promote(change="replay_score", evidence_n=999)
    assert ok is False
    assert "승격" in why or "리플레이" in why
    ok2, _ = can_promote(change="null_manager", evidence_n=999)
    assert ok2 is False


def test_proper_score_brier_and_log_loss(tmp_path):
    data = _history(tmp_path)
    ctx = _ctx()
    raw = json.dumps(ctx, ensure_ascii=False)
    meta = persist_context(raw, cycle_ts=1_768_032_000.0, journal_path=tmp_path / "decisions.jsonl")
    rec = {
        "ts": "2026-01-10T06:00:00+00:00",
        "proposals": [{
            "symbol": "005930", "side": "BUY", "horizon": "swing",
            "p_target_before_stop": 0.8,
        }],
        **{k: meta[k] for k in ("context_ref", "context_sha256", "context_bytes")},
        "manager": {"epoch": "test@abc"},
    }
    jp = tmp_path / "decisions.jsonl"
    jp.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    out = score_journal(journal_path=jp, data_dir=data, min_n=1)
    ps = out["proper_score"]
    assert ps["n"] >= 1
    assert ps["brier"] is not None
    assert ps["log_loss"] is not None
    assert brier_score([(0.8, True)]) == pytest.approx(0.04, abs=1e-6)
    assert log_loss([(0.8, True)]) == pytest.approx(-math.log(0.8), abs=1e-6)


def test_p_target_prompt_present():
    from src.agents.decision_agent import SYSTEM
    assert "p_target_before_stop" in SYSTEM
    assert "conviction 과 다르다" in SYSTEM

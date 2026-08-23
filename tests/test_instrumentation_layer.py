"""매니저 정체성 · 캘리브레이션 · 평가 프로토콜 · thesis 무효화."""
import json
import time
from pathlib import Path

from src.agents.manager_id import manager_snapshot, prompt_hash
from src.agents.cycle import CycleResult, run_cycle
from src.agents.schemas import DecisionOutput, Proposal, ValidationOutput, ValidationVerdict
from src.agents.llm import MockLLM
from src.calibration import conviction_calibration, sizing_enabled
from src.engine.store import Store
from src.eval_protocol import can_promote, register_experiment, shadow_only_label
from src.shadow_ledger import backfill_from_jsonl, book_blocked
from src.thesis_watch import audit_position, default_spec_from_dossier


def test_prompt_hash_stable():
    assert prompt_hash("abc") == prompt_hash("abc")
    assert prompt_hash("abc") != prompt_hash("abd")


def test_manager_snapshot_epoch():
    class L:
        model = "opus"
        last_model = "opus"
        used_fallback = False
        last_source = "cli"
    snap = manager_snapshot(decision_llm=L(), validation_llm=L(),
                            decision_prompt="DEC", validation_prompt="VAL")
    assert snap["decision"]["model"] == "opus"
    assert snap["decision"]["prompt_hash"] == prompt_hash("DEC")
    assert "@" in snap["epoch"]
    assert "fallback" not in snap["epoch"]


def test_manager_fallback_epoch():
    class L:
        model = "opus"
        last_model = "sonnet"
        used_fallback = True
        fallback_model = "sonnet"
        last_source = "cli"
    snap = manager_snapshot(decision_llm=L(), decision_prompt="D",
                            validation_prompt="V")
    assert snap["epoch"].endswith(":fallback")


def test_journal_includes_manager(tmp_path):
    from src.paper_account import PaperAccount
    from src.broker import Broker
    from src.risk_gate import RiskGate
    from src.risk import RiskManager
    from src.agents.decision_agent import DecisionAgent
    from src.agents.validation_agent import ValidationAgent

    def respond(schema, system, user):
        if schema is DecisionOutput:
            return DecisionOutput(
                market_view="t",
                proposals=[Proposal(symbol="005930", market="KR", side="HOLD",
                                    conviction=0.5, target_weight=0.1, thesis="h")])
        return ValidationOutput(verdicts=[])

    llm = MockLLM(respond, model="test-model")
    acct = PaperAccount(cash={"KR": 1_000_000}, fee_rate={"KR": 0.0},
                        slippage_bps={"KR": 0.0}, state_path=tmp_path / "a.json")
    gate = RiskGate({"capital": {"KR": 1_000_000}, "max_position_pct": 0.2,
                     "max_positions": 5, "kill_switch_file": str(tmp_path / "HALT")})
    broker = Broker(account=acct, gate=gate, client=None, mode="paper")
    risk = RiskManager(capital={"KR": 1_000_000}, max_position_pct=0.2)
    jp = tmp_path / "d.jsonl"
    res = run_cycle(context_json="{}", decision_agent=DecisionAgent(llm),
                    validation_agent=ValidationAgent(llm, min_conviction=0),
                    broker=broker, risk=risk, price_lookup={},
                    journal_path=jp)
    assert res.manager and res.manager.get("epoch")
    line = json.loads(jp.read_text(encoding="utf-8").strip())
    assert "manager" in line
    assert line["manager"]["decision"]["model"] == "test-model"


def test_calibration_flat_until_proven(tmp_path):
    store = Store(tmp_path / "t.db")
    cal = conviction_calibration(store)
    assert cal["calibrated"] is False
    assert cal["n"] == 0
    assert sizing_enabled(store, configured=True) is False
    assert sizing_enabled(store, configured=False) is False


def test_calibration_with_meta(tmp_path):
    store = Store(tmp_path / "t.db")
    for i, (c, exit_) in enumerate([(0.3, 90), (0.3, 95), (0.8, 120), (0.8, 110)] * 6):
        pid = store.open_position(f"S{i}", "KR", 10, 100, strategy="t",
                                  meta={"conviction": c})
        store.close_position(pid, exit_price=exit_, reason="t")
    cal = conviction_calibration(store, since_days=365)
    assert cal["n"] >= 20
    assert cal["brier"] is not None
    # 고확신 승률이 더 높으면 calibrated
    assert "by_bin" in cal


def test_eval_promote_protected(tmp_path):
    reg = tmp_path / "reg.json"
    ok, why = can_promote(change="validation_rules", evidence_n=100,
                          registry_path=reg)
    assert ok is False
    exp = register_experiment(
        name="t", hypothesis="h", metric="m", kill_if="k", min_n=20,
        touches=["validation_rules"], path=reg)
    ok2, _ = can_promote(change="validation_rules", evidence_n=5,
                         experiment_id=exp["id"], registry_path=reg)
    assert ok2 is False  # 표본 부족
    # status registered → still blocked (need pass/running)
    data = json.loads(reg.read_text(encoding="utf-8"))
    data["experiments"][0]["status"] = "pass"
    reg.write_text(json.dumps(data), encoding="utf-8")
    ok3, why3 = can_promote(change="validation_rules", evidence_n=25,
                            experiment_id=exp["id"], registry_path=reg)
    assert ok3 is True, why3
    assert "관심 섀도" in shadow_only_label({"n": 3})


def test_thesis_audit_price_and_time():
    now = time.time()
    pos = {
        "symbol": "005930",
        "opened_at": now - 25 * 86400,
        "stop_price": 100,
        "meta": json.dumps({
            "thesis_invalidation": {
                "price": 100,
                "time": {"max_days": 20},
            }
        }),
    }
    hits = audit_position(pos, price=90, now=now)
    kinds = {h.kind for h in hits}
    assert "price" in kinds and "time" in kinds


def test_default_spec_from_dossier():
    spec = default_spec_from_dossier({"invalidation": 50.0}, "swing")
    assert spec["price"] == 50.0
    assert spec["time"]["max_days"] == 20


def test_backfill_from_jsonl(tmp_path):
    store = Store(tmp_path / "t.db")
    hist = tmp_path / "history"
    hist.mkdir()
    (hist / "005930_1d_1y.csv").write_text(
        "Date,Open,High,Low,Close,Volume\n"
        "2026-01-10,100,100,100,100,1\n"
        "2026-01-11,101,101,101,101,1\n",
        encoding="utf-8")
    jl = tmp_path / "dec.jsonl"
    rec = {
        "ts": "2026-01-10T05:00:00+00:00",
        "proposals": [{"symbol": "005930", "market": "KR", "side": "BUY",
                       "conviction": 0.7, "horizon": "swing",
                       "target_weight": 0.1, "thesis": "t"}],
        "verdicts": [{"symbol": "005930", "approved": False, "reason": "rule3",
                      "concerns": ["rule3"]}],
        "executed": [{"symbol": "005930", "action": "BUY", "status": "vetoed",
                      "reason": "rule3"}],
    }
    jl.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    out = backfill_from_jsonl(store, jl, sleeve="brain", data_dir=tmp_path)
    assert out["booked"] == 1
    out2 = backfill_from_jsonl(store, jl, sleeve="brain", data_dir=tmp_path)
    assert out2["dup"] == 1

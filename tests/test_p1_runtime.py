"""Phase 1 회귀 — wiring/orchestrator/cycle journal 경계."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.ops_golden


def test_p1_pipeline_shim_reexports():
    from src.agents import pipeline, wiring, cycle_runner
    assert pipeline.CycleRunner is cycle_runner.CycleRunner
    assert pipeline.build_paper_core is wiring.build_paper_core
    assert pipeline.resolve_execution_mode is wiring.resolve_execution_mode


def test_p1_orchestrator_surface():
    from src.engine.orchestrator import RUNTIME_WORKER_KEYS, run_from_args
    assert callable(run_from_args)
    assert "brain" in RUNTIME_WORKER_KEYS
    assert "watch_loop" in RUNTIME_WORKER_KEYS


def test_p1_watcher_alias_disclosure(tmp_path, monkeypatch):
    """disclosure 블록만 있어도 load_config 가 watcher 로 병합."""
    import yaml
    from src.config import load_config, ROOT

    cfg_path = tmp_path / "c.yaml"
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    disc = dict(raw.get("watcher") or {})
    disc["enabled"] = True
    disc["dart_poll_sec_active"] = 99
    raw.pop("watcher", None)
    raw["disclosure"] = disc
    cfg_path.write_text(yaml.dump(raw), encoding="utf-8")
    monkeypatch.setenv("ARGUS_CONFIG", str(cfg_path))
    monkeypatch.setenv("ARGUS_DISABLE_DYNAMIC_UNIVERSE", "1")
    cfg = load_config()
    assert cfg.raw["watcher"]["dart_poll_sec_active"] == 99


def test_p1_cycle_journals_before_reraise(tmp_path):
    """execute 중 예외 시 이미 쌓인 executed 를 저널에 남긴 뒤 재전파."""
    from src.agents.cycle import run_cycle
    from src.agents.schemas import DecisionOutput, Proposal, ValidationOutput, ValidationVerdict
    from src.risk_gate import Order

    class Dec:
        SYSTEM = "d"
        def decide(self, _ctx):
            return DecisionOutput(
                market_view="t",
                proposals=[
                    Proposal(symbol="AAA", market="KR", side="BUY", conviction=0.9,
                             horizon="swing", target_weight=0.1, thesis="one", key_risks=[]),
                    Proposal(symbol="BBB", market="KR", side="BUY", conviction=0.9,
                             horizon="swing", target_weight=0.1, thesis="two", key_risks=[]),
                ],
            )

    class Val:
        SYSTEM = "v"
        def review(self, _ctx, decision):
            return ValidationOutput(verdicts=[
                ValidationVerdict(symbol="AAA", approved=True, reason="ok"),
                ValidationVerdict(symbol="BBB", approved=True, reason="ok"),
            ])

    class BoomBroker:
        def __init__(self):
            self.n = 0
            self.last_reject_reason = None

        def position(self, _sym):
            return MagicMock(qty=0)

        def execute(self, order: Order, reason: str = "", **kw):
            self.n += 1
            if self.n >= 2:
                raise RuntimeError("boom")
            from src.fill_result import ExecuteResult
            return ExecuteResult(
                ok=True, filled_qty=order.qty, avg_price=order.price, fee=0.0,
                order_qty=order.qty, limit_price=order.price, status="FILLED",
                order_id="1", reject_reason="", side=order.side,
            )

    journal = tmp_path / "decisions.jsonl"
    risk = MagicMock()
    risk.capital = {"KR": 1_000_000}
    risk.size_buy = lambda *a, **k: 1.0

    with pytest.raises(RuntimeError, match="boom"):
        run_cycle(
            context_json="{}",
            decision_agent=Dec(),
            validation_agent=Val(),
            broker=BoomBroker(),
            risk=risk,
            price_lookup={"AAA": 100.0, "BBB": 100.0},
            journal_path=journal,
            dossier_fn=None,
            zone_fn=None,
            arm_fn=None,
        )
    assert journal.is_file()
    rec = json.loads(journal.read_text(encoding="utf-8").strip().splitlines()[0])
    # 첫 체결은 저널에 남아야 함
    assert any(e.get("symbol") == "AAA" for e in rec["executed"])

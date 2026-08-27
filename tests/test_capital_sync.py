"""capital_sync — 실자산 → risk.capital 폴백 동기화."""
from __future__ import annotations

from src.capital_sync import (apply_capital_sync, equity_by_market, should_update)
from src.paper_account import PaperAccount
from src.risk import RiskManager
from src.risk_gate import RiskGate


def test_should_update_thresholds():
    assert should_update(0, 100, min_pct=0.02, min_abs=10)
    assert not should_update(1000, 1010, min_pct=0.02, min_abs=50)   # 1% < 2%, abs 10 < 50
    assert should_update(1000, 1030, min_pct=0.02, min_abs=10)      # 3%
    assert not should_update(1000, 0, min_pct=0.02, min_abs=10)


def test_apply_syncs_gate_risk_and_cfg(tmp_path):
    acct = PaperAccount(cash={"KR": 2_050_000, "US": 800},
                        state_path=tmp_path / "a.json")
    gate = RiskGate({"capital": {"KR": 1_000_000, "US": 500},
                     "kill_switch_file": str(tmp_path / "HALT")})
    risk = RiskManager(capital={"KR": 1_000_000, "US": 500})
    cfg = {"risk": {"capital": {"KR": 1_000_000, "US": 500}}}

    out = apply_capital_sync(
        gate=gate, risk=risk, cfg_raw=cfg, account=acct,
        markets=("KR", "US"),
        sync_cfg={"enabled": True, "min_change_pct": 0.02,
                  "min_change_abs": {"KR": 10_000, "US": 10}})

    assert "KR" in out["changed"] and "US" in out["changed"]
    assert gate.capital["KR"] == 2_050_000
    assert risk.capital["US"] == 800
    assert cfg["risk"]["capital"]["KR"] == 2_050_000


def test_disabled_noop(tmp_path):
    acct = PaperAccount(cash={"KR": 3_000_000}, state_path=tmp_path / "a.json")
    gate = RiskGate({"capital": {"KR": 1_000_000},
                     "kill_switch_file": str(tmp_path / "HALT")})
    out = apply_capital_sync(
        gate=gate, account=acct, markets=("KR",),
        sync_cfg={"enabled": False})
    assert out["enabled"] is False
    assert gate.capital["KR"] == 1_000_000


def test_equity_by_market_skips_nonpositive(tmp_path):
    acct = PaperAccount(cash={"KR": 100, "US": 0}, state_path=tmp_path / "a.json")
    eq = equity_by_market(acct, ("KR", "US"))
    assert eq == {"KR": 100.0}

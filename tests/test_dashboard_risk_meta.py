"""대시보드 risk 메타 — sector_map 없이 RiskGate 를 만들면 경고 스팸."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import dashboard as dash  # noqa: E402


def test_risk_control_meta_passes_sector_map(monkeypatch, caplog):
    cfg = SimpleNamespace(
        raw={"risk": {"max_sector_pct": 0.4, "capital": {"KR": 1_000_000},
                      "max_positions": {"KR": 5, "US": 3}}},
        universe={"KR": [{"symbol": "005930", "sector": "정보기술"}],
                  "US": [{"symbol": "AAPL", "sector": "정보기술"}]},
        risk={"max_sector_pct": 0.4, "capital": {"KR": 1_000_000},
              "max_positions": {"KR": 5, "US": 3}},
    )
    monkeypatch.setattr("src.config.load_config", lambda: cfg)
    with caplog.at_level(logging.WARNING, logger="risk.gate"):
        meta = dash._risk_control_meta({"positions": {}, "symbol_market": {}})
    assert meta["max_positions"]["KR"] == 5
    assert not any("sector_map" in r.getMessage() for r in caplog.records)

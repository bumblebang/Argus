"""exit_policy — horizon별 시간 손절."""
from __future__ import annotations

import time

import yaml

from src.engine.exit_policy import (
    ExitPolicyConfig,
    close_scan_exit_trigger,
    days_held,
    horizon_of,
    parse_exit_policy,
    time_stop_trigger,
)
from src.engine.loop import WatchConfig


def test_parse_disabled_by_default():
    cfg = parse_exit_policy({})
    assert cfg.enabled is False


def test_parse_enabled_from_yaml():
    raw = yaml.safe_load("""
exit_policy:
  enabled: true
  time_stop:
    enabled: true
    by_horizon:
      swing: { max_days: 15 }
      position: { max_days: 90 }
    exclude_strategy: ["value", "custom"]
""")
    cfg = parse_exit_policy(raw)
    assert cfg.enabled is True
    assert cfg.max_days == {"swing": 15, "position": 90}
    assert cfg.exclude_strategies == frozenset({"value", "custom"})


def test_time_stop_fires_swing():
    cfg = ExitPolicyConfig(enabled=True, max_days={"swing": 20, "position": 120},
                           exclude_strategies=frozenset({"value"}))
    now = time.time()
    pos = {
        "symbol": "005930",
        "qty": 10,
        "strategy": "rsi_reversion",
        "opened_at": now - 21 * 86400,
        "meta": {"horizon": "swing"},
    }
    t = time_stop_trigger(pos, cfg=cfg, now=now)
    assert t is not None and t.kind == "time_stop" and t.urgency == "act"


def test_time_stop_skips_value():
    cfg = ExitPolicyConfig(enabled=True, max_days={"swing": 20, "position": 120},
                           exclude_strategies=frozenset({"value"}))
    now = time.time()
    pos = {
        "symbol": "X",
        "qty": 1,
        "strategy": "value",
        "opened_at": now - 200 * 86400,
        "meta": {"horizon": "position"},
    }
    assert time_stop_trigger(pos, cfg=cfg, now=now) is None


def test_time_stop_skips_day_horizon():
    cfg = ExitPolicyConfig(enabled=True, max_days={"swing": 20, "position": 120})
    now = time.time()
    pos = {
        "symbol": "X",
        "qty": 1,
        "strategy": "volatility_breakout",
        "opened_at": now - 5 * 86400,
        "meta": {"horizon": "day"},
    }
    assert time_stop_trigger(pos, cfg=cfg, now=now) is None


def test_horizon_from_strategy_class():
    pos = {"strategy": "ma_crossover", "meta": {}}
    assert horizon_of(pos) == "position"


def test_days_held():
    now = 1_000_000.0
    assert days_held({"opened_at": now - 86400}, now) == 1.0


def test_time_stop_skips_close_scan_horizon():
    cfg = ExitPolicyConfig(enabled=True, max_days={"swing": 20, "position": 120})
    now = time.time()
    pos = {
        "symbol": "X",
        "qty": 1,
        "strategy": "rsi_reversion",
        "opened_at": now - 5 * 86400,
        "meta": {"horizon": "close_scan"},
    }
    assert time_stop_trigger(pos, cfg=cfg, now=now) is None


def test_close_scan_exit_next_trading_day(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    kst = ZoneInfo("Asia/Seoul")
    opened = datetime(2026, 8, 27, 15, 25, tzinfo=kst).timestamp()
    now = datetime(2026, 8, 28, 9, 5, tzinfo=kst).timestamp()
    pos = {
        "symbol": "085620",
        "qty": 1,
        "opened_at": opened,
        "meta": {"horizon": "close_scan"},
    }
    monkeypatch.setattr("src.market_hours.market_day", lambda m, dt: dt.strftime("%Y-%m-%d"))
    monkeypatch.setattr("src.market_hours.is_tradable", lambda *a, **k: True)
    monkeypatch.setattr("src.market_hours.current_session", lambda *a, **k: "regular")
    t = close_scan_exit_trigger(pos, market="KR", now=now)
    assert t is not None and t.kind == "close_scan_exit"


def test_watch_config_parses_exit_policy():
    raw = {
        "exit_policy": {
            "enabled": True,
            "time_stop": {"enabled": True, "by_horizon": {"swing": {"max_days": 10}}},
        }
    }
    wc = WatchConfig.from_config(raw)
    assert wc.exit_policy is not None and wc.exit_policy.enabled
    assert wc.exit_policy.max_days["swing"] == 10

"""외부 뇌 각성 요청(Athena → watch) 회귀."""
from __future__ import annotations

import time
from pathlib import Path

from src.engine.loop import WatchConfig, WatchLoop
from src.engine.store import Store
from src.engine.wake_request import consume_brain_wake, request_brain_wake


class _FakeGW:
    def poll_prices(self, symbols, record=True):
        return []

    def candles(self, *a, **k):
        return []


def test_request_consume_round_trip(tmp_path):
    p = tmp_path / "wake.json"
    request_brain_wake(reason="athena_done", market="KR", path=p,
                       extra={"done": 3}, now=time.time())
    got = consume_brain_wake(p)
    assert got["reason"] == "athena_done" and got["market"] == "KR"
    assert got["extra"]["done"] == 3
    assert consume_brain_wake(p) is None


def test_consume_stale_ignored(tmp_path):
    p = tmp_path / "wake.json"
    request_brain_wake(reason="old", path=p, now=time.time() - 7 * 3600)
    assert consume_brain_wake(p, max_age_sec=6 * 3600) is None
    assert not p.exists()


def test_loop_consumes_wake_when_markets_closed(tmp_path, monkeypatch):
    """휴장 idle 틱에서도 Athena 요청을 소비해 뇌를 깨운다."""
    import src.engine.loop as loop_mod
    monkeypatch.setattr(loop_mod, "is_tradable", lambda *a, **k: False)

    wakes: list[tuple] = []
    store = Store(tmp_path / "bot.db")
    req = tmp_path / "wake.json"
    request_brain_wake(reason="athena_done", market="KR", path=req)

    cfg = WatchConfig(wake_request_path=str(req), brain_interval_sec=0)
    loop = WatchLoop(_FakeGW(), store, lambda: {}, markets=("KR",),
                     config=cfg,
                     on_wake=lambda r, t, *, at=None, market=None: wakes.append((r, market)))
    res = loop.run_once()
    assert res.woke is True
    assert wakes == [("athena_done", "KR")]
    assert not req.exists()


def test_watch_config_parses_wake_path():
    cfg = WatchConfig.from_config({
        "watch": {
            "brain_interval_sec": 3600,
            "brain_sessions": {"KR": ["regular"], "US": []},
            "extra_wakes": {"KR": ["15:30"]},
            "wake_request_path": "data/brain_wake_request.json",
        },
        "trading_sessions": {"KR": ["regular"]},
    })
    assert cfg.brain_interval_sec == 3600
    assert cfg.brain_sessions["KR"] == ("regular",)
    assert cfg.extra_wakes["KR"] == ("15:30",)
    assert cfg.wake_request_path.endswith("brain_wake_request.json")

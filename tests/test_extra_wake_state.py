"""extra_wakes 영속 dedup · catch-up 창 · 재기동 grace."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.engine.extra_wake_state import (
    load_fired, save_fired, should_fire_extra, minutes_since_hhmm, reason_for_extra,
    is_pool_only_reason,
)
from src.engine.loop import WatchLoop, WatchConfig
from src.engine.store import Store
from tests.test_engine_loop import _kst_epoch
from tests.test_premarket_sessions import FakeGateway, _wl_kr, _sessions


def test_load_save_roundtrip(tmp_path):
    p = tmp_path / "extra.json"
    save_fired({("KR", "08:00"): "2026-07-22"}, path=p)
    assert load_fired(p) == {("KR", "08:00"): "2026-07-22"}


def test_minutes_since_hhmm():
    kst = ZoneInfo("Asia/Seoul")
    now = datetime(2026, 7, 22, 9, 2, tzinfo=kst)
    assert minutes_since_hhmm(now, "09:00") == 2
    assert minutes_since_hhmm(now, "09:05") is None
    assert minutes_since_hhmm(now, "10:00") is None


def test_should_fire_window_and_grace():
    fired: dict = {}
    ts = _kst_epoch(2026, 7, 22, 9, 2)
    assert should_fire_extra(
        market="KR", hhmm="09:00", trading_day="2026-07-22",
        fired=fired, now_ts=ts, grace_until=0, window_min=5)
    assert not should_fire_extra(
        market="KR", hhmm="08:00", trading_day="2026-07-22",
        fired=fired, now_ts=ts, grace_until=0, window_min=5)
    assert not should_fire_extra(
        market="KR", hhmm="09:00", trading_day="2026-07-22",
        fired=fired, now_ts=ts, grace_until=ts + 60, window_min=5)


def test_reason_for_extra_gap_slots():
    assert reason_for_extra("KR", "15:15") == "gap_pool_refresh"
    assert is_pool_only_reason("gap_pool_refresh")
    assert reason_for_extra("KR", "15:20") == "gap_rebound_scan"
    assert reason_for_extra("KR", "19:50") == "nxt_gap_scan"
    assert reason_for_extra("KR", "08:00") == "extra"
    assert reason_for_extra("US", "17:00") == "extra"


def test_gap_rebound_scan_wake(tmp_path, monkeypatch):
    """15:20 KR → gap_rebound_scan reason + at/market 메타."""
    _sessions(monkeypatch, {"KR": "regular"})
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})
    cfg = WatchConfig(
        trading_sessions={"KR": ("regular",)},
        extra_wakes={"KR": ("15:20",)},
        extra_wake_state_path=str(tmp_path / "ew.json"),
        extra_wake_grace_sec=0,
        extra_wake_window_min=10,
    )
    woke: list[str] = []
    meta: list[dict] = []

    def _capture(reason, triggers, **kw):
        woke.append(reason)
        meta.append(kw)

    loop = WatchLoop(gw, store, _wl_kr("005930"), markets=("KR",), config=cfg,
                     on_wake=_capture,
                     now_fn=lambda: _kst_epoch(2026, 7, 22, 15, 21))
    loop.run_once()
    assert woke == ["gap_rebound_scan"]
    assert meta[0].get("at") == "15:20"
    assert meta[0].get("market") == "KR"


def test_persist_skips_refire_after_restart(tmp_path, monkeypatch):
    """같은 거래일·슬롯은 재기동 후에도 디스크 dedup."""
    _sessions(monkeypatch, {"KR": "premarket"})
    state = tmp_path / "ew.json"
    db = tmp_path / "t.db"
    gw = FakeGateway({"005930": 70000})
    cfg = WatchConfig(
        trading_sessions={"KR": ("regular", "premarket")},
        extra_wakes={"KR": ("08:00",)},
        extra_wake_state_path=str(state),
        extra_wake_grace_sec=0,
        extra_wake_window_min=10,
    )
    t0 = _kst_epoch(2026, 7, 22, 8, 0)
    woke = []

    loop1 = WatchLoop(gw, Store(db), _wl_kr("005930"), markets=("KR",), config=cfg,
                      on_wake=lambda w, t: woke.append(w), now_fn=lambda: t0)
    loop1.run_once()
    assert woke == ["extra"]
    assert state.is_file()

    loop2 = WatchLoop(gw, Store(db), _wl_kr("005930"), markets=("KR",), config=cfg,
                      on_wake=lambda w, t: woke.append(w), now_fn=lambda: t0)
    loop2.run_once()
    assert woke == ["extra"]


def test_grace_blocks_immediate_after_loop_start(tmp_path, monkeypatch):
    _sessions(monkeypatch, {"KR": "premarket"})
    state = tmp_path / "ew.json"
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})
    cfg = WatchConfig(
        trading_sessions={"KR": ("regular", "premarket")},
        extra_wakes={"KR": ("08:00",)},
        extra_wake_state_path=str(state),
        extra_wake_grace_sec=300,
        extra_wake_window_min=10,
    )
    t0 = _kst_epoch(2026, 7, 22, 8, 0)
    woke = []
    loop = WatchLoop(gw, store, _wl_kr("005930"), markets=("KR",), config=cfg,
                     on_wake=lambda w, t: woke.append(w), now_fn=lambda: t0)
    loop.run_forever(max_ticks=1)
    assert woke == []

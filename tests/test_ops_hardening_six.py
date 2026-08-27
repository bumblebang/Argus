"""하트비트 ok/polled 판정 · 푸시 실패 재시도 · 락 호환 · Store readonly · live-smoke 가드."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def test_evaluate_flags_open_market_zero_poll():
    import alert_check as ac
    r = ac.evaluate(1_000_000.0, hb_age=2.0, market_open=True, brain_mode="ok",
                    hb_ok=False, hb_polled=0, hb_markets_open=["KR"])
    assert any("폴링 실패" in x for x in r)


def test_evaluate_idle_zero_poll_ok():
    """휴장(markets_open=[]) polled=0 은 정상."""
    import alert_check as ac
    r = ac.evaluate(1_000_000.0, hb_age=2.0, market_open=False, brain_mode="ok",
                    hb_ok=True, hb_polled=0, hb_markets_open=[])
    assert r == []


def test_push_rejects_http_error(monkeypatch):
    import alert_check as ac
    monkeypatch.setattr(ac, "_ntfy_topic", lambda: "t")

    class _Resp:
        status_code = 503

    monkeypatch.setattr(ac.requests, "post", lambda *a, **k: _Resp())
    assert ac._push("t", "m") is False


def test_push_accepts_2xx(monkeypatch):
    import alert_check as ac
    monkeypatch.setattr(ac, "_ntfy_topic", lambda: "t")

    class _Resp:
        status_code = 200

    monkeypatch.setattr(ac.requests, "post", lambda *a, **k: _Resp())
    assert ac._push("t", "m") is True


def test_push_live_orders_does_not_advance_on_failure(tmp_path, monkeypatch):
    import alert_check as ac
    from src.engine.store import Store
    db = tmp_path / "bot.db"
    store = Store(db)
    store.log_event("live_order", "005930",
                    {"side": "BUY", "qty": 1, "price": 1, "order_id": "O1"})
    monkeypatch.setattr(ac, "DB", db)
    monkeypatch.setattr(ac, "_PUSH_STATE", tmp_path / "push_state.json")
    monkeypatch.setattr(ac, "_ntfy_topic", lambda: "t")
    monkeypatch.setattr(ac, "_push", lambda *a, **k: False)
    ac._push_live_orders(0)
    st = ac._load_push_state()
    assert "last_order_ts" not in st or st.get("last_order_ts", 0) == 0


def test_new_code_blocks_legacy_derived_lock(tmp_path):
    """신 코드가 레거시 락도 같이 잡으면 구 파생 락 기동이 막힌다."""
    from src.engine.singleton import AlreadyRunning, SingleInstance
    legacy = tmp_path / "data" / "watch.pid.lock"
    primary = tmp_path / "data" / "state" / "watch.pid.lock"
    new = SingleInstance(tmp_path / "data" / "state" / "watch.pid",
                         lockfile=primary,
                         also_lockfiles=[legacy]).acquire()
    try:
        with pytest.raises(AlreadyRunning):
            SingleInstance(tmp_path / "data" / "watch.pid").acquire()  # 구: 파생 락
    finally:
        new.release()


def test_store_readonly_skips_migrate(tmp_path):
    from src.engine.store import Store
    db = tmp_path / "bot.db"
    Store(db).close()
    ro = Store(db, readonly=True)
    try:
        # 쓰기가 막혀야 한다(ro URI)
        with pytest.raises(Exception):
            ro.conn.execute("CREATE TABLE should_fail(x INT)")
            ro.conn.commit()
    finally:
        ro.close()


def test_live_smoke_blocks_dry_run(tmp_path, monkeypatch):
    import live_smoke as ls
    cfg = SimpleNamespace(dry_run=True, raw={"risk": {
        "capital": {"KR": 10_000_000},
        "kill_switch_file": str(tmp_path / "HALT")}})
    assert ls._guard_confirm(cfg, "005930", "BUY", 70_000) is not None
    assert "DRY_RUN" in ls._guard_confirm(cfg, "005930", "BUY", 70_000)


def test_live_smoke_blocks_halt(tmp_path):
    import live_smoke as ls
    halt = tmp_path / "HALT"
    halt.write_text("x")
    cfg = SimpleNamespace(dry_run=False, raw={"risk": {
        "capital": {"KR": 10_000_000},
        "max_position_pct": 1.0,
        "max_positions": 20,
        "daily_loss_limit_pct": 1.0,
        "kill_switch_file": str(halt)}})
    msg = ls._guard_confirm(cfg, "005930", "BUY", 70_000)
    assert msg and "HALT" in msg


def test_heartbeat_ok_false_on_tick_error(tmp_path):
    from src.engine.loop import TickResult, WatchLoop
    from src.engine.store import Store
    # minimal stub — only _beat
    class _L:
        heartbeat_path = tmp_path / "hb.json"
        _ticks = 1
        def _now(self):
            return 1000.0
    WatchLoop._beat(_L(), TickResult(markets_open=["KR"], polled=0), tick_error=True)
    d = json.loads((tmp_path / "hb.json").read_text(encoding="utf-8"))
    assert d["ok"] is False and d["tick_error"] is True and d["polled"] == 0

"""뇌 모드·리셋 파싱·armed 게이트·회로차단 회귀."""
from __future__ import annotations

import time

import pytest

from src.agents.llm import (BrainQuotaError, ClaudeCLIError, ClaudeCLIClient,
                            is_bridge_armed, parse_reset_at, write_bridge_heartbeat)
from src.agents import DecisionOutput
from src.engine import brain_mode as bm
from src.engine.brain import BrainWorker
from src.engine.store import Store


def test_parse_reset_at_weekly_date():
    # 2026-08-07 21:00 KST 기준 → Aug 10 6pm KST
    now = time.mktime(time.strptime("2026-08-07 21:00:00", "%Y-%m-%d %H:%M:%S"))
    # use fixed epoch via datetime for Seoul — parse_reset_at uses ZoneInfo
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime(2026, 8, 7, 21, 0, tzinfo=ZoneInfo("Asia/Seoul")).timestamp()
    msg = "You've hit your weekly limit · resets Aug 10, 6pm (Asia/Seoul)"
    ts = parse_reset_at(msg, now=now)
    assert ts is not None
    got = datetime.fromtimestamp(ts, tz=ZoneInfo("Asia/Seoul"))
    assert (got.month, got.day, got.hour) == (8, 10, 18)


def test_parse_reset_at_clock_only():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime(2026, 8, 7, 21, 0, tzinfo=ZoneInfo("Asia/Seoul")).timestamp()
    ts = parse_reset_at("session limit resets 5:20am", now=now)
    got = datetime.fromtimestamp(ts, tz=ZoneInfo("Asia/Seoul"))
    # 다음날 05:20
    assert got.hour == 5 and got.minute == 20
    assert got.day == 8


def test_parse_reset_at_missing_returns_none():
    assert parse_reset_at("something else", now=1_000_000) is None


def test_bridge_armed_heartbeat(tmp_path):
    inbox = tmp_path / "inbox"
    assert is_bridge_armed(inbox, max_age_sec=90, now=1000) is False
    write_bridge_heartbeat(inbox, now=1000)
    assert is_bridge_armed(inbox, max_age_sec=90, now=1050) is True
    assert is_bridge_armed(inbox, max_age_sec=90, now=1100) is False


def test_cli_unarmed_raises_quota_not_bridge(tmp_path):
    """한도 + require_armed + heartbeat 없음 → BrainQuotaError, bridge 미호출."""
    called = []

    class Bridge:
        inbox_dir = tmp_path / "inbox"

        def structured(self, system, user, schema):
            called.append(1)
            return DecisionOutput(market_view="nope", proposals=[])

    cli = ClaudeCLIClient(command="claude", model="opus", error_dump_path=None,
                          cursor_bridge=Bridge(), require_bridge_armed=True)

    def boom(_prompt):
        raise ClaudeCLIError(
            "claude CLI 오류(rc=1): You've hit your weekly limit · resets Aug 10, 6pm",
            rc=1)

    cli._run = boom  # type: ignore[method-assign]
    with pytest.raises(BrainQuotaError) as ei:
        cli.structured("sys", "{}", DecisionOutput)
    assert ei.value.bridge_armed is False
    assert ei.value.reset_at is not None
    assert called == []


def test_cli_armed_uses_bridge(tmp_path):
    inbox = tmp_path / "inbox"
    write_bridge_heartbeat(inbox, now=time.time())

    class Bridge:
        inbox_dir = inbox

        def structured(self, system, user, schema):
            return DecisionOutput(market_view="from-bridge", proposals=[])

    cli = ClaudeCLIClient(command="claude", model="opus", error_dump_path=None,
                          cursor_bridge=Bridge(), require_bridge_armed=True,
                          bridge_armed_max_age_sec=90)

    def boom(_prompt):
        raise ClaudeCLIError("weekly limit resets 6pm", rc=1)

    cli._run = boom  # type: ignore[method-assign]
    out = cli.structured("sys", "{}", DecisionOutput)
    assert out.market_view == "from-bridge"
    assert cli.last_source == "bridge"


def test_should_skip_circuit_until_reset(tmp_path):
    path = tmp_path / "brain_mode.json"
    now = 1_000_000.0
    bm.set_mode(path, "circuit_open", reason="quota_no_bridge",
                reset_at=now + 3600, now=now)
    st = bm.load_mode(path)
    skip, why = bm.should_skip_wake(st, now=now + 10, bridge_armed=False)
    assert skip and why == "circuit_open"
    # 리셋 후 probe 허용
    skip2, _ = bm.should_skip_wake(st, now=now + 4000, bridge_armed=False)
    assert not skip2
    # 재무장도 probe 허용
    skip3, _ = bm.should_skip_wake(st, now=now + 10, bridge_armed=True)
    assert not skip3


def test_brainworker_circuit_skips_wake(tmp_path):
    mode_path = tmp_path / "brain_mode.json"
    store = Store(tmp_path / "t.db")
    now = [1_000_000.0]
    bm.set_mode(mode_path, "circuit_open", reset_at=now[0] + 10_000, now=now[0])
    calls = []
    bw = BrainWorker(lambda: calls.append(1) or "ok", store=store,
                     mode_path=mode_path, now_fn=lambda: now[0])
    bw.wake()
    assert bw.run_pending() is False
    assert calls == [] and bw.skipped == 1
    kinds = [r["kind"] for r in store.conn.execute("SELECT kind FROM events").fetchall()]
    assert "brain_skip" in kinds


def test_brainworker_quota_opens_circuit(tmp_path):
    mode_path = tmp_path / "brain_mode.json"
    store = Store(tmp_path / "t.db")

    def boom():
        raise BrainQuotaError("quota", reset_at=2_000_000.0, bridge_armed=False)

    bw = BrainWorker(boom, store=store, mode_path=mode_path)
    bw.wake()
    assert bw.run_pending() is False
    st = bm.load_mode(mode_path)
    assert st["mode"] == "circuit_open"
    assert st["reset_at"] == 2_000_000.0


def test_brainworker_bridge_success_sets_mode(tmp_path):
    mode_path = tmp_path / "brain_mode.json"
    store = Store(tmp_path / "t.db")
    bw = BrainWorker(
        lambda: "ok", store=store, mode_path=mode_path,
        source_fn=lambda: "bridge",
        quota_info_fn=lambda: {
            "kind": "weekly", "reset_at": 2_100_000.0,
            "error": "weekly limit resets 6pm",
        })
    bw.wake()
    assert bw.run_pending() is True
    st = bm.load_mode(mode_path)
    assert st["mode"] == "bridge"
    assert st.get("quota_kind") == "weekly"
    assert st.get("reset_at") == 2_100_000.0


def test_brainworker_bridge_fail_threshold_opens(tmp_path):
    mode_path = tmp_path / "brain_mode.json"
    store = Store(tmp_path / "t.db")
    bm.set_mode(mode_path, "bridge", reason="cursor_bridge")

    def boom():
        raise TimeoutError("cursor_bridge 타임아웃(240.0s) schema=DecisionOutput: 응답 없음")

    bw = BrainWorker(boom, store=store, mode_path=mode_path, circuit_fail_threshold=2)
    bw.wake(); assert bw.run_pending() is False
    assert bm.load_mode(mode_path)["mode"] == "bridge"   # 1회는 유지
    bw.wake(); assert bw.run_pending() is False
    assert bm.load_mode(mode_path)["mode"] == "circuit_open"

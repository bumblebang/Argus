"""engine.brain — 비동기 단일실행 워커: 실행/coalesce/쿨다운/예외/로깅 검증.

스레드 타이밍 의존을 피하려고 run_pending() 을 직접 호출(워커 로직 단위테스트).
별도로 start()/stop() 스레드 경로도 한 번 확인한다.
"""
import threading
import time

from src.engine.brain import BrainWorker
from src.engine.store import Store


def test_run_pending_executes_and_logs(tmp_path):
    store = Store(tmp_path / "t.db")
    calls = []
    bw = BrainWorker(lambda: calls.append(1) or "ok", store=store)
    bw.wake("act_triggers", [object(), object()])
    assert bw.run_pending() is True
    assert calls == [1] and bw.runs == 1
    kinds = [r["kind"] for r in store.conn.execute("SELECT kind FROM events").fetchall()]
    assert "brain_start" in kinds and "brain_done" in kinds


def test_wake_passed_to_cycle_fn_with_trigger_summary(tmp_path):
    """cycle_fn(wake=...) 을 받으면 사유·트리거 요약이 전달된다."""
    from src.engine.triggers import Trigger

    store = Store(tmp_path / "t.db")
    seen = []

    def cycle(wake=None):
        seen.append(wake)
        return "ok"

    bw = BrainWorker(cycle, store=store)
    bw.wake("wake_triggers", [
        Trigger("vol_spike", "005930", "act", "급변 +3%", {"change_pct": 0.03}),
    ])
    assert bw.run_pending() is True
    assert len(seen) == 1 and seen[0]["reason"] == "wake_triggers"
    assert seen[0]["n"] == 1
    assert seen[0]["triggers"][0]["kind"] == "vol_spike"
    assert seen[0]["triggers"][0]["symbol"] == "005930"


def test_wake_kwargs_fallback_for_noarg_cycle_fn(tmp_path):
    """wake 인자를 모르는 cycle_fn 은 무인자 호출로 폴백(밸류 워커 호환)."""
    calls = []
    bw = BrainWorker(lambda: calls.append("ran") or "ok", store=Store(tmp_path / "t.db"))
    bw.wake("periodic", [])
    assert bw.run_pending() is True and calls == ["ran"]


def test_cooldown_skips(tmp_path):
    store = Store(tmp_path / "t.db")
    now = [1000.0]
    bw = BrainWorker(lambda: "ok", store=store, cooldown_sec=60, now_fn=lambda: now[0])
    bw.wake()
    assert bw.run_pending() is True            # 첫 실행
    now[0] += 10                                # 10초 후 (쿨다운 60 미만)
    bw.wake()
    assert bw.run_pending() is False           # 쿨다운으로 스킵
    assert bw.runs == 1 and bw.skipped == 1
    now[0] += 100                               # 쿨다운 경과
    bw.wake()
    assert bw.run_pending() is True
    assert bw.runs == 2


def test_gap_scan_bypasses_cooldown(tmp_path):
    """15:20/19:50 갭반등은 brain_cooldown_sec 에 막히지 않는다."""
    store = Store(tmp_path / "t.db")
    now = [1000.0]
    calls = []
    bw = BrainWorker(lambda wake=None: calls.append(wake) or "ok",
                     store=store, cooldown_sec=300, now_fn=lambda: now[0])
    bw.wake("periodic", [])
    assert bw.run_pending() is True
    now[0] += 30
    bw.wake("gap_rebound_scan", [], at="15:20", market="KR")
    assert bw.run_pending() is True
    assert bw.runs == 2 and calls[-1]["reason"] == "gap_rebound_scan"


def test_exception_is_swallowed_and_logged(tmp_path):
    store = Store(tmp_path / "t.db")
    def boom():
        raise RuntimeError("LLM down")
    bw = BrainWorker(boom, store=store)
    bw.wake()
    assert bw.run_pending() is False           # 실패해도 예외 전파 안 함
    assert bw.runs == 0
    errs = store.conn.execute("SELECT payload FROM events WHERE kind='error'").fetchall()
    assert errs and "LLM down" in errs[0]["payload"]


def test_summary_counts_executed(tmp_path):
    store = Store(tmp_path / "t.db")
    class Res:
        executed = [{"status": "filled"}, {"status": "vetoed"}, {"status": "filled"}]
    bw = BrainWorker(lambda: Res(), store=store)
    bw.wake(); bw.run_pending()
    done = store.conn.execute("SELECT payload FROM events WHERE kind='brain_done'").fetchone()
    assert '"filled": 2' in done["payload"] and '"vetoed": 1' in done["payload"]


def test_wake_is_nonblocking_and_thread_runs(tmp_path):
    """start()/stop() 스레드 경로: wake 후 워커가 사이클을 돌린다."""
    store = Store(tmp_path / "t.db")
    done = threading.Event()
    bw = BrainWorker(lambda: done.set() or "ok", store=store)
    bw.start()
    try:
        bw.wake("t", [])
        assert done.wait(timeout=3.0), "워커가 사이클을 실행하지 않음"
    finally:
        bw.stop()
    assert bw.runs >= 1


def test_coalesce_during_run(tmp_path):
    """사이클 도중 들어온 wake 는 합쳐져 끝난 뒤 한 번 더(최신 1건)."""
    store = Store(tmp_path / "t.db")
    started = threading.Event()
    release = threading.Event()
    runs = []

    def slow_cycle():
        runs.append(1)
        started.set()
        release.wait(timeout=3.0)
        return "ok"

    bw = BrainWorker(slow_cycle, store=store)
    bw.start()
    try:
        bw.wake()                               # 1차 실행 시작
        assert started.wait(timeout=3.0)
        bw.wake(); bw.wake(); bw.wake()         # 실행 중 3번 더 → 합쳐서 1회만 더
        started.clear()
        release.set()                           # 1차 종료 → 2차 시작
        assert started.wait(timeout=3.0)        # 2차가 돌기 시작
    finally:
        release.set()
        bw.stop()
    # 4번 wake 였지만 실행은 2회(coalesce). 타이밍상 최소 2, 3을 넘지 않음.
    assert 2 <= len(runs) <= 3

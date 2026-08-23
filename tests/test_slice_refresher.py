"""빠른 슬라이스 갱신기 — 휴장 게이팅·개장 시 build+apply·예외 생존·stop 종료 테스트.

is_open/build/apply/sleep 을 전부 주입해 네트워크·시간 없이 결정적으로 검증.
"""
import threading

from src.engine.slice_refresher import SliceRefresher, start_slice_refresher_thread


def _refresher(is_open_map, calls, *, build_raises=False, sleep_fn=lambda s: None):
    """is_open_map: {market: bool}. calls: build/apply 호출 기록 dict."""
    def build():
        calls["build"] += 1
        if build_raises:
            raise RuntimeError("boom")
        return {"regime": {"KR": {"label": "risk_on"}}, "sentiment": {}, "markets": {}}

    def apply(partial):
        calls["apply"] += 1
        calls["last"] = partial

    return SliceRefresher(build, apply, markets=list(is_open_map),
                          is_open_fn=lambda m: is_open_map.get(m, False),
                          sleep_fn=sleep_fn)


# ── (a) 양 시장 휴장이면 build 호출 안 함 ──────────────────────────
def test_both_closed_skips_build():
    calls = {"build": 0, "apply": 0}
    r = _refresher({"KR": False, "US": False}, calls)
    did = r.tick_once()
    assert did is False
    assert calls["build"] == 0 and calls["apply"] == 0


# ── (b) 개장 시 한 사이클에서 build+apply 1회 ──────────────────────
def test_open_builds_and_applies_once():
    calls = {"build": 0, "apply": 0}
    r = _refresher({"KR": True, "US": False}, calls)   # 하나만 개장이어도
    did = r.tick_once()
    assert did is True
    assert calls["build"] == 1 and calls["apply"] == 1
    assert calls["last"]["regime"]["KR"]["label"] == "risk_on"


# ── (c) build 중 예외 나도 스레드/루프 안 죽음(다음 주기 계속) ──────
def test_build_exception_survives():
    calls = {"build": 0, "apply": 0}
    r = _refresher({"KR": True}, calls, build_raises=True)
    did = r.tick_once()                       # 예외 삼킴
    assert did is False
    assert calls["build"] == 1 and calls["apply"] == 0
    # 다음 사이클도 정상 호출됨(루프가 살아있음)
    did2 = r.tick_once()
    assert did2 is False and calls["build"] == 2


# ── interval: 개장 5분 / 휴장 길게 ─────────────────────────────────
def test_interval_active_vs_idle():
    calls = {"build": 0, "apply": 0}
    r_open = _refresher({"KR": True, "US": False}, calls)
    assert r_open.interval() == r_open.refresh_sec
    r_closed = _refresher({"KR": False, "US": False}, calls)
    assert r_closed.interval() == r_closed.idle_sec


# ── (d) stop_event.set() 으로 run_forever 종료 ─────────────────────
def test_stop_event_terminates_thread():
    calls = {"build": 0, "apply": 0}
    # sleep 이 실제로 짧게 돌게(스레드가 tick 을 몇 번 돌 여유). is_open=False 라 build 안 함.
    r = _refresher({"KR": False, "US": False}, calls,
                   sleep_fn=lambda s: threading.Event().wait(0.005))
    t, stop = start_slice_refresher_thread(r)
    assert t.is_alive()
    stop.set()
    t.join(timeout=2.0)
    assert not t.is_alive()          # stop_event 로 깔끔히 종료


def test_run_forever_ticks_then_stops():
    calls = {"build": 0, "apply": 0}
    stop = threading.Event()

    # 2번 tick 후 스스로 stop 을 세팅하는 sleep_fn — run_forever 가 유한하게 끝나게.
    n = {"c": 0}

    def sleep_fn(_s):
        n["c"] += 1
        if n["c"] >= 2:
            stop.set()

    r = _refresher({"KR": True}, calls, sleep_fn=sleep_fn)
    ticks = r.run_forever(stop)
    assert ticks == 2
    assert calls["build"] == 2 and calls["apply"] == 2

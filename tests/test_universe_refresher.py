"""롤링 유니버스 갱신기 — 코어 창 판정(DST 포함)·거래일당 1회·휴장 skip·무버 게이팅·
on_added·예외 생존·stop 종료.

now(tz-aware datetime)·is_open·core_fn·mover_fn·sleep 을 전부 주입해 네트워크·실시간 없이
결정적으로 검증. 개장시각은 market_hours._SESSIONS(tz-aware)에서 계산 → US DST 자동.
"""
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from src.engine.universe_refresher import (UniverseRefresher,
                                          start_universe_refresher_thread)

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")


def _refresher(now, *, is_open_map=None, core_calls=None, mover_calls=None,
               mover_ret=None, core_raises=False, core_returns_none=False,
               on_added=None, interval_min=60.0, markets=None, core_weekdays=None):
    core_calls = core_calls if core_calls is not None else []
    mover_calls = mover_calls if mover_calls is not None else []

    def core_fn(m):
        core_calls.append(m)
        if core_raises:
            raise RuntimeError("boom")
        if core_returns_none:
            return None          # 우아한 실패(발굴/선정 0) — done 마킹 금지 대상
        return {m: [{"symbol": "X"}]}   # 성공(갱신된 유니버스 dict)

    def mover_fn(m):
        mover_calls.append(m)
        return (mover_ret or {}).get(m, [])

    return UniverseRefresher(
        core_fn, mover_fn,
        markets=markets or ["KR", "US"],
        interval_min=interval_min,
        is_open_fn=lambda m: (is_open_map or {}).get(m, False),
        on_added=on_added,
        now_fn=(now if callable(now) else (lambda: now)),
        sleep_fn=lambda s: None,
        core_weekdays=core_weekdays)


def test_core_none_result_retries_same_day():
    # core_fn 이 None(우아한 실패: 발굴 0 등)이면 done 마킹 없이 다음 틱 재시도.
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime(2026, 7, 6, 7, 35, tzinfo=ZoneInfo("Asia/Seoul"))  # KR 창 안(월)
    calls = []
    r = _refresher(now, core_calls=calls, core_returns_none=True, markets=["KR"])
    r.tick_once()
    r.tick_once()
    assert len(calls) == 2, "None 반환은 완료가 아니므로 재시도돼야 함"
    r2_calls = []
    r2 = _refresher(now, core_calls=r2_calls, markets=["KR"])   # 성공(dict) 케이스
    r2.tick_once()
    r2.tick_once()
    assert len(r2_calls) == 1, "성공하면 그 거래일엔 1회만"


def test_kr_core_weekdays_skips_tuesday():
    """core_weekdays=[0](월)이면 화요일 창 안이어도 코어 안 돈다."""
    tue = datetime(2026, 7, 7, 7, 35, tzinfo=KST)  # 화
    calls = []
    r = _refresher(tue, core_calls=calls, markets=["KR"], core_weekdays=[0])
    assert r._in_core_window("KR", tue) is False
    r.tick_once()
    assert calls == []
    mon = datetime(2026, 7, 6, 7, 35, tzinfo=KST)
    calls2 = []
    r2 = _refresher(mon, core_calls=calls2, markets=["KR"], core_weekdays=[0])
    assert r2._in_core_window("KR", mon) is True
    r2.tick_once()
    assert "KR" in calls2


# ── 코어 창 판정 ────────────────────────────────────────────────────────────
def test_kr_core_window_inside():
    # KR 개장 09:00 KST, preopen 90분 → 창 [07:30, 09:00). 07:35 = 창 안.
    now = datetime(2026, 7, 6, 7, 35, tzinfo=KST)   # 월요일, 휴장 아님
    calls = []
    r = _refresher(now, core_calls=calls)
    assert r._in_core_window("KR", now) is True
    r.tick_once()
    assert "KR" in calls


def test_kr_core_window_before_start():
    # 07:00 KST = 창 시작(07:30) 전 → 창 밖.
    now = datetime(2026, 7, 6, 7, 0, tzinfo=KST)
    r = _refresher(now)
    assert r._in_core_window("KR", now) is False


def test_kr_core_window_at_open_excluded():
    # 09:00 정각(개장) = 창 상한(open) 제외(< open 이어야 함).
    now = datetime(2026, 7, 6, 9, 0, tzinfo=KST)
    r = _refresher(now)
    assert r._in_core_window("KR", now) is False


def test_us_core_window_summer_dst():
    # US 여름(EDT): 개장 09:30 ET, preopen 330분 → 창 시작 04:00 ET = 17:00 KST.
    # 04:00 ET(=17:00 KST)는 창 안. DST 자동(_SESSIONS tz-aware).
    now = datetime(2026, 7, 6, 4, 0, tzinfo=ET)     # 여름, 월요일
    assert now.astimezone(KST).hour == 17           # 여름엔 17:00 KST
    r = _refresher(now)
    assert r._in_core_window("US", now) is True


def test_us_core_window_winter_dst():
    # US 겨울(EST): 창 시작 04:00 ET = 18:00 KST. DST 케이스 둘 다.
    now = datetime(2026, 1, 5, 4, 0, tzinfo=ET)     # 겨울, 월요일(2026-01-05)
    assert now.astimezone(KST).hour == 18           # 겨울엔 18:00 KST
    r = _refresher(now)
    assert r._in_core_window("US", now) is True


def test_us_core_window_before_start_summer():
    # 03:30 ET = 창 시작(04:00 ET) 전 → 창 밖.
    now = datetime(2026, 7, 6, 3, 30, tzinfo=ET)
    r = _refresher(now)
    assert r._in_core_window("US", now) is False


# ── 거래일당 1회 ────────────────────────────────────────────────────────────
def test_core_once_per_trading_day():
    now = datetime(2026, 7, 6, 7, 35, tzinfo=KST)
    calls = []
    r = _refresher(now, core_calls=calls, markets=["KR"])
    r.tick_once()
    r.tick_once()
    r.tick_once()
    assert calls == ["KR"]              # 같은 거래일엔 첫 틱만 코어


def test_core_runs_again_next_day():
    calls = []
    times = iter([datetime(2026, 7, 6, 7, 35, tzinfo=KST),
                  datetime(2026, 7, 7, 7, 35, tzinfo=KST)])   # 다음 거래일
    r = _refresher(lambda: next(times), core_calls=calls, markets=["KR"])
    r.tick_once()
    r.tick_once()
    assert calls == ["KR", "KR"]        # 거래일 바뀌면 다시 1회


# ── 휴장 skip ───────────────────────────────────────────────────────────────
def test_core_skipped_on_weekend():
    now = datetime(2026, 7, 4, 7, 35, tzinfo=KST)   # 토요일
    calls = []
    r = _refresher(now, core_calls=calls, markets=["KR"])
    r.tick_once()
    assert calls == []


def test_core_skipped_on_holiday():
    # 2026-01-01 신정(KR 휴장, _SESSIONS/HOLIDAYS). 07:35 라도 코어 안 함.
    now = datetime(2026, 1, 1, 7, 35, tzinfo=KST)
    calls = []
    r = _refresher(now, core_calls=calls, markets=["KR"])
    assert r._in_core_window("KR", now) is False
    r.tick_once()
    assert calls == []


# ── 코어 실패해도 스레드 생존 + 그날 재시도 ─────────────────────────────────
def test_core_failure_survives_and_retries():
    now = datetime(2026, 7, 6, 7, 35, tzinfo=KST)
    calls = []
    r = _refresher(now, core_calls=calls, core_raises=True, markets=["KR"])
    r.tick_once()                       # 예외 삼킴 → done 표시 안 함
    r.tick_once()                       # 그날 재시도(아직 성공 못 했으므로)
    assert calls == ["KR", "KR"]        # 두 번 시도(실패라 done 미표시)


# ── 무버 게이팅 ─────────────────────────────────────────────────────────────
def test_mover_only_when_open():
    now = datetime(2026, 7, 6, 10, 0, tzinfo=KST)
    mcalls = []
    r = _refresher(now, is_open_map={"KR": True, "US": False},
                   mover_calls=mcalls, markets=["KR", "US"])
    r.tick_once()
    assert mcalls == ["KR"]             # 개장 KR 만 무버, 휴장 US 안 함


def test_mover_interval_gating():
    # interval 60분. 같은 시각으로 여러 틱 → 최초 1회만(간격 미경과).
    now = datetime(2026, 7, 6, 10, 0, tzinfo=KST)
    mcalls = []
    r = _refresher(now, is_open_map={"KR": True}, mover_calls=mcalls,
                   markets=["KR"], interval_min=60.0)
    r.tick_once(); r.tick_once(); r.tick_once()
    assert mcalls == ["KR"]             # 60분 안 지나 1회만


def test_mover_interval_elapsed_scans_again():
    times = iter([datetime(2026, 7, 6, 10, 0, tzinfo=KST),
                  datetime(2026, 7, 6, 11, 1, tzinfo=KST)])   # 61분 뒤
    mcalls = []
    r = _refresher(lambda: next(times), is_open_map={"KR": True},
                   mover_calls=mcalls, markets=["KR"], interval_min=60.0)
    r.tick_once()
    r.tick_once()
    assert mcalls == ["KR", "KR"]       # 간격 경과 → 재스캔


def test_on_added_called_when_movers_admitted():
    now = datetime(2026, 7, 6, 10, 0, tzinfo=KST)
    added_log = []
    r = _refresher(now, is_open_map={"KR": True},
                   mover_ret={"KR": ["NEW1", "NEW2"]},
                   on_added=lambda m, syms: added_log.append((m, syms)),
                   markets=["KR"])
    r.tick_once()
    assert added_log == [("KR", ["NEW1", "NEW2"])]


def test_on_added_not_called_when_no_movers():
    now = datetime(2026, 7, 6, 10, 0, tzinfo=KST)
    added_log = []
    r = _refresher(now, is_open_map={"KR": True}, mover_ret={"KR": []},
                   on_added=lambda m, syms: added_log.append((m, syms)),
                   markets=["KR"])
    r.tick_once()
    assert added_log == []              # 편입 0 → 콜백 없음


def test_mover_failure_survives():
    now = datetime(2026, 7, 6, 10, 0, tzinfo=KST)

    def mover_fn(m):
        raise RuntimeError("scan boom")

    r = UniverseRefresher(lambda m: None, mover_fn, markets=["KR"],
                          is_open_fn=lambda m: True, now_fn=lambda: now,
                          sleep_fn=lambda s: None)
    r.tick_once()                       # 예외 삼킴 → 안 죽음
    r.tick_once()                       # 다음 틱도 정상(interval 은 실패해도 기록됨)
    assert r.ticks == 2


# ── run_forever / stop ──────────────────────────────────────────────────────
def test_stop_event_terminates_thread():
    now = datetime(2026, 7, 4, 7, 35, tzinfo=KST)   # 주말 → 아무 것도 안 함
    r = UniverseRefresher(
        lambda m: None, lambda m: [], markets=["KR"],
        is_open_fn=lambda m: False, now_fn=lambda: now,
        sleep_fn=lambda s: threading.Event().wait(0.005))
    t, stop = start_universe_refresher_thread(r)
    assert t.is_alive()
    stop.set()
    t.join(timeout=2.0)
    assert not t.is_alive()


def test_run_forever_ticks_then_stops():
    now = datetime(2026, 7, 4, 7, 35, tzinfo=KST)
    stop = threading.Event()
    n = {"c": 0}

    def sleep_fn(_s):
        n["c"] += 1
        if n["c"] >= 2:
            stop.set()

    r = UniverseRefresher(lambda m: None, lambda m: [], markets=["KR"],
                          is_open_fn=lambda m: False, now_fn=lambda: now,
                          sleep_fn=sleep_fn)
    ticks = r.run_forever(stop)
    assert ticks == 2

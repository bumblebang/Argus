"""롤링 유니버스 생명주기 스레드 — 코어(개장 전 1회)·무버(장중 롤링)를 데몬이 소유.

data/universe.yaml 을 스케줄 배치가 아니라 상주 데몬이 관리한다(핫리로드
UniverseProvider 가 파일 변경을 런타임 반영하므로, 파일만 갱신하면 뇌 후보·감시목록·
공시워처에 자동 전파). SliceRefresher 패턴을 미러 — 자체 루프 스레드, 예외 삼킴,
stop_event, 데몬 스레드.

두 축:
  - 코어: 시장별 (개장시각 − core_preopen_min) ≤ now < 개장시각 이고 그 시장 거래일에
    아직 안 했으면 core_fn(market) 1회. 개장시각은 market_hours._SESSIONS(tz-aware)에서
    계산하므로 US DST 가 자동 반영(ET 기준 창이 KST 로 여름/겨울 달라짐).
  - 무버: is_open_fn(market)인 동안 interval_min 마다 mover_fn(market). 세션 판정 함수는
    주입식(후속 트랙에서 is_entry_session 으로 교체 가능). 편입 심볼이 있으면 on_added
    콜백(있을 때만).

틱 주기는 짧게(~30s) — 코어 창 판정 해상도. 예외는 삼켜 스레드가 죽지 않게 한다.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from ..logging_setup import get_logger
from .. import market_hours
from ..market_hours import is_open, market_day, is_holiday

log = get_logger("engine.universe_refresher")

# 기본 창 파라미터(config screener.rolling 로 오버라이드).
_DEFAULT_PREOPEN_MIN = {"KR": 90, "US": 330}
_DEFAULT_INTERVAL_MIN = 60.0
_TICK_SEC = 30.0


class UniverseRefresher:
    """코어/무버 갱신기. 데몬 스레드에서 run_forever, 테스트는 tick_once.

    core_fn(market) -> dict|None(성공 시 갱신된 유니버스), mover_fn(market) -> list[str]
    (편입 심볼). is_open/now/sleep 은 결정적 테스트를 위해 주입. now_fn 은 tz-aware datetime
    을 반환해야 한다(창 판정이 시장 로컬 시각으로 이뤄지므로).
    """

    def __init__(self, core_fn: Callable[[str], object],
                 mover_fn: Callable[[str], list], *,
                 markets: list[str] | None = None,
                 core_preopen_min: dict | None = None,
                 interval_min: float = _DEFAULT_INTERVAL_MIN,
                 tick_sec: float = _TICK_SEC,
                 is_open_fn: Callable[[str], bool] = is_open,
                 on_added: Callable[[str, list], None] | None = None,
                 now_fn: Callable[[], datetime] | None = None,
                 sleep_fn: Callable[[float], None] = time.sleep,
                 core_weekdays: list[int] | None = None) -> None:
        self.core_fn = core_fn
        self.mover_fn = mover_fn
        self.markets = markets or ["KR", "US"]
        self.core_preopen_min = {**_DEFAULT_PREOPEN_MIN, **(core_preopen_min or {})}
        self.interval_min = float(interval_min)
        self.tick_sec = float(tick_sec)
        self._is_open = is_open_fn
        self.on_added = on_added
        self._now = now_fn or (lambda: datetime.now(ZoneInfo("UTC")))
        self._sleep = sleep_fn
        # None = 거래일마다. [0]=월요일만(스윙 멤버십 주 1회).
        self.core_weekdays = (None if core_weekdays is None
                              else [int(d) for d in core_weekdays])
        # 코어: 그 시장 거래일에 이미 했는지(market_day 키). 무버: 시장별 마지막 스캔 시각.
        self._core_done: dict[str, str] = {}
        self._last_mover: dict[str, float] = {}
        self.ticks = 0

    # ── 코어 창 판정 ────────────────────────────────────────────
    def _in_core_window(self, market: str, now: datetime) -> bool:
        """시장별 (개장시각 − preopen) ≤ now < 개장시각 인지. 시장 로컬 시각 기준(DST 자동).

        휴장일(주말·정적 캘린더)엔 개장 자체가 없으므로 False.
        """
        sess = market_hours._SESSIONS.get(market)
        if sess is None:
            return False
        tzname, open_t, _close_t = sess
        local = now.astimezone(ZoneInfo(tzname))
        # 그 시장 로컬 날짜가 휴장이면 코어 창 없음.
        if local.weekday() >= 5 or is_holiday(market, now):
            return False
        if self.core_weekdays is not None and local.weekday() not in self.core_weekdays:
            return False
        open_dt = local.replace(hour=open_t.hour, minute=open_t.minute,
                                second=0, microsecond=0)
        start_dt = open_dt - timedelta(minutes=self.core_preopen_min.get(market, 90))
        return start_dt <= local < open_dt

    def _maybe_core(self, market: str, now: datetime) -> bool:
        """코어 창 안이고 그 거래일에 아직 안 했으면 core_fn 1회. 수행했으면 True."""
        if not self._in_core_window(market, now):
            return False
        day = market_day(market, now)
        if self._core_done.get(market) == day:
            return False
        try:
            result = self.core_fn(market)
        except Exception as e:                         # 한 번 실패가 스레드를 죽이지 않게
            log.warning("[%s] 코어 리프레시 실패(계속): %s", market, e)
            # 실패해도 그날 재시도할 수 있게 done 표시는 하지 않는다(다음 틱에서 재시도).
            return False
        if not result:
            # core_refresh 의 우아한 실패(발굴 0·선정 0 → None, 기존 유니버스 유지)도
            # 성공으로 마킹하면 그날 재시도가 없다 — 다음 틱에서 재시도(창 안에서 계속).
            log.warning("[%s] 코어 리프레시 무갱신(발굴/선정 0) — 다음 틱 재시도.", market)
            return False
        self._core_done[market] = day
        log.info("[%s] 코어 리프레시 완료(거래일 %s)", market, day)
        return True

    # ── 무버 게이팅 ─────────────────────────────────────────────
    def _maybe_mover(self, market: str, now: datetime) -> bool:
        """개장 중이고 interval_min 경과했으면 mover_fn 1회. 편입분 있으면 on_added. 수행 True."""
        if not self._is_open(market):
            return False
        ts = now.timestamp()
        last = self._last_mover.get(market)
        if last is not None and (ts - last) < self.interval_min * 60:
            return False
        self._last_mover[market] = ts
        try:
            added = self.mover_fn(market) or []
        except Exception as e:
            log.warning("[%s] 무버 스캔 실패(계속): %s", market, e)
            return False
        if added and self.on_added:
            try:
                self.on_added(market, added)
            except Exception as e:
                log.warning("[%s] on_added 콜백 실패(무시): %s", market, e)
        return True

    def tick_once(self) -> None:
        """한 틱: 각 시장에 대해 코어 창 판정 + 무버 게이팅. 예외는 각 단계에서 삼킨다."""
        self.ticks += 1
        now = self._now()
        for market in self.markets:
            self._maybe_core(market, now)
            self._maybe_mover(market, now)

    def run_forever(self, stop_event: threading.Event | None = None) -> int:
        ticks = 0
        log.info("유니버스 갱신기 기동 — markets=%s, preopen=%s, 무버 %.0f분, 틱 %.0fs",
                 self.markets, self.core_preopen_min, self.interval_min, self.tick_sec)
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            self.tick_once()
            ticks += 1
            self._sleep(self.tick_sec)
        return ticks


def start_universe_refresher_thread(
        refresher: UniverseRefresher) -> tuple[threading.Thread, threading.Event]:
    """데몬 스레드로 갱신기 기동. (thread, stop_event) 반환 — watch.py 종료 시 set."""
    stop = threading.Event()
    t = threading.Thread(target=refresher.run_forever, args=(stop,),
                         name="universe_refresher", daemon=True)
    t.start()
    return t, stop

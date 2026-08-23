"""장중 market_state 빠른 슬라이스 갱신기 — 별도 데몬 스레드에서 5분마다 파일을 신선하게.

data/market_state.json 의 regime/sentiment/markets 3개 슬롯을 장중 5분마다 Yahoo 로만
다시 계산해 부분 merge(느린 슬롯은 배치 2회/일 유지). 절대 WatchLoop.run_once 안에 넣지
않는다 — 13개 Yahoo HTTP 가 1초 틱을 수 초 stall 시켜 빠른손을 마비시킨다. DisclosureWatcher
와 같은 패턴: 자체 루프 스레드, active/idle 주기, 예외 삼킴, stop_event.

게이팅: 양 시장(KR/US) 다 휴장이면 build 를 건너뛰고 길게 대기(idle). 하나라도 개장이면
active 주기(기본 300s)로 build+apply.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from ..logging_setup import get_logger
from ..market_hours import is_open

log = get_logger("engine.slice_refresher")


class SliceRefresher:
    """빠른 슬라이스 주기 갱신기. 데몬 스레드에서 run_forever, 테스트는 tick_once.

    build_fn() -> partial dict(regime/sentiment/markets), apply_fn(partial) -> None.
    is_open/now/sleep 는 결정적 테스트를 위해 주입 가능.
    """

    def __init__(self, build_fn: Callable[[], dict],
                 apply_fn: Callable[[dict], None], *,
                 markets: list[str] | None = None,
                 refresh_sec: float = 300.0, idle_sec: float = 600.0,
                 is_open_fn: Callable[[str], bool] = is_open,
                 now_fn: Callable[[], float] = time.time,
                 sleep_fn: Callable[[float], None] = time.sleep) -> None:
        self.build_fn = build_fn
        self.apply_fn = apply_fn
        self.markets = markets or ["KR", "US"]
        self.refresh_sec = float(refresh_sec)
        self.idle_sec = float(idle_sec)
        self._is_open = is_open_fn
        self._now = now_fn
        self._sleep = sleep_fn
        self.ticks = 0

    def any_open(self) -> bool:
        return any(self._is_open(m) for m in self.markets)

    # ── 주기: 장중(하나라도 개장) 5분 / 양 시장 휴장이면 길게 대기 ──
    def interval(self) -> float:
        return self.refresh_sec if self.any_open() else self.idle_sec

    def tick_once(self) -> bool:
        """한 사이클: 개장 중이면 build+apply 1회. 반환: 갱신 수행 여부.

        양 시장 휴장이면 아무것도 안 하고 False. build/apply 중 예외는 삼켜서 스레드가
        죽지 않게 한다(다음 주기 계속).
        """
        self.ticks += 1
        if not self.any_open():
            return False
        try:
            partial = self.build_fn()
            self.apply_fn(partial)
            return True
        except Exception as e:                        # 한 번의 실패가 갱신기를 죽이지 않게
            log.warning("빠른 슬라이스 갱신 실패(계속): %s", e)
            return False

    def run_forever(self, stop_event: threading.Event | None = None) -> int:
        ticks = 0
        log.info("빠른 슬라이스 갱신기 기동 — 장중 %.0fs / 휴장 %.0fs, markets=%s",
                 self.refresh_sec, self.idle_sec, self.markets)
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            self.tick_once()
            ticks += 1
            self._sleep(self.interval())
        return ticks


def start_slice_refresher_thread(
        refresher: SliceRefresher) -> tuple[threading.Thread, threading.Event]:
    """데몬 스레드로 갱신기 기동. (thread, stop_event) 반환 — watch.py 종료 시 set."""
    stop = threading.Event()
    t = threading.Thread(target=refresher.run_forever, args=(stop,),
                         name="slice_refresher", daemon=True)
    t.start()
    return t, stop

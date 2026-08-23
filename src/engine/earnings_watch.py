"""실적 결과 워처 — "발표 예정일과 컨센서스는 이미 안다, 모르는 건 실제 숫자다".

봇은 발표 예정일·컨센서스를 장전 배치로 미리 안다(data/earnings_calendar.json). 정작
모르는 건 **발표된 실제 숫자**라, 보유 종목이 발표 다음날 -8% 빠져 있어도 왜 빠졌는지
모르는 채 판단한다. 이 워처가 채우는 건 딱 하나 — 컨센서스 대비 정확한 편차(surprise%)다.

DisclosureWatcher 와 같은 골격(주기 폴링 / dedup / 첫 폴 프라이밍 / 3단 라우팅):
  1. 보유/진입대기 종목 결과 → 뇌 즉시 각성(on_wake, 서프라이즈% 첨부)
  2. 유니버스(커버) 종목 결과 → events 큐 적재(다음 사이클 컨텍스트)
  3. 그 외 → 무시(로깅도 안 함)

폴링 주기는 캘린더가 정한다 — 발표 임박 구간(D-3~D+1)에만 촘촘히(10분), 발표가 없는
날엔 Finnhub 를 1시간에 한 번만 두드린다. 데몬 내 별도 스레드로 돌며 토스는 만지지
않는다(Finnhub REST 만).
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Iterable

from ..datasources.earnings import dday_of
from ..logging_setup import get_logger

log = get_logger("engine.earnings_watch")

# 이 D-day 범위(D-3~D+1)에 US 발표가 하나라도 있으면 촘촘히 본다. amc 발표는 다음날
# 캘린더에 actual 이 들어오므로 뒤(+1)보다 앞(-3)을 넉넉히 잡는다.
_ACTIVE_DDAY_MIN, _ACTIVE_DDAY_MAX = -3, 1
_SEEN_MAX = 5000          # dedup 키 상한 — 넘으면 절반 비움(disclosure 와 같은 관행)


class EarningsResultWatcher:
    """Finnhub 실적 결과 폴러 + 3단 라우터. 데몬 스레드에서 run_forever, 테스트는 poll_once.

    fetch_fn() -> {symbol: 실적 결과 표준형}(datasources.earnings.fetch_us_results)
    universe_fn() -> US 유니버스 심볼 집합(동적 유니버스 반영 위해 콜러블)
    on_wake(reason, payloads) — BrainWorker.wake 호환(논블로킹)
    calendar_fn() -> 실적 캘린더 symbols dict (선택). 폴링 주기 결정용.
    """

    def __init__(self, store, fetch_fn: Callable[[], dict[str, dict]],
                 universe_fn: Callable[[], Iterable[str]], *,
                 on_wake: Callable[[str, list], None] | None = None,
                 poll_sec: float = 600.0, idle_poll_sec: float = 3600.0,
                 calendar_fn: Callable[[], dict] | None = None,
                 now_fn: Callable[[], float] = time.time,
                 sleep_fn: Callable[[float], None] = time.sleep) -> None:
        self.store = store
        self.fetch_fn = fetch_fn
        self.universe_fn = universe_fn
        self.on_wake = on_wake
        self.poll_sec = float(poll_sec)
        self.idle_poll_sec = float(idle_poll_sec)
        self.calendar_fn = calendar_fn
        self._now = now_fn
        self._sleep = sleep_fn
        self._seen: set[str] = set()
        self._primed = False      # 첫 폴 = 지난 실적 마킹만(재시작 시 뇌 폭격 방지)
        self.polls = 0

    @staticmethod
    def _key(r: dict) -> str:
        """dedup 키 — 같은 분기 실적으로 반복 각성하지 않게 (심볼:발표일:연도Q분기)."""
        return f"{r.get('symbol')}:{r.get('date')}:{r.get('year')}Q{r.get('quarter')}"

    def _positions_symbols(self) -> set[str]:
        held = {r["symbol"] for r in self.store.get_open_positions()}
        held |= {r["symbol"] for r in self.store.get_armed()}
        return held

    # ── 주기: 발표 임박(D-3~D+1) 10분 / 그 외 1시간 ──
    def interval(self) -> float:
        """캘린더에 임박한 US 발표가 있을 때만 촘촘히 — 발표 없는 날 두드릴 이유가 없다.

        캘린더 조회 실패는 삼키고 poll_sec(보수적: 놓치느니 더 본다)을 돌려준다.
        """
        if not self.calendar_fn:
            return self.poll_sec
        try:
            cal = self.calendar_fn() or {}
            for e in cal.values():
                if not isinstance(e, dict) or e.get("market") != "US":
                    continue
                d = dday_of(e)      # 저장된 dday 는 배치 시점 값 — 오늘 기준 재계산
                if d is not None and _ACTIVE_DDAY_MIN <= d <= _ACTIVE_DDAY_MAX:
                    return self.poll_sec
        except Exception as e:
            log.warning("실적 발표 임박 판정 실패(무시, 촘촘히 유지): %s", e)
            return self.poll_sec
        return self.idle_poll_sec

    def poll_once(self) -> dict:
        """한 번 폴링→라우팅. 반환: {"new": n, "woke": [...], "queued": [...]}."""
        res = {"new": 0, "woke": [], "queued": []}
        results = self.fetch_fn() or {}
        self.polls += 1
        if not self._primed:                      # 기동 직후: 조용히 기준선만 잡는다
            self._seen = {self._key(r) for r in results.values()}
            self._primed = True
            log.info("실적 결과 워처 기동 — 기존 %d건 마킹(각성 없음)", len(self._seen))
            return res

        fresh = [r for r in results.values() if self._key(r) not in self._seen]
        if not fresh:
            return res
        held = self._positions_symbols()
        universe = set(self.universe_fn() or ())
        wake_payloads: list[dict] = []
        for r in fresh:
            self._seen.add(self._key(r))
            res["new"] += 1
            sym = r.get("symbol")
            if not sym:
                continue
            # detected_at: 발표일 대비 감지 지연을 나중에 events 로 분석해 poll_sec 을
            # 조일지 판단하는 근거(계측용 — 매매 판단 입력이 아니다).
            payload = dict(r, detected_at=self._now())
            if sym in held:                       # 1) 내 포지션 → 즉시 각성
                self.store.log_event("earnings_result", sym, payload | {"route": "wake"})
                wake_payloads.append(payload)
                res["woke"].append(sym)
                log.warning("보유/대기 종목 실적 발표 %s: EPS %s(컨센 %s, %s%%), "
                            "매출 %s%% → 뇌 각성", sym, r.get("eps_actual"),
                            r.get("eps_estimate"), r.get("eps_surprise_pct"),
                            r.get("revenue_surprise_pct"))
            elif sym in universe:                 # 2) 커버 종목 → 큐(다음 사이클 첨부)
                self.store.log_event("earnings_result", sym, payload | {"route": "queue"})
                res["queued"].append(sym)
                log.info("커버 종목 실적 발표 %s: EPS 서프라이즈 %s%% → 큐",
                         sym, r.get("eps_surprise_pct"))
        if wake_payloads and self.on_wake:
            try:
                self.on_wake("earnings_result", wake_payloads)
            except Exception as e:
                log.error("실적 결과 각성 콜백 실패: %s", e)
                self.store.log_event("error", None, {"where": "earnings_result_wake",
                                                     "err": str(e)})
        if len(self._seen) > _SEEN_MAX:
            self._seen = set(sorted(self._seen)[_SEEN_MAX // 2:])
        return res

    def run_forever(self, stop_event: threading.Event | None = None) -> int:
        polls = 0
        self.store.log_event("earnings_watch_start", None,
                             {"poll_sec": self.poll_sec,
                              "idle_sec": self.idle_poll_sec})
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                self.poll_once()
            except Exception as e:                # 한 번의 실패가 워처를 죽이지 않게
                log.warning("실적 결과 폴링 실패(계속): %s", e)
            polls += 1
            self._sleep(self.interval())
        return polls


def start_earnings_watcher_thread(watcher: EarningsResultWatcher) -> tuple[threading.Thread,
                                                                          threading.Event]:
    """데몬 스레드로 워처 기동. (thread, stop_event) 반환 — watch.py 종료 시 set."""
    stop = threading.Event()
    t = threading.Thread(target=watcher.run_forever, args=(stop,),
                         name="earnings_watch", daemon=True)
    t.start()
    return t, stop

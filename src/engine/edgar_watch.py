"""EDGAR 필링 워처 — US 중대 공시(8-K/6-K)를 DART 공시 워처와 같은 3단으로.

보유/armed ∪ US 유니버스만 폴링한다(글로벌 8-K 피드는 노이즈·예산 위험).
  1. 보유/진입대기 + 중대 필링 → 뇌 즉시 각성(on_wake)
  2. 유니버스 + 중대 필링 → disclosure 큐(Athena 재소환)
  3. 그 외 → 무시

items 비어 있는 8-K 는 보유만 wake(유니버스 큐 스킵). 7.01/9.01 만은 fetch 단계에서 제외.
기동 첫 폴은 accession 마킹만(재시작 폭주 방지). 폴당 N콜이라 DART보다 주기를 완화한다.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Iterable

from ..datasources.edgar import is_material_filing
from ..logging_setup import get_logger
from ..session_policy import market_monitoring_active, trading_sessions_from_raw

log = get_logger("engine.edgar_watch")

_SEEN_MAX = 10000


class EdgarWatcher:
    """SEC submissions 폴러 + 3단 라우터. 데몬 스레드 run_forever, 테스트는 poll_once.

    fetch_fn() -> [{accession, symbol, form, items, filing_date, report_nm,
                    corp_name, empty_items?}]
    universe_fn() -> US 유니버스 심볼 집합
    on_wake(reason, payloads) — BrainWorker.wake 호환
    """

    def __init__(self, store, fetch_fn: Callable[[], list[dict]],
                 universe_fn: Callable[[], Iterable[str]], *,
                 on_wake: Callable[[str, list], None] | None = None,
                 poll_active_sec: float = 120.0, poll_idle_sec: float = 900.0,
                 after_close_hours: float = 2.0,
                 trading_sessions: dict[str, tuple[str, ...]] | None = None,
                 now_fn: Callable[[], float] = time.time,
                 sleep_fn: Callable[[float], None] = time.sleep) -> None:
        self.store = store
        self.fetch_fn = fetch_fn
        self.universe_fn = universe_fn
        self.on_wake = on_wake
        self.poll_active_sec = float(poll_active_sec)
        self.poll_idle_sec = float(poll_idle_sec)
        self.after_close_hours = float(after_close_hours)
        self.trading_sessions = trading_sessions
        self._now = now_fn
        self._sleep = sleep_fn
        self._seen: set[str] = set()
        self._primed = False
        self.polls = 0

    def interval(self) -> float:
        """US 거래 세션·마감 후 창은 active, 그 외 idle."""
        if market_monitoring_active("US", trading_sessions=self.trading_sessions,
                                   after_close_hours=self.after_close_hours,
                                   now=self._now()):
            return self.poll_active_sec
        return self.poll_idle_sec

    def _positions_symbols(self) -> set[str]:
        held = {r["symbol"] for r in self.store.get_open_positions()
                if (r["market"] or "").upper() == "US"}
        held |= {r["symbol"] for r in self.store.get_armed()
                 if (r["market"] or "").upper() == "US"}
        return held

    def poll_once(self) -> dict:
        """한 번 폴링→라우팅. 반환: {"new": n, "woke": [...], "queued": [...]}."""
        res = {"new": 0, "woke": [], "queued": []}
        filings = self.fetch_fn() or []
        self.polls += 1
        if not self._primed:
            self._seen = {f["accession"] for f in filings if f.get("accession")}
            self._primed = True
            log.info("EDGAR 워처 기동 — 기존 %d건 마킹(각성 없음)", len(self._seen))
            return res

        fresh = [f for f in filings
                 if f.get("accession") and f["accession"] not in self._seen]
        if not fresh:
            return res
        held = self._positions_symbols()
        universe = {str(s).upper() for s in (self.universe_fn() or ())}
        wake_payloads: list[dict] = []
        for f in fresh:
            self._seen.add(f["accession"])
            res["new"] += 1
            sym = (f.get("symbol") or "").upper()
            if not sym:
                continue
            label, empty = is_material_filing(f.get("form"), f.get("items"))
            if "empty_items" in f:
                empty = bool(f["empty_items"])
            report_nm = f.get("report_nm") or label
            # form 이 있으면 중대성 재검증. mock 이 report_nm 만 주면 통과.
            if f.get("form") and label is None and not f.get("report_nm"):
                continue
            if not report_nm:
                continue
            # items 비어 있는 8-K → 보유/armed 만 (유니버스 큐 스킵)
            if empty and sym not in held:
                continue
            payload = {
                "market": "US",
                "accession": f["accession"],
                "form": f.get("form"),
                "items": f.get("items") or [],
                "report_nm": report_nm,
                "keyword": report_nm,
                "corp_name": f.get("corp_name"),
                "rcept_dt": f.get("filing_date"),
                "filing_date": f.get("filing_date"),
                "empty_items": empty,
            }
            if sym in held:
                self.store.log_event("disclosure", sym, payload | {"route": "wake"})
                wake_payloads.append(payload | {"symbol": sym})
                res["woke"].append(sym)
                log.warning("보유/대기 US 중대 필링 %s: %s → 뇌 각성", sym, report_nm)
            elif sym in universe and not empty:
                self.store.log_event("disclosure", sym, payload | {"route": "queue"})
                res["queued"].append(sym)
                log.info("커버 US 중대 필링 %s: %s → 큐", sym, report_nm)

        if wake_payloads and self.on_wake:
            try:
                self.on_wake("disclosure", wake_payloads)
            except Exception as e:
                log.error("EDGAR 각성 콜백 실패: %s", e)
                self.store.log_event("error", None,
                                     {"where": "edgar_wake", "err": str(e)})
        if len(self._seen) > _SEEN_MAX:
            self._seen = set(sorted(self._seen)[_SEEN_MAX // 2:])
        return res

    def run_forever(self, stop_event: threading.Event | None = None) -> int:
        polls = 0
        self.store.log_event("edgar_watch_start", None,
                             {"poll_active_sec": self.poll_active_sec,
                              "poll_idle_sec": self.poll_idle_sec})
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                self.poll_once()
            except Exception as e:
                log.warning("EDGAR 폴링 실패(계속): %s", e)
            polls += 1
            self._sleep(self.interval())
        return polls


def start_edgar_watcher_thread(watcher: EdgarWatcher) -> tuple[threading.Thread,
                                                               threading.Event]:
    """데몬 스레드로 워처 기동. (thread, stop_event) 반환."""
    stop = threading.Event()
    t = threading.Thread(target=watcher.run_forever, args=(stop,),
                         name="edgar_watch", daemon=True)
    t.start()
    return t, stop

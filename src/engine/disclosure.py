"""공시 워처 — "폴링은 촘촘히(코드, 공짜), 각성은 드물게(LLM, 비쌈)".

DART 최신 공시 목록을 장중 10초(장외 완화) 주기로 폴링하고, 코드가 중대성과
포지션 관련성을 걸러 3단 라우팅한다:
  1. 보유/진입대기 종목 + 중대 공시 → 뇌 즉시 각성(on_wake, 공시 내용 첨부)
  2. 유니버스(커버) 종목 + 중대 공시 → events 큐 적재(다음 주기 사이클·Athena 재소환용)
  3. 그 외 → 무시(로깅도 안 함 — 하루 수천 건 소음 차단)

시세 감시(1초 폴링→선별 각성)와 같은 패턴의 공시판. 데몬 내 별도 스레드로 돌며
토스를 전혀 만지지 않는다(DART REST만). 기동 직후 첫 폴은 기존 공시를 조용히
마킹만 한다(재시작 때 지난 공시로 뇌를 폭격하지 않게).
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable, Iterable

from ..logging_setup import get_logger
from ..session_policy import market_monitoring_active

log = get_logger("engine.disclosure")

# 중대 공시 키워드(report_nm 부분일치). 자본구조·실적·계약·지배구조·거래질서 계열.
MATERIAL_KEYWORDS = (
    "유상증자", "무상증자", "감자", "전환사채", "신주인수권", "교환사채",
    "합병", "분할", "영업양수", "영업양도", "포괄적주식교환",
    "공급계약", "단일판매", "수주",
    "잠정실적", "영업(잠정)실적", "매출액또는손익구조", "실적공시",
    "최대주주변경", "경영권", "횡령", "배임",
    "감사의견", "관리종목", "상장폐지", "거래정지", "영업정지",
    "소송", "파산", "회생절차",
    "자기주식취득", "자기주식처분", "주식소각",
    "유형자산양수", "유형자산양도", "타법인주식",
    "조회공시", "풍문",
)


# 그 중 '실적 계열' — 이 키워드로 각성하면 컨센서스를 payload 에 첨부한다(발표 vs 기대 비교).
EARNINGS_KEYWORDS = ("잠정실적", "영업(잠정)실적", "매출액또는손익구조", "실적공시")
_EARNINGS_ACTUALS_EXCLUDE = ("예고", "전망", "예측")

# 공시 직후 document.xml 이 아직 없는 경우(013/014). 오늘 LG 건은 6시간 뒤에 ZIP 이 생겼다.
_ACTUALS_RETRY_SEC = 600.0
_ACTUALS_MAX_AGE_SEC = 12 * 3600.0
_ACTUALS_MAX_TRIES = 80


def is_material(report_nm: str | None) -> str | None:
    """중대 공시면 매칭된 키워드, 아니면 None. (서식명 부분일치 — 코드 레벨 1차 필터)"""
    if not report_nm:
        return None
    for kw in MATERIAL_KEYWORDS:
        if kw in report_nm:
            return kw
    return None


def is_earnings_actuals_report(report_nm: str | None) -> bool:
    """잠정실적·손익구조처럼 '이미 나온 숫자'가 있는 서식인가.

    `실적공시` 키워드는 결산실적공시예고에도 들어가서, 예고/전망은 빼야 한다.
    """
    if not report_nm:
        return False
    if any(x in report_nm for x in _EARNINGS_ACTUALS_EXCLUDE):
        return False
    return any(k in report_nm for k in EARNINGS_KEYWORDS)


class DisclosureWatcher:
    """DART 공시 폴러 + 3단 라우터. 데몬 스레드에서 run_forever, 테스트는 poll_once.

    fetch_fn() -> [{rcept_no, stock_code, corp_name, report_nm, rcept_dt}]
    universe_fn() -> KR 유니버스 심볼 집합(동적 유니버스 반영 위해 콜러블)
    on_wake(reason, payloads) — BrainWorker.wake 호환(논블로킹)
    earnings_fn(symbol) -> 실적 캘린더 표준형|None (선택). 실적 공시 각성 시 컨센서스 첨부용.
    actuals_fn(rcept_no) -> 잠정실적 실제 수치 dict|None (선택). DART document.xml 파서.
    imminent_fn() -> bool (선택). 발표 임박(D-1) 종목이 있으면 장외에도 촘촘히 본다.
    """

    def __init__(self, store, fetch_fn: Callable[[], list[dict]],
                 universe_fn: Callable[[], Iterable[str]], *,
                 on_wake: Callable[[str, list], None] | None = None,
                 poll_active_sec: float = 10.0, poll_idle_sec: float = 60.0,
                 after_close_hours: float = 2.0,
                 earnings_fn: Callable[[str], dict | None] | None = None,
                 actuals_fn: Callable[[str], dict | None] | None = None,
                 imminent_fn: Callable[[], bool] | None = None,
                 imminent_after_close_hours: float = 4.0,
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
        self.earnings_fn = earnings_fn
        self.actuals_fn = actuals_fn
        self.imminent_fn = imminent_fn
        self.imminent_after_close_hours = float(imminent_after_close_hours)
        self.trading_sessions = trading_sessions
        self._now = now_fn
        self._sleep = sleep_fn
        self._seen: set[str] = set()
        self._primed = False          # 첫 폴 = 기존 공시 마킹만(재시작 폭주 방지)
        self._pending_actuals: dict[str, dict] = {}
        self.polls = 0

    def _imminent(self) -> bool:
        """발표 임박(보유/유니버스에 D-1 이하 KR 종목)인가. 콜백 예외는 삼킨다(워처 보호)."""
        if not self.imminent_fn:
            return False
        try:
            return bool(self.imminent_fn())
        except Exception as e:
            log.warning("실적 임박 판정 실패(무시): %s", e)
            return False

    # ── 주기: 장중(+마감 후 실적 러시) 10초, 그 외 완화 — DART 일 쿼터 보호 ──
    def interval(self) -> float:
        """발표 임박일엔 마감 후 창을 넓히고 장외에도 active 주기로 본다.

        실적 공시는 장 마감 후·개장 전에 몰린다 — 임박한 날만 촘촘히 보고, active 주기
        자체(10초)는 줄이지 않는다(DART 일 1만콜 쿼터 보호).
        """
        imminent = self._imminent()
        hours = self.imminent_after_close_hours if imminent else self.after_close_hours
        if market_monitoring_active("KR", trading_sessions=self.trading_sessions,
                                   after_close_hours=hours, now=self._now()):
            return self.poll_active_sec
        return self.poll_active_sec if imminent else self.poll_idle_sec

    def _positions_symbols(self) -> set[str]:
        held = {r["symbol"] for r in self.store.get_open_positions()}
        held |= {r["symbol"] for r in self.store.get_armed()}
        return held

    def poll_once(self) -> dict:
        """한 번 폴링→라우팅. 반환: {"new": n, "woke": [...], "queued": [...]}."""
        res = {"new": 0, "woke": [], "queued": []}
        filings = self.fetch_fn()
        self.polls += 1
        wake_payloads: list[dict] = []
        if not self._primed:                      # 기동 직후: 조용히 기준선만 잡는다
            self._seen = {f["rcept_no"] for f in filings if f.get("rcept_no")}
            self._primed = True
            log.info("공시 워처 기동 — 기존 %d건 마킹(각성 없음)", len(self._seen))
            # 재시작 때 미파싱 잠정실적은 각성 폭격 없이 수치만 회수한다.
            self._recover_pending_actuals()
            self._retry_pending_actuals(res, wake_payloads, force=True)
            self._emit_wake(wake_payloads)
            return res

        fresh = [f for f in filings
                 if f.get("rcept_no") and f["rcept_no"] not in self._seen]
        held = self._positions_symbols()
        universe = set(self.universe_fn() or ())
        for f in fresh:
            self._seen.add(f["rcept_no"])
            res["new"] += 1
            kw = is_material(f.get("report_nm"))
            sym = f.get("stock_code")
            if not kw or not sym:
                continue                          # 비중대/비상장 → 무시(소음 차단)
            payload = {"rcept_no": f["rcept_no"], "report_nm": f["report_nm"],
                       "corp_name": f.get("corp_name"), "keyword": kw,
                       "rcept_dt": f.get("rcept_dt"), "market": "KR"}
            # 실적 공시면 컨센서스(+가능하면 DART 실제 수치)를 붙여 뇌가 각성하는 즉시
            # '발표 vs 기대'를 비교하게 한다. 조회 실패는 삼킨다 — 컨센서스/actuals 가
            # 없어도 각성 자체는 그대로 나가야 한다.
            if is_earnings_actuals_report(f.get("report_nm")):
                consensus = self._consensus_for(sym)
                if consensus:
                    payload["consensus"] = consensus
                self._fetch_and_attach_actuals(
                    f, payload, consensus, held, universe, delayed=False)
            if sym in held:                       # 1) 내 포지션 → 즉시 각성
                self.store.log_event("disclosure", sym, payload | {"route": "wake"})
                wake_payloads.append(payload | {"symbol": sym})
                res["woke"].append(sym)
                log.warning("보유/대기 종목 중대 공시 %s: %s → 뇌 각성", sym, f["report_nm"])
            elif sym in universe:                 # 2) 커버 종목 → 큐(다음 사이클 첨부)
                self.store.log_event("disclosure", sym, payload | {"route": "queue"})
                res["queued"].append(sym)
                log.info("커버 종목 중대 공시 %s: %s → 큐", sym, f["report_nm"])
        # 신규 공시가 없어도 미준비 document 는 재시도한다(10초 폴마다 스캔, 항목별 10분).
        self._retry_pending_actuals(res, wake_payloads, force=False)
        self._emit_wake(wake_payloads)
        # _seen 무한증식 방지: 하루치보다 훨씬 큰 상한에서 절반 비움(rcept_no 는 일자 프리픽스라
        # 오래된 것부터 정렬 삭제 가능).
        if len(self._seen) > 20000:
            self._seen = set(sorted(self._seen)[10000:])
        return res

    def _consensus_for(self, sym: str) -> dict | None:
        if not self.earnings_fn:
            return None
        try:
            e = self.earnings_fn(sym) or {}
            return e.get("consensus")
        except Exception as ex:
            log.warning("[%s] 컨센서스 첨부 실패(무시): %s", sym, ex)
            return None

    def _fetch_and_attach_actuals(self, f: dict, payload: dict | None,
                                  consensus: dict | None, held: set[str],
                                  universe: set[str], *, delayed: bool) -> dict | None:
        """잠정실적 수치를 붙여 earnings_result 를 남긴다. 성공이면 actuals.

        실패가 재시도 가능하면 큐에 넣고 None. delayed=True 는 재시도/회수 경로 —
        이미 남긴 disclosure 행은 고치지 않고, 보유 종목이면 수치 확보 시점에 각성한다.
        """
        if not self.actuals_fn:
            return None
        rcept = f.get("rcept_no")
        sym = f.get("stock_code")
        try:
            actuals = self.actuals_fn(rcept) or {}
        except Exception as ex:
            log.warning("[%s] 실적 수치 첨부 실패(무시): %s", sym, ex)
            actuals = {"parse_ok": False, "retryable": True}
        if actuals.get("parse_ok"):
            self._commit_actuals(f, payload, actuals, consensus, held, universe,
                                 delayed=delayed)
            return actuals
        if actuals.get("retryable") and rcept:
            prev = self._pending_actuals.get(rcept) or {}
            self._pending_actuals[rcept] = {
                "filing": {k: f.get(k) for k in
                           ("rcept_no", "stock_code", "corp_name", "report_nm", "rcept_dt")},
                "consensus": consensus if consensus is not None else prev.get("consensus"),
                "first_ts": prev.get("first_ts", self._now()),
                "last_try": self._now(),
                "tries": int(prev.get("tries") or 0) + 1,
            }
            log.info("잠정실적 문서 미준비 %s %s — 재시도 대기", sym, rcept)
        return None

    def _commit_actuals(self, f: dict, payload: dict | None, actuals: dict,
                        consensus: dict | None, held: set[str], universe: set[str],
                        *, delayed: bool) -> None:
        from ..datasources.dart import surprise_vs_consensus
        surprises = surprise_vs_consensus(actuals, consensus)
        if payload is not None:
            payload["actuals"] = actuals
            if surprises:
                payload["surprise_pct"] = surprises
        sym = f.get("stock_code")
        er = {
            "market": "KR", "symbol": sym,
            "date": f.get("rcept_dt"),
            "rcept_no": f.get("rcept_no"),
            "revenue_actual": actuals.get("revenue"),
            "op_profit_actual": actuals.get("op_profit"),
            "net_income_actual": actuals.get("net_income"),
            "unit": actuals.get("unit"),
            "scope": actuals.get("scope"),
            "parse_ok": True,
            **surprises,
            "detected_at": self._now(),
        }
        if consensus:
            er["revenue_estimate"] = consensus.get("revenue")
            er["op_profit_estimate"] = consensus.get("op_profit")
            er["net_income_estimate"] = consensus.get("net_income")
        route = "wake" if sym in held else (
            "queue" if (sym in universe or delayed) else None)
        if route:
            extra = {"route": route}
            if delayed:
                extra["recovered"] = True
            self.store.log_event("earnings_result", sym, er | extra)
            if delayed:
                log.info("잠정실적 수치 확보(%s) %s rcept=%s",
                         "재시도" if extra.get("recovered") else "회수",
                         sym, f.get("rcept_no"))

    def _retry_pending_actuals(self, res: dict, wake_payloads: list[dict],
                               *, force: bool) -> None:
        if not self.actuals_fn or not self._pending_actuals:
            return
        now = self._now()
        held = self._positions_symbols()
        universe = set(self.universe_fn() or ())
        for rcept, item in list(self._pending_actuals.items()):
            first = float(item.get("first_ts") or now)
            tries = int(item.get("tries") or 0)
            last = float(item.get("last_try") or 0)
            if now - first > _ACTUALS_MAX_AGE_SEC or tries >= _ACTUALS_MAX_TRIES:
                log.warning("잠정실적 재시도 포기 %s age=%.0fh tries=%d",
                            rcept, (now - first) / 3600.0, tries)
                self._pending_actuals.pop(rcept, None)
                continue
            if not force and now - last < _ACTUALS_RETRY_SEC:
                continue
            f = dict(item.get("filing") or {})
            f.setdefault("rcept_no", rcept)
            consensus = item.get("consensus")
            actuals = self._fetch_and_attach_actuals(
                f, None, consensus, held, universe, delayed=True)
            if actuals:
                self._pending_actuals.pop(rcept, None)
                sym = f.get("stock_code")
                if sym in held:
                    payload = {
                        "rcept_no": rcept, "report_nm": f.get("report_nm"),
                        "corp_name": f.get("corp_name"),
                        "keyword": is_material(f.get("report_nm")) or "잠정실적",
                        "rcept_dt": f.get("rcept_dt"), "recovered": True,
                        "symbol": sym, "actuals": actuals, "market": "KR",
                    }
                    wake_payloads.append(payload)
                    res["woke"].append(sym)
                    log.warning("잠정실적 수치 확보(재시도) %s → 뇌 각성", sym)
                continue
            nxt = self._pending_actuals.get(rcept)
            if not nxt or nxt.get("last_try") == last:
                self._pending_actuals.pop(rcept, None)

    def _recover_pending_actuals(self) -> None:
        """스토어의 최근 실적 공시 중 earnings_result 가 없는 건을 재시도 큐에 넣는다."""
        if not self.actuals_fn:
            return
        now = self._now()
        try:
            discs = self.store.recent_events(
                "disclosure", now - _ACTUALS_MAX_AGE_SEC, limit=200)
            results = self.store.recent_events(
                "earnings_result", now - _ACTUALS_MAX_AGE_SEC, limit=200)
        except Exception as e:
            log.warning("잠정실적 회수 조회 실패(무시): %s", e)
            return
        have = set()
        for row in results:
            p = json.loads(row["payload"]) if row["payload"] else {}
            if p.get("rcept_no") and p.get("parse_ok") is not False:
                have.add(str(p["rcept_no"]))
        n = 0
        for row in discs:
            p = json.loads(row["payload"]) if row["payload"] else {}
            rcept = p.get("rcept_no")
            if not rcept or str(rcept) in have or rcept in self._pending_actuals:
                continue
            if not is_earnings_actuals_report(p.get("report_nm")):
                continue
            if (p.get("actuals") or {}).get("parse_ok"):
                continue
            first_ts = float(row["ts"] if row["ts"] is not None else now)
            if now - first_ts > _ACTUALS_MAX_AGE_SEC:
                continue
            self._pending_actuals[rcept] = {
                "filing": {
                    "rcept_no": rcept,
                    "stock_code": row["symbol"],
                    "corp_name": p.get("corp_name"),
                    "report_nm": p.get("report_nm"),
                    "rcept_dt": p.get("rcept_dt"),
                },
                "consensus": p.get("consensus"),
                "first_ts": first_ts,
                "last_try": 0.0,
                "tries": 0,
            }
            n += 1
        if n:
            log.info("잠정실적 미파싱 %d건 회수 큐", n)

    def _emit_wake(self, wake_payloads: list[dict]) -> None:
        if not wake_payloads or not self.on_wake:
            return
        try:
            self.on_wake("disclosure", wake_payloads)
        except Exception as e:
            log.error("공시 각성 콜백 실패: %s", e)
            self.store.log_event("error", None, {"where": "disclosure_wake",
                                                 "err": str(e)})

    def run_forever(self, stop_event: threading.Event | None = None) -> int:
        polls = 0
        self.store.log_event("disclosure_start", None,
                             {"active_sec": self.poll_active_sec,
                              "idle_sec": self.poll_idle_sec})
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                self.poll_once()
            except Exception as e:                # 한 번의 실패가 워처를 죽이지 않게
                log.warning("공시 폴링 실패(계속): %s", e)
            polls += 1
            self._sleep(self.interval())
        return polls


def start_watcher_thread(watcher: DisclosureWatcher) -> tuple[threading.Thread,
                                                              threading.Event]:
    """데몬 스레드로 워처 기동. (thread, stop_event) 반환 — watch.py 종료 시 set."""
    stop = threading.Event()
    t = threading.Thread(target=watcher.run_forever, args=(stop,),
                         name="disclosure", daemon=True)
    t.start()
    return t, stop

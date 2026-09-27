"""lastPrice 이상 틱 필터 — 확장시간대 오류 체결가로 손절이 발동하던 사고 대응.

사고(2026-09-23 06:08 KST, US 애프터): /prices lastPrice 가 HPE 61.1→21.58,
ROIV 38.25→29.12 로 찍혔다(ROIV 호가는 bid 37.98 / ask 38.97 그대로). 감시 루프가
이 값을 검증 없이 손절·논거무효화에 써서 두 종목을 가짜 stop_hit 으로 청산했다.
ROIV 의 오류가는 30분 넘게 그대로 유지돼 "N틱 연속 확인"만으로는 막을 수 없다.

정책: 종목별 마지막 채택가(ref) 대비 max_jump_pct 넘게 튄 틱만 '의심'으로 보고 호가로
교차확인한다(평상시 추가 호출 0).
  - 의심가가 호가 범위 [bid, ask] ± quote_tol_pct 안 → 진짜 급변, 채택.
  - 호가가 의심가를 부정 → 기각. 이번 값은 호가 중간값(없으면 ref)으로 대체.
  - 호가를 못 구함 → 의심 상태가 confirm_sec 이상 이어지면 채택(진짜 급락 청산을
    영영 막지 않게), 그 전엔 ref 유지.
ref 가 없거나 ref_max_age_sec 보다 오래됐으면 판단 근거가 없어 그대로 채택한다.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable

QuoteFn = Callable[[str], "tuple[float | None, float | None] | None"]


class PriceGuard:
    def __init__(self, quote_fn: QuoteFn, *,
                 max_jump_pct: float = 0.08,
                 quote_tol_pct: float = 0.03,
                 confirm_sec: float = 180.0,
                 recheck_sec: float = 30.0,
                 ref_max_age_sec: float = 1800.0,
                 max_quotes_per_sec: float = 3,
                 on_event: Callable[[str, dict], None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.quote_fn = quote_fn
        self.max_jump_pct = float(max_jump_pct)
        self.quote_tol_pct = float(quote_tol_pct)
        self.confirm_sec = float(confirm_sec)
        self.recheck_sec = float(recheck_sec)
        self.ref_max_age_sec = float(ref_max_age_sec)
        self.max_quotes_per_sec = float(max_quotes_per_sec)
        self._quote_ts: deque[float] = deque()
        self.on_event = on_event
        self._clock = clock
        self._lock = threading.Lock()
        self._ref: dict[str, tuple[float, float]] = {}            # sym -> (price, ts)
        self._suspect_since: dict[str, float] = {}
        # 같은 오류가가 매 틱 반복될 때 호가를 매초 다시 부르지 않도록 판정 캐시.
        self._verdict: dict[tuple[str, float], tuple[float, str, float]] = {}

    @classmethod
    def from_config(cls, raw: dict | None, quote_fn: QuoteFn,
                    on_event: Callable[[str, dict], None] | None = None
                    ) -> "PriceGuard | None":
        """watch.price_guard 블록. enabled:false 면 None(가드 없음 = 구 동작)."""
        raw = raw or {}
        if not raw.get("enabled", True):
            return None
        keys = ("max_jump_pct", "quote_tol_pct", "confirm_sec", "recheck_sec",
                "ref_max_age_sec", "max_quotes_per_sec")
        return cls(quote_fn, on_event=on_event,
                   **{k: float(raw[k]) for k in keys if raw.get(k) is not None})

    def filter(self, symbol: str, price: float | None) -> tuple[float | None, str | None]:
        """(쓸 가격, 판정). 판정 None = 평상 채택. 그 외 confirmed/rejected/held/timeout."""
        if not symbol or price is None or price <= 0:
            return price, None
        now = self._clock()
        with self._lock:
            ref = self._ref.get(symbol)
            if ref is None or now - ref[1] > self.ref_max_age_sec:
                self._accept(symbol, price, now)
                return price, None
            ref_px = ref[0]
            if abs(price / ref_px - 1) <= self.max_jump_pct:
                self._accept(symbol, price, now)
                return price, None
            since = self._suspect_since.setdefault(symbol, now)
            cached = self._verdict.get((symbol, price))
            if cached and now - cached[2] < self.recheck_sec:
                use, verdict, _ = cached
                if verdict == "rejected":
                    return use, verdict
                if verdict == "held" and now - since < self.confirm_sec:
                    return ref_px, verdict
            # 호가 조회 상한 — 오류가가 수십 종목에 동시에 뜨면(09-23 06:08 분당 ~110건)
            # 한 틱이 호가 조회로 수 초 막힌다. 초과분은 이번 틱만 직전가 유지.
            while self._quote_ts and now - self._quote_ts[0] >= 1.0:
                self._quote_ts.popleft()
            if len(self._quote_ts) >= self.max_quotes_per_sec:
                if now - since >= self.confirm_sec:
                    self._accept(symbol, price, now)
                    return price, "timeout"
                return ref_px, "held"
            self._quote_ts.append(now)

        # 호가 조회는 락 밖에서(네트워크). 실패는 None 과 같게 취급.
        try:
            quote = self.quote_fn(symbol)
        except Exception:
            quote = None
        mid = _mid(quote)

        with self._lock:
            info: dict[str, Any] = {"symbol": symbol, "raw": price, "ref": ref_px,
                                    "jump_pct": round(price / ref_px - 1, 4)}
            if mid is not None:
                info["quote_mid"] = mid
                if _within_quote(price, quote, self.quote_tol_pct):
                    verdict, use = "confirmed", price
                else:
                    verdict, use = "rejected", mid
                self._accept(symbol, use, now)
            elif now - since >= self.confirm_sec:
                verdict, use = "timeout", price
                self._accept(symbol, price, now)
            else:
                verdict, use = "held", ref_px
            prev = self._verdict.get((symbol, price))
            self._verdict[(symbol, price)] = (use, verdict, now)
            if len(self._verdict) > 2000:
                self._verdict.clear()
        # 같은 (종목, 오류가) 는 판정이 바뀔 때만 이벤트 — 매 틱 스팸 방지.
        if self.on_event and (prev is None or prev[1] != verdict):
            info["verdict"] = verdict
            info["used"] = use
            try:
                self.on_event("price_guard", info)
            except Exception:
                pass
        return use, verdict

    def _accept(self, symbol: str, price: float, now: float) -> None:
        self._ref[symbol] = (price, now)
        self._suspect_since.pop(symbol, None)


def _within_quote(price: float, quote, tol: float) -> bool:
    """체결가가 호가 범위 [bid, ask] 를 tol 만큼 넓힌 구간 안이면 True.

    중간값 기준이면 시간외처럼 스프레드가 넓을 때 정상 체결도 기각된다.
    """
    bid, ask = quote
    lo = bid if bid and bid > 0 else ask
    hi = ask if ask and ask > 0 else bid
    return lo * (1 - tol) <= price <= hi * (1 + tol)


def _mid(quote) -> float | None:
    if not quote:
        return None
    bid, ask = quote
    bid = bid if bid and bid > 0 else None
    ask = ask if ask and ask > 0 else None
    if bid and ask:
        return (bid + ask) / 2
    return bid or ask

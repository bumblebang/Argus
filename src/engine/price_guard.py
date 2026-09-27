"""lastPrice 이상 틱 필터 — 확장시간대 오류 체결가로 손절이 발동하던 사고 대응.

사고(2026-09-23 06:08 KST, US 애프터): /prices lastPrice 가 HPE 61.1→21.58,
ROIV 38.25→29.12 로 찍혔다(ROIV 호가는 bid 37.98 / ask 38.97 그대로). 감시 루프가
이 값을 검증 없이 손절·논거무효화에 써서 두 종목을 가짜 stop_hit 으로 청산했다.
ROIV 의 오류가는 30분 넘게 그대로 유지돼 "N틱 연속 확인"만으로는 막을 수 없다.

정책: 종목별 마지막 채택가(ref) 대비 max_jump_pct 넘게 튄 틱만 '의심'으로 보고 호가로
교차확인한다(평상시 추가 호출 0).
  - 의심가가 호가 범위 [bid, ask] ± quote_tol_pct 안 → 진짜 급변, 채택.
  - 호가가 의심가를 부정 → 기각. 이번 값은 호가 중간값으로 대체. 기각은 의심 시계를
    리셋하지 않는다. 호가가 낡았으면(호가 시각이 의심 시작 이전에 멈춤) 의심이
    confirm_sec 넘게 이어지고 체결가가 2가지 이상 찍힐 때 채택한다 — 멈춘 호가가 진짜
    급락 손절을 영영 막지 않게. 한 값에 고정된 오류가(ROIV)는 끝까지 기각된다.
  - 호가를 못 구함 → 의심 상태가 confirm_sec 이상 이어지면 채택(진짜 급락 청산을
    영영 막지 않게), 그 전엔 ref 유지. 이번 의심 중 호가가 한 번이라도 부정했으면
    호가를 못 구하는 틱에도 기각을 유지한다(호가 예산 초과로 굶는 종목 보호).
ref 가 없으면(재기동) seed_refs 로 직전 스냅샷 가격을 기준으로 삼는다. ref 가
ref_max_age_sec 보다 오래됐어도 튄 틱은 호가로 확인하되, 호가를 못 구하면 낡은
기준으로 붙잡지 않고 채택한다(개장 갭).
호가 조회 실패는 종목별 quote_fail_cooldown_sec 동안 재시도하지 않고, 연속 실패가
쌓이면 전 종목 조회를 잠시 끊는다 — 한 종목 호가 장애가 매 틱을 붙잡지 않게.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable

# (bid, ask) 또는 (bid, ask, 호가시각 epoch|None). 조회 실패는 None.
QuoteFn = Callable[[str], "tuple | None"]

_MAX_SUSPECT_RAWS = 16
_QUOTE_FAIL_STREAK_TRIP = 3


class PriceGuard:
    def __init__(self, quote_fn: QuoteFn, *,
                 max_jump_pct: float = 0.08,
                 quote_tol_pct: float = 0.03,
                 confirm_sec: float = 180.0,
                 recheck_sec: float = 30.0,
                 ref_max_age_sec: float = 1800.0,
                 max_quotes_per_sec: float = 3,
                 quote_fail_cooldown_sec: float = 30.0,
                 on_event: Callable[[str, dict], None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.quote_fn = quote_fn
        self.max_jump_pct = float(max_jump_pct)
        self.quote_tol_pct = float(quote_tol_pct)
        self.confirm_sec = float(confirm_sec)
        self.recheck_sec = float(recheck_sec)
        self.ref_max_age_sec = float(ref_max_age_sec)
        self.max_quotes_per_sec = float(max_quotes_per_sec)
        self.quote_fail_cooldown_sec = float(quote_fail_cooldown_sec)
        self._quote_ts: deque[float] = deque()
        self.on_event = on_event
        self._clock = clock
        self._lock = threading.Lock()
        self._ref: dict[str, tuple[float, float]] = {}            # sym -> (price, ts)
        self._seeded: set[str] = set()
        self._suspect_since: dict[str, float] = {}
        self._suspect_raws: dict[str, set[float]] = {}
        # 이번 의심 중 호가가 의심가를 부정한 마지막 판정 sym -> (mid, ts).
        self._last_reject: dict[str, tuple[float, float]] = {}
        # 같은 오류가가 매 틱 반복될 때 호가를 매초 다시 부르지 않도록 판정 캐시.
        # sym -> {raw: (use, verdict, ts)}. 정상 채택 시 종목 단위로 비운다.
        self._verdict: dict[str, dict[float, tuple[float, str, float]]] = {}
        self._quote_fail_until: dict[str, float] = {}
        self._quote_fail_streak = 0
        self._quote_block_until = 0.0

    @classmethod
    def from_config(cls, raw: dict | None, quote_fn: QuoteFn,
                    on_event: Callable[[str, dict], None] | None = None
                    ) -> "PriceGuard | None":
        """watch.price_guard 블록. enabled:false 면 None(가드 없음 = 구 동작)."""
        raw = raw or {}
        if not raw.get("enabled", True):
            return None
        keys = ("max_jump_pct", "quote_tol_pct", "confirm_sec", "recheck_sec",
                "ref_max_age_sec", "max_quotes_per_sec", "quote_fail_cooldown_sec")
        return cls(quote_fn, on_event=on_event,
                   **{k: float(raw[k]) for k in keys if raw.get(k) is not None})

    # ── 재기동 시드 ─────────────────────────────────────────
    def unseeded(self, symbols) -> list[str]:
        """ref 도 없고 시드도 시도 안 한 종목(호출측이 직전 가격을 찾아 seed_refs)."""
        with self._lock:
            return [s for s in symbols
                    if s and s not in self._ref and s not in self._seeded]

    def seed_refs(self, refs: dict[str, tuple[float, float]],
                  tried: list[str] | None = None) -> None:
        """재기동 직후 기준가 주입 {sym: (price, ts)}. 이미 ref 가 있으면 무시."""
        with self._lock:
            for s in tried or refs.keys():
                self._seeded.add(s)
            for s, (px, ts) in refs.items():
                if s not in self._ref and px and px > 0:
                    self._ref[s] = (float(px), float(ts))

    # ── 판정 ────────────────────────────────────────────────
    def filter(self, symbol: str, price: float | None) -> tuple[float | None, str | None]:
        """(쓸 가격, 판정). 판정 None = 평상 채택.
        그 외 confirmed/rejected/held/timeout/stale_quote."""
        if not symbol or price is None or price <= 0:
            return price, None
        now = self._clock()
        with self._lock:
            ref = self._ref.get(symbol)
            if ref is None:
                self._accept(symbol, price, now)
                return price, None
            ref_px, ref_ts = ref
            if abs(price / ref_px - 1) <= self.max_jump_pct:
                self._accept(symbol, price, now)
                return price, None
            stale_ref = now - ref_ts > self.ref_max_age_sec
            since = self._suspect_since.setdefault(symbol, now)
            raws = self._suspect_raws.setdefault(symbol, set())
            if len(raws) < _MAX_SUSPECT_RAWS:
                raws.add(price)
            cached = self._verdict.get(symbol, {}).get(price)
            if cached and now - cached[2] < self.recheck_sec:
                use, verdict, _ = cached
                if verdict == "rejected":
                    return use, verdict
                if verdict == "held" and now - since < self.confirm_sec:
                    return ref_px, verdict
            # 호가 조회 상한 — 오류가가 수십 종목에 동시에 뜨면(09-23 06:08 분당 ~110건)
            # 한 틱이 호가 조회로 수 초 막힌다. 초과분·조회 쿨다운 중엔 호가 없이 판정.
            while self._quote_ts and now - self._quote_ts[0] >= 1.0:
                self._quote_ts.popleft()
            cooling = (now < self._quote_block_until
                       or now < self._quote_fail_until.get(symbol, 0.0))
            if cooling or len(self._quote_ts) >= self.max_quotes_per_sec:
                return self._judge_without_quote(symbol, price, ref_px, since,
                                                 stale_ref, now)
            self._quote_ts.append(now)

        # 호가 조회는 락 밖에서(네트워크). 실패는 None 과 같게 취급.
        try:
            quote = self.quote_fn(symbol)
        except Exception:
            quote = None
        bid, ask, quote_at = _unpack(quote)
        mid = _mid(bid, ask)

        with self._lock:
            info: dict[str, Any] = {"symbol": symbol, "raw": price, "ref": ref_px,
                                    "jump_pct": round(price / ref_px - 1, 4)}
            if mid is None:
                self._note_quote_failure(symbol, now)
                use, verdict = self._judge_without_quote(symbol, price, ref_px, since,
                                                         stale_ref, now)
            else:
                self._quote_fail_streak = 0
                self._quote_fail_until.pop(symbol, None)
                info["quote_mid"] = mid
                if _within_quote(price, bid, ask, self.quote_tol_pct):
                    verdict, use = "confirmed", price
                    self._accept(symbol, price, now)
                elif self._quote_frozen(quote_at, since, now,
                                        len(self._suspect_raws.get(symbol, ()))):
                    verdict, use = "stale_quote", price
                    info["quote_at"] = quote_at
                    self._accept(symbol, price, now)
                else:
                    verdict, use = "rejected", mid
                    # 기준가는 호가 중간값으로 옮기되 의심 시계는 유지한다.
                    self._ref[symbol] = (mid, now)
                    self._last_reject[symbol] = (mid, now)
            prev = self._verdict.get(symbol, {}).get(price)
            if verdict in ("rejected", "held"):
                self._verdict.setdefault(symbol, {})[price] = (use, verdict, now)
                if sum(len(v) for v in self._verdict.values()) > 2000:
                    self._verdict.clear()
        # 같은 (종목, 오류가) 는 판정이 바뀔 때만 이벤트 — 매 틱 스팸 방지.
        if self.on_event and (prev is None or prev[1] != verdict):
            info["verdict"] = verdict
            info["used"] = use
            info["suspect_sec"] = round(now - since, 1)
            try:
                self.on_event("price_guard", info)
            except Exception:
                pass
        return use, verdict

    def _judge_without_quote(self, symbol: str, price: float, ref_px: float,
                             since: float, stale_ref: bool, now: float
                             ) -> tuple[float, str]:
        """락 안: 호가 없이 판정. 이번 의심 중 호가가 부정한 적 있으면 기각 유지."""
        rej = self._last_reject.get(symbol)
        if rej is not None and rej[1] >= since:
            return rej[0], "rejected"
        if stale_ref or now - since >= self.confirm_sec:
            self._accept(symbol, price, now)
            return price, "timeout"
        return ref_px, "held"

    def _quote_frozen(self, quote_at: float | None, since: float, now: float,
                      distinct_raws: int) -> bool:
        """호가가 의심가를 부정하지만 믿을 수 없는가(멈춘 호가).

        호가 시각이 의심 시작 뒤로 한 번이라도 갱신됐으면 살아 있는 호가 — 기각이 맞다.
        그 외엔 의심이 confirm_sec 이상 이어지고 체결가가 2가지 이상 찍혔을 때만
        멈춘 호가로 본다. 한 값에 고정된 오류가(ROIV 29.12 30분)는 해당 없음.
        """
        if quote_at is not None and quote_at >= since:
            return False
        return now - since >= self.confirm_sec and distinct_raws >= 2

    def _note_quote_failure(self, symbol: str, now: float) -> None:
        self._quote_fail_until[symbol] = now + self.quote_fail_cooldown_sec
        self._quote_fail_streak += 1
        if self._quote_fail_streak >= _QUOTE_FAIL_STREAK_TRIP:
            self._quote_block_until = now + self.quote_fail_cooldown_sec
            self._quote_fail_streak = 0

    def _accept(self, symbol: str, price: float, now: float) -> None:
        self._ref[symbol] = (price, now)
        self._suspect_since.pop(symbol, None)
        self._suspect_raws.pop(symbol, None)
        self._last_reject.pop(symbol, None)
        self._verdict.pop(symbol, None)


def _unpack(quote) -> tuple[float | None, float | None, float | None]:
    if not quote:
        return None, None, None
    bid, ask = quote[0], quote[1]
    at = quote[2] if len(quote) > 2 else None
    bid = bid if bid and bid > 0 else None
    ask = ask if ask and ask > 0 else None
    return bid, ask, at


def _within_quote(price: float, bid: float | None, ask: float | None,
                  tol: float) -> bool:
    """체결가가 호가 범위 [bid, ask] 를 tol 만큼 넓힌 구간 안이면 True.

    중간값 기준이면 시간외처럼 스프레드가 넓을 때 정상 체결도 기각된다.
    """
    lo = bid or ask
    hi = ask or bid
    return lo * (1 - tol) <= price <= hi * (1 + tol)


def _mid(bid: float | None, ask: float | None) -> float | None:
    if bid and ask:
        return (bid + ask) / 2
    return bid or ask

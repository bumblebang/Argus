"""세션 정책 SSOT — trading/brain/value 세션 파싱과 장중 판정.

watch 루프·밸류·대시보드·경보·갱신기·워처가 config 와 같은 기준으로
'이 시장이 지금 활성인가?' 를 판단하게 한다.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .market_hours import current_session, is_tradable, within_after_close

DEFAULT_TRADING_SESSIONS: dict[str, tuple[str, ...]] = {
    "KR": ("regular",), "US": ("regular",),
}
DEFAULT_BRAIN_SESSIONS: dict[str, tuple[str, ...]] = {
    "KR": ("regular",), "US": ("regular",),
}
DEFAULT_VALUE_SESSIONS: dict[str, tuple[str, ...]] = {
    "KR": ("regular",), "US": ("regular",),
}


def parse_session_map(block: Any,
                      default: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    """{market: [세션,...]} 블록 파싱 — loop.WatchConfig 와 동일 규칙."""
    out = dict(default)
    if isinstance(block, dict):
        for m, names in block.items():
            if isinstance(names, str):
                names = [names]
            if not isinstance(names, (list, tuple)):
                names = []
            out[str(m).upper()] = tuple(str(s).strip() for s in names if str(s).strip())
    return out


def trading_sessions_from_raw(raw: dict | None) -> dict[str, tuple[str, ...]]:
    return parse_session_map((raw or {}).get("trading_sessions"), DEFAULT_TRADING_SESSIONS)


def brain_sessions_from_raw(raw: dict | None) -> dict[str, tuple[str, ...]]:
    w = (raw or {}).get("watch", {}) or {}
    return parse_session_map(w.get("brain_sessions"), DEFAULT_BRAIN_SESSIONS)


def value_sessions_from_raw(raw: dict | None) -> dict[str, tuple[str, ...]]:
    vt = (raw or {}).get("value_trade", {}) or {}
    return parse_session_map(vt.get("sessions"), DEFAULT_VALUE_SESSIONS)


def _allowed(sessions: dict[str, tuple[str, ...]], market: str) -> tuple[str, ...] | None:
    return sessions.get(str(market or "").upper())


def market_tradable(market: str, sessions: dict[str, tuple[str, ...]] | None = None,
                    now: float | None = None) -> bool:
    """config trading_sessions 기준 주문·폴링 허용 여부."""
    sess = sessions if sessions is not None else DEFAULT_TRADING_SESSIONS
    return is_tradable(market, _allowed(sess, market), now)


def any_market_tradable(markets: list[str],
                        sessions: dict[str, tuple[str, ...]] | None = None,
                        now: float | None = None) -> bool:
    return any(market_tradable(m, sessions, now) for m in markets)


def market_brain_active(market: str, sessions: dict[str, tuple[str, ...]] | None = None,
                        now: float | None = None) -> bool:
    """정기 뇌 각성(brain_sessions) 허용 세션인지."""
    sess = sessions if sessions is not None else DEFAULT_BRAIN_SESSIONS
    allowed = _allowed(sess, market)
    if not allowed:
        return False
    return current_session(market, now) in allowed


def market_value_due(market: str, sessions: dict[str, tuple[str, ...]] | None = None,
                     now: float | None = None) -> bool:
    """밸류 트랙 due 판정 — 기본 정규장, value_trade.sessions 로 확장 가능."""
    sess = sessions if sessions is not None else DEFAULT_VALUE_SESSIONS
    return is_tradable(market, _allowed(sess, market), now)


def make_tradable_fn(sessions: dict[str, tuple[str, ...]] | None = None,
                     now_fn: Callable[[], float] | None = None) -> Callable[[str], bool]:
    """SliceRefresher / UniverseRefresher 주입용."""
    nf = now_fn or time.time

    def fn(market: str) -> bool:
        return market_tradable(market, sessions, nf())

    return fn


def market_monitoring_active(market: str, *,
                             trading_sessions: dict[str, tuple[str, ...]] | None = None,
                             after_close_hours: float = 0,
                             now: float | None = None) -> bool:
    """공시·EDGAR 등 정보 수집 활성 — 거래 세션 또는 장마감 후 N시간."""
    ts = time.time() if now is None else float(now)
    if market_tradable(market, trading_sessions, ts):
        return True
    if after_close_hours > 0:
        dt = datetime.fromtimestamp(ts, ZoneInfo("UTC"))
        if within_after_close(market, after_close_hours, dt):
            return True
    return False

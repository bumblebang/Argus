"""장 상태 교차검증 — 토스 세션 캐시 vs 외부(US Finnhub/Yahoo, KR 정규장 달력).

alert_check(5분 주기) 전용. 외부 조회 실패는 fail-open(경보 없음).
정규장(regular)만 비교 — 프리/애프터는 Argus trading_sessions 설정과 정의가 달라 오탐 방지.
KR/US 모두 Yahoo marketState(또는 US Finnhub) — 자기 is_open 달력과 비교하지 않는다.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .logging_setup import get_logger
from .market_hours import (
    _SESSIONS_CACHE,
    _cache_valid,
    current_session,
    is_open,
    trading_date,
)

log = get_logger("src.market_status_crosscheck")

_KST = ZoneInfo("Asia/Seoul")
_FINNHUB_BASE = "https://finnhub.io/api/v1"
_YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/SPY"
_YAHOO_CHART_KR = "https://query1.finance.yahoo.com/v8/finance/chart/%5EKS11"
_YAHOO_UA = {"User-Agent": "Mozilla/5.0 argus"}


def argus_regular_open(market: str, now: float | None = None) -> bool:
    """토스 세션 캐시(없으면 is_open 폴백) 기준 정규장 여부."""
    return current_session(market, now) == "regular"


def _normalize_finnhub_session(raw: str | None) -> str:
    return str(raw or "").lower().replace("-", "").replace("_", "").strip()


def fetch_us_market_status_finnhub(api_key: str, *, timeout: float = 8) -> dict | None:
    """Finnhub GET /stock/market-status?exchange=US. 실패 시 None."""
    if not api_key:
        return None
    try:
        r = requests.get(
            f"{_FINNHUB_BASE}/stock/market-status",
            params={"exchange": "US", "token": api_key},
            timeout=timeout,
        )
        if r.status_code != 200:
            log.debug("Finnhub market-status http=%s", r.status_code)
            return None
        data = r.json()
        return data if isinstance(data, dict) else None
    except Exception as e:
        log.debug("Finnhub market-status 실패: %s", e)
        return None


def us_regular_open_finnhub(api_key: str | None = None, *, now: float | None = None) -> bool | None:
    """Finnhub US 정규장 open. None=조회 불가."""
    del now  # Finnhub 응답이 실시간
    key = (api_key if api_key is not None else os.getenv("FINNHUB_API_KEY") or "").strip()
    data = fetch_us_market_status_finnhub(key) if key else None
    if not data:
        return None
    sess = _normalize_finnhub_session(data.get("session"))
    if sess == "regular":
        return bool(data.get("isOpen"))
    if sess in ("", "closed"):
        return False
    return False


def _yahoo_regular_open(url: str, *, timeout: float = 8) -> bool | None:
    """Yahoo chart meta.marketState == REGULAR. None=조회 불가."""
    try:
        r = requests.get(
            url,
            params={"interval": "1d", "range": "1d"},
            headers=_YAHOO_UA,
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        results = (r.json().get("chart") or {}).get("result") or []
        if not results:
            return None
        state = str((results[0].get("meta") or {}).get("marketState") or "").upper()
        if state == "REGULAR":
            return True
        if state in ("PRE", "PREPRE", "POST", "POSTPOST", "CLOSED", ""):
            return False
        return False
    except Exception as e:
        log.debug("Yahoo marketState 실패(%s): %s", url, e)
        return None


def us_regular_open_yahoo(*, timeout: float = 8) -> bool | None:
    """Yahoo SPY chart meta.marketState == REGULAR. None=조회 불가."""
    return _yahoo_regular_open(_YAHOO_CHART, timeout=timeout)


def kr_regular_open_yahoo(*, timeout: float = 8) -> bool | None:
    """Yahoo ^KS11 chart meta.marketState == REGULAR. None=조회 불가."""
    return _yahoo_regular_open(_YAHOO_CHART_KR, timeout=timeout)


def external_regular_open(market: str, now: float | None = None) -> bool | None:
    """독립 소스 정규장 여부. None=fail-open."""
    m = str(market or "").upper()
    if m == "US":
        ext = us_regular_open_finnhub()
        if ext is not None:
            return ext
        return us_regular_open_yahoo()
    if m == "KR":
        return kr_regular_open_yahoo()
    return None


def kr_cache_date_mismatch(now: float | None = None) -> str | None:
    """KR 세션 캐시 date ≠ 오늘 거래일 — stale 의심(정규장 시간대만)."""
    ts = time.time() if now is None else float(now)
    local = datetime.fromtimestamp(ts, _KST)
    if not is_open("KR", local):
        return None
    try:
        cache = json.loads(_SESSIONS_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = cache.get("KR") if isinstance(cache, dict) else None
    if not isinstance(entry, dict):
        return None
    expected = trading_date("KR", ts)
    cached_date = str(entry.get("date") or "")
    if not cached_date or cached_date == expected:
        return None
    if _cache_valid("KR", entry, ts):
        return None
    return (f"KR 세션 캐시 date={cached_date} ≠ 거래일 {expected} "
            f"(정규장) — market-calendar 갱신 필요")


def crosscheck_reasons(now: float | None = None) -> list[str]:
    """정규장 불일치·KR 캐시 date 경보 문구. 빈 리스트=정상 또는 fail-open."""
    ts = time.time() if now is None else float(now)
    reasons: list[str] = []
    for market in ("KR", "US"):
        argus = argus_regular_open(market, ts)
        ext = external_regular_open(market, ts)
        if ext is None:
            continue
        if argus != ext:
            reasons.append(
                f"장 상태 불일치 {market}: Argus 정규장={argus} 외부={ext} "
                f"— 세션 캐시 stale 의심")
    date_msg = kr_cache_date_mismatch(ts)
    if date_msg:
        reasons.append(date_msg)
    return reasons

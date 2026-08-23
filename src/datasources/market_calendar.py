"""토스 market-calendar 세션표 조회·캐시 (프리마켓/정규장/애프터마켓 인지).

is_open(정규장 단일 구간)만으로는 NXT 애프터마켓/프리마켓을 못 잡는다. 토스
GET /api/v1/market-calendar/{country} 가 세션별 운영시간을 주므로 하루 1회 받아
data/market_sessions.json 에 epoch 구간으로 캐시한다. market_hours.current_session
이 이 캐시를 읽어 현재 세션명을 판정한다. 실패는 전부 무시(폴백은 is_open).

응답 스키마(openapi.json 확인):
- KR: today.integrated.{preMarket, regularMarket, afterMarket}  (integrated 가 null 이면 휴장)
- US: today.{dayMarket, preMarket, regularMarket, afterMarket}
- 각 세션의 startTime/endTime 은 ISO8601 date-time(+09:00 오프셋 포함).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..logging_setup import get_logger

log = get_logger("src.market_calendar")

_KST = ZoneInfo("Asia/Seoul")
CACHE_PATH = Path("data/market_sessions.json")

# 토스 세션키 -> 표준 세션명(소문자). US 는 daymarket 추가.
_SESSION_MAP = [
    ("preMarket", "premarket"),
    ("dayMarket", "daymarket"),
    ("regularMarket", "regular"),
    ("afterMarket", "aftermarket"),
]


def _to_epoch(iso: str | None) -> float | None:
    """ISO8601 date-time(예: '2026-03-25T08:00:00+09:00') -> epoch(초). 실패 시 None."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError):
        return None


def fetch_sessions(client, market: str) -> dict:
    """client.get_market_calendar(market) -> today 세션을 표준 dict 로 정규화.

    반환 예: {"market":"KR", "date":"2026-07-14",
             "sessions":[{"name":"premarket","start":<epoch>,"end":<epoch>}, ...]}.
    KR 은 세션이 today.integrated 아래, US 는 today 바로 아래에 있다. start/end 를
    epoch(초)로 변환하고, 시각 파싱에 실패한 세션은 건너뛴다.
    """
    m = market.upper()
    raw = client.get_market_calendar(m) or {}
    today = raw.get("today") or {}
    date = today.get("date")
    # KR: today.integrated 아래에 세션(휴장이면 integrated=null). US: today 바로 아래.
    container = (today.get("integrated") or {}) if m == "KR" else today
    sessions: list[dict] = []
    for api_key, name in _SESSION_MAP:
        sess = container.get(api_key)
        if not isinstance(sess, dict):
            continue
        start = _to_epoch(sess.get("startTime"))
        end = _to_epoch(sess.get("endTime"))
        if start is None or end is None:
            continue
        sessions.append({"name": name, "start": start, "end": end})
    return {"market": m, "date": date, "sessions": sessions}


def load_sessions() -> dict:
    """캐시 전체({"KR":{...}, "US":{...}})를 로드. 없거나 깨졌으면 {}."""
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_sessions(cache: dict) -> None:
    """원자적 쓰기(tmp+os.replace). 실패는 무시(폴백이 있으니 데몬을 막지 않는다)."""
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_name(CACHE_PATH.name + ".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CACHE_PATH)
    except OSError as e:
        log.warning("세션 캐시 저장 실패(무시): %s", e)


def _same_kst_day(epoch: float | None) -> bool:
    """epoch(초)가 오늘(KST)과 같은 날짜인지. 하루 1회 갱신 판정용."""
    if not epoch:
        return False
    try:
        return (datetime.fromtimestamp(float(epoch), _KST).date()
                == datetime.now(_KST).date())
    except (ValueError, TypeError, OSError, OverflowError):
        return False


def refresh_sessions(client, markets=("KR", "US")) -> dict:
    """각 시장 세션표를 받아 캐시 갱신 후 전체 캐시를 반환.

    오늘(KST) 이미 받았으면(fetched 가 오늘) 재호출 스킵(하루 1회 원칙 — 어제 것이면
    갱신). 실패한 시장은 로깅 후 스킵하고 다른 시장은 계속한다. 자주 불러도 안전.
    """
    cache = load_sessions()
    changed = False
    for market in markets:
        m = market.upper()
        cached = cache.get(m)
        if isinstance(cached, dict) and _same_kst_day(cached.get("fetched")):
            continue  # 오늘 이미 받음 — 재호출 스킵
        try:
            entry = fetch_sessions(client, m)
        except Exception as e:
            log.warning("%s 세션표 조회 실패(스킵): %s", m, e)
            continue
        entry["fetched"] = time.time()
        cache[m] = entry
        changed = True
        log.info("%s 세션표 갱신 — date=%s, 세션=%s", m, entry.get("date"),
                 [s["name"] for s in entry.get("sessions", [])])
    if changed:
        save_sessions(cache)
    return cache

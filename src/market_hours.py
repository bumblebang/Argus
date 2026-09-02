"""장중 여부 판단 (정규장 기준 + 정적 휴장일 캘린더).

휴장일을 모르면 상주 데몬이 휴장에 폴링·뇌 각성(세션 한도 낭비)을 한다.
정적 캘린더는 연 1회 갱신 필요 — 동적 보강(토스 MarketInfo API)은 후속.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")
# 세션 인지 캐시(datasources.market_calendar.refresh_sessions 가 하루 1회 채움).
_SESSIONS_CACHE = Path("data/market_sessions.json")

_SESSIONS = {
    # market: (tz, open, close)
    "KR": ("Asia/Seoul", time(9, 0), time(15, 30)),
    "US": ("America/New_York", time(9, 30), time(16, 0)),
}

# 휴장일(그 시장 타임존 날짜). 반일장(단축거래)은 정규장 취급.
HOLIDAYS: dict[str, set[str]] = {
    # NYSE 2026 (확정 캘린더)
    "US": {
        "2026-01-01",  # New Year's Day
        "2026-01-19",  # MLK Day
        "2026-02-16",  # Washington's Birthday
        "2026-04-03",  # Good Friday
        "2026-05-25",  # Memorial Day
        "2026-06-19",  # Juneteenth
        "2026-07-03",  # Independence Day(7/4=토) 대체휴장
        "2026-09-07",  # Labor Day
        "2026-11-26",  # Thanksgiving
        "2026-12-25",  # Christmas
    },
    # KRX 2026 (베스트에포트 — 임시공휴일 등 변동 시 갱신)
    "KR": {
        "2026-01-01",              # 신정
        "2026-02-16", "2026-02-17", "2026-02-18",  # 설 연휴
        "2026-03-02",              # 삼일절(3/1=일) 대체
        "2026-05-01",              # 근로자의 날
        "2026-05-05",              # 어린이날
        "2026-05-25",              # 부처님오신날(5/24=일) 대체
        "2026-06-03",              # 전국동시지방선거
        "2026-08-17",              # 광복절(8/15=토) 대체
        "2026-09-24", "2026-09-25",  # 추석 연휴
        "2026-10-05",              # 개천절(10/3=토) 대체
        "2026-10-09",              # 한글날
        "2026-12-25",              # 성탄절
        "2026-12-31",              # KRX 연말 휴장
    },
}


def _local_now(market: str, now: datetime | None) -> datetime:
    tzname = _SESSIONS[market][0]
    return now.astimezone(ZoneInfo(tzname)) if now else datetime.now(ZoneInfo(tzname))


def is_holiday(market: str, now: datetime | None = None) -> bool:
    """그 시장 타임존 기준 오늘이 휴장일인지."""
    if market not in _SESSIONS:
        return False
    local = _local_now(market, now)
    return local.date().isoformat() in HOLIDAYS.get(market, set())


def calendar_covers(market: str, now: datetime | None = None) -> bool:
    """휴장일 캘린더가 '그 시장 기준 올해'를 담고 있는지.

    HOLIDAYS 는 손으로 채우는 정적 캘린더라 연 1회 갱신이 필요하다. 갱신을 놓친 채
    해가 바뀌면 is_open 이 요일만 보게 되어(휴장일 집합이 비어 있으므로) 데몬이 휴장에도
    폴링·뇌 각성을 하고 주문까지 시도한다. 연도 롤오버를 조용히 지나치지 않도록
    호출측(데몬 기동·주기 점검)이 이 함수로 커버리지를 확인해 크게 경고한다.
    """
    if market not in _SESSIONS:
        return False
    year = str(_local_now(market, now).year)
    return any(d.startswith(year) for d in HOLIDAYS.get(market, set()))


def is_open(market: str, now: datetime | None = None) -> bool:
    if market not in _SESSIONS:
        return False
    local = _local_now(market, now)
    if local.weekday() >= 5:  # 토/일
        return False
    if local.date().isoformat() in HOLIDAYS.get(market, set()):
        return False
    open_t, close_t = _SESSIONS[market][1], _SESSIONS[market][2]
    return open_t <= local.time() <= close_t


def within_after_close(market: str, hours: float, now: datetime | None = None) -> bool:
    """장 마감 후 N시간 이내인지 (마감 직후 실적·공시 러시 커버용)."""
    if market not in _SESSIONS:
        return False
    tzname, _open_t, close_t = _SESSIONS[market]
    now = now.astimezone(ZoneInfo(tzname)) if now else datetime.now(ZoneInfo(tzname))
    if now.weekday() >= 5 or now.date().isoformat() in HOLIDAYS.get(market, set()):
        return False
    close_dt = now.replace(hour=close_t.hour, minute=close_t.minute,
                           second=0, microsecond=0)
    delta = (now - close_dt).total_seconds()
    return 0 < delta <= hours * 3600


def market_day(market: str, now: datetime | None = None) -> str:
    """해당 시장 타임존 기준 오늘 날짜(ISO). 일 손실 한도의 '일' 경계."""
    tzname = _SESSIONS[market][0] if market in _SESSIONS else "Asia/Seoul"
    now = now.astimezone(ZoneInfo(tzname)) if now else datetime.now(ZoneInfo(tzname))
    return now.date().isoformat()


def trading_date(market: str, ts: float | None = None) -> str:
    """해당 시장 타임존 기준 거래일(ISO). 세션표 date 필드와 대조한다.

    _SESSIONS 미등록 시장(COMMODITY 등)은 ts 를 UTC 달력일로 — ts 무시하고
    KST '오늘'을 쓰지 않는다.
    """
    ts_val = datetime.now(_KST).timestamp() if ts is None else float(ts)
    if market not in _SESSIONS:
        return datetime.fromtimestamp(ts_val, timezone.utc).date().isoformat()
    tzname = _SESSIONS[market][0]
    return datetime.fromtimestamp(ts_val, ZoneInfo(tzname)).date().isoformat()


def _now_epoch(now: datetime | None) -> float:
    if now is None:
        return datetime.now(_KST).timestamp()
    if now.tzinfo is None:
        return now.replace(tzinfo=_KST).timestamp()
    return now.timestamp()


def last_session_end_ts(market: str, allowed: Iterable[str] | None,
                        ts: float) -> float | None:
    """오늘 캐시에서 허용 세션 중 가장 늦은 end(epoch). 없거나 낡으면 None."""
    sessions = _today_sessions(market, ts)
    if not sessions:
        return None
    names = set(allowed) if allowed is not None else {"regular"}
    ends: list[float] = []
    for s in sessions:
        if not isinstance(s, dict) or s.get("name") not in names:
            continue
        try:
            ends.append(float(s["end"]))
        except (KeyError, TypeError, ValueError):
            continue
    return max(ends) if ends else None


def near_session_end(market: str, minutes: int = 5, now: datetime | None = None,
                     allowed: Iterable[str] | None = None) -> bool:
    """오늘 마지막 허용 세션 종료 N분 전인지(데이트레 종가 청산).

    allowed 가 있으면 세션 캐시에서 그 세션들 중 가장 늦은 end 를 쓴다 — 프리+애프터를
    연 운영은 정규 15:30 이 아니라 애프터 20:00 이 하루의 끝. 캐시가 없거나 낡으면
    정규장 close(KR 15:30) 폴백. allowed=None 은 기존과 같다(정규장 전용).
    """
    ts = _now_epoch(now)
    if allowed is not None:
        end = last_session_end_ts(market, allowed, ts)
        if end is not None:
            return 0 <= (end - ts) <= minutes * 60
    if market not in _SESSIONS:
        return False
    tzname, _open_t, close_t = _SESSIONS[market]
    local = now.astimezone(ZoneInfo(tzname)) if now else datetime.now(ZoneInfo(tzname))
    if local.weekday() >= 5 or local.date().isoformat() in HOLIDAYS.get(market, set()):
        return False
    close_dt = local.replace(hour=close_t.hour, minute=close_t.minute,
                             second=0, microsecond=0)
    delta = (close_dt - local).total_seconds()
    return 0 <= delta <= minutes * 60


# ── 세션 인지(프리마켓/정규장/애프터마켓) ────────────────────────────────
def _cache_valid(market: str, entry: dict, ts: float) -> bool:
    """세션 캐시가 ts 시점에 신뢰 가능한지 — entry.date·세션 구간으로 판정.

    fetched(KST) 날짜만 보던 구 로직은 US 정규장 첫 90분(22:30~00:00 KST)에 캐시를
    버려 미장 폴링이 죽었다. 거래일(date)과 epoch 구간이 맞으면 KST 자정 이후에도 유효.
    """
    if not isinstance(entry, dict):
        return False
    date = entry.get("date")
    if not date:
        return False
    if str(date) == trading_date(market, ts):
        return True
    sessions = entry.get("sessions")
    if isinstance(sessions, list):
        for s in sessions:
            try:
                if float(s["start"]) <= ts < float(s["end"]):
                    return True
            except (KeyError, TypeError, ValueError):
                continue
    return False


def _today_sessions(market: str, ts: float) -> list | None:
    """오늘 캐시의 sessions 리스트. 없거나 거래일이 맞지 않으면 None."""
    try:
        cache = json.loads(_SESSIONS_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = cache.get(market.upper()) if isinstance(cache, dict) else None
    if not isinstance(entry, dict) or not _cache_valid(market, entry, ts):
        return None
    sessions = entry.get("sessions")
    return sessions if isinstance(sessions, list) else None


def _session_from_cache(market: str, ts: float) -> str | None:
    """세션 캐시에서 ts(epoch)가 속한 세션명을 반환. 캐시가 없거나 오늘 것이 아니면
    None(→ 폴백). 신선한 캐시인데 어느 세션에도 안 들면 'closed'."""
    sessions = _today_sessions(market, ts)
    if sessions is None:
        return None
    for s in sessions:
        try:
            start, end = float(s["start"]), float(s["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if start <= ts < end and s.get("name"):
            return str(s["name"])
    return "closed"


def current_session(market: str, now: float | None = None) -> str:
    """현재(또는 now epoch)가 어느 세션인지 반환: premarket/regular/aftermarket/
    daymarket/closed.

    data/market_sessions.json 캐시(epoch 구간)로 판정한다. 캐시가 없거나 거래일(date)이
    맞지 않거나 파싱에 실패하면 기존 is_open 으로 폴백('regular'/'closed'). 대시보드가
    매 요청 호출하므로 절대 예외를 던지지 않는다.
    """
    ts = datetime.now(_KST).timestamp() if now is None else float(now)
    try:
        sess = _session_from_cache(market, ts)
        if sess is not None:
            return sess
    except Exception:  # 캐시 파싱 등 어떤 실패도 폴백으로 흡수
        pass
    dt = datetime.fromtimestamp(ts, _KST)
    return "regular" if is_open(market, dt) else "closed"


def is_tradable(market: str, allowed: Iterable[str] | None = None,
                now: float | None = None) -> bool:
    """지금(또는 now epoch) 그 시장이 '주문을 내도 되는 세션'인지.

    allowed 는 허용 세션명 목록(premarket/regular/aftermarket/daymarket). None(기본)이면
    ("regular",) — 인자를 안 주면 정규장 전용이라 기존 동작(is_open 게이트)과 같다(하위호환).

    안전 실패: 세션 캐시(data/market_sessions.json)가 없거나 낡으면 current_session 이
    is_open 폴백이라 'regular'/'closed' 만 나온다 → 시간외를 허용해 둬도 자동으로 정규장
    전용으로 축소된다(캐시가 죽었는데 시간외 주문이 나가는 일은 없다).
    current_session 이 예외를 던지지 않으므로 이 함수도 던지지 않는다.
    """
    sessions = tuple(allowed) if allowed is not None else ("regular",)
    return current_session(market, now) in sessions

"""세션 인지(프리마켓/정규장/애프터마켓) — market_calendar + market_hours.current_session."""
from datetime import datetime
from zoneinfo import ZoneInfo

import src.market_hours as mh
from src.datasources import market_calendar as mc

KST = ZoneInfo("Asia/Seoul")


def _kst(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=KST).timestamp()


def _iso(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=KST).isoformat()


class _FakeClient:
    """get_market_calendar 호출을 세는 mock. resp[country] 를 반환."""

    def __init__(self, resp):
        self.resp = resp
        self.calls = 0

    def get_market_calendar(self, country, date=None):
        self.calls += 1
        return self.resp[country.upper()]


# ── fetch_sessions: 정규화 + epoch 변환 ────────────────────────────────
def test_fetch_sessions_kr_normalizes_epoch():
    resp = {"KR": {"today": {"date": "2026-07-14", "integrated": {
        "preMarket": {"startTime": _iso(2026, 7, 14, 8), "endTime": _iso(2026, 7, 14, 9)},
        "regularMarket": {"startTime": _iso(2026, 7, 14, 9),
                          "endTime": _iso(2026, 7, 14, 15, 30)},
        "afterMarket": {"startTime": _iso(2026, 7, 14, 15, 30),
                        "endTime": _iso(2026, 7, 14, 20)},
    }}}}
    out = mc.fetch_sessions(_FakeClient(resp), "KR")
    assert out["market"] == "KR" and out["date"] == "2026-07-14"
    names = [s["name"] for s in out["sessions"]]
    assert names == ["premarket", "regular", "aftermarket"]
    pre = out["sessions"][0]
    assert pre["start"] == _kst(2026, 7, 14, 8)
    assert pre["end"] == _kst(2026, 7, 14, 9)


def test_fetch_sessions_us_has_daymarket_and_skips_bad():
    resp = {"US": {"today": {"date": "2026-07-14",
        "dayMarket": {"startTime": _iso(2026, 7, 14, 22), "endTime": _iso(2026, 7, 15, 5)},
        "preMarket": {"startTime": "not-a-date", "endTime": _iso(2026, 7, 14, 22)},  # 스킵
        "regularMarket": None,                                                        # 스킵
        "afterMarket": {"startTime": _iso(2026, 7, 15, 5), "endTime": _iso(2026, 7, 15, 7)},
    }}}
    out = mc.fetch_sessions(_FakeClient(resp), "US")
    names = [s["name"] for s in out["sessions"]]
    assert names == ["daymarket", "aftermarket"]  # premarket(파싱실패)·regular(null) 제외


def test_fetch_sessions_kr_holiday_empty():
    resp = {"KR": {"today": {"date": "2026-07-14", "integrated": None}}}  # 휴장
    out = mc.fetch_sessions(_FakeClient(resp), "KR")
    assert out["sessions"] == []


# ── refresh_sessions: 유효 캐시면 client 미호출 ─────────────
def test_refresh_skips_when_cached_valid(tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "CACHE_PATH", tmp_path / "market_sessions.json")
    today = datetime.now(KST).date()
    y, mo, d = today.year, today.month, today.day
    iso = today.isoformat()
    resp = {"KR": {"today": {"date": iso, "integrated": {
        "regularMarket": {"startTime": _iso(y, mo, d, 9),
                          "endTime": _iso(y, mo, d, 15, 30)}}}}}
    client = _FakeClient(resp)
    mc.refresh_sessions(client, ("KR",))
    assert client.calls == 1
    mc.refresh_sessions(client, ("KR",))
    assert client.calls == 1  # 거래일·세션 유효 → 재호출 스킵


def test_refresh_refetches_when_cache_stale(tmp_path, monkeypatch):
    path = tmp_path / "market_sessions.json"
    monkeypatch.setattr(mc, "CACHE_PATH", path)
    # 어제 fetched 된 캐시를 심어둔다 → 갱신되어야 함
    import json
    yest = datetime.now(KST).timestamp() - 86400
    path.write_text(json.dumps({"KR": {"market": "KR", "date": "2026-07-13",
                                       "sessions": [], "fetched": yest}}), encoding="utf-8")
    resp = {"KR": {"today": {"date": "2026-07-14", "integrated": {
        "regularMarket": {"startTime": _iso(2026, 7, 14, 9),
                          "endTime": _iso(2026, 7, 14, 15, 30)}}}}}
    client = _FakeClient(resp)
    mc.refresh_sessions(client, ("KR",))
    assert client.calls == 1  # 어제 캐시 → 재취득


# ── current_session: 캐시 구간 판정 + 폴백 ─────────────────────────────
def _write_cache(path, fetched):
    import json
    cache = {"KR": {"market": "KR", "date": "2026-07-14", "fetched": fetched, "sessions": [
        {"name": "premarket", "start": _kst(2026, 7, 14, 8), "end": _kst(2026, 7, 14, 9)},
        {"name": "regular", "start": _kst(2026, 7, 14, 9), "end": _kst(2026, 7, 14, 15, 30)},
        {"name": "aftermarket", "start": _kst(2026, 7, 14, 15, 30),
         "end": _kst(2026, 7, 14, 20)},
    ]}}
    path.write_text(json.dumps(cache), encoding="utf-8")


def test_current_session_from_cache(tmp_path, monkeypatch):
    path = tmp_path / "market_sessions.json"
    _write_cache(path, fetched=_kst(2026, 7, 14, 7))  # 당일 07:00 취득
    monkeypatch.setattr(mh, "_SESSIONS_CACHE", path)
    assert mh.current_session("KR", _kst(2026, 7, 14, 8, 30)) == "premarket"
    assert mh.current_session("KR", _kst(2026, 7, 14, 12)) == "regular"
    assert mh.current_session("KR", _kst(2026, 7, 14, 17)) == "aftermarket"
    # 신선한 캐시인데 세션 밖(21:00, 07:30) → closed (폴백 아님)
    assert mh.current_session("KR", _kst(2026, 7, 14, 21)) == "closed"
    assert mh.current_session("KR", _kst(2026, 7, 14, 7, 30)) == "closed"


def test_current_session_end_exclusive(tmp_path, monkeypatch):
    path = tmp_path / "market_sessions.json"
    _write_cache(path, fetched=_kst(2026, 7, 14, 7))
    monkeypatch.setattr(mh, "_SESSIONS_CACHE", path)
    # 경계: 09:00 은 regular 시작(정규장), premarket 끝은 배제
    assert mh.current_session("KR", _kst(2026, 7, 14, 9)) == "regular"


def test_current_session_missing_cache_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(mh, "_SESSIONS_CACHE", tmp_path / "nope.json")
    # 2026-07-14 은 화요일·비휴장. 정규장 시각 → is_open True → 'regular'
    assert mh.current_session("KR", _kst(2026, 7, 14, 11)) == "regular"
    # 장 마감 후(18:00) → is_open False → 'closed'
    assert mh.current_session("KR", _kst(2026, 7, 14, 18)) == "closed"


def test_current_session_wrong_date_falls_back(tmp_path, monkeypatch):
    path = tmp_path / "market_sessions.json"
    # date 가 거래일과 다르면 캐시 무시 → is_open 폴백
    import json
    cache = {"KR": {"market": "KR", "date": "2026-07-13", "fetched": _kst(2026, 7, 14, 7),
                    "sessions": [
                        {"name": "aftermarket", "start": _kst(2026, 7, 13, 15, 30),
                         "end": _kst(2026, 7, 13, 20)},
                    ]}}
    path.write_text(json.dumps(cache), encoding="utf-8")
    monkeypatch.setattr(mh, "_SESSIONS_CACHE", path)
    assert mh.current_session("KR", _kst(2026, 7, 14, 17)) == "closed"
    assert mh.current_session("KR", _kst(2026, 7, 14, 11)) == "regular"


def test_current_session_us_survives_kst_midnight(tmp_path, monkeypatch):
    """US 정규장 첫 90분(22:30~00:00 KST) — KST 자정 넘어도 세션표 date 로 유효."""
    path = tmp_path / "market_sessions.json"
    import json
    cache = {"US": {"market": "US", "date": "2026-07-14", "fetched": _kst(2026, 7, 14, 9),
                    "sessions": [
                        {"name": "regular", "start": _kst(2026, 7, 14, 22, 30),
                         "end": _kst(2026, 7, 15, 5, 0)},
                    ]}}
    path.write_text(json.dumps(cache), encoding="utf-8")
    monkeypatch.setattr(mh, "_SESSIONS_CACHE", path)
    ts = _kst(2026, 7, 15, 0, 30)  # KST 다음날 00:30, US 7/14 정규장 중
    assert mh.current_session("US", ts) == "regular"
    assert mh.is_tradable("US", ("regular",), ts) is True

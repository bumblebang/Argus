"""실적 캘린더 — 네이버/Finnhub 정규화 + 공시 워처 컨센서스 첨부·임박 주기(네트워크 없음)."""
from __future__ import annotations

import json
from datetime import date, timedelta

import src.datasources.earnings as em
import src.engine.disclosure as dmod
from src.engine.disclosure import DisclosureWatcher
from src.engine.store import Store


class _Resp:
    def __init__(self, data, ok=True):
        self._data = data
        self._ok = ok

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError("HTTP 500")

    def json(self):
        return self._data


def _integration(sched=None):
    return {"irScheduleInfo": sched,
            "consensusInfo": {"createDate": "20260710", "recommMean": "4.04",
                              "priceTargetMean": "513,958"}}


_SCHED = {"itemCode": "005930", "stockName": "삼성전자",
          "title": "2026년 2분기 경영실적 발표", "irScheduleDate": "2026-07-30",
          "irScheduleDday": 9}


def _quarter(consensus_revenue="1,038,644", hist=("791,405", "745,663", "860,617", "938,374")):
    keys = ["202509", "202512", "202603", "202606"][:len(hist)]
    cols_rev = {k: {"value": v} for k, v in zip(keys, hist)}
    cols_rev["202609"] = {"value": consensus_revenue}
    return {"financeInfo": {
        "trTitleList": ([{"isConsensus": "N", "title": k, "key": k} for k in keys]
                        + [{"isConsensus": "Y", "title": "2026.09.", "key": "202609"}]),
        "rowList": [
            {"title": "매출액", "columns": cols_rev},
            {"title": "영업이익", "columns": {"202609": {"value": "134,258"}}},
            {"title": "당기순이익", "columns": {"202609": {"value": "-"}}},   # 결측
            {"title": "EPS", "columns": {"202609": {"value": "2,410"}}},
        ]}}


def _naver_router(monkeypatch, integ, quarter):
    """URL 로 두 네이버 엔드포인트를 갈라 응답하는 가짜 requests.get."""
    def fake_get(url, *a, **k):
        return _Resp(quarter if "finance/quarter" in url else integ)
    monkeypatch.setattr(em.requests, "get", fake_get)


# ── KR 정규화 ─────────────────────────────────────────────────────
class TestFetchKrEarnings:
    def test_일정_컨센서스_정규화(self, monkeypatch):
        _naver_router(monkeypatch, _integration(_SCHED), _quarter())
        out = em.fetch_kr_earnings("005930")
        assert out["market"] == "KR" and out["symbol"] == "005930"
        assert out["date"] == "2026-07-30" and out["dday"] == 9
        assert out["title"] == "2026년 2분기 경영실적 발표" and out["hour"] is None
        c = out["consensus"]
        assert c["period"] == "202609" and c["unit"] == "억원" and c["suspect"] is False
        assert c["revenue"] == 1038644.0            # 콤마 제거
        assert c["op_profit"] == 134258.0 and c["eps"] == 2410.0
        assert c["net_income"] is None              # "-" → 결측
        assert out["analyst"] == {"recomm_mean": 4.04, "target_price": 513958.0}
        assert out["surprise_history"] == []

    def test_일정_null이어도_dict_반환(self, monkeypatch):
        _naver_router(monkeypatch, _integration(None), _quarter())
        out = em.fetch_kr_earnings("214320")
        assert out is not None
        assert out["date"] is None and out["dday"] is None and out["title"] is None
        assert out["consensus"]["revenue"] == 1038644.0     # 컨센서스만으로도 유효

    def test_컨센서스_컬럼_없으면_None(self, monkeypatch):
        q = {"financeInfo": {"trTitleList": [{"isConsensus": "N", "title": "2026.03.",
                                              "key": "202603"}], "rowList": []}}
        _naver_router(monkeypatch, _integration(_SCHED), q)
        out = em.fetch_kr_earnings("005930")
        assert out["consensus"] is None and out["date"] == "2026-07-30"

    def test_컨센서스_값이_전부_결측이면_None(self, monkeypatch):
        # 컬럼(isConsensus=Y)은 있는데 값이 전부 "-" → 있는 척하지 않는다
        q = _quarter()
        for row in q["financeInfo"]["rowList"]:
            row["columns"]["202609"] = {"value": "-"}
        _naver_router(monkeypatch, _integration(_SCHED), q)
        assert em.fetch_kr_earnings("194700")["consensus"] is None

    def test_매출_중앙값_2배초과면_suspect(self, monkeypatch, caplog):
        # 직전 4분기 중앙값 826,011 → 그 2배(1,652,022) 초과
        _naver_router(monkeypatch, _integration(_SCHED), _quarter("1,738,644"))
        out = em.fetch_kr_earnings("005930")
        assert out["consensus"]["suspect"] is True
        assert out["consensus"]["revenue"] == 1738644.0     # 값은 버리지 않는다

    def test_조회실패시_None(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("net down")
        monkeypatch.setattr(em.requests, "get", boom)
        assert em.fetch_kr_earnings("005930") is None


# ── US 정규화 ─────────────────────────────────────────────────────
def _cal(rows):
    return _Resp({"earningsCalendar": rows})


class TestFetchUsEarnings:
    def test_심볼별_오늘이후_가장가까운날짜(self, monkeypatch):
        today = date.today()
        d = lambda n: (today + timedelta(days=n)).isoformat()      # noqa: E731
        rows = [
            {"symbol": "AAPL", "date": d(-2), "hour": "amc", "quarter": 2, "year": 2026,
             "epsEstimate": 1.5, "revenueEstimate": 1e11},
            {"symbol": "AAPL", "date": d(9), "hour": "amc", "quarter": 3, "year": 2026,
             "epsEstimate": 1.9299, "revenueEstimate": 110740860032},
            {"symbol": "AAPL", "date": d(100), "hour": "", "quarter": 4, "year": 2026,
             "epsEstimate": 2.1, "revenueEstimate": None},
            {"symbol": "NVDA", "date": d(3), "hour": "", "quarter": 2, "year": 2027,
             "epsEstimate": None, "revenueEstimate": None},
            {"symbol": "ZZZZ", "date": d(1), "hour": "bmo", "quarter": 1, "year": 2026,
             "epsEstimate": 1.0, "revenueEstimate": 1.0},          # 유니버스 밖 → 제외
        ]
        monkeypatch.setattr(em.requests, "get", lambda *a, **k: _cal(rows))
        out = em.fetch_us_earnings("KEY", ["AAPL", "NVDA"], spacing_sec=0)
        assert set(out) == {"AAPL", "NVDA"}
        a = out["AAPL"]
        assert a["date"] == d(9) and a["dday"] == 9 and a["hour"] == "amc"
        assert a["market"] == "US" and a["consensus"]["eps"] == 1.9299
        assert a["consensus"]["unit"] == "USD" and a["consensus"]["suspect"] is False
        assert out["NVDA"]["hour"] is None            # "" → None(미정)
        assert out["NVDA"]["consensus"] is None       # 추정치 전무 → None

    def test_미래없으면_가장최근_과거(self, monkeypatch):
        today = date.today()
        rows = [{"symbol": "F", "date": (today - timedelta(days=30)).isoformat(),
                 "hour": "bmo", "quarter": 1, "year": 2026, "epsEstimate": 0.4},
                {"symbol": "F", "date": (today - timedelta(days=2)).isoformat(),
                 "hour": "bmo", "quarter": 2, "year": 2026, "epsEstimate": 0.5}]
        monkeypatch.setattr(em.requests, "get", lambda *a, **k: _cal(rows))
        out = em.fetch_us_earnings("KEY", ["F"], days_ahead=7, spacing_sec=0)
        assert out["F"]["dday"] == -2

    def test_응답상한이면_창분할_재조회(self, monkeypatch):
        """1500행 상한에 닿으면 앞 날짜가 잘리므로 창을 쪼개 다시 부른다."""
        today = date.today()
        target = {"symbol": "AAPL", "date": (today + timedelta(days=2)).isoformat(),
                  "hour": "amc", "quarter": 3, "year": 2026, "epsEstimate": 1.9}
        calls = []

        def fake_get(url, *a, params=None, **k):
            span = (date.fromisoformat(params["to"])
                    - date.fromisoformat(params["from"])).days
            calls.append(span)
            if span > 3:                       # 넓은 창 = 상한(앞 날짜 잘림)
                return _cal([{"symbol": "ZZ", "date": params["to"], "hour": ""}] * 1500)
            return _cal([target])
        monkeypatch.setattr(em.requests, "get", fake_get)
        out = em.fetch_us_earnings("KEY", ["AAPL"], days_ahead=7, spacing_sec=0)
        assert out["AAPL"]["dday"] == 2 and len(calls) > 1

    def test_키없거나_실패시_빈dict(self, monkeypatch):
        assert em.fetch_us_earnings("", ["AAPL"]) == {}

        def boom(*a, **k):
            raise RuntimeError("net down")
        monkeypatch.setattr(em.requests, "get", boom)
        assert em.fetch_us_earnings("KEY", ["AAPL"], days_ahead=7, spacing_sec=0) == {}


class TestFetchUsSurpriseHistory:
    def test_최근n개_정규화(self, monkeypatch):
        data = [{"period": "2026-03-31", "estimate": 1.5, "actual": 1.6,
                 "surprise": 0.1, "surprisePercent": 6.67},
                {"period": "2025-12-31", "estimate": 2.3, "actual": 2.4,
                 "surprisePercent": 4.3}] * 3
        monkeypatch.setattr(em.requests, "get", lambda *a, **k: _Resp(data))
        out = em.fetch_us_surprise_history("KEY", "AAPL", n=4)
        assert len(out) == 4
        assert out[0] == {"period": "2026-03-31", "estimate": 1.5, "actual": 1.6,
                          "surprise_pct": 6.67}

    def test_예외시_빈리스트(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("net down")
        monkeypatch.setattr(em.requests, "get", boom)
        assert em.fetch_us_surprise_history("KEY", "AAPL") == []


# ── 공시 워처 연동 ─────────────────────────────────────────────────
def _filing(rcept, sym="005930", name="연결재무제표기준영업(잠정)실적(공정공시)"):
    return {"rcept_no": rcept, "stock_code": sym, "corp_name": "삼성전자",
            "report_nm": name, "rcept_dt": "20260730"}


_CONSENSUS = {"period": "202606", "revenue": 1038644.0, "op_profit": 134258.0,
              "net_income": None, "eps": 2410.0, "unit": "억원", "suspect": False}


def _watcher(store, filings, **kw):
    return DisclosureWatcher(store, lambda: list(filings),
                             lambda: {"005930"}, **kw)


def test_실적공시에_컨센서스_첨부(tmp_path):
    store = Store(tmp_path / "t.db")
    store.open_position("005930", "KR", 10, 70000)
    woke, filings = [], []
    w = _watcher(store, filings, on_wake=lambda why, p: woke.append(p),
                 earnings_fn=lambda s: {"consensus": _CONSENSUS})
    w.poll_once()                                        # 프라이밍
    filings.append(_filing("R1"))
    assert w.poll_once()["woke"] == ["005930"]
    assert woke[0][0]["consensus"] == _CONSENSUS         # 각성 payload 에 기대치가 실린다
    ev = store.recent_events("disclosure", 0)[0]
    assert json.loads(ev["payload"])["consensus"]["revenue"] == 1038644.0


def test_비실적_공시엔_컨센서스_미첨부(tmp_path):
    store = Store(tmp_path / "t.db")
    store.open_position("005930", "KR", 10, 70000)
    woke, filings = [], []
    w = _watcher(store, filings, on_wake=lambda why, p: woke.append(p),
                 earnings_fn=lambda s: {"consensus": _CONSENSUS})
    w.poll_once()
    filings.append(_filing("R1", name="주요사항보고서(유상증자결정)"))
    w.poll_once()
    assert woke and "consensus" not in woke[0][0]


def test_earnings_fn_예외여도_각성_진행(tmp_path):
    store = Store(tmp_path / "t.db")
    store.open_position("005930", "KR", 10, 70000)
    woke, filings = [], []

    def boom(_sym):
        raise RuntimeError("캘린더 깨짐")

    w = _watcher(store, filings, on_wake=lambda why, p: woke.append(p), earnings_fn=boom)
    w.poll_once()
    filings.append(_filing("R1"))
    assert w.poll_once()["woke"] == ["005930"]
    assert woke[0][0]["report_nm"] and "consensus" not in woke[0][0]


def test_earnings_fn_없으면_기존동작(tmp_path):
    store = Store(tmp_path / "t.db")
    store.open_position("005930", "KR", 10, 70000)
    woke, filings = [], []
    w = _watcher(store, filings, on_wake=lambda why, p: woke.append(p))
    w.poll_once()
    filings.append(_filing("R1"))
    assert w.poll_once()["woke"] == ["005930"] and "consensus" not in woke[0][0]


# ── 임박 시 폴링 창 ────────────────────────────────────────────────
def test_interval_임박이면_장외에도_active(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    monkeypatch.setattr(dmod, "market_monitoring_active",
                        lambda market, **kw: False)

    w = _watcher(store, [], imminent_fn=lambda: True)
    assert w.interval() == 10.0                          # 임박 → 장외에도 촘촘히
    w2 = _watcher(store, [], imminent_fn=lambda: False)
    assert w2.interval() == 60.0                         # 평시 장외 = idle(기존 동작)


def test_interval_임박이면_마감후창_확장(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    seen = []
    monkeypatch.setattr(dmod, "market_monitoring_active",
                        lambda market, **kw: seen.append(kw.get("after_close_hours")) or False)
    _watcher(store, [], imminent_fn=lambda: True, imminent_after_close_hours=4.0).interval()
    _watcher(store, [], imminent_fn=lambda: False).interval()
    assert seen == [4.0, 2.0]                            # 임박일만 4시간 창


def test_earnings_near_창(tmp_path):
    """뇌 컨텍스트 부착 창: 발표 3일 후 ~ 3주 전. 게이트가 아니라 소음 필터."""
    from src.agents.pipeline import earnings_near
    assert earnings_near({"dday": 0}) and earnings_near({"dday": 21})
    assert earnings_near({"dday": -3})
    assert not earnings_near({"dday": -4}) and not earnings_near({"dday": 22})
    assert not earnings_near({"dday": None}) and not earnings_near(None)


def test_dday_는_저장값이_아니라_오늘기준_재계산(tmp_path):
    """캘린더는 장전 배치 산출물 — 배치가 밀리면 저장된 dday 가 낡는다.

    date 가 있으면 저장값을 무시하고 오늘 기준으로 다시 계산해야 한다. 안 그러면
    임박 판정이 정작 D-1 에 안 켜지거나, 지나간 D-1 로 계속 켜져 쿼터를 태운다.
    """
    from src.agents.pipeline import earnings_near
    내일 = (date.today() + timedelta(days=1)).isoformat()
    낡음 = {"market": "KR", "date": 내일, "dday": 9}      # 8일 전 배치가 남긴 값
    assert em.dday_of(낡음) == 1                          # 저장값 9 가 아니라 1
    assert em.with_fresh_dday(낡음)["dday"] == 1
    assert 낡음["dday"] == 9                              # 원본(캐시)은 불변

    지남 = {"market": "KR", "date": (date.today() - timedelta(days=30)).isoformat(),
            "dday": 1}                                    # 한 달 전 D-1 로 굳은 값
    assert em.dday_of(지남) == -30
    assert not earnings_near(지남)                        # 창 밖 → 뇌에 안 실림

    # date 가 없으면(일정 미공지) 저장값 폴백, 그것도 없으면 None
    assert em.dday_of({"dday": 3}) == 3
    assert em.dday_of({"date": None, "dday": None}) is None
    assert em.with_fresh_dday(None) is None


def test_interval_imminent_fn_예외는_삼킴(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    monkeypatch.setattr(dmod, "market_monitoring_active",
                        lambda market, **kw: False)

    def boom():
        raise RuntimeError("캘린더 깨짐")

    assert _watcher(store, [], imminent_fn=boom).interval() == 60.0

"""동적 유니버스 — Naver 발굴(거래대금 정렬/필터/전일 캐시) + config universe.yaml 해석."""
import json
import time

import pytest
import yaml

import src.datasources.discovery as D
from src.datasources.discovery import (top_by_trading_value, fetch_ranking,
                                       top_us_by_trading_value, fetch_us_actives,
                                       fetch_us_value_pool)
from src.config import resolve_universe


@pytest.fixture(autouse=True)
def _redirect_ranking_cache(tmp_path, monkeypatch):
    """랭킹 캐시를 tmp 로 — 실 data/ranking_cache.json 오염 방지(모든 테스트에 적용)."""
    monkeypatch.setattr(D, "_RANKING_CACHE", tmp_path / "ranking_cache.json")
    return tmp_path / "ranking_cache.json"


def _fake_fetch(naver_market, pool=100):
    rows = {
        "KOSPI": [
            {"symbol": "005930", "name": "삼성", "price": 339500, "trading_value": 11000, "fluctuation": -5},
            {"symbol": "000660", "name": "하이닉스", "price": 500000, "trading_value": 19000, "fluctuation": 1},
            {"symbol": "001", "name": "싼주식", "price": 1000, "trading_value": 50000, "fluctuation": 3},
        ],
        "KOSDAQ": [
            {"symbol": "247540", "name": "에코프로비엠", "price": 100000, "trading_value": 8000, "fluctuation": 2},
        ],
    }
    return rows.get(naver_market, [])


def test_top_by_trading_value_sorts_and_filters_min_price():
    out = top_by_trading_value("KR", count=3, min_price=2000, fetch_fn=_fake_fetch)
    syms = [r["symbol"] for r in out]
    # 001(거래대금 50000)은 가격 1000<2000 제외 → 거래대금순 하이닉스>삼성>에코프로
    assert syms == ["000660", "005930", "247540"]


def test_top_count_limit_and_no_filter():
    out = top_by_trading_value("KR", count=1, min_price=0, fetch_fn=_fake_fetch)
    assert len(out) == 1 and out[0]["symbol"] == "001"   # 거래대금 1위


def test_fetch_ranking_parses(monkeypatch):
    payload = {"stocks": [{"itemCode": "005930", "stockName": "삼성",
                           "closePriceRaw": "339500", "accumulatedTradingValueRaw": "11000",
                           "fluctuationsRatio": "-5.3",
                           "marketValueRaw": "1666189403280000"}]}

    class R:
        text = '{"stocks":[]}'

        def json(self):
            return payload

    monkeypatch.setattr(D.requests, "get", lambda *a, **k: R())
    rows = fetch_ranking("KOSPI", pool=5)
    assert rows[0]["symbol"] == "005930"
    assert rows[0]["trading_value"] == 11000.0 and rows[0]["price"] == 339500.0
    assert rows[0]["market_cap"] == 1666189403280000.0


# ── fetch_ranking 페이지네이션(pageSize 100 상한 → page 순회) ─────────────
def _page_stock(code):
    return {"itemCode": code, "stockName": f"N{code}", "closePriceRaw": "1000",
            "accumulatedTradingValueRaw": "500", "fluctuationsRatio": "1.0"}


def _make_paged_get(pages, calls):
    """page 파라미터로 가짜 페이지 응답을 돌려주는 requests.get 대체(호출 params 기록)."""
    def get(*a, **k):
        params = k.get("params", {})
        calls.append(params)
        stocks = pages.get(params.get("page"), [])

        class R:
            text = '{"stocks":[]}'

            def json(self):
                return {"stocks": stocks}
        return R()
    return get


def test_fetch_ranking_paginates_multiple_pages(monkeypatch):
    # page 1/2/3 각 100 고유종목 → pool=250이면 250개(고유) 수집.
    pages = {p: [_page_stock(f"{p:02d}{i:03d}") for i in range(100)] for p in (1, 2, 3)}
    calls = []
    monkeypatch.setattr(D.requests, "get", _make_paged_get(pages, calls))
    rows = fetch_ranking("KOSPI", pool=250)
    assert len(rows) == 250
    assert len({r["symbol"] for r in rows}) == 250      # 전부 고유
    assert calls[0]["pageSize"] == 100                  # pageSize 항상 100 고정


def test_fetch_ranking_stops_on_empty_page(monkeypatch):
    # page 2가 빈 리스트 → page 1의 100개만 반환하고 즉시 중단(무한루프 안 됨).
    pages = {1: [_page_stock(f"01{i:03d}") for i in range(100)], 2: []}
    calls = []
    monkeypatch.setattr(D.requests, "get", _make_paged_get(pages, calls))
    rows = fetch_ranking("KOSPI", pool=500)
    assert len(rows) == 100
    assert [p["page"] for p in calls] == [1, 2]         # page 2에서 멈춤


def test_fetch_ranking_backcompat_single_page(monkeypatch):
    # pool=100이면 page 1만 조회(요청 1회) — 기존 호출자 하위호환.
    pages = {1: [_page_stock(f"01{i:03d}") for i in range(100)],
             2: [_page_stock(f"02{i:03d}") for i in range(100)]}
    calls = []
    monkeypatch.setattr(D.requests, "get", _make_paged_get(pages, calls))
    rows = fetch_ranking("KOSPI", pool=100)
    assert len(rows) == 100
    assert len(calls) == 1 and calls[0]["page"] == 1


def test_fetch_ranking_dedupes_across_pages(monkeypatch):
    # page 1/2에 겹치는 itemCode(01050)가 있어도 결과엔 한 번만.
    pages = {1: [_page_stock(f"01{i:03d}") for i in range(100)],
             2: [_page_stock("01050")] + [_page_stock(f"02{i:03d}") for i in range(99)]}
    calls = []
    monkeypatch.setattr(D.requests, "get", _make_paged_get(pages, calls))
    rows = fetch_ranking("KOSPI", pool=250)
    syms = [r["symbol"] for r in rows]
    assert syms.count("01050") == 1
    assert len(syms) == len(set(syms)) == 199           # 200 - 중복 1


def test_fetch_ranking_dict_fields(monkeypatch):
    pages = {1: [_page_stock("005930")]}
    monkeypatch.setattr(D.requests, "get", _make_paged_get(pages, []))
    row = fetch_ranking("KOSPI", pool=100)[0]
    assert set(row) == {"symbol", "name", "price", "trading_value", "fluctuation",
                        "market_cap"}


# ── 전일 랭킹 캐시(개장 전 누적 거래대금=0 대응) ─────────────────────────
def _fake_fetch_preopen(naver_market, pool=100):
    """새 거래일 개장 전 상태 — 시총 순 풀은 오지만 누적 거래대금 전부 0."""
    rows = {
        "KOSPI": [
            {"symbol": "005930", "name": "삼성", "price": 339500, "trading_value": 0, "fluctuation": 0},
            {"symbol": "000660", "name": "하이닉스", "price": 500000, "trading_value": 0, "fluctuation": 0},
        ],
    }
    return rows.get(naver_market, [])


def test_healthy_ranking_saves_cache(_redirect_ranking_cache):
    top_by_trading_value("KR", count=2, min_price=0, fetch_fn=_fake_fetch)
    data = json.loads(_redirect_ranking_cache.read_text(encoding="utf-8"))
    assert [r["symbol"] for r in data["KR"]["rows"]][:2] == ["001", "000660"]  # 거래대금순


def test_preopen_uses_yesterday_cache_order(_redirect_ranking_cache):
    # 어제(건강한 랭킹) → 캐시 축적 → 오늘 개장 전(전부 0) → 캐시의 전일 거래대금 순 사용.
    top_by_trading_value("KR", count=3, min_price=2000, fetch_fn=_fake_fetch)   # 캐시 축적
    out = top_by_trading_value("KR", count=3, min_price=2000,
                               fetch_fn=_fake_fetch_preopen)
    syms = [r["symbol"] for r in out]
    assert syms[:2] == ["000660", "005930"], "전일 거래대금 순(하이닉스>삼성)이어야 함"


def test_preopen_stale_cache_falls_back_to_pool_order(_redirect_ranking_cache, monkeypatch):
    top_by_trading_value("KR", count=3, min_price=2000, fetch_fn=_fake_fetch)   # 캐시 축적
    # 캐시를 TTL 초과로 낡게 조작 → 시총 순(풀 원순서) 최후 폴백.
    data = json.loads(_redirect_ranking_cache.read_text(encoding="utf-8"))
    data["KR"]["ts"] = time.time() - 97 * 3600
    _redirect_ranking_cache.write_text(json.dumps(data), encoding="utf-8")
    out = top_by_trading_value("KR", count=2, min_price=2000,
                               fetch_fn=_fake_fetch_preopen)
    assert [r["symbol"] for r in out] == ["005930", "000660"]   # 풀(시총) 원순서


def test_no_cache_fallback_when_disabled(_redirect_ranking_cache):
    top_by_trading_value("KR", count=3, min_price=2000, fetch_fn=_fake_fetch)
    out = top_by_trading_value("KR", count=3, min_price=2000,
                               fetch_fn=_fake_fetch_preopen,
                               allow_cache_fallback=False)
    assert out == []


# ── US 발굴(Yahoo most_actives → 거래대금 정렬) ──────────────
def _fake_us_actives(pool=50):
    return [
        {"symbol": "AAPL", "name": "Apple", "price": 283.78,
         "trading_value": 283.78 * 261775450, "fluctuation": 1.2},
        {"symbol": "OPEN", "name": "Opendoor", "price": 4.37,
         "trading_value": 4.37 * 171652413, "fluctuation": 5.0},
        {"symbol": "NVDA", "name": "Nvidia", "price": 192.53,
         "trading_value": 192.53 * 179304147, "fluctuation": -2.0},
    ]


def test_top_us_by_trading_value_sorts_and_filters():
    out = top_us_by_trading_value(count=5, min_price=5.0, fetch_fn=_fake_us_actives)
    syms = [r["symbol"] for r in out]
    assert "OPEN" not in syms                          # $4.37 < min_price 5 제외
    assert syms == ["AAPL", "NVDA"]                    # 거래대금순(AAPL>NVDA)


def test_fetch_us_actives_parses(monkeypatch):
    payload = {"finance": {"result": [{"quotes": [
        {"symbol": "AAPL", "shortName": "Apple Inc.", "regularMarketPrice": 283.78,
         "regularMarketVolume": 261775450, "regularMarketChangePercent": 1.2}]}]}}

    class R:
        text = '{"finance":{}}'

        def json(self):
            return payload

    monkeypatch.setattr(D.requests, "get", lambda *a, **k: R())
    rows = fetch_us_actives(pool=5)
    assert rows[0]["symbol"] == "AAPL" and rows[0]["price"] == 283.78
    assert rows[0]["trading_value"] == 283.78 * 261775450


# ── US 저평가 발굴(Yahoo 저평가 스크리너 3종 합집합) ──────────────
def _us_screener_quotes():
    """scrId 별 가짜 quotes. undervalued_large_caps 와 day_losers 가 GE 를 겹쳐 반환.

    전부 quoteType="EQUITY"(개별주) — ETF 제외 로직은 별도 테스트에서 검증.
    """
    return {
        "undervalued_large_caps": [
            {"symbol": "INTC", "shortName": "Intel", "regularMarketPrice": 22.5,
             "regularMarketVolume": 40_000_000, "regularMarketChangePercent": -1.5,
             "quoteType": "EQUITY", "trailingPE": 12.345, "forwardPE": 10.5,
             "priceToBook": 1.234, "epsTrailingTwelveMonths": 1.82,
             "marketCap": 95_500_000_000},
            {"symbol": "GE", "shortName": "GE Aero", "regularMarketPrice": 190.0,
             "regularMarketVolume": 5_000_000, "regularMarketChangePercent": -2.0,
             "quoteType": "EQUITY"},
        ],
        "undervalued_growth_stocks": [
            {"symbol": "PYPL", "shortName": "PayPal", "regularMarketPrice": 68.0,
             "regularMarketVolume": 12_000_000, "regularMarketChangePercent": -0.5,
             "quoteType": "EQUITY"},
        ],
        "day_losers": [
            {"symbol": "GE", "shortName": "GE Aero", "regularMarketPrice": 190.0,
             "regularMarketVolume": 5_000_000, "regularMarketChangePercent": -2.0,
             "quoteType": "EQUITY"},
            {"symbol": "F", "shortName": "Ford", "regularMarketPrice": 11.0,
             "regularMarketVolume": 60_000_000, "regularMarketChangePercent": -6.0,
             "quoteType": "EQUITY"},
        ],
    }


def _make_screener_get(quotes_by_scr, calls=None):
    """scrIds 파라미터로 가짜 스크리너 응답을 돌려주는 requests.get 대체."""
    def get(*a, **k):
        scr = k.get("params", {}).get("scrIds")
        if calls is not None:
            calls.append(scr)
        qs = quotes_by_scr.get(scr, [])

        class R:
            text = '{"finance":{}}'

            def json(self):
                return {"finance": {"result": [{"quotes": qs}]}}
        return R()
    return get


def test_fetch_us_value_pool_union_and_dedupe(monkeypatch):
    calls = []
    monkeypatch.setattr(D.requests, "get",
                        _make_screener_get(_us_screener_quotes(), calls))
    rows = fetch_us_value_pool(pool=400)
    syms = [r["symbol"] for r in rows]
    # 3종 합집합 = INTC, GE, PYPL, F (GE 는 두 소스에 있어도 1회만).
    assert syms == ["INTC", "GE", "PYPL", "F"]
    assert syms.count("GE") == 1
    assert calls == ["undervalued_large_caps", "undervalued_growth_stocks", "day_losers"]


def test_fetch_us_value_pool_dict_fields(monkeypatch):
    monkeypatch.setattr(D.requests, "get",
                        _make_screener_get(_us_screener_quotes()))
    row = next(r for r in fetch_us_value_pool(pool=400) if r["symbol"] == "INTC")
    assert set(row) == {"symbol", "name", "price", "trading_value", "fluctuation",
                        "fundamentals"}
    assert row["name"] == "Intel" and row["price"] == 22.5
    assert row["trading_value"] == 22.5 * 40_000_000 and row["fluctuation"] == -1.5


def test_fetch_us_value_pool_fundamentals_parsed(monkeypatch):
    # INTC 는 밸류 필드 완비 → 숫자 파싱(소수 2자리 반올림) + 시총 십억달러 환산.
    # GE 는 밸류 필드 전무 → 5개 키 모두 None(적자/결측도 정보라 생략 말고 None).
    monkeypatch.setattr(D.requests, "get",
                        _make_screener_get(_us_screener_quotes()))
    rows = fetch_us_value_pool(pool=400)
    by = {r["symbol"]: r for r in rows}
    intc = by["INTC"]["fundamentals"]
    assert set(intc) == {"pe_trailing", "pe_forward", "pb", "eps_ttm",
                         "market_cap_busd"}
    assert intc == {"pe_trailing": 12.35, "pe_forward": 10.5, "pb": 1.23,
                    "eps_ttm": 1.82, "market_cap_busd": 95.5}
    ge = by["GE"]["fundamentals"]
    assert ge == {"pe_trailing": None, "pe_forward": None, "pb": None,
                  "eps_ttm": None, "market_cap_busd": None}


def test_fetch_us_value_pool_fundamentals_partial(monkeypatch):
    # 일부 필드만 있는 경우: 있는 것은 파싱, 없는 것(marketCap/forwardPE)은 None.
    quotes = {"undervalued_large_caps": [
        {"symbol": "XYZ", "shortName": "Xyz", "regularMarketPrice": 10.0,
         "regularMarketVolume": 1_000_000, "regularMarketChangePercent": -3.0,
         "quoteType": "EQUITY", "trailingPE": 8.0, "priceToBook": 0.9,
         "epsTrailingTwelveMonths": -0.45}]}
    monkeypatch.setattr(D.requests, "get", _make_screener_get(quotes))
    f = next(r for r in fetch_us_value_pool(pool=400))["fundamentals"]
    assert f == {"pe_trailing": 8.0, "pe_forward": None, "pb": 0.9,
                 "eps_ttm": -0.45, "market_cap_busd": None}


def test_fetch_us_value_pool_caps_at_pool(monkeypatch):
    monkeypatch.setattr(D.requests, "get",
                        _make_screener_get(_us_screener_quotes()))
    rows = fetch_us_value_pool(pool=2)      # 첫 소스 2개(INTC, GE)에서 컷
    assert [r["symbol"] for r in rows] == ["INTC", "GE"]


def test_fetch_us_value_pool_skips_failed_scrid(monkeypatch):
    # 한 scrId 가 비-JSON(HTML 등)이어도 그 소스만 건너뛰고 나머지는 수집.
    quotes = _us_screener_quotes()

    def get(*a, **k):
        scr = k.get("params", {}).get("scrIds")

        class R:
            text = "<html>error</html>" if scr == "undervalued_large_caps" else '{"finance":{}}'

            def json(self):
                return {"finance": {"result": [{"quotes": quotes.get(scr, [])}]}}
        return R()
    monkeypatch.setattr(D.requests, "get", get)
    syms = [r["symbol"] for r in fetch_us_value_pool(pool=400)]
    assert "INTC" not in syms and set(syms) == {"GE", "PYPL", "F"}


def test_fetch_us_value_pool_excludes_non_equity(monkeypatch):
    # quoteType!="EQUITY"(ETF/MUTUALFUND) 는 제외, 개별주(EQUITY)만 통과.
    quotes = {
        "undervalued_large_caps": [
            {"symbol": "GLD", "shortName": "SPDR Gold", "regularMarketPrice": 300.0,
             "regularMarketVolume": 8_000_000, "regularMarketChangePercent": -1.0,
             "quoteType": "ETF"},
            {"symbol": "INTC", "shortName": "Intel", "regularMarketPrice": 22.5,
             "regularMarketVolume": 40_000_000, "regularMarketChangePercent": -1.5,
             "quoteType": "EQUITY"},
            {"symbol": "VFINX", "shortName": "Vanguard 500", "regularMarketPrice": 500.0,
             "regularMarketVolume": 1_000_000, "regularMarketChangePercent": -0.5,
             "quoteType": "MUTUALFUND"},
        ],
        "undervalued_growth_stocks": [
            {"symbol": "PYPL", "shortName": "PayPal", "regularMarketPrice": 68.0,
             "regularMarketVolume": 12_000_000, "regularMarketChangePercent": -0.5},  # quoteType 누락
        ],
        "day_losers": [
            {"symbol": "F", "shortName": "Ford", "regularMarketPrice": 11.0,
             "regularMarketVolume": 60_000_000, "regularMarketChangePercent": -6.0,
             "quoteType": "EQUITY"},
        ],
    }
    monkeypatch.setattr(D.requests, "get", _make_screener_get(quotes))
    syms = [r["symbol"] for r in fetch_us_value_pool(pool=400)]
    # ETF(GLD)/MUTUALFUND(VFINX)/quoteType 누락(PYPL) 전부 제외 → EQUITY 만.
    assert set(syms) == {"INTC", "F"}


# ── config: universe.yaml(동적) 우선 + 안전장치 ──────────────
def _raw(enabled=True, **sc):
    return {"universe": {"KR": [{"symbol": "005930"}]},
            "screener": {"enabled": enabled, **sc}}


def test_resolve_universe_static_when_disabled(tmp_path):
    assert resolve_universe(_raw(enabled=False), tmp_path)["KR"][0]["symbol"] == "005930"


def test_resolve_universe_static_when_no_file(tmp_path):
    assert resolve_universe(_raw(), tmp_path)["KR"][0]["symbol"] == "005930"


def test_resolve_universe_dynamic_when_fresh(tmp_path):
    (tmp_path / "universe.yaml").write_text(
        yaml.safe_dump({"KR": [{"symbol": "DYN"}]}), encoding="utf-8")
    out = resolve_universe(_raw(), tmp_path, now=time.time())
    assert out["KR"][0]["symbol"] == "DYN"            # 동적 발굴 우선


def test_resolve_universe_stale_falls_back(tmp_path):
    f = tmp_path / "universe.yaml"
    f.write_text(yaml.safe_dump({"KR": [{"symbol": "DYN"}]}), encoding="utf-8")
    future = f.stat().st_mtime + 2 * 3600             # 2시간 뒤(>max 1h)
    out = resolve_universe(_raw(universe_max_age_hours=1), tmp_path, now=future)
    assert out["KR"][0]["symbol"] == "005930"         # 낡음 → 정적 폴백

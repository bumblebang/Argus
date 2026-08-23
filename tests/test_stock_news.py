"""종목별 최근 뉴스 fetch — Finnhub company-news / 네이버 종목뉴스(전부 mock, 네트워크 없음)."""
from __future__ import annotations

from datetime import datetime, timezone

import src.datasources.finnhub as fh
import src.datasources.news as nw


class _Resp:
    def __init__(self, data, ok=True):
        self._data = data
        self._ok = ok

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError("HTTP 500")

    def json(self):
        return self._data


def _epoch_to_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


class TestFetchCompanyNews:
    def test_헤드라인_날짜_파싱_및_per컷(self, monkeypatch):
        data = [
            {"headline": "H1", "datetime": 1735689600, "source": "Reuters"},
            {"headline": "H2", "datetime": 1735776000, "source": "Bloomberg"},
            {"headline": "", "datetime": 1735862400, "source": "X"},   # 빈 제목은 스킵
            {"headline": "H3", "datetime": 1735862400},                # source 없음 → Finnhub
        ]
        monkeypatch.setattr(fh.requests, "get", lambda *a, **k: _Resp(data))
        out = fh.fetch_company_news("KEY", "AAPL", per=2)
        assert [d["title"] for d in out] == ["H1", "H2"]
        assert out[0]["source"] == "Reuters"
        assert out[0]["date"] == _epoch_to_date(1735689600)

    def test_source_없으면_Finnhub_기본값(self, monkeypatch):
        data = [{"headline": "H3", "datetime": 1735689600},
                {"headline": "H4", "datetime": 1735689600, "source": ""}]
        monkeypatch.setattr(fh.requests, "get", lambda *a, **k: _Resp(data))
        out = fh.fetch_company_news("KEY", "AAPL", per=5)
        assert all(d["source"] == "Finnhub" for d in out)

    def test_키_없으면_빈리스트(self):
        assert fh.fetch_company_news("", "AAPL") == []

    def test_예외시_빈리스트(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("net down")
        monkeypatch.setattr(fh.requests, "get", boom)
        assert fh.fetch_company_news("KEY", "AAPL") == []

    def test_비리스트_응답_빈리스트(self, monkeypatch):
        monkeypatch.setattr(fh.requests, "get", lambda *a, **k: _Resp({"error": "x"}))
        assert fh.fetch_company_news("KEY", "AAPL") == []


class TestFetchKrStockNews:
    def test_다중블록_펼침_태그제거_최신순_per컷(self, monkeypatch):
        data = [
            {"total": 1, "items": [
                {"title": "b1", "titleFull": "<b>삼성</b>전자 &amp; 실적",
                 "datetime": "202607011200", "officeName": "한경", "mobileNewsUrl": "u1"},
            ]},
            {"total": 2, "items": [
                {"title": "옛뉴스", "datetime": "202606300900", "officeName": "매경"},
                {"title": "최신", "titleFull": "가장 <i>최신</i> 뉴스",
                 "datetime": "202607020800", "officeName": "연합"},
            ]},
        ]
        monkeypatch.setattr(nw.requests, "get", lambda *a, **k: _Resp(data))
        out = nw.fetch_kr_stock_news("005930", per=2)
        assert len(out) == 2
        # 최신순: 202607020800 > 202607011200 > 202606300900(컷)
        assert out[0]["title"] == "가장 최신 뉴스"
        assert out[0]["date"] == "2026-07-02"
        assert out[0]["source"] == "연합"
        # 태그 제거 + html 엔티티 언이스케이프
        assert out[1]["title"] == "삼성전자 & 실적"
        assert out[1]["date"] == "2026-07-01"

    def test_titleFull_없으면_title_사용(self, monkeypatch):
        data = [{"items": [{"title": "제목만", "datetime": "202607010000"}]}]
        monkeypatch.setattr(nw.requests, "get", lambda *a, **k: _Resp(data))
        out = nw.fetch_kr_stock_news("005930", per=3)
        assert out[0]["title"] == "제목만"

    def test_예외시_빈리스트(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("net down")
        monkeypatch.setattr(nw.requests, "get", boom)
        assert nw.fetch_kr_stock_news("005930") == []

    def test_비리스트_응답_빈리스트(self, monkeypatch):
        monkeypatch.setattr(nw.requests, "get", lambda *a, **k: _Resp({"msg": "x"}))
        assert nw.fetch_kr_stock_news("005930") == []

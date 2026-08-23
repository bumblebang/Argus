"""Finnhub 미국 뉴스 소스. market_state.news 에 합쳐진다.

일반 시장 뉴스(general) + 미국 종목별 뉴스(company-news). 무료 60콜/분이라 pacing 적용.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import requests

from .base import DataSource, SourceContext
from ..logging_setup import get_logger

log = get_logger("src.finnhub")

BASE = "https://finnhub.io/api/v1"


def fetch_company_news(api_key: str, symbol: str, per: int = 3, days: int = 5,
                       timeout: int = 15) -> list[dict]:
    """미국 종목 최근 뉴스 헤드라인 per 개. 반환 [{title, date, source}] (실패 시 []).

    value_scan 도시에 생성 직전 후보에 주입해 LLM 이 하락 촉매를 실제 헤드라인으로
    판단하게 한다. 스캔을 죽이지 않도록 예외·비리스트 응답이면 [] 를 돌려준다.
    """
    if not api_key:
        return []
    try:
        frm = (date.today() - timedelta(days=days)).isoformat()
        to = date.today().isoformat()
        r = requests.get(BASE + "/company-news", timeout=timeout,
                         params={"symbol": symbol, "from": frm, "to": to, "token": api_key})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("[%s] 종목뉴스 조회 실패: %s", symbol, e)
        return []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for d in data:
        if len(out) >= per:
            break
        title = d.get("headline", "")
        if not title:
            continue
        ts = d.get("datetime")
        dt = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else ""
        out.append({"title": title, "date": dt, "source": d.get("source") or "Finnhub"})
    return out


class FinnhubNewsSource(DataSource):
    name = "news_finnhub"

    def __init__(self, api_key: str, symbols: list[str] | None = None,
                 max_general: int = 15, per_symbol: int = 3, days: int = 2,
                 spacing_sec: float = 1.1):
        self.api_key = api_key
        self.symbols = symbols or []
        self.max_general = max_general
        self.per_symbol = per_symbol
        self.days = days
        self.spacing = spacing_sec

    def _get(self, path: str, params: dict) -> list:
        params = dict(params, token=self.api_key)
        r = requests.get(BASE + path, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    @staticmethod
    def _item(d: dict, symbol: str | None = None) -> dict:
        ts = d.get("datetime")
        pub = datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else ""
        return {"source": "Finnhub" + (f"/{symbol}" if symbol else ""),
                "symbol": symbol, "title": d.get("headline", ""),
                "url": d.get("url", ""), "published": pub}

    def fetch(self, ctx: SourceContext) -> dict:
        if ctx.dry:
            return {"news": [{"source": "Finnhub", "title": "dry", "url": "", "published": ""}]}
        items: list[dict] = []
        try:
            for d in self._get("/news", {"category": "general"})[:self.max_general]:
                items.append(self._item(d))
        except Exception as e:
            log.warning("일반 뉴스 실패: %s", e)

        frm = (date.today() - timedelta(days=self.days)).isoformat()
        to = date.today().isoformat()
        for i, sym in enumerate(self.symbols):
            try:
                for d in self._get("/company-news", {"symbol": sym, "from": frm, "to": to})[:self.per_symbol]:
                    items.append(self._item(d, sym))
            except Exception as e:
                log.warning("[%s] 종목뉴스 실패: %s", sym, e)
            if self.spacing and i < len(self.symbols) - 1:
                time.sleep(self.spacing)
        log.info("Finnhub 뉴스 %d건", len(items))
        return {"news": items}

"""뉴스 소스: 한국 경제 RSS + DART 공시 속보.

RSS(한경·연합·매경·investing)는 키 불필요. DART 공시는 list.json 으로 최근 공시를 가져온다.
결과는 market_state.news 리스트에 합쳐진다: {source, title, url, published, symbol?}.
"""
from __future__ import annotations

import html
import re
import time
from datetime import date, timedelta
from xml.etree import ElementTree as ET

import requests

from .base import DataSource, SourceContext
from .dart import load_corp_map
from ..logging_setup import get_logger

log = get_logger("src.news")

_UA = {"User-Agent": "Mozilla/5.0 argus"}
DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"

NAVER_STOCK_NEWS_URL = "https://m.stock.naver.com/api/news/stock/{code}"
_NAVER_UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"}


def fetch_kr_stock_news(code: str, per: int = 3, timeout: int = 10) -> list[dict]:
    """한국 종목(6자리) 최근 뉴스 헤드라인 per 개. 반환 [{title, date, source}] (실패 시 []).

    네이버 종목뉴스(무인증)는 블록들의 list 로 오고 items 가 여러 블록에 흩어져 있다 →
    모든 블록의 items 를 펼쳐 datetime 내림차순으로 앞 per 개. value_scan 도시에 생성
    직전 KR 후보에 주입한다. 예외·비리스트 응답이면 스캔을 죽이지 않도록 [] 반환.
    """
    try:
        r = requests.get(NAVER_STOCK_NEWS_URL.format(code=code), headers=_NAVER_UA,
                         params={"pageSize": per * 4, "page": 1}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("[%s] 네이버 종목뉴스 실패: %s", code, e)
        return []
    if not isinstance(data, list):
        return []
    items: list[dict] = []
    for block in data:
        if isinstance(block, dict):
            items.extend(block.get("items") or [])
    items.sort(key=lambda x: str(x.get("datetime", "")), reverse=True)
    out: list[dict] = []
    for it in items:
        if len(out) >= per:
            break
        raw = it.get("titleFull") or it.get("title") or ""
        title = html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()
        if not title:
            continue
        dt = str(it.get("datetime", ""))
        if len(dt) >= 8 and dt[:8].isdigit():
            fdate = f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}"    # "YYYYMMDDHHMM" → "YYYY-MM-DD"
        else:
            fdate = dt[:8]
        out.append({"title": title, "date": fdate, "source": it.get("officeName", "")})
    return out


class NewsSource(DataSource):
    """한국 경제 RSS 헤드라인."""
    name = "news_rss"

    def __init__(self, feeds: dict[str, str], max_per_feed: int = 8):
        self.feeds = feeds
        self.max_per_feed = max_per_feed

    def fetch(self, ctx: SourceContext) -> dict:
        if ctx.dry:
            return {"news": [{"source": "dry", "title": "샘플 뉴스", "url": "", "published": ""}]}
        items: list[dict] = []
        for src, url in self.feeds.items():
            try:
                r = requests.get(url, headers=_UA, timeout=12)
                root = ET.fromstring(r.content)
                for it in root.iter("item"):
                    items.append({
                        "source": src,
                        "title": (it.findtext("title") or "").strip(),
                        "url": (it.findtext("link") or "").strip(),
                        "published": (it.findtext("pubDate") or "").strip(),
                    })
                    if sum(1 for x in items if x["source"] == src) >= self.max_per_feed:
                        break
            except Exception as e:
                log.warning("[%s] RSS 실패: %s", src, e)
        log.info("RSS 뉴스 %d건", len(items))
        return {"news": items}


class DartNewsSource(DataSource):
    """DART 공시 속보 (KR 유니버스 종목의 최근 공시)."""
    name = "news_dart"

    def __init__(self, api_key: str, symbols: list[str], days: int = 2,
                 max_total: int = 30, spacing_sec: float = 0.15):
        self.api_key = api_key
        self.symbols = symbols
        self.days = days
        self.max_total = max_total
        self.spacing = spacing_sec

    def fetch(self, ctx: SourceContext) -> dict:
        if ctx.dry:
            return {"news": []}
        try:
            cmap = load_corp_map(self.api_key)
        except Exception as e:
            log.warning("DART 공시: corp 맵 실패: %s", e)
            return {"news": []}
        bgn = (date.today() - timedelta(days=self.days)).strftime("%Y%m%d")
        items: list[dict] = []
        for i, sym in enumerate(self.symbols):
            corp = cmap.get(sym)
            if not corp:
                continue
            try:
                r = requests.get(DART_LIST_URL, params={
                    "crtfc_key": self.api_key, "corp_code": corp,
                    "bgn_de": bgn, "page_count": "10"}, timeout=15)
                body = r.json()
                if body.get("status") == "000":
                    for d in body.get("list", []):
                        items.append({
                            "source": "DART공시",
                            "symbol": sym,
                            "title": f"[{d.get('corp_name')}] {d.get('report_nm')}",
                            "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={d.get('rcept_no')}",
                            "published": d.get("rcept_dt", ""),
                        })
            except Exception as e:
                log.warning("[%s] DART 공시 실패: %s", sym, e)
            if self.spacing and i < len(self.symbols) - 1:
                time.sleep(self.spacing)
        items.sort(key=lambda x: x.get("published", ""), reverse=True)
        log.info("DART 공시 %d건", len(items))
        return {"news": items[:self.max_total]}

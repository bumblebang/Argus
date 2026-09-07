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

# 배치 종목뉴스 기본 상한. free tier ~60콜/분 + general 1콜 → 여유 두고 캡.
DEFAULT_NEWS_US_MAX = 20


def _pct(v) -> float | None:
    """Finnhub 는 비율을 % 로 준다(roeTTM 11.19 = 11.19%). 소수로 환산."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f / 100.0, 4)


def _num(v) -> float | None:
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


def fetch_basic_financials(api_key: str, symbol: str, *,
                           timeout: int = 12) -> dict | None:
    """US 재무 건전성 지표 — /stock/metric(무료 티어 포함).

    KR(DART) 도시어와 **같은 키 이름**으로 돌려줘 Score 의 QualityTilt 가 시장 구분 없이
    읽게 한다. 단 debt_ratio 정의는 다르다: KR=부채총계/자기자본, US=총차입금/자기자본.
    QualityTilt 는 절대 임계라 미국 쪽이 구조적으로 후하게 나오는데, 순위 비교가 시장
    안에서만 일어나므로(밸류는 시장별 실행) 문제되지 않는다.

    실패·비JSON 은 None(호출측이 삼켜 결측=중립 처리).
    """
    if not api_key or not symbol:
        return None
    try:
        r = requests.get(f"{BASE}/stock/metric",
                         params={"symbol": symbol, "metric": "all", "token": api_key},
                         timeout=timeout)
        if r.status_code != 200 or not r.text.strip().startswith("{"):
            return None
        m = (r.json() or {}).get("metric") or {}
    except Exception as e:
        log.debug("[finnhub][%s] basic financials 실패: %s", symbol, e)
        return None
    if not isinstance(m, dict) or not m:
        return None
    out = {
        "roe": _pct(m.get("roeTTM") if m.get("roeTTM") is not None else m.get("roeRfy")),
        "debt_ratio": _num(m.get("totalDebt/totalEquityQuarterly")
                           if m.get("totalDebt/totalEquityQuarterly") is not None
                           else m.get("totalDebt/totalEquityAnnual")),
        "revenue_growth": _pct(m.get("revenueGrowthTTMYoy")),
        "net_income_growth": _pct(m.get("epsGrowthTTMYoy")),   # EPS 성장 대용
        "net_margin": _pct(m.get("netProfitMarginTTM")),
        "current_ratio": _num(m.get("currentRatioQuarterly")),
        "quality_source": "finnhub",
    }
    if all(v is None for k, v in out.items() if k != "quality_source"):
        return None
    return {k: v for k, v in out.items() if v is not None}


def select_us_news_symbols(
    universe: list[str] | None,
    *,
    priority: list[str] | None = None,
    max_n: int = DEFAULT_NEWS_US_MAX,
) -> list[str]:
    """US 종목뉴스 대상: priority(보유·armed 등) 먼저, 유니버스로 패딩, max_n 캡.

    KR 6자리 코드는 제외. 순서·중복 제거 유지.
    """
    if max_n <= 0:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in list(priority or []) + list(universe or []):
        s = str(raw or "").strip()
        if not s or s in seen:
            continue
        if s.isdigit() and len(s) == 6:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= max_n:
            break
    return out


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
        try:
            r.raise_for_status()
        except requests.HTTPError:
            from ..http_sanitize import response_error_brief
            raise requests.HTTPError(response_error_brief(r), response=r) from None
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

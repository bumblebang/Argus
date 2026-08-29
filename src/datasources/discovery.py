"""동적 유니버스 발굴 — Naver 모바일 랭킹(거래대금 상위).

시총 상위 풀(marketValue)을 받아 **거래대금(accumulatedTradingValue)**으로 재정렬 →
가장 활발히 거래되는 liquid 종목 top-N. 스크리너의 후보 풀("어떤 종목을 볼지")을 매일
동적으로 채운다. 역할분리: **발굴=Naver, 백테스트 히스토리=Yahoo, 라이브 매매=토스 공식.**

Naver 모바일 API(m.stock.naver.com)는 flows.py 와 같은 무인증 엔드포인트 — 준실시간
(localTradedAt 체결시각). 일 단위 후보 발굴엔 충분(틱 단위 불요).
"""
from __future__ import annotations

import json
import os
import time

import requests

from ..config import ROOT
from ..logging_setup import get_logger

log = get_logger("src.discovery")

# 전일 랭킹 캐시 — 새 거래일 개장 전엔 당일 누적 거래대금이 0으로 리셋돼 랭킹이 비므로
# (Naver API 에 '전일 거래대금' 필드 없음), 장중 스캔(무버 60분 주기)이
# 성공한 랭킹을 여기 축적해 두고 개장 전 코어 리프레시가 이를 전일 기준으로 쓴다.
# 폴백 체인: 당일 라이브 → 전일 캐시 → 시총 순(최후). TTL 은 주말(금 마감→월 아침 ~63h) 커버.
_RANKING_CACHE = ROOT / "data" / "ranking_cache.json"
_CACHE_MAX_AGE_H = 96.0


def _save_ranking_cache(market: str, rows: list[dict]) -> None:
    """성공한(건강한) 랭킹을 시장별로 저장(원자적, 실패는 무시 — 발굴을 막지 않는다)."""
    try:
        data = {}
        if _RANKING_CACHE.exists():
            data = json.loads(_RANKING_CACHE.read_text(encoding="utf-8")) or {}
        data[market] = {"ts": time.time(), "rows": rows}
        _RANKING_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _RANKING_CACHE.with_name(_RANKING_CACHE.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _RANKING_CACHE)
    except Exception as e:
        log.warning("랭킹 캐시 저장 실패(무시): %s", e)


def _load_ranking_cache(market: str, max_age_h: float = _CACHE_MAX_AGE_H) -> list[dict]:
    """시장별 캐시 랭킹(신선할 때만). 없거나 낡았으면 []."""
    try:
        if not _RANKING_CACHE.exists():
            return []
        data = json.loads(_RANKING_CACHE.read_text(encoding="utf-8")) or {}
        ent = data.get(market) or {}
        if time.time() - float(ent.get("ts", 0)) > max_age_h * 3600:
            return []
        return ent.get("rows") or []
    except Exception as e:
        log.warning("랭킹 캐시 로드 실패(무시): %s", e)
        return []

_UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"}
NAVER = "https://m.stock.naver.com/api/stocks/marketValue/{market}"
# 시장 → Naver 카테고리 시장코드(KR=코스피+코스닥 합쳐 거래대금 랭킹)
_NAVER_MARKETS = {"KR": ["KOSPI", "KOSDAQ"]}

# 미국 발굴: Yahoo 거래량 상위(most_actives). Naver 모바일 API 는 KR 전용이라 US 는 Yahoo.
YAHOO_SCREENER = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"


def _num(v) -> float:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _f(v):
    """밸류에이션 지표 파싱 — 숫자면 float(소수 2자리), 아니면 None.

    결측(적자로 PER 없음 등)은 0 이 아니라 None 으로 남긴다 — 그 자체가 정보다.
    """
    try:
        return round(float(v), 2) if v is not None else None
    except (TypeError, ValueError):
        return None


def fetch_ranking(naver_market: str, pool: int = 100) -> list[dict]:
    """Naver 시총상위 pool개 + 종목별 거래대금/가격/등락률 (네트워크).

    pageSize 100 상한이라 pool>100은 page 순회로 모은다.
    """
    out: list[dict] = []
    seen: set[str] = set()
    page = 1
    while len(out) < pool and page <= 20:      # 안전 상한: page 20 초과 시 중단(무한루프 방지)
        r = requests.get(NAVER.format(market=naver_market),
                         params={"page": page, "pageSize": 100}, headers=_UA, timeout=12)
        if not r.text.strip().startswith("{"):
            break
        stocks = r.json().get("stocks", [])
        if not stocks:                          # 빈 페이지 → 더 내려갈 종목 없음
            break
        for s in stocks:
            code = s.get("itemCode")
            if not code or code in seen:
                continue
            seen.add(code)
            out.append({
                "symbol": code,
                "name": s.get("stockName", code),
                "price": _num(s.get("closePriceRaw") or s.get("closePrice")),
                "trading_value": _num(s.get("accumulatedTradingValueRaw")
                                      or s.get("accumulatedTradingValue")),
                "fluctuation": _num(s.get("fluctuationsRatio")),
                # 시가총액(원) — value_scan 의 KR PBR/PER 계산용. 기존 소비자는 이 키를
                # 무시하므로 하위호환(top_by_trading_value 등 영향 없음).
                "market_cap": _num(s.get("marketValueRaw")),
            })
        page += 1
    return out[:pool]


def top_by_trading_value(market: str = "KR", count: int = 20, pool: int = 100,
                         min_price: float = 0.0,
                         fetch_fn=fetch_ranking,
                         allow_cache_fallback: bool = True) -> list[dict]:
    """거래대금 상위 종목 발굴. 반환: [{symbol,name,price,trading_value,fluctuation}] count개.

    fetch_fn 주입 가능(테스트 시 네트워크 대체).
    allow_cache_fallback=False 이면 당일 라이브만(갭 풀 15:15 등 — 전일 캐시·시총 보충 금지).
    """
    rows: list[dict] = []
    for nm in _NAVER_MARKETS.get(market, [market]):
        try:
            rows += fetch_fn(nm, pool=pool)
        except Exception as e:
            log.warning("[%s] 랭킹 조회 실패: %s", nm, e)
    priced = [r for r in rows if r["price"] >= min_price]
    ranked = [r for r in priced if r["trading_value"] > 0]
    ranked.sort(key=lambda r: r["trading_value"], reverse=True)
    if len(ranked) >= count:
        # 건강한 라이브 랭킹 → 전일 캐시로 축적(다음날 개장 전 코어가 전일 기준으로 씀).
        _save_ranking_cache("KR", ranked)
    elif allow_cache_fallback:
        # 개장 전 폴백: 새 거래일은 누적 거래대금 0. ①전일 캐시(진짜 전일 거래대금 순)
        # ②그래도 모자라면 시총 순(Naver 풀 원순서) — 최후 폴백.
        seen = {r["symbol"] for r in ranked}
        cached = [r for r in _load_ranking_cache("KR")
                  if r.get("symbol") not in seen
                  and float(r.get("price") or 0) >= min_price]
        if cached:
            log.info("발굴 KR 라이브 랭킹 %d < %d — 전일 캐시 %d종목 보충(전일 거래대금 순)",
                     len(ranked), count, len(cached))
            ranked += cached
            seen |= {r["symbol"] for r in cached}
        if len(ranked) < count and priced:
            fill = [r for r in priced if r["symbol"] not in seen]
            if fill:
                log.info("발굴 KR 여전히 %d < %d — 시총 순 %d종목 보충(최후 폴백)",
                         len(ranked), count, min(len(fill), count - len(ranked)))
            ranked += fill
    elif ranked:
        log.info("발굴 KR 라이브 %d건(폴백 비활성, 요청 %d)", len(ranked), count)
    log.info("발굴 KR %d종목(거래대금 상위, pool=%d, 후보 %d)",
             min(count, len(ranked)), pool, len(ranked))
    return ranked[:count]


def fetch_us_actives(pool: int = 50) -> list[dict]:
    """Yahoo 거래량 상위(most_actives) pool개. 거래대금=price×volume 으로 환산해 담는다."""
    r = requests.get(YAHOO_SCREENER, params={"scrIds": "most_actives", "count": pool},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
    if not r.text.strip().startswith("{"):
        return []
    res = (r.json().get("finance", {}) or {}).get("result", [])
    quotes = res[0].get("quotes", []) if res else []
    out = []
    for q in quotes:
        sym = q.get("symbol")
        if not sym:
            continue
        px, vol = _num(q.get("regularMarketPrice")), _num(q.get("regularMarketVolume"))
        out.append({
            "symbol": sym,
            "name": q.get("shortName") or sym,
            "price": px,
            "trading_value": px * vol,          # 거래대금($) — KR과 같은 기준으로 통일
            "fluctuation": _num(q.get("regularMarketChangePercent")),
        })
    return out


def top_us_by_trading_value(count: int = 10, pool: int = 50, min_price: float = 0.0,
                            fetch_fn=fetch_us_actives) -> list[dict]:
    """미국 거래대금(=거래량×가격) 상위 종목 발굴. KR top_by_trading_value 와 같은 반환형."""
    rows = [r for r in fetch_fn(pool=pool)
            if r["price"] >= min_price and r["trading_value"] > 0]
    rows.sort(key=lambda r: r["trading_value"], reverse=True)
    if len(rows) >= count:
        _save_ranking_cache("US", rows)         # KR 과 동일한 전일 캐시 축적
    else:
        seen = {r["symbol"] for r in rows}
        cached = [r for r in _load_ranking_cache("US")
                  if r.get("symbol") not in seen
                  and float(r.get("price") or 0) >= min_price]
        if cached:
            log.info("발굴 US 라이브 랭킹 %d < %d — 전일 캐시 %d종목 보충",
                     len(rows), count, len(cached))
            rows += cached
    log.info("발굴 US %d종목(거래대금 상위, 후보 %d)", min(count, len(rows)), len(rows))
    return rows[:count]


# 미국 저평가 발굴용 Yahoo predefined 스크리너 3종(합집합). most_actives(거래대금 상위,
# 대형주)와 목적이 다르다 — 저평가주 스캔(value_scan)이 이 풀을 쓴다.
_US_VALUE_SCRIDS = ("undervalued_large_caps", "undervalued_growth_stocks", "day_losers")


def fetch_us_value_pool(pool: int = 400) -> list[dict]:
    """Yahoo 저평가 스크리너 3종 합집합 → 저평가 후보 풀 pool개 (네트워크).

    undervalued_large_caps(밸류에이션 저평가 대형주) + undervalued_growth_stocks(저평가
    성장주) + day_losers(당일 낙폭) 를 symbol 기준 중복 제거해 합친다. **저평가 발굴 전용**
    — 거래대금 상위(대형주)를 뽑는 fetch_us_actives/most_actives 와 목적이 다르다.
    특정 scrId 실패(비-JSON/네트워크)는 그 소스만 건너뛰고 나머지로 계속. 반환형은
    fetch_ranking/fetch_us_actives 와 동일 키(symbol/name/price/trading_value/fluctuation).
    quoteType==EQUITY 만(ETF/펀드 제외 — 개별 저평가주 발굴 대상이 아니라 LLM 예산 낭비).
    """
    out: list[dict] = []
    seen: set[str] = set()
    for scr in _US_VALUE_SCRIDS:
        if len(out) >= pool:
            break
        try:
            r = requests.get(YAHOO_SCREENER, params={"scrIds": scr, "count": 250},
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
            if not r.text.strip().startswith("{"):
                continue
            res = (r.json().get("finance", {}) or {}).get("result", [])
            quotes = res[0].get("quotes", []) if res else []
        except Exception as e:
            log.warning("[value] US 스크리너 %s 실패(스킵): %s", scr, e)
            continue
        for q in quotes:
            sym = q.get("symbol")
            if not sym or sym in seen:
                continue
            if q.get("quoteType") != "EQUITY":   # ETF/MUTUALFUND 등 제외(개별주만)
                continue
            seen.add(sym)
            px = _num(q.get("regularMarketPrice"))
            out.append({
                "symbol": sym,
                "name": q.get("shortName") or sym,
                "price": px,
                "trading_value": px * _num(q.get("regularMarketVolume")),
                "fluctuation": _num(q.get("regularMarketChangePercent")),
                # 밸류에이션 지표 — 스크리너 응답에 이미 들어있어 추가 호출 0. 결측은
                # None(적자로 PER 없음도 정보). 시총은 십억달러 단위로 압축.
                "fundamentals": {
                    "pe_trailing": _f(q.get("trailingPE")),
                    "pe_forward": _f(q.get("forwardPE")),
                    "pb": _f(q.get("priceToBook")),
                    "eps_ttm": _f(q.get("epsTrailingTwelveMonths")),
                    "market_cap_busd": (round(float(q["marketCap"]) / 1e9, 2)
                                        if q.get("marketCap") else None),
                },
            })
            if len(out) >= pool:
                break
    log.info("발굴 US 저평가 풀 %d종목(스크리너 %d종 합집합)", len(out), len(_US_VALUE_SCRIDS))
    return out[:pool]

"""종목 섹터(업종) 분류 수집·캐시 — 섹터 집중도 감독의 데이터 공급원.

유니버스가 동적으로 굴러가면서 새로 편입되는 종목에는 sector 가 없다. 그러면
`risk_gate` 의 섹터 집중도 검사가 그 종목을 통째로 건너뛰어(캡 비활성) 조용히 무력화된다.
여기서 KR·US 실제 업종을 받아 `sector_taxonomy` 의 11섹터로 정규화해 채운다.

  - KR: KRX 업종분류현황(MDCSTAT03901). 전종목을 한 번에 받으므로 1회 호출로 끝난다.
  - US: Finnhub /stock/profile2 의 finnhubIndustry. 심볼당 1회(무료 티어 60req/min).

업종은 사실상 정적이라 캐시 TTL 은 30일. 캐시 miss 만 네트워크를 탄다. 조회 실패는
fail-soft — 섹터를 못 채워도 매매는 계속되어야 하므로 경고만 남기고 진행한다(대신
커버리지 경고가 `agents.wiring` 에서 크게 뜬다).
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta
from pathlib import Path

from ..logging_setup import get_logger
from ..sector_taxonomy import normalize_sector

log = get_logger("src.sector_class")

CACHE_PATH = Path("data/sector_cache.json")
TTL_SEC = 30 * 86400.0                    # 업종은 정적 — 30일
MISS_TTL_SEC = 86400.0                    # 분류 실패분은 하루 뒤 재시도
KR_BLD = "dbms/MDC/STAT/standard/MDCSTAT03901"   # 업종분류현황
FINNHUB_PROFILE = "https://finnhub.io/api/v1/stock/profile2"
US_SPACING_SEC = 1.1                      # 무료 티어 60req/min
US_MAX_PER_RUN = 60                       # 1회 롤에서 새로 조회할 US 심볼 상한


# ── 캐시 입출력 ──────────────────────────────────────────────
def load_cache(path: Path | None = None) -> dict:
    p = Path(path or CACHE_PATH)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict, path: Path | None = None) -> None:
    """tmp+replace 원자적 저장. 실패는 경고만(캐시는 재생 가능)."""
    p = Path(path or CACHE_PATH)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, p)
    except OSError as e:
        log.warning("sector_cache 저장 실패(무시): %s", e)


def _market_block(cache: dict, market: str) -> dict:
    blk = cache.get(market)
    if not isinstance(blk, dict):
        blk = {}
        cache[market] = blk
    return blk


def _fresh(entry: dict | None, now: float) -> bool:
    """캐시 유효 판정. 분류에 실패한 항목은 짧은 TTL 로 재시도한다.

    분류 실패를 30일 붙잡고 있으면 신규 상장/매핑 누락이 한 달간 미분류로 남는다.
    성공분만 길게 재사용한다.
    """
    if not isinstance(entry, dict):
        return False
    ttl = TTL_SEC if entry.get("sector") else MISS_TTL_SEC
    return (now - float(entry.get("fetched") or 0.0)) < ttl


def _put(cache: dict, market: str, symbol: str, raw: str | None, now: float) -> bool:
    """raw 업종 문자열을 정규화해 캐시에 기록. 정규화 실패면 raw 만 남긴다."""
    sec = normalize_sector(raw)
    _market_block(cache, market)[str(symbol)] = {
        "sector": sec, "raw": raw, "fetched": now}
    return sec is not None


def sector_for(cache: dict, market: str, symbol: str) -> str | None:
    """캐시된 정규화 섹터. 없거나 정규화 실패면 None(=미지정)."""
    entry = (cache.get(str(market).upper()) or {}).get(str(symbol))
    if not isinstance(entry, dict):
        return None
    return entry.get("sector") or None


# ── KR: KRX 업종분류현황(전종목 벌크) ─────────────────────────
def _recent_days(n: int = 7) -> list[str]:
    """오늘부터 과거로 n일(YYYYMMDD). 휴장일이면 rows=0 이라 하루씩 물러난다."""
    today = date.today()
    return [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]


def fetch_kr_industries(client=None, *, days: int = 7) -> dict[str, str]:
    """KRX 업종분류현황 → {종목코드: 업종명}. 실패/무자격이면 빈 dict."""
    from .krx_client import bld_for, connect

    c = client or connect()
    if not getattr(c, "has_creds", False):
        log.warning("KRX 자격 없음 — KR 섹터 수집 건너뜀")
        return {}
    bld = bld_for("krx_sector_class") or KR_BLD
    out: dict[str, str] = {}
    for day in _recent_days(days):
        for mkt in ("STK", "KSQ"):
            for row in c.get_rows(bld, mktId=mkt, trdDd=day, money="1",
                                  csvxls_isNo="false"):
                sym, ind = row.get("ISU_SRT_CD"), row.get("IDX_IND_NM")
                if sym and ind:
                    out[str(sym)] = str(ind)
        if out:
            log.info("KRX 업종분류현황 %s: %d종목", day, len(out))
            return out
    log.warning("KRX 업종분류현황 조회 0건(최근 %d일) — KR 섹터 수집 실패", days)
    return {}


def refresh_kr(cache: dict, symbols, now: float, *, client=None) -> int:
    """요청 심볼 중 캐시가 만료된 게 있으면 KRX 전종목을 한 번 받아 채운다.

    벌크 1회 호출이라 심볼 수와 무관. 반환값 = 새로 정규화에 성공한 심볼 수.
    """
    want = [str(s) for s in symbols if s]
    blk = _market_block(cache, "KR")
    if all(_fresh(blk.get(s), now) for s in want):
        return 0
    table = fetch_kr_industries(client)
    if not table:
        return 0
    hit = 0
    for sym, ind in table.items():        # 전종목 저장 — 다음 편입 종목도 캐시 히트
        if _put(cache, "KR", sym, ind, now):
            hit += 1
    missing = [s for s in want if s not in table]
    if missing:
        # 업종분류현황은 KOSPI+KOSDAQ 상장 **주식** 전수(스팩 포함)다. 여기 없는 코드는
        # 사실상 ETF/ETN — 산업 축이 없는 지수 상품이므로 ETF 버킷에 넣는다.
        for s in missing:
            _put(cache, "KR", s, "ETF", now)
            hit += 1
        log.info("KR 섹터: KRX 주식 목록에 없는 %d종목 → ETF 버킷 %s",
                 len(missing), missing[:10])
    return hit


# ── US: Finnhub profile2(심볼별) ─────────────────────────────
def fetch_us_industry(symbol: str, api_key: str, *, timeout: float = 15.0) -> str | None:
    """Finnhub finnhubIndustry. 실패/빈값이면 None."""
    import requests

    try:
        r = requests.get(FINNHUB_PROFILE,
                         params={"symbol": symbol, "token": api_key}, timeout=timeout)
        if r.status_code != 200:
            log.warning("Finnhub profile2 %s: HTTP %s", symbol, r.status_code)
            return None
        return (r.json() or {}).get("finnhubIndustry") or None
    except Exception as e:                # 네트워크/파싱 전반 — fail-soft
        log.warning("Finnhub profile2 %s 실패: %s", symbol, e)
        return None


def refresh_us(cache: dict, symbols, now: float, *, api_key: str | None = None,
               max_fetch: int = US_MAX_PER_RUN, spacing_sec: float = US_SPACING_SEC,
               sleep=time.sleep) -> int:
    """캐시 만료된 US 심볼만 Finnhub 로 조회. 1회 실행 상한 max_fetch."""
    key = api_key or os.getenv("FINNHUB_API_KEY") or ""
    blk = _market_block(cache, "US")
    stale = [str(s) for s in symbols if s and not _fresh(blk.get(str(s)), now)]
    if not stale:
        return 0
    if not key:
        log.warning("FINNHUB_API_KEY 없음 — US 섹터 %d종목 수집 건너뜀", len(stale))
        return 0
    todo = stale[:max_fetch]
    if len(stale) > max_fetch:
        log.info("US 섹터: 미조회 %d종목 중 %d종목만 이번 실행(나머지는 다음 롤)",
                 len(stale), max_fetch)
    hit = 0
    for i, sym in enumerate(todo):
        if i:
            sleep(spacing_sec)
        if _put(cache, "US", sym, fetch_us_industry(sym, key), now):
            hit += 1
    return hit


# ── 진입점 ───────────────────────────────────────────────────
def ensure_sectors(symbols_by_market: dict, *, cache_path: Path | None = None,
                   now: float | None = None, krx_client=None,
                   api_key: str | None = None,
                   max_us_fetch: int = US_MAX_PER_RUN) -> dict:
    """요청 심볼들의 섹터를 캐시에 채우고 캐시 dict 를 반환(디스크에도 저장).

    네트워크 실패는 삼킨다 — 섹터를 못 채워도 매매는 계속되어야 한다.
    """
    now = time.time() if now is None else now
    cache = load_cache(cache_path)
    changed = 0
    for market, syms in (symbols_by_market or {}).items():
        m = str(market).upper()
        try:
            if m == "KR":
                changed += refresh_kr(cache, syms, now, client=krx_client)
            elif m == "US":
                changed += refresh_us(cache, syms, now, api_key=api_key,
                                      max_fetch=max_us_fetch)
        except Exception as e:
            log.warning("[%s] 섹터 수집 실패(무시하고 진행): %s", m, e)
    if changed:
        save_cache(cache, cache_path)
        log.info("섹터 캐시 갱신 %d종목", changed)
    return cache


__all__ = ["ensure_sectors", "load_cache", "save_cache", "sector_for",
           "refresh_kr", "refresh_us", "fetch_kr_industries", "fetch_us_industry",
           "CACHE_PATH"]

"""토스 종목 적격성(매수 안전가드) 조회·캐시.

라이브 실매매 직전, 봇이 관리종목·거래정지·상폐예정·ETF/ETN 같은 부적격 종목을 사지
못하게 막는 매수 게이트의 데이터 소스. 두 토스 엔드포인트로 판정한다:
- GET /api/v1/stocks?symbols=... (StockInfo): securityType/status 로 정적 부적격 판정.
- GET /api/v1/stocks/{symbol}/warnings (StockWarning[]): 진행중 위험경고로 동적 판정.

조회 실패는 **fail-open**(매수 허용)한다 — 안전가드 조회 실패로 정상 매매까지 멈추면
더 위험하기 때문. 대신 실패는 로깅으로 관측한다(가드 조회 자체는 매매를 막지 않는다).

캐시: StockInfo 는 정적이라 하루 TTL, StockWarning 은 동적이라 30분 TTL. 캐시 dict 는
프로세스 내에서 공유하고, 갱신 시 원자적으로 디스크에 기록한다.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..logging_setup import get_logger

log = get_logger("src.stock_info")

_KST = ZoneInfo("Asia/Seoul")

INFO_CACHE_PATH = Path("data/stock_info_cache.json")
WARN_CACHE_PATH = Path("data/stock_warnings_cache.json")

INFO_TTL_SEC = 86400.0      # StockInfo: 정적 → 하루
WARN_TTL_SEC = 1800.0       # StockWarning: 동적 → 30분

# 매수 차단 securityType(부적격 유형). 허용 = STOCK/FOREIGN_STOCK/REIT/INFRASTRUCTURE_FUND.
BLOCKED_SECURITY_TYPES = frozenset({
    "ETF", "FOREIGN_ETF", "ETN", "DEPOSITARY_RECEIPT", "STOCK_WARRANTS",
})

# 매수 차단 warningType(진행중이면 차단). 무시 = VI_*(변동성완화, 일시적) / STOCK_WARRANTS.
BLOCKED_WARNING_TYPES = frozenset({
    "LIQUIDATION_TRADING", "INVESTMENT_WARNING", "INVESTMENT_RISK", "OVERHEATED",
})


# ── 순수 판정 헬퍼(테스트 가능) ────────────────────────────
def _is_tradable_info(info: dict) -> tuple[bool, str]:
    """StockInfo 정적 적격성. (매수가능, 사유). status/securityType 만 본다."""
    status = info.get("status")
    if status != "ACTIVE":                       # SCHEDULED(상장예정)/DELISTED(상폐) 제외
        return False, f"비활성: {status}"
    sec = info.get("securityType")
    if sec in BLOCKED_SECURITY_TYPES:
        return False, f"부적격유형: {sec}"
    return True, ""


def _active_warnings(warnings: list[dict], today: str) -> list[str]:
    """진행 중인 차단 대상 경고의 warningType 목록. today 는 'YYYY-MM-DD'(KST).

    진행중 = startDate<=today<=endDate. endDate null → 진행중, startDate null → 시작된
    것으로 간주. VI_*/STOCK_WARRANTS 등 BLOCKED_WARNING_TYPES 밖은 무시한다.
    ISO 날짜(YYYY-MM-DD)는 사전순 비교가 곧 날짜 비교이므로 앞 10자로 비교한다.
    """
    active: list[str] = []
    for w in warnings or []:
        wt = w.get("warningType")
        if wt not in BLOCKED_WARNING_TYPES:
            continue
        start = (w.get("startDate") or "")[:10]  # null → "" → 항상 시작된 것으로 간주
        end_raw = w.get("endDate")
        end = end_raw[:10] if end_raw else None   # null → 진행중
        if start and start > today:               # 아직 시작 전
            continue
        if end is not None and end < today:        # 이미 종료됨
            continue
        active.append(wt)
    return active


# ── 조회(배치) ────────────────────────────────────────────
def fetch_stock_info(client, symbols) -> dict[str, dict]:
    """배치 조회 → {symbol: {securityType, status, isCommonShare, name, market}}.

    symbols 는 list|str. 토스 배치 상한(200)에 맞춰 청크로 나눠 호출한다.
    """
    syms = [symbols] if isinstance(symbols, str) else [s for s in symbols if s]
    out: dict[str, dict] = {}
    for i in range(0, len(syms), 200):
        chunk = syms[i:i + 200]
        for r in client.get_stock_info(chunk) or []:
            sym = r.get("symbol") or r.get("code")
            if not sym:
                continue
            detail = r.get("koreanMarketDetail") or {}
            nxt = r.get("nxtSupported")
            if nxt is None and isinstance(detail, dict):
                nxt = detail.get("nxtSupported")
            out[sym] = {
                "securityType": r.get("securityType"),
                "status": r.get("status"),
                "isCommonShare": r.get("isCommonShare"),
                "name": r.get("name"),
                "market": r.get("market"),
                "nxtSupported": nxt,
            }
    return out


def fetch_warnings(client, symbol: str) -> list[dict]:
    """종목별 경고 배열(StockWarning[]) 그대로 반환."""
    return client.get_stock_warnings(symbol) or []


# ── 캐시 로드/세이브(원자적) ───────────────────────────────
def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(path: Path, cache: dict) -> None:
    """원자적 쓰기(tmp+os.replace). 실패는 무시(가드가 매매를 막지 않는다)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        log.warning("[매수가드] 캐시 저장 실패(무시) %s: %s", path.name, e)


def load_info_cache() -> dict:
    return _load(INFO_CACHE_PATH)


def load_warn_cache() -> dict:
    return _load(WARN_CACHE_PATH)


def _kst_today(now: float) -> str:
    return datetime.fromtimestamp(now, _KST).strftime("%Y-%m-%d")


def _get_info(symbol: str, client, cache: dict, now: float) -> dict | None:
    """캐시 우선 StockInfo 조회. miss 면 client 조회 후 캐시. 실패는 None(fail-open)."""
    entry = cache.get(symbol)
    if entry and (now - entry.get("fetched", 0)) < INFO_TTL_SEC:
        return entry.get("info")
    try:
        infos = fetch_stock_info(client, symbol)
    except Exception as e:
        log.warning("[매수가드] StockInfo 조회 실패(fail-open) %s: %s", symbol, e)
        return None
    info = infos.get(symbol)
    if info is None and len(infos) == 1:          # 심볼 표기 차이 방어(응답이 하나뿐이면 그것)
        info = next(iter(infos.values()))
    if info is None:
        log.warning("[매수가드] StockInfo 응답에 %s 없음(fail-open)", symbol)
        return None
    cache[symbol] = {"fetched": now, "info": info}
    _save(INFO_CACHE_PATH, cache)
    return info


def _get_warnings(symbol: str, client, cache: dict, now: float) -> list[dict] | None:
    """캐시 우선 StockWarning 조회. miss 면 client 조회 후 캐시. 실패는 None(fail-open)."""
    entry = cache.get(symbol)
    if entry and (now - entry.get("fetched", 0)) < WARN_TTL_SEC:
        return entry.get("warnings", [])
    try:
        warnings = fetch_warnings(client, symbol)
    except Exception as e:
        log.warning("[매수가드] StockWarning 조회 실패(fail-open) %s: %s", symbol, e)
        return None
    cache[symbol] = {"fetched": now, "warnings": warnings}
    _save(WARN_CACHE_PATH, cache)
    return warnings


def check_tradable(symbol: str, market: str, *, client, info_cache: dict,
                   warn_cache: dict, now: float | None = None) -> tuple[bool, str]:
    """매수 적격성 판정. (매수가능, 사유). 캐시 우선, miss 면 client 조회 후 캐시.

    StockInfo(정적)로 status/securityType 를, StockWarning(동적)으로 진행중 위험경고를
    검사한다. 조회 실패(네트워크 등)는 fail-open(True) — 로깅만 남기고 매수는 막지 않는다.
    US 종목은 warnings 가 대개 빈 배열이라 그대로 통과한다.
    """
    now = time.time() if now is None else now
    info = _get_info(symbol, client, info_cache, now)
    if info is not None:
        ok, reason = _is_tradable_info(info)
        if not ok:
            return False, reason
    warnings = _get_warnings(symbol, client, warn_cache, now)
    if warnings is not None:
        active = _active_warnings(warnings, _kst_today(now))
        if active:
            return False, f"경고: {active[0]}"
    return True, ""


# 국내 증권거래세 면제 유형 — ETF/ETN 매도에는 증권거래세가 붙지 않는다.
SELL_TAX_EXEMPT_TYPES = frozenset({"ETF", "ETN", "FOREIGN_ETF"})


def is_sell_tax_exempt(symbol: str, market: str, info_cache: dict) -> bool:
    """국내 매도 거래세 면제 종목(ETF/ETN)인지 — **캐시만** 보고 판정(네트워크 없음).

    페이퍼/백테스트의 비용 모델용이다. 라이브는 토스 체결 대사(execution.tax)에서 실제
    세금을 그대로 받으므로 이 함수가 필요 없다(면제가 자동 반영된다). 캐시에 없거나 KR 이
    아니면 False — 즉 세금을 그대로 매겨 보수적으로(비용 과소추정 방지) 둔다.
    """
    if market != "KR":
        return False
    entry = (info_cache or {}).get(symbol) or {}
    info = entry.get("info") or {}
    return info.get("securityType") in SELL_TAX_EXEMPT_TYPES

"""매수 불가 증권유형 조기 제외 — 유니버스·도씨에·후보 토큰 낭비 방지.

브로커 `check_tradable` 의 BLOCKED_SECURITY_TYPES(ETF/ETN/…)와 같은 집합을
스크리너·Athena·유니버스 로드 앞단에서도 쓴다. Toss 캐시 securityType 이 있으면
그걸 우선하고, 없으면 KR 종목명 브랜드 휴리스틱(밸류 스캔과 동일).
"""
from __future__ import annotations

import json
from pathlib import Path

from .datasources.stock_info import BLOCKED_SECURITY_TYPES, INFO_CACHE_PATH
from .logging_setup import get_logger

log = get_logger("src.security_filter")

# 한국 ETF/ETN 브랜드 프리픽스 — "KODEX 200" 첫 토큰. 개별주는 한글사명이라 충돌 없음.
KR_ETF_BRANDS = frozenset({
    "KODEX", "TIGER", "KBSTAR", "ARIRANG", "HANARO", "KOSEF", "ACE", "SOL",
    "PLUS", "RISE", "WON", "TIMEFOLIO", "KIWOOM", "BNK", "TREX", "FOCUS",
    "HK", "KCGI", "마이다스", "파워", "히어로즈", "마이티", "UNICORN", "VITA",
})


def is_kr_etf_name(name: str) -> bool:
    """종목명 첫 토큰이 ETF/ETN 브랜드면 True. 빈 이름 False."""
    if not name:
        return False
    return name.split()[0] in KR_ETF_BRANDS


def _info_entry(symbol: str, info_cache: dict | None = None) -> dict | None:
    cache = info_cache
    if cache is None:
        try:
            blob = json.loads(Path(INFO_CACHE_PATH).read_text(encoding="utf-8"))
            cache = blob.get("data") if isinstance(blob.get("data"), dict) else blob
        except (OSError, ValueError, TypeError):
            return None
    if not isinstance(cache, dict):
        return None
    row = cache.get(symbol)
    if isinstance(row, dict) and "securityType" in row:
        return row
    # 캐시 래퍼: {symbol: {ts, info: {...}}}
    if isinstance(row, dict) and isinstance(row.get("info"), dict):
        return row["info"]
    return row if isinstance(row, dict) else None


def is_buy_ineligible(symbol: str, market: str = "KR", name: str = "",
                      *, info_cache: dict | None = None) -> tuple[bool, str]:
    """매수 풀에서 뺄 증권이면 (True, reason).

    1) stock_info 캐시 securityType ∈ BLOCKED_SECURITY_TYPES
    2) KR + 종목명 ETF 브랜드
    경고(warnings)·비ACTIVE 는 여기 안 봄 — 그건 주문 직전 가드 몫(시변).
    """
    info = _info_entry(symbol, info_cache)
    if info:
        sec = info.get("securityType")
        if sec in BLOCKED_SECURITY_TYPES:
            return True, f"부적격유형: {sec}"
    if (market or "KR").upper() == "KR" and is_kr_etf_name(name or ""):
        return True, "부적격유형: ETF(name)"
    return False, ""


def filter_universe(uni: dict, *, info_cache: dict | None = None,
                    log_drops: bool = True) -> dict:
    """{market: [items]} 에서 매수 불가 유형 제거. item 은 symbol/name 필요."""
    if not isinstance(uni, dict):
        return {}
    out: dict = {}
    n_drop = 0
    for market, items in uni.items():
        kept = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            sym = it.get("symbol") or ""
            name = it.get("name") or ""
            bad, reason = is_buy_ineligible(sym, market, name, info_cache=info_cache)
            if bad:
                n_drop += 1
                if log_drops:
                    log.info("[%s] 유니버스 제외 %s (%s)", market, sym, reason)
                continue
            kept.append(it)
        out[market] = kept
    if n_drop and log_drops:
        log.info("매수불가 증권 %d종목 유니버스에서 제외", n_drop)
    return out


def filter_candidates(cands: list[tuple[str, str, str]],
                      *, info_cache: dict | None = None) -> list[tuple[str, str, str]]:
    """발굴 후보 (market, symbol, name) 필터."""
    out = []
    for market, symbol, name in cands:
        bad, reason = is_buy_ineligible(symbol, market, name, info_cache=info_cache)
        if bad:
            log.info("[%s] 발굴 제외 %s (%s)", market, symbol, reason)
            continue
        out.append((market, symbol, name))
    return out


__all__ = [
    "KR_ETF_BRANDS", "is_kr_etf_name", "is_buy_ineligible",
    "filter_universe", "filter_candidates", "BLOCKED_SECURITY_TYPES",
]

"""후보 피처 온디맨드 보강 — market_state 에 없는 fundamentals/flows 패치.

갭반등 scan: -5% pre-filter 통과분 전량(상한 gap_enrich_max).
그 외 사이클: fundamentals 결측 후보를 pool 우선순위로 patch_missing_max 까지만.
"""
from __future__ import annotations

import os

from .logging_setup import get_logger

log = get_logger("src.candidate_enrich")

_POOL_PRIORITY = {"gap_decline": 0, "swing": 1, "day": 2}


def _sort_for_enrich(candidates: list[dict]) -> list[dict]:
    return sorted(
        candidates,
        key=lambda c: (
            _POOL_PRIORITY.get(str(c.get("pool") or ""), 9),
            str(c.get("symbol") or ""),
        ),
    )


def fetch_fundamentals_kr(symbols: list[str], *, api_key: str | None = None,
                          spacing_sec: float = 0.6) -> dict[str, dict]:
    """DART 단일회사 재무 — symbols 목록만. 키 없으면 {}."""
    key = (api_key or os.getenv("DART_API_KEY") or "").strip()
    syms = [str(s) for s in symbols if s and str(s).isdigit() and len(str(s)) == 6]
    if not key or not syms:
        return {}
    try:
        from .datasources.dart import DartSource
        from .datasources.base import SourceContext
        src = DartSource(key, syms, spacing_sec=spacing_sec)
        raw = src.fetch(SourceContext(client=None, symbols_by_market={}, dry=False))
        funds = raw.get("fundamentals") or {}
        return {str(k): v for k, v in funds.items() if isinstance(v, dict)}
    except Exception as e:
        log.warning("온디맨드 fundamentals 실패: %s", e)
        return {}


def patch_fundamentals(candidates: list[dict], funds_by_sym: dict[str, dict]) -> int:
    n = 0
    for c in candidates:
        sym = str(c.get("symbol") or "")
        if sym and sym in funds_by_sym and funds_by_sym[sym]:
            c["fundamentals"] = funds_by_sym[sym]
            n += 1
    return n


def enrich_candidates(
    candidates: list[dict],
    market_state: dict | None,
    *,
    gap_scan: bool = False,
    enrich_fundamentals: bool = True,
    enrich_flows: bool = True,
    gap_enrich_max: int = 25,
    patch_missing_max: int = 5,
    dart_spacing_sec: float = 0.6,
) -> dict[str, int]:
    """assemble 이후 호출. 반환: {fundamentals, flows} 패치 건수."""
    from .agents.serve_policy import fetch_ondemand_flows, patch_candidate_flows

    stats = {"fundamentals": 0, "flows": 0}
    if not candidates:
        return stats

    ms = market_state or {}
    ms_funds = ms.get("fundamentals") or {}

    need_f: list[str] = []
    for c in candidates:
        sym = str(c.get("symbol") or "")
        if not sym or c.get("market", "KR") != "KR":
            continue
        if c.get("fundamentals") or ms_funds.get(sym):
            if not c.get("fundamentals") and ms_funds.get(sym):
                c["fundamentals"] = ms_funds[sym]
                stats["fundamentals"] += 1
            continue
        need_f.append(sym)

    if enrich_fundamentals and need_f:
        ordered = [c["symbol"] for c in _sort_for_enrich(
            [x for x in candidates if x.get("symbol") in need_f])]
        cap = gap_enrich_max if gap_scan else patch_missing_max
        to_fetch = ordered[:max(0, cap)]
        if to_fetch:
            got = fetch_fundamentals_kr(to_fetch, spacing_sec=dart_spacing_sec)
            stats["fundamentals"] += patch_fundamentals(candidates, got)

    if enrich_flows:
        need_flow = [
            str(c["symbol"]) for c in candidates
            if c.get("market", "KR") == "KR"
            and not c.get("flows")
            and not (ms.get("flows") or {}).get(c.get("symbol"))
        ]
        if gap_scan:
            flow_syms = need_flow[:gap_enrich_max]
        else:
            flow_syms = [
                c["symbol"] for c in _sort_for_enrich(candidates)
                if c.get("symbol") in need_flow
            ][:patch_missing_max]
        if flow_syms:
            try:
                flows = fetch_ondemand_flows(flow_syms)
                stats["flows"] += patch_candidate_flows(candidates, flows)
            except Exception as e:
                log.warning("온디맨드 flows 실패: %s", e)

    return stats

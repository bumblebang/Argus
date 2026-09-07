"""KR 밸류 재무 캐시 백필 — S0.

풀(Naver 시총) × DART fetch_financials → data/value_financials_cache.json.
corp_map 미스매치 리포트 + 시총분위 커버리지(분위별 n_min).

  python scripts/value_fin_backfill.py
  python scripts/value_fin_backfill.py --limit 50
  python scripts/value_fin_backfill.py --weekly   # miss-only (기본과 동일, 명시용)
  python scripts/value_fin_backfill.py --force    # 윈도우 연도 재조회
  argus value-fin-backfill
"""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Callable

from .config import ROOT
from .logging_setup import get_logger
from .value_scan import _FIN_CACHE_KEYS, _load_fin_cache, _save_fin_cache, _year_complete
from .value_score import DEFAULT_QUINTILE_N_MIN, quintile_coverage_report

log = get_logger("value_fin_backfill")

REPORT_PATH = ROOT / "data" / "value_fin_backfill_report.json"


def _years(today: date | None = None) -> tuple[int, int]:
    y0 = (today or date.today()).year - 1
    return y0, y0 - 1


def _fin_in_window(cache_corp: dict, years: tuple[int, ...]) -> dict | None:
    """_kr_fundamentals 와 같이 years 우선순위로 완전 엔트리 선택."""
    for y in years:
        ent = cache_corp.get(str(y))
        if _year_complete(ent):
            return ent
    return None


def build_pool_rows(
    *,
    pool: int = 600,
    fetch_ranking_fn: Callable | None = None,
) -> list[dict]:
    """KR KOSPI+KOSDAQ 시총 풀 — market_cap 포함(이미 discovery가 실음)."""
    if fetch_ranking_fn is None:
        from .datasources.discovery import fetch_ranking as fetch_ranking_fn
    from .value_scan import _kr_pool
    return _kr_pool(fetch_ranking_fn, pool)


def backfill_financials(
    api_key: str,
    pool_rows: list[dict],
    *,
    corp_map: dict[str, str] | None = None,
    cache: dict | None = None,
    years: tuple[int, ...] | None = None,
    weekly: bool = False,
    force: bool = False,
    sleep_s: float = 0.05,
    n_min: int = DEFAULT_QUINTILE_N_MIN,
    fetch_fn: Callable | None = None,
    load_corp_map_fn: Callable | None = None,
    now_fn: Callable[[], float] = time.time,
) -> dict:
    """풀 종목에 대해 DART 연간 재무를 캐시에 채운다.

    기본·weekly: 윈도우 연도 중 불완전/결측만 조회.
    force: 윈도우 연도 전부 재조회(덮어쓰기).
    """
    if load_corp_map_fn is None:
        from .datasources.dart import load_corp_map as load_corp_map_fn
    if fetch_fn is None:
        from .datasources.dart import fetch_financials as fetch_fn
    if corp_map is None:
        corp_map = load_corp_map_fn(api_key)
    if cache is None:
        cache = _load_fin_cache()
    if years is None:
        years = _years()

    miss: list[dict] = []
    fetched = 0
    skipped = 0
    errors = 0
    calls = 0
    hard_miss = 0

    for row in pool_rows:
        sym = row.get("symbol")
        if not sym:
            continue
        corp = corp_map.get(sym)
        if not corp:
            miss.append({
                "symbol": sym,
                "name": row.get("name"),
                "market_cap": row.get("market_cap"),
                "reason": "corp_map_miss",
            })
            continue
        ce = cache.get(corp) or {}
        if force:
            need_years = list(years)
        else:
            need_years = [y for y in years if not _year_complete(ce.get(str(y)))]
        if not need_years:
            skipped += 1
            continue

        got_any = False
        for y in need_years:
            try:
                calls += 1
                got = fetch_fn(api_key, corp, y)
                if sleep_s > 0:
                    time.sleep(sleep_s)
            except Exception as e:
                errors += 1
                log.debug("[backfill][%s] %s 실패: %s", sym, y, e)
                continue
            if got:
                if corp not in cache:
                    cache[corp] = {}
                cache[corp][str(y)] = {k: got.get(k) for k in _FIN_CACHE_KEYS}
                fetched += 1
                got_any = True
        if not got_any:
            hard_miss += 1

    _save_fin_cache(cache)

    cov_rows = []
    for row in pool_rows:
        sym = row.get("symbol")
        corp = corp_map.get(sym or "")
        mcap = row.get("market_cap")
        fund: dict = {}
        if corp and corp in cache and mcap and mcap > 0:
            fin = _fin_in_window(cache[corp], years)
            if fin:
                eq, ni = fin.get("equity"), fin.get("net_income")
                if eq and eq > 0:
                    fund["pb"] = round(mcap / eq, 2)
                if ni and ni > 0:
                    fund["pe_trailing"] = round(mcap / ni, 2)
                if eq and eq > 0 and fin.get("total_liabilities") is not None:
                    fund["debt_ratio"] = round(fin["total_liabilities"] / eq, 4)
                fund["roe"] = (round(ni / eq, 4)
                               if (eq and eq > 0 and ni is not None) else None)
        cov_rows.append({
            "symbol": sym,
            "market_cap": mcap,
            "fundamentals": fund,
        })
    coverage = quintile_coverage_report(cov_rows, n_min=n_min)

    summary = {
        "ts": now_fn(),
        "pool": len(pool_rows),
        "years": list(years),
        "weekly": weekly,
        "force": force,
        "corp_map_size": len(corp_map),
        "corp_map_miss": len(miss),
        "corp_map_miss_rate": round(len(miss) / len(pool_rows), 4) if pool_rows else 0.0,
        "fetched_year_entries": fetched,
        "skipped_complete": skipped,
        "hard_miss_symbols": hard_miss,
        "dart_calls": calls,
        "errors": errors,
        "cache_corps": len(cache),
        "n_min": n_min,
        "coverage": coverage,
        "miss_sample": miss[:30],
    }
    log.info("[backfill] %s", {k: summary[k] for k in (
        "pool", "corp_map_miss", "fetched_year_entries", "skipped_complete",
        "dart_calls", "errors", "hard_miss_symbols")})
    return summary


def save_report(summary: dict, path: Path = REPORT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def last_backfill_ts(path: Path = REPORT_PATH) -> float | None:
    """마지막 백필 리포트 시각. 없거나 깨졌으면 None(=즉시 대상)."""
    try:
        if not path.exists():
            return None
        return float(json.loads(path.read_text(encoding="utf-8")).get("ts") or 0) or None
    except (OSError, ValueError, TypeError):
        return None


def maybe_weekly_backfill(
    cfg,
    *,
    now: float | None = None,
    report_path: Path = REPORT_PATH,
    api_key: str | None = None,
    build_pool_rows_fn: Callable | None = None,
    backfill_fn: Callable | None = None,
) -> dict:
    """스캔 주기에 얹는 주간 백필 — 마지막 리포트가 N일보다 오래됐을 때만 실행.

    캐시가 이미 채워져 있으면 miss-only 라 DART 호출이 수십 건 수준이다(리포트의
    skipped_complete 참조). 이게 없으면 시총 풀에 새로 편입된 종목이 계속 재무 결측
    상태로 남아 ValueFactor 가 중립에 머문다.

    반환 {"ran": bool, "why": str, ...}. 예외는 호출측(run_scan)이 삼킨다.
    """
    import os

    now = time.time() if now is None else float(now)
    vcfg = (getattr(cfg, "raw", None) or {}).get("value_scan") or {}
    interval_days = float(vcfg.get("fin_backfill_days", 7) or 0)
    if interval_days <= 0:
        return {"ran": False, "why": "disabled"}
    if "KR" not in (vcfg.get("markets") or ["KR"]):
        return {"ran": False, "why": "no_kr"}
    api_key = api_key if api_key is not None else os.getenv("DART_API_KEY", "")
    if not api_key:
        return {"ran": False, "why": "no_dart_key"}

    last = last_backfill_ts(report_path)
    if last is not None and (now - last) < interval_days * 86400:
        return {"ran": False, "why": "fresh",
                "age_days": round((now - last) / 86400, 2)}

    pool_fn = build_pool_rows_fn or build_pool_rows
    run_fn = backfill_fn or backfill_financials
    pool_rows = pool_fn(pool=int(vcfg.get("pool", 600)))
    if not pool_rows:
        return {"ran": False, "why": "empty_pool"}
    summary = run_fn(api_key, pool_rows, weekly=True,
                     n_min=int((vcfg.get("value_score") or {})
                               .get("quintile_n_min", DEFAULT_QUINTILE_N_MIN)))
    save_report(summary, report_path)
    log.info("[backfill] 주간 자동 실행 — calls=%s fetched=%s coverage_ok=%s",
             summary.get("dart_calls"), summary.get("fetched_year_entries"),
             (summary.get("coverage") or {}).get("ok"))
    return {"ran": True, "why": "stale", "summary": summary}

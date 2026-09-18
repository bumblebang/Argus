"""US 밸류 품질지표 miss-only 백필 — Finnhub basic financials.

watchlist US 항목에 roe/debt_ratio 등이 없으면 QualityTilt 가 0으로 남는다.
LLM 갱신 경로에만 붙어 있어 커버리지가 낮으므로, KR DART 백필과 대칭으로
지도에 직접 miss-only 채운다(캐시 파일 없음 — watchlist 가 SSOT).

  python scripts/value_us_quality_backfill.py
  python scripts/value_us_quality_backfill.py --limit 50
  python scripts/value_us_quality_backfill.py --force
  argus value-us-quality-backfill
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from .config import ROOT
from .logging_setup import get_logger

log = get_logger("value_us_quality_backfill")

REPORT_PATH = ROOT / "data" / "value_us_quality_backfill_report.json"
WATCHLIST_DEFAULT = ROOT / "data" / "value_watchlist.json"


def needs_us_quality(entry: dict | None, *, force: bool = False) -> bool:
    """US 이고 Finnhub 품질이 없거나 force 면 True."""
    if not isinstance(entry, dict) or entry.get("market") != "US":
        return False
    if force:
        return True
    fund = entry.get("fundamentals") if isinstance(entry.get("fundamentals"), dict) else {}
    return fund.get("quality_source") != "finnhub"


def list_us_quality_misses(
    watchlist: dict,
    *,
    force: bool = False,
    limit: int | None = None,
) -> list[str]:
    """백필 대상 심볼(삽입 순 유지). limit 이 있으면 앞에서 자른다."""
    out: list[str] = []
    for sym, entry in (watchlist or {}).items():
        if needs_us_quality(entry, force=force):
            out.append(sym)
            if limit is not None and len(out) >= max(0, int(limit)):
                break
    return out


def backfill_us_quality(
    api_key: str,
    watchlist: dict,
    *,
    force: bool = False,
    limit: int | None = None,
    sleep_s: float = 1.05,
    fetch_fn: Callable | None = None,
    now_fn: Callable[[], float] = time.time,
    annotate: bool = True,
    annotate_fn: Callable | None = None,
) -> dict:
    """miss-only(또는 force)로 Finnhub 품질을 fundamentals 에 병합.

    annotate=True 이면 시장별 composite_value 를 다시 병기(호출측이 save).
    """
    if fetch_fn is None:
        from .datasources.finnhub import fetch_basic_financials as fetch_fn

    targets = list_us_quality_misses(watchlist, force=force, limit=limit)
    fetched = 0
    empty = 0
    errors = 0
    calls = 0
    updated: list[str] = []

    for i, sym in enumerate(targets):
        entry = watchlist.get(sym) or {}
        try:
            calls += 1
            extra = fetch_fn(api_key, sym)
        except Exception as e:
            errors += 1
            log.debug("[us_quality][%s] 실패: %s", sym, e)
            extra = None
        if sleep_s > 0 and i < len(targets) - 1:
            time.sleep(sleep_s)
        if not extra:
            empty += 1
            continue
        fund = dict(entry.get("fundamentals") or {})
        fund.update(extra)
        entry = dict(entry)
        entry["fundamentals"] = fund
        watchlist[sym] = entry
        fetched += 1
        updated.append(sym)

    if annotate and fetched:
        if annotate_fn is None:
            from .value_ops import annotate_watchlist_scores
            annotate_fn = annotate_watchlist_scores
        try:
            annotate_fn(watchlist, now=now_fn())
        except Exception as e:
            log.warning("[us_quality] Score 재병기 실패(데이터는 유지): %s", e)

    us_n = sum(1 for e in watchlist.values()
               if isinstance(e, dict) and e.get("market") == "US")
    covered = sum(
        1 for e in watchlist.values()
        if isinstance(e, dict) and e.get("market") == "US"
        and isinstance(e.get("fundamentals"), dict)
        and e["fundamentals"].get("quality_source") == "finnhub"
    )
    summary = {
        "ts": now_fn(),
        "force": force,
        "limit": limit,
        "targets": len(targets),
        "finnhub_calls": calls,
        "fetched": fetched,
        "empty": empty,
        "errors": errors,
        "us_total": us_n,
        "us_with_quality": covered,
        "coverage_pct": round(100.0 * covered / us_n, 1) if us_n else 0.0,
        "updated_sample": updated[:30],
    }
    log.info("[us_quality] %s", {k: summary[k] for k in (
        "targets", "finnhub_calls", "fetched", "empty", "errors",
        "us_with_quality", "coverage_pct")})
    return summary


def save_report(summary: dict, path: Path = REPORT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def last_backfill_ts(path: Path = REPORT_PATH) -> float | None:
    try:
        if not path.exists():
            return None
        return float(json.loads(path.read_text(encoding="utf-8")).get("ts") or 0) or None
    except (OSError, ValueError, TypeError):
        return None


def maybe_us_quality_backfill(
    cfg,
    *,
    watchlist: dict | None = None,
    watchlist_path: Path | str | None = None,
    now: float | None = None,
    report_path: Path = REPORT_PATH,
    api_key: str | None = None,
    backfill_fn: Callable | None = None,
    load_fn: Callable | None = None,
    save_fn: Callable | None = None,
) -> dict:
    """스캔 주기에 얹는 US 품질 백필 — 리포트가 N일보다 오래됐을 때만.

    한 번에 전량을 채우지 않고 max_per_run 상한(무료 티어 pacing).
    반환 {"ran": bool, "why": str, ...}.
    """
    import os

    now = time.time() if now is None else float(now)
    vcfg = (getattr(cfg, "raw", None) or {}).get("value_scan") or {}
    interval_days = float(vcfg.get("us_quality_backfill_days", 7) or 0)
    if interval_days <= 0:
        return {"ran": False, "why": "disabled"}
    if "US" not in (vcfg.get("markets") or []):
        return {"ran": False, "why": "no_us"}
    api_key = api_key if api_key is not None else os.getenv("FINNHUB_API_KEY", "")
    if not api_key:
        return {"ran": False, "why": "no_finnhub_key"}

    last = last_backfill_ts(report_path)
    if last is not None and (now - last) < interval_days * 86400:
        return {"ran": False, "why": "fresh",
                "age_days": round((now - last) / 86400, 2)}

    if load_fn is None:
        from .value_scan import load_watchlist as load_fn
    if save_fn is None:
        from .value_scan import save_watchlist as save_fn
    path = Path(watchlist_path) if watchlist_path else WATCHLIST_DEFAULT
    wl = watchlist if watchlist is not None else load_fn(path)
    if not wl:
        return {"ran": False, "why": "empty_watchlist"}

    max_n = int(vcfg.get("us_quality_backfill_max", 80) or 0)
    if max_n <= 0:
        return {"ran": False, "why": "max_zero"}
    sleep_s = float(vcfg.get("us_quality_backfill_sleep_s", 1.05) or 0)

    run_fn = backfill_fn or backfill_us_quality
    summary = run_fn(
        api_key, wl, force=False, limit=max_n, sleep_s=sleep_s, now_fn=lambda: now)
    save_fn(wl, path)
    save_report(summary, report_path)
    log.info("[us_quality] 자동 실행 — calls=%s fetched=%s coverage=%s%%",
             summary.get("finnhub_calls"), summary.get("fetched"),
             summary.get("coverage_pct"))
    return {"ran": True, "why": "stale", "summary": summary}

"""value_us_quality_backfill CLI — US Finnhub 품질 miss-only.

  python scripts/value_us_quality_backfill.py
  python scripts/value_us_quality_backfill.py --limit 40
  python scripts/value_us_quality_backfill.py --force
  argus value-us-quality-backfill
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from src.config import load_config
from src.logging_setup import setup_logging, get_logger
from src.value_scan import load_watchlist, save_watchlist, _vcfg, WATCHLIST
from src.value_us_quality_backfill import (
    REPORT_PATH, backfill_us_quality, save_report,
)

log = get_logger("value_us_quality_backfill.cli")


def main() -> int:
    ap = argparse.ArgumentParser(description="Finnhub → value_watchlist US 품질 백필")
    ap.add_argument("--limit", type=int, default=None,
                    help="이번 실행 최대 종목(기본=결측 전량)")
    ap.add_argument("--force", action="store_true",
                    help="이미 finnhub 품질 있는 US도 재조회")
    ap.add_argument("--sleep", type=float, default=None,
                    help="콜 간격 초(기본 config 또는 1.05)")
    ap.add_argument("--dry", action="store_true",
                    help="조회 없이 결측 목록만 리포트")
    args = ap.parse_args()
    setup_logging("INFO", log_file="value_us_quality_backfill.log")

    cfg = load_config()
    vcfg = _vcfg(cfg)
    sleep_s = (args.sleep if args.sleep is not None
               else float(vcfg.get("us_quality_backfill_sleep_s", 1.05) or 0))

    key = os.getenv("FINNHUB_API_KEY", "")
    if not key and not args.dry:
        log.error("FINNHUB_API_KEY 없음")
        return 2

    wl = load_watchlist(WATCHLIST)
    us_n = sum(1 for e in wl.values()
               if isinstance(e, dict) and e.get("market") == "US")
    log.info("watchlist US %d / 전체 %d", us_n, len(wl))

    if args.dry:
        from src.value_us_quality_backfill import list_us_quality_misses
        misses = list_us_quality_misses(wl, force=args.force, limit=args.limit)
        summary = {
            "dry": True,
            "force": args.force,
            "us_total": us_n,
            "misses": len(misses),
            "miss_sample": misses[:40],
        }
        save_report(summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    summary = backfill_us_quality(
        key, wl, force=args.force, limit=args.limit, sleep_s=sleep_s)
    save_watchlist(wl, WATCHLIST)
    save_report(summary)
    printable = {k: v for k, v in summary.items() if k != "updated_sample"}
    printable["updated_sample_n"] = len(summary.get("updated_sample") or [])
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    log.info("리포트 %s · coverage %s%%", REPORT_PATH, summary.get("coverage_pct"))
    return 0 if summary.get("errors", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""value_fin_backfill CLI — S0.

  python scripts/value_fin_backfill.py
  python scripts/value_fin_backfill.py --limit 30
  python scripts/value_fin_backfill.py --weekly
  python scripts/value_fin_backfill.py --force
  argus value-fin-backfill
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# .env 로드(있으면) — DART_API_KEY
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from src.logging_setup import setup_logging, get_logger
from src.value_fin_backfill import (
    REPORT_PATH, backfill_financials, build_pool_rows, save_report,
)
from src.value_scan import _vcfg
from src.value_score import DEFAULT_QUINTILE_N_MIN
from src.config import load_config

log = get_logger("value_fin_backfill.cli")


def main() -> int:
    ap = argparse.ArgumentParser(description="DART → value_financials_cache 백필")
    ap.add_argument("--pool", type=int, default=None, help="시총 풀 크기(기본 config)")
    ap.add_argument("--limit", type=int, default=None, help="풀 상위 N종만(프로빙)")
    ap.add_argument("--weekly", action="store_true",
                    help="miss-only(기본과 동일·명시용). 불완전 연도만 조회")
    ap.add_argument("--force", action="store_true",
                    help="윈도우 연도(전년·전전년) 전부 재조회")
    ap.add_argument("--sleep", type=float, default=0.05, help="DART 콜 간 초")
    ap.add_argument("--dry-pool", action="store_true",
                    help="DART 재무 없이 풀·corp_map 미스만 리포트")
    args = ap.parse_args()
    setup_logging("INFO", log_file="value_fin_backfill.log")

    cfg = load_config()
    vcfg = _vcfg(cfg)
    pool_n = args.pool if args.pool is not None else int(vcfg.get("pool", 600))
    vs = (cfg.raw.get("value_scan") or {}).get("value_score") or {}
    n_min = int(vs.get("quintile_n_min", DEFAULT_QUINTILE_N_MIN))

    dart_key = os.getenv("DART_API_KEY", "")
    if not dart_key and not args.dry_pool:
        log.error("DART_API_KEY 없음")
        return 2

    log.info("풀 발굴 pool=%d …", pool_n)
    rows = build_pool_rows(pool=pool_n)
    if args.limit is not None:
        rows = rows[: max(0, args.limit)]
    log.info("풀 %d종", len(rows))

    if args.dry_pool:
        from src.datasources.dart import load_corp_map
        if not dart_key:
            log.error("dry-pool 도 corp_map 용 DART_API_KEY 필요")
            return 2
        cmap = load_corp_map(dart_key)
        miss = [r for r in rows if r.get("symbol") not in cmap]
        summary = {
            "dry_pool": True,
            "pool": len(rows),
            "corp_map_size": len(cmap),
            "corp_map_miss": len(miss),
            "corp_map_miss_rate": round(len(miss) / len(rows), 4) if rows else 0,
            "miss_sample": [{"symbol": r.get("symbol"), "name": r.get("name")}
                            for r in miss[:40]],
        }
        save_report(summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        log.info("리포트 %s", REPORT_PATH)
        return 0

    summary = backfill_financials(
        dart_key, rows, weekly=args.weekly, force=args.force,
        sleep_s=args.sleep, n_min=n_min)
    save_report(summary)
    printable = {k: v for k, v in summary.items() if k != "miss_sample"}
    printable["miss_sample_n"] = len(summary.get("miss_sample") or [])
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    log.info("리포트 %s", REPORT_PATH)
    return 0 if summary.get("coverage", {}).get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

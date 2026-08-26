"""그림자 장부 채점/백필 배치.

  python scripts/score_shadow_ledger.py
  python scripts/score_shadow_ledger.py --stats
  python scripts/score_shadow_ledger.py --backfill
  python scripts/score_shadow_ledger.py --backfill --limit 500
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import load_config
from src.engine.store import Store
from src.logging_setup import setup_logging
from src.shadow_ledger import (backfill_from_jsonl, score_open_shadows,
                               shadow_stats)
from src import paths as _paths

DATA = ROOT / "data"


def main() -> None:
    setup_logging()
    ap = argparse.ArgumentParser(description="Shadow ledger score / backfill")
    ap.add_argument("--stats", action="store_true", help="skip scoring, print stats only")
    ap.add_argument("--backfill", action="store_true",
                    help="replay decisions.jsonl (+ value) into shadow_positions")
    ap.add_argument("--limit", type=int, default=None,
                    help="backfill max journal lines per file")
    ap.add_argument("--db", type=Path,
                    default=_paths.resolve("db", configured="data/bot.db"))
    args = ap.parse_args()

    cfg = load_config(ROOT / "config.yaml")
    # Path 오버라이드도 resolve 경유(레거시 상대경로면 dual-find)
    db = args.db
    if str(db).replace("\\", "/") in ("data/bot.db", "data/state/bot.db"):
        store = Store(_paths.resolve("db", configured=str(db)))
    else:
        store = Store(db)

    if args.backfill:
        brain_j = _paths.resolve("decisions", configured="data/decisions.jsonl")
        value_j = DATA / "value_decisions.jsonl"  # MIGRATE 밖(루트 유지)
        for path, sleeve in ((brain_j, "brain"), (value_j, "value")):
            if path.exists():
                bf = backfill_from_jsonl(
                    store, path, sleeve=sleeve, data_dir=DATA,
                    cfg=cfg.raw, limit=args.limit)
                print(json.dumps({"backfill": sleeve, **bf}, ensure_ascii=False))

    if not args.stats:
        result = score_open_shadows(store, data_dir=DATA, cfg=cfg.raw)
        print(json.dumps({"scored": result}, ensure_ascii=False))

    stats = shadow_stats(store, cfg=cfg.raw)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    ov = stats.get("overall") or {}
    from src.eval_protocol import apply_kill_rules
    apply_kill_rules(
        metrics={
            "shadow.avg_ret_pct": ov.get("avg_ret_pct"),
            "shadow.win_rate": ov.get("win_rate"),
            "shadow.avg_ret_pct__n": ov.get("n_scored"),
        },
        n=ov.get("n_scored"),
    )


if __name__ == "__main__":
    from src.cli.legacy import warn_legacy_script
    warn_legacy_script("argus shadow-score")
    main()

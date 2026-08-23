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

DATA = ROOT / "data"


def main() -> None:
    setup_logging()
    ap = argparse.ArgumentParser(description="Shadow ledger score / backfill")
    ap.add_argument("--stats", action="store_true", help="skip scoring, print stats only")
    ap.add_argument("--backfill", action="store_true",
                    help="replay decisions.jsonl (+ value) into shadow_positions")
    ap.add_argument("--limit", type=int, default=None,
                    help="backfill max journal lines per file")
    ap.add_argument("--db", type=Path, default=DATA / "bot.db")
    args = ap.parse_args()

    cfg = load_config(ROOT / "config.yaml")
    store = Store(args.db)

    if args.backfill:
        for path, sleeve in ((DATA / "decisions.jsonl", "brain"),
                             (DATA / "value_decisions.jsonl", "value")):
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


if __name__ == "__main__":
    main()

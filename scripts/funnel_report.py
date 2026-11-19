"""매수 퍼널·현금 활용률 리포트 — 현금을 왜 못 쓰는지 단계별로 본다(읽기 전용).

  python scripts/funnel_report.py              # 최근 30일
  python scripts/funnel_report.py --days 7
  python scripts/funnel_report.py --json       # 기계 판독용
  python scripts/funnel_report.py --out data/funnel_latest.json   # 정기 실행(측정 리포트)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import funnel  # noqa: E402
from src.config import load_config  # noqa: E402
from src.engine.store import Store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="매수 퍼널·현금 활용률")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--db", default=str(ROOT / "data" / "state" / "bot.db"))
    ap.add_argument("--value-funnel", default=None,
                    help="밸류 퍼널 jsonl(기본 data/value_funnel.jsonl)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default=None, help="JSON 결과 파일(정기 실행용)")
    args = ap.parse_args()
    try:
        cfg_capital = (load_config().risk or {}).get("capital") or {}
    except Exception:
        cfg_capital = {}
    store = Store(args.db, readonly=True)
    data = funnel.build(
        store, args.days, cfg_capital=cfg_capital,
        funnel_path=Path(args.value_funnel) if args.value_funnel else None)
    if args.out:
        out = Path(args.out)
        out = out if out.is_absolute() else ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"days": args.days, **data}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        print(funnel.format_report(data["brain"], data["value"], data["util"], args.days))
    return 0


if __name__ == "__main__":
    sys.exit(main())

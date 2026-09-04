"""배선 mismatch 리포트 (LIVE_OBSERVATION §C).

  python scripts/wiring_mismatch_report.py
  python scripts/wiring_mismatch_report.py --days 14 --threshold 3
  python scripts/wiring_mismatch_report.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import paths as _paths
from src.eval.wiring_mismatch import summarize_wiring


def _print_human(rep: dict) -> None:
    print("\n=== 배선 mismatch (Tier 0) ===")
    print(f"기준: {rep.get('asof')}  window={rep.get('window_days')}d  "
          f"threshold={rep.get('threshold')}")
    print(f"BUY proposals: {rep.get('buy_n')}  mismatch events: {rep.get('mismatch_n')}  "
          f"missing_context: {rep.get('missing_context')}")
    by = rep.get("by_kind") or {}
    if not by:
        print("종류별: (없음)")
    else:
        print("종류별:")
        for k, n in sorted(by.items(), key=lambda x: -x[1]):
            flag = " **FLAG**" if k in (rep.get("flagged_kinds") or {}) else ""
            print(f"  {k}: {n}{flag}")
    if rep.get("actionable"):
        print(f"\nactionable=YES - {list((rep.get('flagged_kinds') or {}).keys())}")
        print("-> LIVE_OBS: wiring review (not promote)")
    else:
        print("\nactionable=NO - keep observing (below threshold)")
    ex = rep.get("examples") or {}
    for kind, rows in ex.items():
        if kind not in (rep.get("flagged_kinds") or {}) and kind not in by:
            continue
        print(f"\nexamples [{kind}]:")
        for r in rows[:3]:
            print(f"  {r.get('symbol')} strat={r.get('strategy')} fit={r.get('fit_best')} "
                  f"hz={r.get('horizon')} pool={r.get('pool')} | {r.get('detail')}")
    note = (rep.get("note") or "").replace("—", "-").replace("–", "-")
    print(f"\n{note}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="배선 mismatch 주간 카운터")
    ap.add_argument("--days", type=float, default=14.0)
    ap.add_argument("--threshold", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", type=Path, help="JSON 저장")
    ap.add_argument("--journal", type=Path, default=None)
    args = ap.parse_args()

    journal = args.journal
    if journal is None:
        journal = _paths.resolve("decisions", configured="data/ledgers/decisions.jsonl")

    rep = summarize_wiring(
        journal, window_days=args.days, threshold=args.threshold)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"저장: {args.out}")

    if args.json:
        # examples 만 남기고 전체
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    elif not args.out:
        _print_human(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

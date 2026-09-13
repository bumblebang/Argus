"""관측용 events 만료분 삭제(기본 7일) + 선택 VACUUM.

데몬은 `loop._maybe_prune_events` 가 주기적으로 같은 일을 한다(watch.events_*).
이 스크립트는 **이미 쌓인 분량의 일회성 정리**와 디스크 회수(VACUUM)용.

기본 대상 kind = `Store.PRUNABLE_EVENT_KINDS` (athena_queue·athena_scan).
원장·귀속이 읽는 kind(decision·fill·live_order…)는 건드리지 않는다.

  python scripts/prune_events.py --stats                  # 삭제 없이 kind별 분포
  python scripts/prune_events.py --retain-days 7 --vacuum # 정리 + 디스크 회수
  python scripts/prune_events.py --kinds athena_queue athena_scan precision
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.engine.store import Store  # noqa: E402


def _fmt_size(path: Path) -> str:
    try:
        n = path.stat().st_size
    except OSError:
        return "?"
    return f"{n / (1024 ** 3):.2f}GB" if n >= 1024 ** 3 else f"{n / (1024 ** 2):.0f}MB"


def _stats(store: Store, cutoff: float) -> list[tuple]:
    with store._lock:
        return store.conn.execute(
            "SELECT kind, COUNT(*) n, SUM(ts < ?) expired,"
            " CAST(COALESCE(SUM(LENGTH(payload)), 0) AS INTEGER) bytes"
            " FROM events GROUP BY kind ORDER BY n DESC",
            (cutoff,)).fetchall()


def main() -> int:
    ap = argparse.ArgumentParser(description="관측용 events 만료 prune")
    ap.add_argument("--db", default=str(ROOT / "data" / "state" / "bot.db"))
    ap.add_argument("--retain-days", type=float, default=7.0, help="보존 일수(기본 7)")
    ap.add_argument("--kinds", nargs="*", default=None,
                    help=f"대상 kind (기본 {' '.join(Store.PRUNABLE_EVENT_KINDS)})")
    ap.add_argument("--batch", type=int, default=50_000, help="배치당 DELETE 행 수")
    ap.add_argument("--stats", action="store_true", help="삭제 없이 kind별 분포만")
    ap.add_argument("--vacuum", action="store_true",
                    help="끝나면 VACUUM(디스크 회수, watch 중지 후)")
    args = ap.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"DB 없음: {path}", file=sys.stderr)
        return 1

    retain = max(60.0, float(args.retain_days) * 86400.0)
    cutoff = time.time() - retain
    kinds = args.kinds if args.kinds else list(Store.PRUNABLE_EVENT_KINDS)
    store = Store(path)
    print(f"db={path} size={_fmt_size(path)} retain={args.retain_days}d "
          f"kinds={','.join(kinds)}")

    rows = _stats(store, cutoff)
    print(f"\n{'kind':<26}{'rows':>10}{'expired':>10}{'payload':>10}")
    for r in rows[:20]:
        mark = " *" if r["kind"] in kinds else ""
        print(f"{r['kind']:<26}{r['n']:>10,}{int(r['expired'] or 0):>10,}"
              f"{r['bytes'] / 1e6:>9.0f}M{mark}")
    print(f"{'(총)':<26}{sum(r['n'] for r in rows):>10,}")
    if args.stats:
        print("\n* = 이번 대상. 삭제는 --stats 없이 실행.")
        return 0

    t0 = time.time()
    total = 0
    while True:
        info = store.prune_events(kinds=kinds, older_than_sec=retain,
                                  batch_limit=int(args.batch), max_batches=20)
        deleted = int(info.get("deleted") or 0)
        total += deleted
        print(f"  batch deleted={deleted:,} total={total:,} done={info.get('done')}")
        if info.get("done"):
            break
    print(f"done deleted={total:,} elapsed={time.time() - t0:.1f}s")

    if args.vacuum:
        print(f"VACUUM 시작(size={_fmt_size(path)})…")
        t1 = time.time()
        store.conn.close()
        conn = sqlite3.connect(str(path), timeout=600)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
        print(f"VACUUM 완료 elapsed={time.time() - t1:.1f}s size={_fmt_size(path)}")
    else:
        print("파일 크기 회수는 --vacuum (watch 중지 후 권장)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

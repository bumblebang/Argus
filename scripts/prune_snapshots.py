"""snapshots 만료분 일괄 삭제(기본 24h).

watch 루프도 시간당 배치 prune 하지만, bot.db 가 이미 억 단위면
장외에서 이 스크립트로 먼저 깎는 편이 낫다.

  python scripts/prune_snapshots.py
  python scripts/prune_snapshots.py --retain-sec 86400 --batch 500000
  python scripts/prune_snapshots.py --vacuum   # 파일 크기 회수(장중 금지, 수 분~수십 분)

VACUUM 없이 DELETE 만 하면 파일 크기는 그대로일 수 있다(여유 페이지만 늘어남).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.engine.store import Store  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="snapshots 만료 prune")
    ap.add_argument("--db", default=str(ROOT / "data" / "state" / "bot.db"))
    ap.add_argument("--retain-sec", type=float, default=86400.0,
                    help="보존 초(기본 24h)")
    ap.add_argument("--batch", type=int, default=500_000,
                    help="배치당 DELETE 행 수")
    ap.add_argument("--vacuum", action="store_true",
                    help="끝나면 VACUUM(디스크 회수, 장중 금지)")
    ap.add_argument("--dry-count", action="store_true",
                    help="삭제 없이 만료 행 수만 출력")
    args = ap.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"DB 없음: {path}", file=sys.stderr)
        return 1

    store = Store(path)
    retain = max(60.0, float(args.retain_sec))
    cutoff = time.time() - retain
    with store._lock:
        n_old = store.conn.execute(
            "SELECT COUNT(*) FROM snapshots WHERE ts < ?", (cutoff,)
        ).fetchone()[0]
        n_all = store.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    print(f"db={path} snapshots={n_all:,} expired(<{retain:.0f}s)={n_old:,}")
    if args.dry_count:
        return 0

    total = 0
    t0 = time.time()
    while True:
        info = store.prune_snapshots(
            older_than_sec=retain,
            batch_limit=int(args.batch),
        )
        deleted = int(info.get("deleted") or 0)
        total += deleted
        print(f"  batch deleted={deleted:,} total={total:,} done={info.get('done')}")
        if info.get("done"):
            break
        # prune_snapshots 는 호출당 max_batches=5 — 바로 이어서.
    print(f"done deleted={total:,} elapsed={time.time() - t0:.1f}s")

    if args.vacuum:
        print("VACUUM 시작(오래 걸림)…")
        t1 = time.time()
        with store._lock:
            store.conn.execute("VACUUM")
        print(f"VACUUM 완료 elapsed={time.time() - t1:.1f}s")
    else:
        print("파일 크기 회수는 --vacuum (장외 권장)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""snapshots 만료분 삭제(기본 24h) + 선택 VACUUM.

억 단위면 배치 DELETE 대신 **최근분만 새 테이블로 옮기고 DROP** 이 빠르다
(COUNT(*) 전수 스캔은 안 함).

  python scripts/prune_snapshots.py --vacuum
  python scripts/prune_snapshots.py --retain-sec 86400 --batch 500000   # 소량용 배치
  python scripts/prune_snapshots.py --dry-count   # 느림(전수 COUNT) — 진단용만
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


def _fmt_gb(path: Path) -> str:
    try:
        return f"{path.stat().st_size / (1024 ** 3):.2f}GB"
    except OSError:
        return "?"


def _rebuild_keep(db: Path, *, cutoff: float) -> dict:
    """ts >= cutoff 만 남기고 테이블 재구축. 반환 {kept, elapsed}."""
    t0 = time.time()
    # Store 경유 시 거대 테이블에 idx_snapshots_ts 생성이 먼저 돌 수 있어 직접 연결.
    conn = sqlite3.connect(str(db), timeout=600)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=600000")
        conn.execute("DROP TABLE IF EXISTS snapshots_keep")
        conn.execute(
            "CREATE TABLE snapshots_keep ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  ts REAL NOT NULL,"
            "  symbol TEXT NOT NULL,"
            "  price REAL,"
            "  payload TEXT"
            ")"
        )
        cur = conn.execute(
            "INSERT INTO snapshots_keep(ts, symbol, price, payload) "
            "SELECT ts, symbol, price, payload FROM snapshots WHERE ts >= ?",
            (cutoff,),
        )
        kept = int(cur.rowcount or 0)
        conn.execute("DROP TABLE snapshots")
        conn.execute("ALTER TABLE snapshots_keep RENAME TO snapshots")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_ts "
            "ON snapshots(symbol, ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts)"
        )
        conn.commit()
    finally:
        conn.close()
    return {"kept": kept, "elapsed": time.time() - t0}


def main() -> int:
    ap = argparse.ArgumentParser(description="snapshots 만료 prune")
    ap.add_argument("--db", default=str(ROOT / "data" / "state" / "bot.db"))
    ap.add_argument("--retain-sec", type=float, default=86400.0,
                    help="보존 초(기본 24h)")
    ap.add_argument("--batch", type=int, default=500_000,
                    help="--mode batch 일 때 배치당 DELETE 행 수")
    ap.add_argument(
        "--mode", choices=("rebuild", "batch"), default="rebuild",
        help="rebuild=최근분만 재구축(대용량 권장), batch=Store.prune_snapshots",
    )
    ap.add_argument("--vacuum", action="store_true",
                    help="끝나면 VACUUM(디스크 회수, watch 중지 후)")
    ap.add_argument("--dry-count", action="store_true",
                    help="삭제 없이 만료 행 수만 출력(느림)")
    args = ap.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"DB 없음: {path}", file=sys.stderr)
        return 1

    retain = max(60.0, float(args.retain_sec))
    cutoff = time.time() - retain
    print(f"db={path} size={_fmt_gb(path)} retain={retain:.0f}s mode={args.mode}")

    if args.dry_count:
        store = Store(path)
        with store._lock:
            n_old = store.conn.execute(
                "SELECT COUNT(*) FROM snapshots WHERE ts < ?", (cutoff,)
            ).fetchone()[0]
            n_all = store.conn.execute(
                "SELECT COUNT(*) FROM snapshots"
            ).fetchone()[0]
        print(f"snapshots={n_all:,} expired={n_old:,}")
        return 0

    if args.mode == "rebuild":
        print(f"rebuild keep ts>={cutoff:.0f} …")
        info = _rebuild_keep(path, cutoff=cutoff)
        print(f"rebuild kept={info['kept']:,} elapsed={info['elapsed']:.1f}s "
              f"size_now={_fmt_gb(path)}")
    else:
        store = Store(path)
        total = 0
        t0 = time.time()
        while True:
            info = store.prune_snapshots(
                older_than_sec=retain,
                batch_limit=int(args.batch),
                max_batches=20,
            )
            deleted = int(info.get("deleted") or 0)
            total += deleted
            print(f"  batch deleted={deleted:,} total={total:,} done={info.get('done')}")
            if info.get("done"):
                break
        print(f"done deleted={total:,} elapsed={time.time() - t0:.1f}s")

    if args.vacuum:
        print(f"VACUUM 시작(size={_fmt_gb(path)})…")
        t1 = time.time()
        conn = sqlite3.connect(str(path), timeout=600)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
        print(f"VACUUM 완료 elapsed={time.time() - t1:.1f}s size={_fmt_gb(path)}")
    else:
        print("파일 크기 회수는 --vacuum (watch 중지 후 권장)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

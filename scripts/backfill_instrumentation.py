"""측정층 역채우기 — manager 스탬프 + 그림자 백필.

  python scripts/backfill_instrumentation.py
  python scripts/backfill_instrumentation.py --jsonl-only
  python scripts/backfill_instrumentation.py --db-only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.agents.manager_id import legacy_manager_stamp
from src.config import load_config
from src.engine.store import Store
from src.logging_setup import setup_logging
from src.shadow_ledger import backfill_from_jsonl

DATA = ROOT / "data"
LEGACY = legacy_manager_stamp()


def backfill_db_manager(store: Store) -> dict[str, int]:
    """decisions.payload 에 manager 없으면 legacy 스탬프."""
    out = {"updated": 0, "skipped": 0}
    with store._lock:
        rows = store.conn.execute(
            "SELECT id, payload FROM decisions WHERE payload IS NOT NULL"
        ).fetchall()
        for r in rows:
            try:
                pay = json.loads(r["payload"]) if r["payload"] else {}
            except (ValueError, TypeError):
                out["skipped"] += 1
                continue
            if pay.get("manager"):
                out["skipped"] += 1
                continue
            pay["manager"] = LEGACY
            store.conn.execute(
                "UPDATE decisions SET payload=? WHERE id=?",
                (json.dumps(pay, ensure_ascii=False), r["id"]))
            out["updated"] += 1
        store.conn.commit()
    return out


def backfill_jsonl_manager(path: Path) -> dict[str, int]:
    """decisions.jsonl 각 줄에 manager 없으면 legacy 추가(파일 덮어쓰기)."""
    out = {"updated": 0, "skipped": 0, "lines": 0}
    if not path.exists():
        return out
    lines_out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        out["lines"] += 1
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            lines_out.append(line)
            out["skipped"] += 1
            continue
        if rec.get("manager"):
            lines_out.append(line)
            out["skipped"] += 1
            continue
        rec["manager"] = LEGACY
        lines_out.append(json.dumps(rec, ensure_ascii=False))
        out["updated"] += 1
    if out["updated"]:
        path.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    return out


def main() -> None:
    setup_logging()
    ap = argparse.ArgumentParser(description="Instrumentation backfill")
    ap.add_argument("--db-only", action="store_true")
    ap.add_argument("--jsonl-only", action="store_true")
    ap.add_argument("--no-shadow", action="store_true", help="skip shadow backfill")
    ap.add_argument("--db", type=Path, default=DATA / "bot.db")
    args = ap.parse_args()

    cfg = load_config(ROOT / "config.yaml")
    store = Store(args.db)

    if not args.jsonl_only:
        db_r = backfill_db_manager(store)
        print(json.dumps({"db_manager": db_r}, ensure_ascii=False))

    if not args.db_only:
        for name in ("decisions.jsonl", "value_decisions.jsonl"):
            p = DATA / name
            jl = backfill_jsonl_manager(p)
            print(json.dumps({"jsonl_manager": name, **jl}, ensure_ascii=False))

    if not args.no_shadow:
        for path, sleeve in ((DATA / "decisions.jsonl", "brain"),
                             (DATA / "value_decisions.jsonl", "value")):
            if path.exists():
                bf = backfill_from_jsonl(
                    store, path, sleeve=sleeve, data_dir=DATA,
                    cfg=cfg.raw)
                print(json.dumps({"shadow_backfill": sleeve, **bf}, ensure_ascii=False))

    store.close()


if __name__ == "__main__":
    main()

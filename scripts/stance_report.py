"""판정 라벨 변별력 리포트 (Tier 0) — 도시에 stance · 밸류 stance 의 지수 대비 초과수익.
  python scripts/stance_report.py                    # 도시에 + 밸류 요약
  python scripts/stance_report.py --json
  python scripts/stance_report.py --backfill-value-log logs/value_scan.log
      # 이력 파일이 생기기 전(10-02 이전) 밸류 판정을 로그에서 복원해 이력에 합친다
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src import paths as _paths  # noqa: E402
from src.config import load_config  # noqa: E402
from src.engine.store import Store  # noqa: E402
from src.eval.stance_excess import (dossier_observations, load_value_history,  # noqa: E402
                                    stance_excess)
from src.shadow_ledger import KST  # noqa: E402
from src.value_scan import STANCE_HISTORY_NAME  # noqa: E402

_LOG_RE = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*\[value\]\[([^\]]+)\] (\w+) \(conv ([\d.]+)\)")


def backfill_value_log(log_path: Path, hist_path: Path) -> int:
    """value_scan.log 판정 줄 → 이력 jsonl. 이미 있는 (symbol, 초 단위 ts) 는 건너뛴다."""
    have: set[tuple[str, int]] = set()
    if hist_path.is_file():
        for line in hist_path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                have.add((str(r.get("symbol")), int(float(r.get("ts")))))
            except (ValueError, TypeError):
                continue
    rows = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _LOG_RE.match(line)
        if not m:
            continue
        ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST).timestamp()
        key = (m.group(2), int(ts))
        if key in have:
            continue
        have.add(key)
        rows.append({"ts": ts, "symbol": m.group(2), "stance": m.group(3),
                     "conviction": float(m.group(4)), "source": "log_backfill"})
    if rows:
        with open(hist_path, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)


def _print(title: str, sx: dict) -> None:
    print(f"\n=== {title} — 지수 대비 초과({sx.get('window_days')}일, 종목 에피소드) ===")
    by = sx.get("by_market") or {}
    if not by:
        print("  (성숙 표본 없음)")
    for mkt, labels in by.items():
        for label, b in labels.items():
            flag = " (small)" if b.get("small_sample") else ""
            print(f"  [{mkt}] {label:12s} n={b['n']:3d} 평균 {b['excess_avg_pp']:+6.2f}%p "
                  f"중앙 {b['excess_median_pp']:+6.2f}%p 이긴비율 {b['beat_rate']:.0%}{flag}")
    print(f"  skipped: {sx.get('skipped')}")


def main() -> int:
    ap = argparse.ArgumentParser(description="판정 라벨 변별력 리포트")
    ap.add_argument("--days", type=float, default=120.0, help="관측 윈도(일)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--backfill-value-log", type=Path, default=None)
    args = ap.parse_args()

    cfg = load_config(ROOT / "config.yaml")
    data_dir = ROOT / "data"
    hist_path = data_dir / STANCE_HISTORY_NAME
    if args.backfill_value_log:
        n = backfill_value_log(args.backfill_value_log, hist_path)
        print(f"밸류 판정 백필: {n}줄 추가 → {hist_path}")

    since = time.time() - args.days * 86400
    store = Store(_paths.resolve("db", configured="data/bot.db"))
    rep = {
        "dossier": stance_excess(dossier_observations(store, since=since),
                                 data_dir=data_dir, cfg=cfg.raw),
        "value": stance_excess(load_value_history(hist_path, since=since),
                               data_dir=data_dir, cfg=cfg.raw),
    }
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        _print("도시에 stance", rep["dossier"])
        _print("밸류 stance", rep["value"])
        print("\n좋은 라벨(bullish·undervalued)이 나머지보다 높아야 라벨이 정보를 담은 것. "
              "같은 기간 표본이라 국면 편중 주의 — 승격·튜닝 단독 근거 금지.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

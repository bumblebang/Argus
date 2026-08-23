"""매크로 이벤트 캘린더 배치 → data/macro_calendar.json.

  python scripts/macro_cal.py
  python scripts/macro_cal.py --dry

run_market_state.bat 에 earnings_cal 옆 편입. focus 가 이 파일을 읽어
FOMC/금통위 렌즈를 켠다. LLM·토스 호출 없음. 하드게이트 없음.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import ROOT
from src.logging_setup import setup_logging, get_logger
from src.datasources.macro_cal import build_calendar

OUT = ROOT / "data" / "macro_calendar.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    setup_logging("INFO")
    log = get_logger("macro.batch")

    if args.dry:
        cal = build_calendar(api_key="")  # curated only
        log.info("[dry] curated=%d events", len(cal["events"]))
    else:
        cal = build_calendar(api_key=os.getenv("FINNHUB_API_KEY"))
        log.info("macro_calendar: events=%d (curated=%d finnhub=%d)",
                 len(cal["events"]), cal.get("n_curated"), cal.get("n_finnhub"))

    # 임박 창 요약 로그
    near = [e for e in cal["events"]
            if isinstance(e.get("dday"), int) and -5 <= e["dday"] <= 1]
    for e in near:
        log.info("  임박: %s %s D%+d", e.get("label"), e.get("date"), e["dday"])

    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_name(OUT.name + ".tmp")
    tmp.write_text(json.dumps(cal, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(OUT)
    log.info("저장: %s", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())

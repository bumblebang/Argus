"""등록 실험 자동 채점 + 도시에 bullish 비중 하한 감시 — 평일 측정 작업에서 호출.
  python scripts/eval_experiments.py              # 채점·저장·(변화 시) ntfy
  python scripts/eval_experiments.py --no-push    # 푸시 없이
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src import paths as _paths  # noqa: E402
from src.agents.athena import ATHENA_PROMPT_REV  # noqa: E402
from src.config import load_config  # noqa: E402
from src.engine.store import Store  # noqa: E402
from src.eval.experiments import (alert_lines, bullish_floor_check,  # noqa: E402
                                  dedupe_daily, evaluate_experiments)

DATA = ROOT / "data"


def _push(title: str, message: str, cfg_raw: dict) -> bool:
    topic = ((os.getenv("NTFY_TOPIC") or "").strip()
             or str((cfg_raw.get("alerts") or {}).get("ntfy_topic") or "").strip())
    if not topic:
        return False
    try:
        import requests
        r = requests.post(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
                          headers={"Title": title}, timeout=5)
        return 200 <= int(r.status_code) < 300
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="등록 실험 자동 채점")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--out", type=Path, default=DATA / "eval_experiments_latest.json")
    args = ap.parse_args()

    cfg = load_config(ROOT / "config.yaml")
    store = Store(_paths.resolve("db", configured="data/bot.db"))
    result = evaluate_experiments(store=store, data_dir=DATA, cfg=cfg.raw,
                                  registry_path=DATA / "eval_registry.json")
    floor_hits = bullish_floor_check(store, cfg=cfg.raw, prompt_rev=ATHENA_PROMPT_REV)
    out = {**result, "bullish_floor": floor_hits}
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    for e in result["evaluated"]:
        print(f"[실험] {e['id']} status={e.get('status')} metrics={e['metrics']}")
    for h in floor_hits:
        print(f"[하한] {h}")

    lines = dedupe_daily(alert_lines(result, floor_hits),
                         DATA / "eval_experiments_push_state.json")
    if lines and not args.no_push:
        ok = _push("Argus eval", "\n".join(lines), cfg.raw)
        print(f"푸시 {'OK' if ok else '실패/토픽없음'}: {len(lines)}줄")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

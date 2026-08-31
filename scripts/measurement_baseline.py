"""측정층 기준선 스냅샷 — 정의를 바꾸기 전/후 숫자 이동을 증명한다.

  python scripts/measurement_baseline.py                 # tests/golden 에 기록
  python scripts/measurement_baseline.py --compare       # 기존 스냅샷과 차이만 출력

JUDGMENT_BACKLOG J9~J12 는 청산 정의·비용 가정을 바꾼다. 바꾸고 나면
"원래 얼마였는지" 를 되찾을 방법이 없으므로 착수 전에 동결한다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import paths as _paths
from src.attribution import strategy_stats
from src.calibration import conviction_calibration
from src.config import load_config
from src.engine.store import Store
from src.eval.dossier_quality import summarize_dossiers
from src.shadow_ledger import shadow_stats

DEFAULT_OUT = ROOT / "tests" / "golden" / "measurement_baseline.json"


def _postmortem_headline() -> dict:
    """저장된 gate_postmortem.json 의 헤드라인 수치만 발췌(재계산하지 않음)."""
    path = ROOT / "data" / "gate_postmortem.json"
    if not path.exists():
        return {"present": False}
    doc = json.loads(path.read_text(encoding="utf-8"))
    actual = doc.get("actual_closed") or {}
    cf_overall = ((doc.get("counterfactual") or {}).get("overall") or {})
    rows = actual.get("rows") or []
    return {
        "present": True,
        "asof": doc.get("asof"),
        "comparison_forbidden": bool(doc.get("comparison_forbidden")),
        "comparison_note": doc.get("comparison_note"),
        "actual_closed": {k: actual.get(k) for k in ("n", "wins", "win_rate", "definition")},
        "actual_rows_with_null_pnl": sum(1 for r in rows if r.get("pnl") is None),
        "counterfactual_overall": {
            h: {k: v.get(k) for k in ("n", "win_rate", "avg_ret_pct")}
            for h, v in cf_overall.items()
        },
        "counterfactual_definition": (doc.get("counterfactual") or {}).get("definition"),
    }


def collect(store, cfg: dict, since_days: float) -> dict:
    cal = conviction_calibration(store, since_days=since_days)
    sh = shadow_stats(store, since_days=since_days, cfg=cfg)
    strat = strategy_stats(store, since_days=since_days)
    ms_path = ROOT / "data" / "market_state.json"
    dq = summarize_dossiers(
        store, cfg=cfg, data_dir=ROOT / "data",
        market_state_path=ms_path if ms_path.exists() else None,
        label_days=since_days)
    return {
        "since_days": since_days,
        "dossier_quality": dq,
        "calibration": {
            "n": cal.get("n"),
            "calibrated": cal.get("calibrated"),
            "brier": cal.get("brier"),
            "by_bin": {k: {kk: v.get(kk) for kk in ("n", "hit_rate", "small_sample")}
                       for k, v in (cal.get("by_bin") or {}).items()},
        },
        "shadow": {
            "overall": sh.get("overall"),
            "by_bucket": sh.get("by_bucket"),
            "verifier_value_add": sh.get("verifier_value_add"),
        },
        "strategy_stats": strat,
        "gate_postmortem": _postmortem_headline(),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="측정층 기준선 스냅샷")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--since-days", type=float, default=90.0)
    ap.add_argument("--compare", action="store_true",
                    help="기록하지 않고 기존 스냅샷과 비교만")
    args = ap.parse_args()

    cfg = load_config(ROOT / "config.yaml")
    store = Store(_paths.resolve("db", configured="data/bot.db"))
    snap = collect(store, cfg.raw, args.since_days)

    if args.compare:
        if not args.out.exists():
            print(json.dumps({"error": "기준선 없음", "path": str(args.out)},
                             ensure_ascii=False))
            return
        old = json.loads(args.out.read_text(encoding="utf-8"))
        print(json.dumps({"baseline": old, "current": snap},
                         ensure_ascii=False, indent=2))
        return

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(snap, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(json.dumps({"written": str(args.out),
                      "calibration_n": snap["calibration"]["n"],
                      "shadow_n_scored": (snap["shadow"]["overall"] or {}).get("n_scored")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()

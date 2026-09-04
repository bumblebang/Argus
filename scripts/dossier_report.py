"""도시에 품질 리포트 (Tier 0).

  python scripts/dossier_report.py              # 요약 출력
  python scripts/dossier_report.py --json       # JSON 전체
  python scripts/dossier_report.py --out data/dossier_quality.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import paths as _paths
from src.config import load_config
from src.engine.store import Store
from src.eval.dossier_quality import summarize_dossiers


def _print_human(rep: dict) -> None:
    print("\n=== 도시에 품질 (Tier 0) ===")
    print(f"기준 시각: {rep.get('asof')}")
    print(f"신선 도시에: {rep.get('fresh_count')}건")
    st = rep.get("stance") or {}
    print(f"stance - bullish {st.get('bullish', 0)} / neutral {st.get('neutral', 0)} "
          f"/ bearish {st.get('bearish', 0)} / unknown {st.get('unknown', 0)}")
    if rep.get("bullish_pct") is not None:
        print(f"bullish 비율: {rep['bullish_pct']:.1%}")
    print(f"bullish+레벨 완비: {rep.get('bullish_with_levels')}")
    age = rep.get("age_hours") or {}
    print(f"나이(h) - p50 {age.get('p50')} / p90 {age.get('p90')} / max {age.get('max')}")
    rr = rep.get("rr_bullish") or {}
    if rr.get("n"):
        print(f"RR(bullish) - n={rr['n']} mean={rr.get('mean')} median={rr.get('median')} "
              f"below_1.5={rr.get('below_1_5')}")
    zone = rep.get("zone_bullish") or {}
    if any(zone.values()):
        print(f"존 위치(bullish) - in {zone.get('in')} / below {zone.get('below')} "
              f"/ above {zone.get('above')} / unknown {zone.get('unknown')}")
    zur = rep.get("zone_unknown_rate")
    if zur is not None:
        print(f"존 unknown 비율: {zur:.1%}  (높으면 가격 센서 실패)")
    pc = rep.get("price_coverage") or {}
    if pc:
        sc = pc.get("source_counts") or {}
        print(f"가격 커버: {pc.get('n')}/{pc.get('wanted')} "
              f"({(pc.get('coverage_pct') or 0)*100:.0f}%) "
              f"src={sc}")
    cov = rep.get("coverage") or {}
    for mkt, c in sorted(cov.items()):
        print(f"커버리지 [{mkt}] {c.get('fresh')}/{c.get('universe')} "
              f"({(c.get('pct') or 0)*100:.0f}%) 미커버 {c.get('uncovered')}")
    out = rep.get("outcomes") or {}
    print(f"outcome 라벨 - n={out.get('n')} status={out.get('status')}")
    for st_name, b in (out.get("by_stance") or {}).items():
        rate = b.get("target_first_rate")
        rate_s = f"{rate:.1%}" if rate is not None else "–"
        print(f"  {st_name}: target_first {b.get('target_first')}/{b.get('n')} ({rate_s})")
    skipped = out.get("skipped") or {}
    if skipped:
        top = sorted(skipped.items(), key=lambda x: -x[1])[:6]
        print(f"  skipped: {dict(top)}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="도시에 품질 리포트")
    ap.add_argument("--json", action="store_true", help="JSON stdout")
    ap.add_argument("--out", type=Path, help="JSON 파일 저장")
    ap.add_argument("--label-days", type=float, default=60.0,
                    help="outcome 라벨 윈도(일)")
    args = ap.parse_args()

    cfg = load_config(ROOT / "config.yaml")
    store = Store(_paths.resolve("db", configured="data/bot.db"))
    ms_path = ROOT / "data" / "market_state.json"
    data_dir = ROOT / "data"
    rep = summarize_dossiers(
        store, cfg=cfg.raw, data_dir=data_dir,
        market_state_path=ms_path, label_days=args.label_days)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"저장: {args.out}")

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    elif not args.out:
        _print_human(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

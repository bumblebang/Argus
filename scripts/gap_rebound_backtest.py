"""갭반등 구간별 승률·조건부 prior 백테스트 (Yahoo 일봉 프록시).

사용:
  python scripts/gap_rebound_backtest.py
  python scripts/gap_rebound_backtest.py --floor -5 --no-decline-pool
  python scripts/gap_rebound_backtest.py --out data/gap_rebound_bt.json --write-prior
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import ROOT
from src.gap_rebound_backtest import (
    PRIOR_PATH,
    build_panel,
    build_prior,
    collect_events,
    enrich_events,
    pick_history_files,
    save_prior,
    summarize_by_bucket,
    summarize_conditional,
    summarize_overall,
    summarize_winner_loser,
)


def _print_table(df, cols: list[str]) -> None:
    if df.empty:
        print("(없음)")
        return
    sub = df[[c for c in cols if c in df.columns]]
    print(sub.to_string(index=False))


def main() -> int:
    p = argparse.ArgumentParser(description="갭반등 구간별 승률 (Yahoo 일봉 프록시)")
    p.add_argument("--liq-top", type=int, default=300)
    p.add_argument("--decline-top", type=int, default=100)
    p.add_argument("--floor", type=float, default=-5.0,
                   help="intraday_ret_pct 상한(이하만 진입, 기본 -5)")
    p.add_argument("--step", type=float, default=1.0, help="구간 폭(%%p)")
    p.add_argument("--tail", type=float, default=-15.0,
                   help="이하를 한 bucket으로 묶는 하한")
    p.add_argument("--min-symbols", type=int, default=200,
                   help="일별 최소 유니버스 종목 수")
    p.add_argument("--no-decline-pool", action="store_true",
                   help="하락 top100 생략 — 거래대금 top만")
    p.add_argument("--out", type=str, default="",
                   help="JSON 저장 경로 (data/ 기준 상대 또는 절대)")
    p.add_argument("--write-prior", action="store_true",
                   help=f"prior 저장 ({PRIOR_PATH.name})")
    args = p.parse_args()

    files = pick_history_files()
    print(f"히스토리 캐시 {len(files)}종목 로드 중…")
    panel = build_panel(files)
    if panel.empty:
        print("패널 비어 있음 — data/history/*.csv 확인")
        return 1

    events = collect_events(
        panel,
        liq_top=args.liq_top,
        decline_top=args.decline_top,
        intraday_floor=args.floor,
        use_decline_pool=not args.no_decline_pool,
        min_symbols=args.min_symbols,
    )
    events = enrich_events(events, panel)
    overall = summarize_overall(events)
    buckets = summarize_by_bucket(
        events, floor=args.floor, step=args.step, tail=args.tail,
    )
    conditional = summarize_conditional(events)
    winner_loser = summarize_winner_loser(events)
    prior = build_prior(events, overall)

    mode = "liq+decline" if not args.no_decline_pool else "liq_only"
    print(f"\n=== 갭반등 프록시 백테스트 ({mode}) ===")
    print(f"조건: 거래대금 top {args.liq_top}"
          + (f" -> 하락 top {args.decline_top}" if not args.no_decline_pool else "")
          + f", intraday<={args.floor}%%, ETF 제외")
    print("진입=당일 종가(15:20 근사), 청산=익일 시가/종가\n")

    if overall:
        print(
            f"기간 {overall['date_from']} ~ {overall['date_to']} | "
            f"이벤트 {overall['n_events']}건 / "
            f"{overall['n_days']}거래일 / {overall['n_symbols']}종목"
        )
        print(
            f"전체 승률 - 익일 시가 {overall['win_open_pct']}% "
            f"(avg {overall['avg_ret_open']:+.2f}%p), "
            f"익일 종가 {overall['win_close_pct']}% "
            f"(avg {overall['avg_ret_close']:+.2f}%p)\n"
        )

    print("구간별 (intraday_ret_pct):")
    _print_table(buckets, [
        "bucket", "n", "win_open_pct", "win_close_pct",
        "avg_ret_open", "avg_ret_close", "med_ret_open", "avg_gap_open",
        "avg_intraday", "avg_daily",
    ])

    print("\n조건부 승률:")
    _print_table(conditional, [
        "id", "label", "n", "win_open_pct", "avg_ret_open",
        "win_close_pct", "avg_ret_close", "small_sample",
    ])

    if winner_loser:
        print("\n승/패 평균 피처 (익일 시가 기준):")
        for key in ("win_open", "lose_open"):
            row = winner_loser.get(key) or {}
            if row:
                n = row.pop("n", 0)
                print(f"  {key} n={n}: {row}")

    if args.out or args.write_prior:
        payload = {
            "params": vars(args),
            "mode": mode,
            "caveats": prior.get("caveats"),
            "overall": overall,
            "buckets": buckets.to_dict(orient="records"),
            "conditional": conditional.to_dict(orient="records"),
            "winner_loser": winner_loser,
            "prior": prior,
        }
        if args.out:
            out_path = Path(args.out)
            if not out_path.is_absolute():
                out_path = ROOT / out_path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            print(f"\n저장: {out_path}")
        if args.write_prior:
            ppath = save_prior(prior)
            print(f"prior 저장: {ppath}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

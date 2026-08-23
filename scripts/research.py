"""전략 리서치 매트릭스 — 전략 × 종목 × 타임프레임 일괄 백테스트 → 랭킹.

라이브 전에 "어떤 전략이 어떤 종목/주기에서 통하는가"의 shortlist 를 만든다. 각 조합에서
전략별 최적 파라미터(스윕)를 찾고, **매수후보유(B&H) 대비 우위(edge)**와 함께 보여준다.
edge<=0 이면 그 전략은 그 구간에서 가치 없음(그냥 들고 있는 게 나음).

사용:
  python scripts/research.py                              # universe 전체 × [1d,1wk]
  python scripts/research.py --symbols 005930 000660 --intervals 1d 60m --range 1y
  python scripts/research.py --intervals 1wk --range 5y --top 15 --save data/research.csv

타임프레임 가이드: 중장기=1wk/1d, 단기=1d/60m, 초단기=5m/1m(분봉은 최근 며칠만).
주의: 단일 종목 대세상승 구간은 추세전략이 과대평가됨 → edge(vs B&H)로 걸러 읽을 것.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.datasources.history import fetch_history
from src.backtest import best_per_strategy, buy_and_hold


def _universe(cfg) -> list[tuple[str, str]]:
    out = []
    for market, lst in (cfg.universe or {}).items():
        for it in (lst or []):
            if it.get("symbol"):
                out.append((it["symbol"], market))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", help="종목 코드들(미지정 시 config.universe 전체)")
    ap.add_argument("--market", default="KR", choices=["KR", "US"],
                    help="--symbols 지정 시 적용할 시장")
    ap.add_argument("--intervals", nargs="*", default=["1d", "1wk"],
                    help="캔들 주기들: 5m/60m/1d/1wk ...")
    ap.add_argument("--range", default="2y")
    ap.add_argument("--steps", type=int, default=3, help="스윕 시 파라미터당 분할 수")
    ap.add_argument("--top", type=int, default=20, help="상위 N개만 출력")
    ap.add_argument("--refresh", action="store_true", help="캐시 무시 재다운로드")
    ap.add_argument("--save", help="결과 CSV 경로(선택)")
    args = ap.parse_args()

    cfg = load_config()
    symbols = ([(s, args.market) for s in args.symbols] if args.symbols
               else _universe(cfg))

    rows: list[dict] = []
    for symbol, market in symbols:
        for interval in args.intervals:
            df = fetch_history(symbol, interval=interval, range_=args.range,
                               market=market, refresh=args.refresh)
            if df is None or df.empty or len(df) < 20:
                print(f"  (skip) {symbol} {interval}: 데이터 부족({0 if df is None else len(df)}봉)")
                continue
            bnh = buy_and_hold(df)
            for name, res in best_per_strategy(df, steps=args.steps).items():
                rows.append({
                    "symbol": symbol, "market": market, "interval": interval,
                    "bars": len(df), "strategy": name,
                    "return_pct": res.return_pct, "bnh_pct": bnh,
                    "edge": res.return_pct - bnh,
                    "win_rate": res.win_rate, "n_trades": res.n_trades,
                    "mdd": res.mdd, "params": res.params,
                })

    if not rows:
        print("결과 없음(데이터를 못 가져왔거나 너무 짧음).")
        return 1

    rows.sort(key=lambda r: r["edge"], reverse=True)    # B&H 대비 우위 기준 랭킹
    print(f"\n[리서치 매트릭스] 조합 {len(rows)} | edge(전략-B&H) 내림차순 | 상위 {args.top}")
    print(f"{'종목':8} {'주기':5} {'전략':18} {'수익률':>9} {'B&H':>9} {'edge':>9} "
          f"{'승률':>5} {'거래':>4} {'MDD':>7}")
    for r in rows[:args.top]:
        print(f"{r['symbol']:8} {r['interval']:5} {r['strategy']:18} "
              f"{r['return_pct']:+8.1%} {r['bnh_pct']:+8.1%} {r['edge']:+8.1%} "
              f"{r['win_rate']:4.0%} {r['n_trades']:4d} {r['mdd']:6.1%}")

    pos = [r for r in rows if r["edge"] > 0]
    print(f"\nB&H 를 이긴 조합: {len(pos)}/{len(rows)} "
          f"(edge>0 인 것만 라이브 후보로 고려할 가치 있음)")

    if args.save:
        p = Path(args.save)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            for r in rows:
                w.writerow({**r, "params": str(r["params"])})
        print(f"저장: {p} ({len(rows)}행)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

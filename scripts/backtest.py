"""백테스트 CLI (샌드박스가 없으므로 실거래 전 필수 검증). 엔진은 src/backtest.py.

사용:
  # 실데이터(Yahoo 과거 캔들) — 다중 타임프레임
  python scripts/backtest.py --strategy ma_crossover --symbol 005930 --interval 1d --range 2y
  python scripts/backtest.py --strategy rsi_reversion --symbol 005930 --interval 1wk --range 5y --sweep
  python scripts/backtest.py --strategy volatility_breakout --symbol AAPL --market US --interval 60m --range 1mo
  # 합성/CSV
  python scripts/backtest.py --strategy volatility_breakout --synthetic 400 --drift 0.001 --vol 0.03
  python scripts/backtest.py --strategy ma_crossover --csv data/sample.csv

데이터: --symbol(실데이터) | --csv | --synthetic 중 하나.
CSV 형식: time,open,high,low,close,volume (헤더, 시간 오름차순)
--sweep: config 단일 파라미터 대신 ParamSpec 하드바운드를 스윕해 최적 파라미터 탐색.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.config import load_config
from src.strategies import build_strategy
from src.backtest import backtest, sweep, synthetic


def _load_df(args) -> pd.DataFrame | None:
    if args.symbol:
        from src.datasources.history import fetch_history
        df = fetch_history(args.symbol, interval=args.interval, range_=args.range,
                           market=args.market, refresh=args.refresh)
        if df is None or df.empty:
            print(f"과거 캔들을 가져오지 못함: {args.symbol} {args.interval} {args.range}")
            return None
        return df
    if args.csv:
        return pd.read_csv(args.csv)
    if args.synthetic:
        return synthetic(args.synthetic, drift=args.drift, vol=args.vol)
    print("--symbol, --csv, --synthetic 중 하나가 필요합니다.")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--symbol", help="실데이터 종목(KR 6자리코드 또는 US 티커)")
    ap.add_argument("--interval", default="1d", help="캔들 주기: 1m/5m/30m/60m(1h)/1d/1wk/1mo")
    ap.add_argument("--range", default="2y", help="기간: 5d/1mo/6mo/1y/2y/5y/max")
    ap.add_argument("--market", default="KR", choices=["KR", "US"])
    ap.add_argument("--refresh", action="store_true", help="캐시 무시하고 재다운로드")
    ap.add_argument("--csv")
    ap.add_argument("--synthetic", type=int, default=0)
    ap.add_argument("--sweep", action="store_true", help="파라미터 하드바운드 스윕(튜닝)")
    ap.add_argument("--steps", type=int, default=3, help="스윕 시 파라미터당 분할 수")
    ap.add_argument("--drift", type=float, default=0.0005, help="합성 추세(국면)")
    ap.add_argument("--vol", type=float, default=0.02, help="합성 변동성(국면)")
    args = ap.parse_args()

    df = _load_df(args)
    if df is None:
        return 1

    if args.sweep:
        ranked = sweep(args.strategy, df, steps=args.steps)
        print(f"\n[스윕] {args.strategy} | 캔들 {len(df)} | 조합 {len(ranked)} (return_pct 내림차순)")
        for r in ranked[:10]:
            s = r.summary()
            print(f"  ret {s['return_pct']:+.2%}  승률 {s['win_rate']:.0%}  "
                  f"거래 {s['n_trades']:2d}  MDD {s['mdd']:.1%}  {s['params']}")
        print(f"\n최적: {ranked[0].summary()['params']}" if ranked else "조합 없음")
        return 0

    cfg = load_config()
    strat = build_strategy(args.strategy, cfg.strategies.get(args.strategy, {}))
    r = backtest(strat, df)
    s = r.summary()
    print(f"\n전략: {args.strategy} | 캔들 {len(df)} | 파라미터 {s['params']}")
    print(f"수익률: {s['return_pct']:+.2%}  | 승률: {s['win_rate']:.0%}  "
          f"| 거래: {s['n_trades']}  | MDD: {s['mdd']:.2%}")
    print("\n최근 매매:")
    for t in r.trades[-10:]:
        print(f"  {t[0]:10} {t[1]}  @ {t[2]:.2f}  ({t[3]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

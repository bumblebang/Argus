"""페이퍼 계좌 성과 리포트.

  python scripts/report.py            # 평균단가 기준 평가
  python scripts/report.py --live     # 토스 현재가로 미실현손익까지 평가

data/paper_account.json 을 읽어 시장별 현금·평가자산·손익과 보유종목·최근 체결을 출력한다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.logging_setup import setup_logging, get_logger
from src.paper_account import PaperAccount


def _fmt(market: str, v: float) -> str:
    return f"{v:,.0f}원" if market == "KR" else f"${v:,.2f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="토스 현재가로 미실현손익 평가")
    args = ap.parse_args()
    setup_logging("WARNING")
    log = get_logger("report")

    cfg = load_config()
    paper_cfg = cfg.raw.get("paper", {})
    acct = PaperAccount(cash=paper_cfg.get("cash", {"KR": 10_000_000, "US": 10_000}),
                        fee_rate=paper_cfg.get("fee_rate", {}),
                        slippage_bps=paper_cfg.get("slippage_bps", {}))

    # 미실현손익 평가용 현재가
    price_lookup: dict[str, float] = {}
    if args.live:
        from src.engine.gateway import TossGateway
        from src.toss_client import TossAPIError
        gateway = TossGateway.from_config(cfg)
        syms = [s for s, p in acct.positions.items() if p.is_open]
        try:
            for row in gateway.get_prices(syms):
                price_lookup[row["symbol"]] = float(row["lastPrice"])
        except TossAPIError as e:
            log.warning("현재가 조회 실패, 평균단가로 평가: %s", e)

    print("\n" + "=" * 60)
    print(" 페이퍼 계좌 성과 리포트")
    print("=" * 60)

    markets = sorted(set(list(acct.cash) + list(acct.symbol_market.values())))
    for m in markets:
        start = float(acct.start_cash.get(m, 0))
        equity = acct.equity(m, price_lookup)
        realized = acct.realized_pnl.get(m, 0.0)
        ret = (equity - start) / start if start else 0.0
        print(f"\n[{m}] 시작자본 {_fmt(m, start)} | 평가자산 {_fmt(m, equity)} "
              f"| 수익률 {ret:+.2%}")
        print(f"     현금 {_fmt(m, acct.cash.get(m, 0))} | 실현손익 {_fmt(m, realized)}")
        rows = [(s, p) for s, p in acct.positions.items()
                if p.is_open and acct.symbol_market.get(s) == m]
        if rows:
            print(f"     보유종목 {len(rows)}개:")
            for s, p in rows:
                px = price_lookup.get(s, p.avg_price)
                upnl = (px - p.avg_price) * p.qty
                print(f"       - {s:8} x{p.qty:<10g} 평단 {p.avg_price:,.2f} "
                      f"현재 {px:,.2f}  평가손익 {upnl:+,.2f}")

    if acct.journal:
        print(f"\n최근 체결 {min(10, len(acct.journal))}건:")
        for f in acct.journal[-10:]:
            print(f"  {f.ts[:19]} {f.side:4} {f.symbol:8} x{f.qty:<8g} @ {f.price:,.2f} "
                  f"(fee {f.fee:.2f}) {f.reason}")
    else:
        print("\n아직 체결 내역이 없습니다. (페이퍼 봇을 돌리면 여기에 쌓입니다)")
    print("=" * 60 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

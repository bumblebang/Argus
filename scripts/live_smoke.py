"""라이브 실주문 왕복 스모크 테스트 — 사람이 직접 돌리는 감독 검증 도구.

  python scripts/live_smoke.py --check                # 잔고/매수여력/보유 조회(읽기 전용)
  python scripts/live_smoke.py --buy 005930           # 무엇을 할지 출력만(주문 안 함)
  python scripts/live_smoke.py --buy 005930 --confirm # 1주 시장가 매수(실돈!)
  python scripts/live_smoke.py --sell 005930 --confirm# 1주 시장가 매도(실돈!)

주의:
  - 이 스크립트는 Broker/하드게이트를 **경유하지 않는다**. 오직 토스 API 스펙 왕복
    검증용이며, 수량은 항상 1주로 **하드코딩**(인자로 늘릴 수 없음 — 안전).
  - 토스는 client 당 토큰 1개 정책이라 watch 와 동시에 돌리면 토큰이 충돌한다.
    실행 전 watch 를 멈춘다. 끝나면 다시 상주.
  - 401 invalid-token 이 한 번 떠도 TossClient 가 자동복구하니 놀라지 말 것.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.logging_setup import setup_logging, get_logger
from src.toss_client import TossClient, TossAPIError

log = get_logger("live_smoke")

QTY = 1   # 안전: 스모크는 항상 1주 고정(인자로 못 늘림)


def _resolve_seq(client: TossClient, cfg) -> tuple[object | None, list]:
    """(account_seq, accounts). 우선순위: 계좌목록의 실 accountSeq > config broker.account_seq
    > .env TOSS_ACCOUNT_NO. 실 accountSeq 를 우선해 config 값과 일치하는지 운영자가 대조."""
    accts: list = []
    try:
        accts = client.get_accounts()
    except TossAPIError as e:
        log.warning("계좌목록 조회 실패: %s", e)
    real_seq = accts[0].get("accountSeq") if accts else None
    cfg_seq = (cfg.raw.get("broker", {}) or {}).get("account_seq")
    env_seq = cfg.creds.account_no or None
    return (real_seq if real_seq is not None else (cfg_seq if cfg_seq is not None else env_seq),
            accts)


def _price(client: TossClient, symbol: str) -> float | None:
    try:
        row = client.get_price(symbol)
        if row:
            return float(row.get("lastPrice"))
    except (TossAPIError, TypeError, ValueError) as e:
        log.warning("현재가 조회 실패(%s): %s", symbol, e)
    return None


def _print_holdings(client: TossClient, seq) -> None:
    try:
        h = client.get_holdings(seq)
        items = (h or {}).get("items", []) if isinstance(h, dict) else []
        print(f"  보유 종목 {len(items)}개:")
        for it in items:
            print(f"    - {it.get('symbol')} {it.get('name','')} "
                  f"qty={it.get('quantity')} 평단={it.get('averagePurchasePrice')} "
                  f"평가액={it.get('marketValue')} 손익={it.get('profitLoss')}")
        if isinstance(h, dict):
            print(f"  총매입={h.get('totalPurchaseAmount')} 평가액={h.get('marketValue')} "
                  f"손익={h.get('profitLoss')}")
    except TossAPIError as e:
        log.warning("보유 조회 실패: %s", e)


def cmd_check(client: TossClient, cfg) -> int:
    seq, accts = _resolve_seq(client, cfg)
    cfg_seq = (cfg.raw.get("broker", {}) or {}).get("account_seq")
    print("=" * 60)
    print("  [live_smoke --check] 읽기 전용 계좌 점검")
    print("=" * 60)
    print(f"  계좌목록: {accts}")
    print(f"  사용 account_seq={seq} (config broker.account_seq={cfg_seq})")
    if seq is None:
        print("  [경고] account_seq 미확보 — 잔고/매수여력 조회 불가.")
        return 1
    try:
        bp = client.get_buying_power(seq, "KR")
        print(f"  매수여력(KR): {bp}")
    except TossAPIError as e:
        log.warning("매수여력 조회 실패: %s", e)
    _print_holdings(client, seq)
    print("=" * 60)
    return 0


def cmd_order(client: TossClient, cfg, symbol: str, side: str, confirm: bool,
              price: float | None = None) -> int:
    seq, _ = _resolve_seq(client, cfg)
    px = _price(client, symbol)
    est = ((price or px) * QTY) if (price or px) else None
    order_type = "LIMIT" if price else "MARKET"
    ptxt = f"지정가 {price:g}" if price else "시장가"
    print("=" * 60)
    print(f"  [live_smoke --{side.lower()}] {symbol} {QTY}주 {ptxt} (account_seq={seq})")
    print(f"  현재가={px}  예상금액≈{est}")
    print("=" * 60)
    if seq is None:
        print("  [중단] account_seq 미확보.")
        return 1
    if not confirm:
        print("  --confirm 없음 → 주문을 내지 않고 종료(무엇을 할지 출력만).")
        return 0
    print(f"  >>> 실주문 전송: {side} {symbol} x{QTY} {order_type}"
          + (f" @ {price:g}" if price else "") + " ...")
    try:
        resp = client.place_order(account_seq=seq, symbol=symbol, side=side,
                                  qty=QTY, order_type=order_type, price=price)
    except TossAPIError as e:
        print(f"  [실패] 주문 오류: {e}")
        return 1
    print(f"  응답 전문: {resp}")
    order_id = resp.get("orderId") if isinstance(resp, dict) else None
    print(f"  orderId={order_id}")
    print("  3초 후 체결/보유 재조회 ...")
    time.sleep(3)
    _print_holdings(client, seq)
    print("=" * 60)
    return 0 if order_id else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="잔고/매수여력/보유 조회(읽기 전용)")
    ap.add_argument("--buy", metavar="SYMBOL", help="1주 시장가 매수")
    ap.add_argument("--sell", metavar="SYMBOL", help="1주 시장가 매도")
    ap.add_argument("--price", type=float, default=None, metavar="PRICE",
                    help="지정가(LIMIT) 가격. 생략 시 시장가(MARKET). 넥스트 시간외엔 지정가만 지원.")
    ap.add_argument("--confirm", action="store_true", help="실주문 실행(없으면 계획만 출력)")
    args = ap.parse_args()
    setup_logging("INFO")

    print("[안내] watch 와 동시 실행 금지 — 토큰 충돌. 실행 전 상주를 멈춘다.")
    cfg = load_config()
    if not cfg.creds.client_id or not cfg.creds.client_secret:
        log.error("TOSS_CLIENT_ID/SECRET 누락 — .env 확인.")
        return 1
    client = TossClient(cfg.creds)

    if args.buy:
        return cmd_order(client, cfg, args.buy, "BUY", args.confirm, args.price)
    if args.sell:
        return cmd_order(client, cfg, args.sell, "SELL", args.confirm, args.price)
    if args.check:
        return cmd_check(client, cfg)
    ap.print_help()
    return 0


if __name__ == "__main__":
    from src.cli.legacy import warn_legacy_script
    warn_legacy_script("argus live-smoke")
    sys.exit(main())

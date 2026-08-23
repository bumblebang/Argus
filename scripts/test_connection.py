"""토스 연결 점검 (읽기 전용, 실주문 없음).

  python scripts/test_connection.py

토큰 → 계좌 존재 → 시세 한 종목. 계좌번호·잔고·토큰은 로그에 남기지 않는다.
기동 전 점검은 `python scripts/doctor.py` 가 본 경로다.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import load_config
from src.logging_setup import setup_logging, get_logger
from src.engine.gateway import TossGateway
from src.toss_client import TossAPIError

from doctor import mask_acct

log = get_logger("test_conn")


def main() -> int:
    setup_logging("INFO")
    cfg = load_config()
    if not cfg.creds.client_id or not cfg.creds.client_secret:
        log.error("CLIENT_ID/SECRET 누락 — .env 를 채우세요.")
        return 1

    gateway = TossGateway.from_config(cfg)
    client = gateway.client
    try:
        client._ensure_token()
        log.info("토큰 OK")
    except TossAPIError as e:
        log.error("토큰 발급 실패: %s", e)
        return 1

    seq = cfg.creds.account_no or None
    try:
        accts = gateway.get_accounts() or []
        bits = [f"seq={a.get('accountSeq')} no={mask_acct(a.get('accountNo'))}"
                for a in accts[:4]]
        log.info("계좌 %d건 (%s)", len(accts), ", ".join(bits) or "없음")
        if accts and not seq:
            seq = accts[0].get("accountSeq")
    except TossAPIError as e:
        log.warning("계좌 조회 실패: %s", e)

    try:
        px = gateway.get_prices(["005930"])
        last = None
        if px:
            r0 = px[0] if isinstance(px, list) else px
            if isinstance(r0, dict):
                last = r0.get("lastPrice") or r0.get("last") or r0.get("close")
        log.info("시세 005930 %s", last if last is not None else "OK")
    except TossAPIError as e:
        log.warning("시세 실패: %s", e)

    if seq:
        try:
            holds = gateway.get_holdings(seq) or {}
            items = holds.get("items") if isinstance(holds, dict) else holds
            n = len(items) if isinstance(items, list) else 1
            log.info("보유 조회 OK (%s종목) — 잔고 금액은 출력하지 않음", n)
        except TossAPIError as e:
            log.warning("보유 조회 실패: %s", e)
        try:
            gateway.get_buying_power(seq, "KR")
            log.info("매수여력 조회 OK — 금액은 출력하지 않음")
        except TossAPIError as e:
            log.warning("매수여력 조회 실패: %s", e)
    else:
        log.info("accountSeq 미확보 — 보유/매수여력 생략")
    return 0


if __name__ == "__main__":
    sys.exit(main())

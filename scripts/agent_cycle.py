"""에이전트 1사이클 — 수동 실행. 실주문 없음 (live_client 미주입).

  python scripts/agent_cycle.py --dry                      # 1회 (MockLLM, 키 불필요)
  python scripts/agent_cycle.py --dry --interval 5 --max-cycles 3   # 5초 간격 3회 데모
  python scripts/agent_cycle.py --cli                      # claude CLI
  python scripts/agent_cycle.py --live                     # API 키 백엔드

ANTHROPIC_API_KEY 없으면 자동 dry. --interval 0(기본) 이면 1회만. --max-cycles 0 이면 무한 반복.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.logging_setup import setup_logging, get_logger
from src.engine.gateway import TossGateway
from src.agents.pipeline import (CycleRunner, select_backend, build_live_llm,
                                 synth_candles, dry_llm_factory)

log = get_logger("agent_cycle")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="MockLLM + 합성가격 (인증 불필요)")
    ap.add_argument("--cli", action="store_true", help="claude CLI 백엔드 (구독, API 크레딧 0)")
    ap.add_argument("--live", action="store_true", help="API 키 백엔드 (종량제)")
    ap.add_argument("--interval", type=int, default=0, help="반복 간격(초). 0=1회만")
    ap.add_argument("--max-cycles", type=int, default=1, help="최대 반복 수. 0=무한")
    args = ap.parse_args()
    setup_logging("INFO")
    cfg = load_config()

    api_key = os.getenv("ANTHROPIC_API_KEY")
    # 백엔드 결정: --dry(mock) | --cli(구독) | --live/키(API) | 기본(키 있으면 API, 없으면 dry)
    dry, use_cli, subscription = select_backend(dry=args.dry, cli=args.cli,
                                                live=args.live, api_key=api_key)
    if not (args.dry or args.cli or args.live or api_key):
        log.warning("인증 없음 -> 자동 dry. 구독은 --cli, API 키는 --live.")
    if use_cli:
        log.warning("claude CLI(구독) 백엔드 사용. `claude` 가 PATH 에 있어야 합니다.")
    if subscription:
        log.warning("API OAuth(ant 프로필) 사용. 크레딧 필요. 크레딧 0이면 --cli 권장.")

    # 백엔드별 LLM 팩토리 + 캔들 소스(dry=합성, live=TossClient). 배선은 CycleRunner 가 담당.
    if dry:
        fetch_raw = synth_candles
        llm_factory = dry_llm_factory
    else:
        live_llm = build_live_llm(cfg, use_cli=use_cli, subscription=subscription,
                                  api_key=api_key)
        llm_factory = lambda cands: live_llm
        gateway = TossGateway.from_config(cfg)
        fetch_raw = lambda s, m: gateway.candles(s, "1d", 30)

    runner = CycleRunner(cfg, llm_factory=llm_factory, fetch_candles=fetch_raw)

    cycle = 0
    while True:
        cycle += 1
        try:
            res = runner.run()
        except Exception as e:
            log.error("LLM 호출 실패: %s", e)
            if use_cli:
                log.error("`claude` 가 PATH 에 있고 로그인돼 있는지 확인하세요. 먼저 "
                          "`python scripts/check_cli.py` 로 점검. 우선은 --dry 로 테스트.")
            elif subscription:
                log.error("API 크레딧 부족일 수 있음(구독≠API). console 에서 충전하거나 --cli 사용.")
            return 1

        verdicts = {v.symbol: v for v in res.validation.verdicts}
        print(f"\n=== 사이클 {cycle} | 시장관: {res.decision.market_view} ===")
        for ex in res.executed:
            v = verdicts.get(ex["symbol"])
            mark = "[approve]" if (v and v.approved) else "[veto]   "
            print(f"  {ex['symbol']:8} {ex['action']:4} -> {ex['status']:14} "
                  f"{mark} {ex['reason'][:40]}")
        if not res.executed:
            print("  (집행 시도 없음 — 전부 HOLD)")

        if args.interval <= 0 or (args.max_cycles and cycle >= args.max_cycles):
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())

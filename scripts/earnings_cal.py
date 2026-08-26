"""실적발표 캘린더 배치 — 유니버스+보유 종목의 발표일·컨센서스를 data/earnings_calendar.json 으로.

  python scripts/earnings_cal.py         # KR 네이버 + US Finnhub 실조회
  python scripts/earnings_cal.py --dry   # 외부 호출 없이 파일 스키마만 확인

run_market_state.bat(장전)에 물려 매일 갱신된다. 뇌(CycleRunner)가 이 파일을 읽어
후보/보유 종목에 '다음 발표일·컨센서스·과거 서프라이즈'를 붙이고, 공시 워처는 실적
공시 각성 시 컨센서스를 payload 에 첨부한다. LLM·토스 호출 없음.

발표 임박을 이유로 진입을 막거나 포지션을 줄이는 로직은 **여기에도 어디에도 없다** —
실적은 리스크이자 기회이고, 판단은 뇌가 한다. 코드는 데이터만 제때 먹인다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config, ROOT
from src.logging_setup import setup_logging, get_logger
from src.datasources.earnings import (fetch_kr_earnings, fetch_us_earnings,
                                      fetch_us_surprise_history)
from src import paths as _paths

OUT = ROOT / "data" / "earnings_calendar.json"


def _held_symbols(log) -> dict[str, str]:
    """보유 종목 {symbol: market}. 파일 없거나 깨졌으면 {} (유니버스만 대상)."""
    account = _paths.resolve("paper", configured="data/paper_account.json")
    try:
        acct = json.loads(account.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.info("보유 계좌 로드 생략(유니버스만 대상): %s", e)
        return {}
    smap = acct.get("symbol_market") or {}
    out: dict[str, str] = {}
    for sym in (acct.get("positions") or {}):
        # symbol_market 우선, 없으면 6자리 숫자=KR 관례로 판별
        out[sym] = smap.get(sym) or ("KR" if sym.isdigit() and len(sym) == 6 else "US")
    return out


def _targets(cfg, log) -> dict[str, list[str]]:
    """{market: [symbol,...]} — 유니버스 + 현재 보유(합집합, 순서 보존)."""
    targets: dict[str, list[str]] = {}
    for market, lst in (cfg.universe or {}).items():
        targets[market] = [it["symbol"] for it in (lst or []) if it.get("symbol")]
    for sym, market in _held_symbols(log).items():
        bucket = targets.setdefault(market, [])
        if sym not in bucket:
            bucket.append(sym)
    return targets


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true",
                    help="외부 호출 없이 빈 symbols 로 저장(스키마 확인)")
    ap.add_argument("--days-ahead", type=int, default=60, help="US 캘린더 조회 범위(일)")
    args = ap.parse_args()

    setup_logging("INFO")
    log = get_logger("earnings.batch")
    cfg = load_config()

    t0 = time.time()
    symbols: dict[str, dict] = {}
    if args.dry:
        log.info("[dry] 외부 호출 생략 — 빈 캘린더로 스키마만 기록")
    else:
        targets = _targets(cfg, log)

        # ── US: 캘린더 1콜 + 심볼별 서프라이즈 이력(1.1초 간격, 무료 60콜/분 보호) ──
        us = targets.get("US") or []
        api_key = os.getenv("FINNHUB_API_KEY")
        if us and not api_key:
            log.warning("FINNHUB_API_KEY 없음 → US 실적 캘린더 생략(KR 만 수집)")
        elif us:
            found = fetch_us_earnings(api_key, us, days_ahead=args.days_ahead)
            hist_syms = list(found)
            for i, sym in enumerate(hist_syms):
                found[sym]["surprise_history"] = fetch_us_surprise_history(api_key, sym)
                if i < len(hist_syms) - 1:
                    time.sleep(1.1)
            symbols.update(found)
            log.info("US 실적 캘린더 %d/%d종목", len(found), len(us))

        # ── KR: 종목당 2콜(일정+컨센서스), 0.3초 간격 ──
        kr = targets.get("KR") or []
        n_kr = 0
        for i, sym in enumerate(kr):
            try:
                res = fetch_kr_earnings(sym)
            except Exception as e:              # 한 종목 실패가 배치를 못 죽이게
                log.warning("[%s] 실적 캘린더 실패: %s", sym, e)
                res = None
            if res:
                symbols[sym] = res
                n_kr += 1
            if i < len(kr) - 1:
                time.sleep(0.3)
        log.info("KR 실적 캘린더 %d/%d종목", n_kr, len(kr))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "asof": datetime.now(timezone.utc).isoformat(),
        "symbols": symbols,
    }, ensure_ascii=False), encoding="utf-8")
    soon = [s for s, e in symbols.items()
            if isinstance(e.get("dday"), int) and 0 <= e["dday"] <= 3]
    log.info("실적 캘린더 %d종목(임박 %d: %s) -> %s (%.1fs)",
             len(symbols), len(soon), ", ".join(soon) or "-", OUT, time.time() - t0)
    return 0


if __name__ == "__main__":
    from src.cli.legacy import warn_legacy_script
    warn_legacy_script("argus earnings-cal")
    sys.exit(main())

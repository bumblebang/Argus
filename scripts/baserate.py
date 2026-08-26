"""베이스레이트 배치 — 유니버스 전 종목의 셋업 통계를 data/base_rates.json 으로.

  python scripts/baserate.py            # 유니버스(동적 우선) 전 종목 5y 일봉 분석
  python scripts/baserate.py --range 3y # 히스토리 기간 변경

run_market_state.bat(장전 08:30/16:00)에 물려 매일 갱신된다. 뇌(CycleRunner)가
이 파일을 읽어 '지금 활성인 셋업 + 과거 승률/수익폭'을 후보 피처에 붙인다.
Yahoo 캐시는 20h 신선도라 하루 한 번만 실조회 — 스팸 없음. LLM·토스 호출 없음.

부산물로 data/ma20.json(종목별 20일 이동평균)도 같이 쓴다. 여기서 이미 전 종목의 일봉을
받으니 네트워크 추가가 0이고, MA20 은 일봉 기반이라 장중에 안 변한다 → 감시 루프가 이
캐시 + 매 틱 폴링하는 실시간가만으로 '유니버스 전 종목 실시간 브레드스'를 조회 없이
계산한다(src/engine/loop.py). 지수 2개 프록시(0%/50%/100% 계단)를 대체한다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config, ROOT
from src.logging_setup import setup_logging, get_logger
from src.baserate import analyze
from src.datasources.history import fetch_history
from src.indicators import sma

OUT = ROOT / "data" / "base_rates.json"
MA20_OUT = ROOT / "data" / "ma20.json"


def ma20_row(df, market: str) -> dict | None:
    """일봉 DataFrame -> {ma20, close, n, market}. 종가 20봉 미만이면 None(캐시에서 제외).

    분모에 못 들어갈 종목을 넣는 것보다 빼는 게 안전하다 — 감시 루프는 이 캐시에 있는
    종목만 브레드스 분모로 잡는다.
    """
    close = df["close"].astype(float).dropna()
    if len(close) < 20:
        return None
    m = float(sma(close, 20).iloc[-1])
    if m != m:                                   # NaN 방어
        return None
    return {"ma20": round(m, 4), "close": round(float(close.iloc[-1]), 4),
            "n": int(len(close)), "market": market}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--range", default="5y", help="히스토리 기간 (기본 5y)")
    ap.add_argument("--max-age-hours", type=float, default=20.0,
                    help="Yahoo 캐시 신선도(시간). 이보다 오래되면 재조회")
    args = ap.parse_args()

    setup_logging("INFO")
    log = get_logger("baserate.batch")
    cfg = load_config()

    t0 = time.time()
    symbols: dict[str, dict] = {}
    ma20: dict[str, dict] = {}
    n_active = 0
    for market, lst in (cfg.universe or {}).items():
        for it in (lst or []):
            sym = it.get("symbol")
            if not sym:
                continue
            try:
                df = fetch_history(sym, interval="1d", range_=args.range, market=market,
                                   max_age_hours=args.max_age_hours)
                res = analyze(df)
            except Exception as e:              # 한 종목 실패가 배치를 못 죽이게
                log.warning("[%s] 베이스레이트 실패: %s", sym, e)
                continue
            symbols[sym] = res
            # MA20 부산물(장중 실시간 브레드스용). 이미 받은 df 라 네트워크 추가 없음.
            try:
                row = ma20_row(df, market)
            except Exception as e:              # MA20 실패도 그 종목만 건너뛴다
                log.warning("[%s] MA20 계산 실패(캐시 제외): %s", sym, e)
                row = None
            if row:
                ma20[sym] = row
            if res["active_now"]:
                n_active += 1
                log.info("[%s] 활성 셋업: %s", sym, ", ".join(res["active_now"]))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "asof": datetime.now(timezone.utc).isoformat(),
        "range": args.range,
        "symbols": symbols,
    }, ensure_ascii=False), encoding="utf-8")
    log.info("베이스레이트 %d종목(활성 %d) -> %s (%.1fs)",
             len(symbols), n_active, OUT, time.time() - t0)

    # MA20 캐시는 별도 파일로 — base_rates.json 스키마는 기존 소비자 때문에 그대로 둔다.
    MA20_OUT.write_text(json.dumps({
        "asof": datetime.now(timezone.utc).isoformat(),
        "symbols": ma20,
    }, ensure_ascii=False), encoding="utf-8")
    log.info("MA20 캐시 %d종목 -> %s", len(ma20), MA20_OUT)
    return 0


if __name__ == "__main__":
    from src.cli.legacy import warn_legacy_script
    warn_legacy_script("argus baserate")
    sys.exit(main())

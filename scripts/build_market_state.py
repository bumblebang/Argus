"""market state 빌드: 데이터소스들을 돌려 구조화된 시장 스냅샷을 만든다.

  python scripts/build_market_state.py          # 실데이터 (환율/캘린더/브레드스)
  python scripts/build_market_state.py --dry     # 외부호출 없이 합성으로 동작 확인

장전에 한 번 돌리면 data/market_state.json 이 갱신되고, 이후 '뇌'(판단 에이전트)가 읽는다.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.logging_setup import setup_logging, get_logger
from src.market_state import MarketState
from src.datasources import (SourceContext, TossInfoSource, BreadthSource,
                             EdgarSource, DartSource, SectorSource,
                             NewsSource, DartNewsSource, SentimentSource,
                             MarketsSource, FlowsSource, FlowsMarketSource,
                             PositioningSource, MacroSource, EcosMacroSource,
                             FinnhubNewsSource, assess_fear,
                             ProgramFlowsSource, KrxFlowsMarketSource,
                             KrxFlowsSource, ForeignExhaustionSource,
                             KrxAlertsSource, VkospiSource, KrxBreadthSource,
                             IndexConstituentsSource)
from src.datasources.fear_greed import summary_line as fear_summary
from src.screener import load_candidates
from src.engine.gateway import TossGateway

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _synth(symbol: str, market: str) -> pd.DataFrame:
    rng = np.random.default_rng(abs(hash((symbol, market))) % (2**32))
    close = 100 * np.cumprod(1 + rng.normal(0.001, 0.02, 30))
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": [1000] * 30})


def _priority_us_symbols(cfg) -> list[str]:
    """보유·armed US 티커 — 배치 뉴스에서 유니버스 앞자리보다 우선."""
    out: list[str] = []
    try:
        from src.engine.store import Store
        db = (cfg.raw.get("run") or {}).get("db_path", "data/bot.db")
        store = Store(db)
        for rows in (store.get_open_positions(), store.get_armed()):
            for r in rows:
                sym = str(r["symbol"] or "").strip()
                if not sym:
                    continue
                mkt = str(r["market"] or "").upper()
                if mkt == "US" or not (sym.isdigit() and len(sym) == 6):
                    out.append(sym)
    except Exception as e:
        log = get_logger("build_state")
        log.warning("US 뉴스 priority(보유/armed) 조회 실패(유니버스만): %s", e)
    return list(dict.fromkeys(out))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    setup_logging("INFO")
    log = get_logger("build_state")

    cfg = load_config()
    markets = cfg.run.get("trade_markets", ["KR", "US"])
    ms_cfg = cfg.raw.get("market_state", {})

    # 시장 국면은 소수 지수 프록시로만 판정(BASIC tier 호출 절약). 미설정 시 유니버스 앞 5종목.
    symbols_by_market: dict[str, list[str]] = ms_cfg.get("regime_symbols", {})
    if not symbols_by_market:
        for market, symbol, _name in load_candidates(DATA_DIR, markets):
            symbols_by_market.setdefault(market, [])
            if len(symbols_by_market[market]) < 5:
                symbols_by_market[market].append(symbol)

    gateway = None if args.dry else TossGateway.from_config(cfg)
    client = None if gateway is None else gateway.client
    ctx = SourceContext(client=client, symbols_by_market=symbols_by_market, dry=args.dry,
                        spacing_sec=float(ms_cfg.get("request_spacing_sec", 0.6)))

    # 재무는 실제 종목 대상 (지수 프록시 아님). 유니버스에서 상한만큼.
    cap = int(ms_cfg.get("fundamentals_max", 10))
    us_universe = [s for (_m, s, _n) in load_candidates(DATA_DIR, ["US"])]
    us_tickers = us_universe[:cap]
    kr_codes = [s for (_m, s, _n) in load_candidates(DATA_DIR, ["KR"])][:cap]

    sources = [
        TossInfoSource(),
        BreadthSource(synthetic_fetch=_synth if args.dry else None),
        SectorSource(ms_cfg.get("sector_symbols", {}), lookback=20,
                     synthetic_fetch=_synth if args.dry else None),
        EdgarSource(tickers=us_tickers,
                    user_agent=ms_cfg.get("sec_user_agent", "argus example@example.com")),
        NewsSource(ms_cfg.get("news_feeds", {})),
        SentimentSource(ms_cfg.get("sentiment_symbols", {"vix": "^VIX"})),
        MarketsSource(ms_cfg.get("markets_symbols", {})),
        FlowsSource(kr_codes),
        FlowsMarketSource(),
        PositioningSource(kr_codes),
    ]
    # KRX 정보데이터(로그인 선택) — Naver/기존 슬롯을 보강. 자격 없으면 각 소스가 빈 dict.
    krx_user = (os.getenv("KRX_USER") or "").strip()
    if krx_user or args.dry:
        sources.extend([
            KrxFlowsMarketSource(),          # flows_market 덮어쓰기(krx 우선)
            KrxFlowsSource(kr_codes),
            ProgramFlowsSource(),
            ForeignExhaustionSource(kr_codes),
            KrxAlertsSource(),
            VkospiSource(),
            KrxBreadthSource(),
            IndexConstituentsSource(),
        ])
    else:
        log.info("KRX_USER 없음 → KRX 확장 슬롯 스킵(positioning 만 자격 시 동작)")
    fred_key = os.getenv("FRED_API_KEY")
    if fred_key:
        sources.append(MacroSource(api_key=fred_key))
    else:
        log.warning("FRED_API_KEY 없음 -> 매크로 스킵")
    ecos_key = os.getenv("ECOS_API_KEY")
    if ecos_key or args.dry:   # --dry 는 키 없이도 합성값으로 슬롯 확인
        sources.append(EcosMacroSource(api_key=ecos_key or "dry"))
    else:
        log.warning("ECOS_API_KEY 없음 -> macro_kr 스킵")
    finnhub_key = os.getenv("FINNHUB_API_KEY")
    if finnhub_key:
        from src.datasources.finnhub import (
            DEFAULT_NEWS_US_MAX, select_us_news_symbols)
        news_max = int(ms_cfg.get("news_us_max", DEFAULT_NEWS_US_MAX))
        news_syms = select_us_news_symbols(
            us_universe, priority=_priority_us_symbols(cfg), max_n=news_max)
        log.info("Finnhub 종목뉴스 대상 %d종 (cap=%d)", len(news_syms), news_max)
        sources.append(FinnhubNewsSource(api_key=finnhub_key, symbols=news_syms))
    else:
        log.warning("FINNHUB_API_KEY 없음 -> 미국 뉴스 스킵")
    dart_key = os.getenv("DART_API_KEY")
    if dart_key:
        sources.append(DartSource(api_key=dart_key, symbols=kr_codes))
        sources.append(DartNewsSource(api_key=dart_key, symbols=kr_codes))
    else:
        log.warning("DART_API_KEY 없음 -> 국내 재무·공시 스킵")

    out_path = DATA_DIR / "market_state.json"
    state = MarketState.load(out_path) if out_path.exists() else MarketState()
    for s in sources:
        try:
            partial = s.fetch(ctx)
            if partial:
                state.merge(partial)
        except Exception as e:
            log.warning("소스 %s 실패(직전값 유지): %s", type(s).__name__, e)

    # 공포지수는 regime/markets 가 다 채워진 뒤에 합성한다(--dry 는 네트워크 무접촉).
    if not args.dry:
        # 장전 배치는 KRX Open API 캐시를 강제 갱신(장중 슬라이스는 TTL hit).
        if (os.getenv("KRX_API_KEY") or "").strip():
            try:
                from src.datasources.krx_open import refresh_fear_cache
                refresh_fear_cache(force=True)
            except Exception as e:
                log.warning("KRX fear 캐시 갱신 실패(기존 캐시/스킵): %s", e)
        state.merge({"sentiment": assess_fear(state.to_dict(), cfg)})

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat()
    state.batch_asof = now_iso
    state.asof = now_iso
    state.save(out_path)
    log.info("market_state 저장 완료")
    log.info("  fx=%s", state.fx)
    log.info("  regime=%s", state.regime)
    log.info("  fundamentals: %d종목", len(state.fundamentals))
    log.info("  sectors: %s", {m: v.get("leaders") for m, v in state.sectors.items()})
    log.info("  sentiment: %s", state.sentiment)
    fear = fear_summary(state.sentiment)
    if fear:
        log.info("  %s", fear)
    log.info("  markets: %s", {k: v.get("last") for k, v in state.markets.items()})
    log.info("  macro: %s", state.macro)
    log.info("  macro_kr: 기준금리=%s 국고3Y=%s (%s개)", state.macro_kr.get("bok_base_rate"),
             state.macro_kr.get("kr_treasury_3y"), state.macro_kr.get("raw_n"))
    log.info("  flows: %d종목", len(state.flows))
    fm = state.flows_market or {}
    log.info("  flows_market: KOSPI=%s KOSDAQ=%s src=%s",
             (fm.get("KOSPI") or {}).get("foreign_net"),
             (fm.get("KOSDAQ") or {}).get("foreign_net"),
             fm.get("source"))
    log.info("  program_flows: %s", {
        k: (v or {}).get("total_net") if isinstance(v, dict) else v
        for k, v in (state.program_flows or {}).items()
        if k in ("KOSPI", "KOSDAQ")})
    log.info("  vkospi: %s", (state.vkospi or {}).get("close"))
    log.info("  warnings: %d", len(state.warnings or {}))
    log.info("  news: %d건", len(state.news))
    return 0


if __name__ == "__main__":
    from src.cli.legacy import warn_legacy_script
    warn_legacy_script("argus market-state")
    sys.exit(main())

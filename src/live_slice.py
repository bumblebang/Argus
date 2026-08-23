"""장중 market_state '빠른 슬라이스' — regime/sentiment/markets/flows_market 갱신.

data/market_state.json 은 하루 2회 배치(scripts/build_market_state.py)로만 갱신되는데,
그 파일의 regime(시장 국면)을 장중 감시 루프의 regime_flip 트리거와 뇌(pipeline)가 매
사이클 소비한다. 2회/일이면 장중 국면 전환이 다음 배치까지 안 보여 트리거가 사실상 안
먹는다 → 장중 5분마다 느린 슬롯(fundamentals/news/flows/sectors/macro)은 놔두고 빠른
슬롯만 다시 계산해 파일을 신선하게 유지한다. 소비자는 이미 매번 파일을 다시 읽으므로
소비자 코드는 안 건드려도 자동 반영된다.

토스 무접촉: 장중 1초 감시 루프가 토스 게이트웨이(레이트리밋·직렬화 락)를 쓰므로, 갱신기가
토스를 또 건드리면 경합한다. 슬롯 전부 Yahoo/Naver 로 계산한다.
  - regime  : 감시 루프의 실시간 유니버스 브레드스(breadth_fn) 우선, 없으면 지수 프록시
  - sentiment: SentimentSource (Yahoo, ctx.client 안 씀) + 공포지수(CNN·KR 대리지표)
  - markets  : MarketsSource   (Yahoo, ctx.client 안 씀)
  - flows_market: FlowsMarketSource (네이버 지수 수급)

regime 2층 구조: 감시 루프는 이미 매 틱 유니버스 전 종목의 실시간가를 폴링하므로, 장전
배치의 MA20 캐시만 있으면 '전 종목 브레드스'를 조회 0회로 안다(source=universe_live).
지수 2개 프록시(source=index_proxy)는 0%/50%/100% 계단이라 크기 정보가 사실상 없다 →
종목수가 충분한(breadth_min_n) 시장은 실시간 값을 쓰고 Yahoo 지수 조회 자체를 건너뛴다.
"""
from __future__ import annotations

from pathlib import Path

from .datasources import (BreadthSource, SentimentSource, MarketsSource,
                          FlowsMarketSource, assess_fear)
from .datasources.base import SourceContext
from .datasources.fear_greed import summary_line as fear_summary
from .datasources.history import fetch_history
from .logging_setup import get_logger
from .market_state import MarketState

log = get_logger("src.live_slice")

# Yahoo 는 KR 지수 ETF(KODEX 200=069500 등)를 부실하게 서빙한다(간헐적으로 1봉만 반환) →
# 브레드스가 n=0 이 되어 오판. 배치의 regime_symbols(ETF)는 토스용이고, Yahoo 경로에선
# Yahoo 가 안정적으로 주는 '지수 티커'를 쓴다(KOSPI/KOSDAQ/S&P/나스닥). config 로 오버라이드 가능.
_YAHOO_REGIME_DEFAULT = {"KR": ["^KS11", "^KQ11"], "US": ["^GSPC", "^IXIC"]}


def _live_regime(breadth_fn, min_n: int) -> dict:
    """감시 루프의 실시간 브레드스 스냅샷 중 '채택 가능한' 시장만 뽑는다.

    표본이 얇으면(n < min_n) 지수 프록시보다 나을 게 없다 → 채택 안 함. breadth_fn 이
    터지면(루프 미기동·스레드 예외) 삼키고 전부 프록시로 폴백 — regime 이 통째로 비는
    것보다 대략치라도 있는 게 낫다.
    """
    if breadth_fn is None:
        return {}
    try:
        snap = breadth_fn() or {}
    except Exception as e:
        log.warning("실시간 브레드스 조회 실패 — 전부 지수 프록시로 폴백: %s", e)
        return {}
    out: dict = {}
    for m, v in (snap.items() if isinstance(snap, dict) else []):
        if not isinstance(v, dict):
            continue
        try:
            n = int(v.get("n") or 0)
        except (TypeError, ValueError):
            continue
        if n >= min_n:
            out[m] = {**v, "source": v.get("source") or "universe_live"}
    return out


def build_fast_slice(cfg, breadth_fn=None) -> dict:
    """regime/sentiment/markets 부분 dict 를 Yahoo 로만 조립해 반환.

    반환: {"regime": {...}, "sentiment": {...}, "markets": {...}} (일부 슬롯은 조회 실패 시
    빈 dict 일 수 있음). 세 소스 모두 실데이터(dry=False) 로 호출하되 토스는 만지지 않는다.

    breadth_fn: 감시 루프의 breadth_snapshot(실시간 유니버스 브레드스). 주면 종목수가
    충분한 시장은 그 값을 regime 으로 채택하고 그 시장의 Yahoo 지수 조회를 아예 건너뛴다.
    기본 None = 기존 동작(전 시장 지수 프록시) 그대로.
    """
    ms_cfg = cfg.raw.get("market_state", {})
    # regime: Yahoo 안정 지수 티커로 브레드스 계산(config regime_symbols_yahoo 로 오버라이드).
    symbols_by_market: dict[str, list[str]] = (
        ms_cfg.get("regime_symbols_yahoo") or _YAHOO_REGIME_DEFAULT)
    spacing = float(ms_cfg.get("request_spacing_sec", 0.6))
    min_n = int(ms_cfg.get("breadth_min_n", 10))

    def _yahoo_candles(sym: str, market: str):
        # refresh=True 로 캐시 우회 — 장중 재조회(빠른 슬라이스의 목적).
        return fetch_history(sym, interval="1d", range_="3mo", market=market, refresh=True)

    sentiment = SentimentSource(ms_cfg.get("sentiment_symbols", {"vix": "^VIX"}))
    markets = MarketsSource(ms_cfg.get("markets_symbols", {}))

    partial: dict = {}
    # 1층: 실시간 유니버스 브레드스(조회 0회). 채택된 시장은 아래 프록시 대상에서 빠진다.
    regime = _live_regime(breadth_fn, min_n)
    proxy_symbols = {m: syms for m, syms in symbols_by_market.items() if m not in regime}
    # 2층: 남은 시장만 지수 프록시. 전 시장이 채택되면 이 블록 자체를 안 타 Yahoo 호출 0회.
    if proxy_symbols:
        breadth = BreadthSource(candle_fetch=_yahoo_candles)
        # regime 은 프록시 캔들 조회에 간격을 두어 Yahoo 예의(polite).
        regime_ctx = SourceContext(client=None, symbols_by_market=proxy_symbols,
                                   dry=False, spacing_sec=spacing)
        proxy = breadth.fetch(regime_ctx).get("regime", {})
        # 데이터 미확보(n=0) 시장은 regime 에서 제거 → merge 가 마지막 정상값(배치)을 보존한다.
        # (조회 실패가 breadth 0.0 → risk_off 로 둔갑해 regime_flip 을 헛발화시키는 것 방지.)
        dropped = [m for m, v in list(proxy.items()) if not v.get("n")]
        for m in dropped:
            proxy.pop(m)
        if dropped:
            log.warning("regime 조회 실패 시장 %s — 이번 슬라이스에서 제외(직전 배치값 유지).",
                        dropped)
        # 출처를 남긴다 — 뇌·대시보드가 '지수 2개 대략치'임을 알고 과신하지 않게.
        for m, v in proxy.items():
            regime[m] = {**v, "source": "index_proxy"}
    partial["regime"] = regime
    # sentiment/markets 는 ctx.client 를 안 쓰므로 최소 컨텍스트로 호출.
    empty_ctx = SourceContext(client=None, symbols_by_market={}, dry=False)
    partial.update(sentiment.fetch(empty_ctx))
    partial.update(markets.fetch(empty_ctx))
    # 시장 전체 수급 — 토스 무접촉(네이버). 장중에도 방향이 바뀌므로 빠른 슬라이스에 포함.
    try:
        partial.update(FlowsMarketSource().fetch(empty_ctx))
    except Exception as e:
        log.warning("flows_market 슬라이스 실패(직전값 유지): %s", e)
    # 공포지수 레이어 — regime/markets 가 채워진 뒤라야 KR 대리지표를 합성할 수 있다.
    partial.setdefault("sentiment", {}).update(assess_fear(partial, cfg))
    log.info("빠른 슬라이스 조립 — regime=%s sentiment_keys=%s markets=%d flows_mkt=%s",
             {m: f"{v.get('label')}/{v.get('source')}/n={v.get('n')}"
              for m, v in partial.get("regime", {}).items()},
             list(partial.get("sentiment", {}).keys()), len(partial.get("markets", {})),
             bool((partial.get("flows_market") or {}).get("KOSPI")))
    fear = fear_summary(partial.get("sentiment", {}))
    if fear:
        log.info("  %s", fear)
    return partial


def apply_fast_slice(partial: dict, path: str | Path = "data/market_state.json") -> None:
    """기존 파일을 읽어 regime/sentiment/markets 만 부분 merge → asof 갱신 → 원자적 save.

    merge 는 dict 하위키 update 라 무관 슬롯(fundamentals/news/flows/sectors/macro/fx…)은
    그대로 보존된다. 파일이 없으면 빈 MarketState 에서 시작. save 는 os.replace 로 원자적.
    """
    from datetime import datetime, timezone
    p = Path(path)
    state = MarketState.load(p) if p.exists() else MarketState()
    state.merge(partial or {})
    state.asof = datetime.now(timezone.utc).isoformat()
    state.save(p)

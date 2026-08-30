"""메인 트레이딩 루프.

각 종목마다 캔들을 폴링 -> 지정된 전략으로 신호 산출 -> 리스크 사이징 -> 집행.
토스는 웹소켓이 없어 REST 폴링 기반이다.
"""
from __future__ import annotations

import time as _time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

from .config import AppConfig, ROOT
from .market_hours import trading_date, near_session_end
from .logging_setup import get_logger
from .session_policy import market_tradable, trading_sessions_from_raw
from .broker import Broker
from .risk import RiskManager, risk_manager_from_cfg
from .risk_gate import Order
from .strategies import build_strategy
from .strategies.base import Action

log = get_logger("runner")

# 캔들 응답에서 흔히 쓰이는 키 후보 (스펙 확정 후 정리 가능).
_KEY_ALIASES = {
    "open": ["open", "openPrice", "o"],
    "high": ["high", "highPrice", "h"],
    "low": ["low", "lowPrice", "l"],
    "close": ["close", "closePrice", "c", "price"],
    "volume": ["volume", "vol", "v"],
    "time": ["time", "timestamp", "date", "datetime"],
}


def candles_to_df(raw: list[dict]) -> pd.DataFrame:
    """API 캔들 리스트 -> 표준 OHLCV DataFrame (시간 오름차순)."""
    if not raw:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    rows = []
    for c in raw:
        row = {}
        for field, aliases in _KEY_ALIASES.items():
            for a in aliases:
                if a in c:
                    row[field] = c[a]
                    break
        rows.append(row)
    df = pd.DataFrame(rows)
    if "time" in df.columns:
        df = df.sort_values("time").reset_index(drop=True)
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _market_tz(market: str) -> ZoneInfo:
    from .market_hours import _SESSIONS
    tzname = _SESSIONS.get(market, ("Asia/Seoul",))[0]
    return ZoneInfo(tzname)


def last_bar_trading_date(df: pd.DataFrame | None, market: str = "KR") -> str | None:
    """마지막 봉의 거래일(ISO). time 열 없으면 None(판정 불가).

    naive 시각은 **시장 로컬**(KR→KST, US→ET)로 간주한다. UTC 로 localize 하면
    KST 15:00+ 일봉·토스 캔들이 다음날로 밀린다. tz-aware 는 market TZ 로 변환.
    """
    if df is None or len(df) == 0 or "time" not in df.columns:
        return None
    t = df["time"].iloc[-1]
    if pd.isna(t):
        return None
    ts = pd.Timestamp(t)
    tz = _market_tz(market)
    if ts.tzinfo is None:
        ts = ts.tz_localize(tz)
    else:
        ts = ts.tz_convert(tz)
    return ts.date().isoformat()


def patch_live_price(df: pd.DataFrame, price: float | None, *,
                     market: str = "KR",
                     append_if_new_day: bool = False) -> pd.DataFrame:
    """마지막 봉의 종가를 실시간가로 갱신 — **당일 봉만** 패치.

    TTL 일봉 캐시(20h)가 어제 15:20 봉인데 라이브가를 어제 봉 종가에 덮으면
    어제 OHLC + 오늘 종가 혼합봉이 되어 intraday/gap_shape 가 깨진다.
    time 이 있고 마지막 봉이 오늘 거래일이 아니면 패치하지 않는다.
    append_if_new_day=True 면 당일 degenerate 봉을 append(시가=현재가 근사 — 차선).

    캔들은 TTL 캐시(수 초 지연)지만 매 틱 실시간가로 마지막봉을 패치하면, 전략 decide()가
    1초 단위로 현재가에 반응한다(데이트레 진입/청산 반응성). df 는 호출마다 새로 생성되므로
    캐시 원본을 훼손하지 않는다.
    """
    if price is None or df is None or len(df) == 0 or "close" not in df.columns:
        return df
    out = df.copy()
    bar_day = last_bar_trading_date(out, market)
    today = trading_date(market)
    if bar_day is not None and bar_day != today:
        if not append_if_new_day:
            return out
        tz = _market_tz(market)
        row = {"open": price, "high": price, "low": price, "close": price,
               "volume": 0.0}
        if "time" in out.columns:
            row["time"] = pd.Timestamp(datetime.now(tz).date(), tzinfo=tz)
        return pd.concat([out, pd.DataFrame([row])], ignore_index=True)
    i = out.index[-1]
    out.loc[i, "close"] = price
    if "high" in out.columns and price > out.loc[i, "high"]:
        out.loc[i, "high"] = price
    if "low" in out.columns and price < out.loc[i, "low"]:
        out.loc[i, "low"] = price
    return out


class TradingBot:
    def __init__(self, cfg: AppConfig, client, broker: Broker):
        self.cfg = cfg
        self.client = client
        self.broker = broker
        risk_cfg = cfg.risk
        self.risk = risk_manager_from_cfg(risk_cfg)
        # 유니버스 결정: 스크리너 사용 시 data/universe.yaml 우선, 없으면 config 의 수동 목록
        universe, source = self._resolve_universe(cfg)
        self.targets = []
        for market, items in universe.items():
            for it in items or []:
                strat_name = it["strategy"]
                params = cfg.strategies.get(strat_name, {})
                self.targets.append({
                    "market": market,
                    "symbol": it["symbol"],
                    "name": it.get("name", it["symbol"]),
                    "strategy": build_strategy(strat_name, params),
                })
        log.info("대상 종목 %d개 로드 (%s). dry_run=%s",
                 len(self.targets), source, cfg.dry_run)

    @staticmethod
    def _resolve_universe(cfg: AppConfig) -> tuple[dict, str]:
        if cfg.raw.get("screener", {}).get("enabled"):
            gen = ROOT / "data" / "universe.yaml"
            if gen.exists():
                data = yaml.safe_load(gen.read_text(encoding="utf-8")) or {}
                if data:
                    return data, f"스크리너 {gen.name}"
            log.warning("스크리너가 켜져 있으나 data/universe.yaml 이 없습니다. "
                        "scripts/screen.py 를 먼저 실행하세요. 우선 config.yaml 사용.")
        return cfg.universe, "config.yaml 수동 목록"

    def step(self) -> None:
        """한 사이클: 모든 종목을 한 번씩 평가."""
        # 종목 간 간격으로 캔들 그룹(MARKET_DATA_CHART=5/s) 한도 아래 유지.
        spacing = float(self.cfg.run.get("request_spacing_sec", 0.3))
        for i, t in enumerate(self.targets):
            try:
                self._evaluate(t)
            except Exception as e:  # 한 종목 실패가 전체를 멈추지 않게
                log.exception("[%s] 평가 실패: %s", t["symbol"], e)
            if spacing and i < len(self.targets) - 1:
                _time.sleep(spacing)

    def _evaluate(self, t: dict) -> None:
        market, symbol, strat = t["market"], t["symbol"], t["strategy"]
        if market not in self.cfg.run.get("trade_markets", ["KR", "US"]):
            return
        if not market_tradable(market, trading_sessions_from_raw(self.cfg.raw)):
            return

        # 토스 캔들은 symbol 으로 시장이 결정됨(KRX 6자리/US 티커). market 파라미터 없음.
        raw = self.client.get_candles(symbol, strat.candle_interval,
                                      count=max(strat.min_candles, 60))
        df = candles_to_df(raw)
        if len(df) < strat.min_candles:
            log.debug("[%s] 캔들 부족(%d)", symbol, len(df))
            return

        pos = self.broker.position(symbol)
        signal = strat.decide(df, pos)
        price = float(df["close"].iloc[-1])

        # 종가 청산: 변동성 돌파 전략의 보유분은 장 마감 직전 청산
        if (pos.is_open and strat.params.get("exit_at_session_end")
                and near_session_end(market)):
            self.broker.execute(Order(symbol, market, "SELL", pos.qty, price), "종가 청산",
                                store=self.broker.store)
            return

        if signal.action == Action.HOLD:
            log.debug("[%s] HOLD - %s", symbol, signal.reason)
            return

        # 신호 -> 주문. 한도/여력/킬스위치 검증은 모두 broker 내부의 하드 게이트가 수행.
        if signal.action == Action.BUY:
            # 전략 신호 weight 있으면 사용, 없으면 config base_position_pct
            w = (signal.target_weight if signal.target_weight is not None
                 else getattr(self.risk, "base_position_pct", 0.20))
            equity = self.risk.sizing_base_amount(self.broker, market)
            hard = float(getattr(self.risk, "max_position_pct", 0.25) or 0.25)
            # 보유분을 뺀 잔여 한도 — 안 빼면 이미 상한을 채운 종목에 계속 얹힌다
            # (게이트가 막지만 매 틱 거부 주문을 내는 낭비이고, min_lot 부활 경로에선
            # 실제로 통과했다). cycle.py 의 headroom 정의와 맞춘다.
            headroom = max(0.0, equity * hard - pos.qty * price)
            qty = self.risk.size_buy(
                market, price, w,
                base_equity=equity,
                notional_cap=headroom)
            self.broker.execute(Order(symbol, market, "BUY", qty, price), signal.reason,
                                store=self.broker.store)
        elif signal.action == Action.SELL and pos.is_open:
            self.broker.execute(Order(symbol, market, "SELL", pos.qty, price), signal.reason,
                                store=self.broker.store)

    def run_forever(self) -> None:
        interval = int(self.cfg.run.get("poll_interval_sec", 60))
        log.info("트레이딩 루프 시작 (폴링 %ds). Ctrl+C 로 종료.", interval)
        while True:
            try:
                self.step()
            except KeyboardInterrupt:
                log.info("종료합니다.")
                break
            except Exception as e:
                log.exception("사이클 오류: %s", e)
            _time.sleep(interval)

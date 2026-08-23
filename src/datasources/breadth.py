"""시장 국면(regime) 소스: 유니버스의 20일 이동평균 상회 비율(브레드스)로 risk-on/off 판정.

브레드스 = (종가 > MA20 인 종목 수) / (유효 종목 수).
  >= 0.6  -> risk_on
  <= 0.4  -> risk_off
  그 외   -> neutral
"""
from __future__ import annotations

import time

from .base import DataSource, SourceContext
from ..indicators import sma
from ..logging_setup import get_logger

log = get_logger("src.breadth")


def label_of(pct: float) -> str:
    """브레드스 비율 -> 국면 라벨. 임계값의 단일 정의점.

    장중 감시 루프(engine.loop)의 실시간 유니버스 브레드스도 이 함수를 재사용한다 —
    임계값이 두 곳에 흩어지면 같은 비율이 시장별로 다른 라벨을 받는다.
    """
    if pct >= 0.6:
        return "risk_on"
    if pct <= 0.4:
        return "risk_off"
    return "neutral"


_label = label_of        # 기존 내부 이름 호환(외부에서 쓰던 곳이 있으면 그대로 동작)


class BreadthSource(DataSource):
    name = "breadth"

    def __init__(self, synthetic_fetch=None, candle_fetch=None):
        # 테스트/드라이런용 캔들 공급자 (symbol, market) -> DataFrame
        self._synth = synthetic_fetch
        # 실데이터 캔들 공급자 (symbol, market) -> DataFrame. 미지정 시 토스(ctx.client) 사용
        # → 장중 갱신기는 Yahoo(fetch_history)를 주입해 토스 무접촉으로 브레드스 계산.
        self._candle_fetch = candle_fetch

    def fetch(self, ctx: SourceContext) -> dict:
        from ..runner import candles_to_df  # 지연 import (순환 회피)
        regime: dict = {}
        for market, symbols in ctx.symbols_by_market.items():
            above = total = 0
            for i, sym in enumerate(symbols):
                try:
                    if ctx.dry and self._synth:
                        df = self._synth(sym, market)
                    elif ctx.dry:
                        continue
                    elif self._candle_fetch:
                        df = self._candle_fetch(sym, market)
                    else:
                        df = candles_to_df(ctx.client.get_candles(sym, "1d", 30))
                    close = df["close"].astype(float)
                    if len(close) < 20:
                        continue
                    total += 1
                    if close.iloc[-1] > sma(close, 20).iloc[-1]:
                        above += 1
                except Exception as e:
                    log.warning("[%s] 브레드스 계산 실패: %s", sym, e)
                if ctx.spacing_sec and not ctx.dry and i < len(symbols) - 1:
                    time.sleep(ctx.spacing_sec)
            pct = (above / total) if total else 0.0
            # source: 이 소스는 항상 소수의 프록시 심볼(지수/ETF)로 계산된다 — 유니버스
            # 전 종목 실시간 브레드스(source=universe_live, engine.loop)와 구분해 뇌가
            # n 이 작은 대략치임을 알게 한다. 배치·폴백 경로 모두 같은 태그를 갖는다.
            regime[market] = {"breadth_above_ma20": round(pct, 3),
                              "n": total, "label": label_of(pct),
                              "source": "index_proxy"}
            log.info("[%s] 브레드스 %.0f%% (%d종목) -> %s", market, pct * 100, total,
                     regime[market]["label"])
        return {"regime": regime}

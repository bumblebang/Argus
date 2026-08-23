"""섹터·매크로 소스: 섹터 ETF 상대강도(모멘텀)로 주도/소외 섹터 판정.

각 섹터 대표 ETF 의 lookback 기간 수익률을 구해 랭킹한다.
상위=주도(leaders), 하위=소외(laggards). 토스 캔들을 쓰므로 호출은 pacing 으로 절약.
"""
from __future__ import annotations

import time

from .base import DataSource, SourceContext
from ..logging_setup import get_logger

log = get_logger("src.sector")


class SectorSource(DataSource):
    name = "sector"

    def __init__(self, sector_symbols: dict, lookback: int = 20, synthetic_fetch=None):
        # sector_symbols = {"US": {"Technology": "XLK", ...}, "KR": {...}}
        self.sector_symbols = sector_symbols
        self.lookback = lookback
        self._synth = synthetic_fetch

    def fetch(self, ctx: SourceContext) -> dict:
        from ..runner import candles_to_df
        out: dict = {}
        for market, sectors in self.sector_symbols.items():
            rets: dict[str, float] = {}
            items = list(sectors.items())
            for i, (name, sym) in enumerate(items):
                try:
                    if ctx.dry and self._synth:
                        df = self._synth(sym, market)
                    elif ctx.dry:
                        continue
                    else:
                        df = candles_to_df(ctx.client.get_candles(sym, "1d", self.lookback + 5))
                    close = df["close"].astype(float)
                    if len(close) > self.lookback:
                        rets[name] = round(close.iloc[-1] / close.iloc[-self.lookback] - 1, 4)
                except Exception as e:
                    log.warning("[%s/%s] 섹터 모멘텀 실패: %s", market, sym, e)
                if ctx.spacing_sec and not ctx.dry and i < len(items) - 1:
                    time.sleep(ctx.spacing_sec)
            ranked = sorted(rets, key=rets.get, reverse=True)
            out[market] = {
                "by_sector": rets,
                "leaders": ranked[:3],
                "laggards": ranked[-3:][::-1],
            }
            if ranked:
                log.info("[%s] 주도섹터 %s | 소외 %s", market,
                         out[market]["leaders"], out[market]["laggards"])
        return {"sectors": out}

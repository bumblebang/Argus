"""시장 심리/변동성 소스. 무료(Yahoo), 키 불필요.

VIX = S&P500 옵션의 30일 기대(내재) 변동성 지수. 본질은 '변동성' 지표지만
급락장에서 치솟아 공포의 대리지표로 쓰여 흔히 '공포지수'로 불린다.
참고: 미국(S&P500) 기준이며, 한국판은 VKOSPI(코스피200 변동성).

VIX 레벨로 risk 국면 라벨링 (변동성/공포 강도):
  < 15  calm · 15~20 normal · 20~30 elevated · > 30 fear
"""
from __future__ import annotations

import requests

from .base import DataSource, SourceContext
from ..logging_setup import get_logger

log = get_logger("src.sentiment")

_UA = {"User-Agent": "Mozilla/5.0 argus"}
YF_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def _vix_label(v: float) -> str:
    if v < 15:
        return "calm"
    if v < 20:
        return "normal"
    if v < 30:
        return "elevated"
    return "fear"


def _yahoo_last(symbol: str) -> float | None:
    r = requests.get(YF_CHART.format(symbol=symbol),
                     params={"range": "5d", "interval": "1d"}, headers=_UA, timeout=12)
    if r.status_code != 200:
        return None
    meta = r.json().get("chart", {}).get("result", [{}])[0].get("meta", {})
    px = meta.get("regularMarketPrice")
    return float(px) if px is not None else None


class SentimentSource(DataSource):
    name = "sentiment"

    # 추가 무료 지표를 여기에 확장 가능 (지수/원자재/크립토 등)
    def __init__(self, symbols: dict | None = None):
        # {"vix": "^VIX", ...}
        self.symbols = symbols or {"vix": "^VIX"}

    def fetch(self, ctx: SourceContext) -> dict:
        if ctx.dry:
            return {"sentiment": {"vix": 18.0, "vix_label": "normal"}}
        out: dict = {}
        for key, sym in self.symbols.items():
            try:
                val = _yahoo_last(sym.replace("^", "%5E"))
                if val is not None:
                    out[key] = round(val, 2)
                    if key == "vix":
                        out["vix_label"] = _vix_label(val)
            except Exception as e:
                log.warning("[%s] %s 조회 실패: %s", key, sym, e)
        if "vix" in out:
            log.info("VIX %.2f (%s)", out["vix"], out.get("vix_label"))
        return {"sentiment": out}

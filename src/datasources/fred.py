"""매크로 소스 (FRED, 미 연준 경제데이터). market_state.macro 에 저장.

핵심 매크로: 기준금리·국채금리·장단기 금리차(침체 신호)·달러인덱스·실업률·CPI(YoY).
"""
from __future__ import annotations

import time

import requests

from .base import DataSource, SourceContext
from ..logging_setup import get_logger

log = get_logger("src.macro")

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"

# 표시이름 -> FRED 시리즈 ID
SERIES = {
    "fed_funds": "FEDFUNDS",       # 실효 연방기금금리(월)
    "ust_10y": "DGS10",            # 10년 국채금리(일)
    "ust_2y": "DGS2",              # 2년 국채금리(일)
    "yield_curve_10y_2y": "T10Y2Y",  # 10Y-2Y 스프레드(음수=역전=침체신호)
    "usd_index": "DTWEXBGS",       # 무역가중 달러인덱스(광의)
    "unemployment": "UNRATE",      # 실업률(월)
}


class MacroSource(DataSource):
    name = "macro"

    def __init__(self, api_key: str, spacing_sec: float = 0.1):
        self.api_key = api_key
        self.spacing = spacing_sec

    def _obs(self, series_id: str, limit: int) -> list[dict]:
        r = requests.get(FRED_URL, params={
            "series_id": series_id, "api_key": self.api_key, "file_type": "json",
            "sort_order": "desc", "limit": limit}, timeout=15)
        r.raise_for_status()
        # FRED 결측치는 "." -> 건너뜀
        return [o for o in r.json().get("observations", []) if o.get("value") not in (".", "")]

    def _latest(self, series_id: str) -> float | None:
        vals = self._obs(series_id, limit=8)
        return float(vals[0]["value"]) if vals else None

    def fetch(self, ctx: SourceContext) -> dict:
        if ctx.dry:
            return {"macro": {"fed_funds": 4.5, "ust_10y": 4.2, "ust_2y": 4.0,
                              "yield_curve_10y_2y": 0.2, "cpi_yoy": 0.03}}
        out: dict = {}
        items = list(SERIES.items())
        for i, (name, sid) in enumerate(items):
            try:
                out[name] = self._latest(sid)
            except Exception as e:
                log.warning("[%s] FRED 실패: %s", sid, e)
            if self.spacing and i < len(items) - 1:
                time.sleep(self.spacing)
        # CPI 전년동월 대비(YoY): 최근값 vs 12개월 전
        try:
            cpi = self._obs("CPIAUCSL", limit=15)
            if len(cpi) > 12:
                out["cpi_yoy"] = round(float(cpi[0]["value"]) / float(cpi[12]["value"]) - 1, 4)
        except Exception as e:
            log.warning("CPI YoY 실패: %s", e)
        log.info("macro: 10Y=%s 곡선(10-2)=%s CPI_YoY=%s", out.get("ust_10y"),
                 out.get("yield_curve_10y_2y"), out.get("cpi_yoy"))
        return {"macro": out}

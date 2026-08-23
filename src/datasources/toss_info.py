"""토스 시황 소스: 환율(USDKRW) + 개장 캘린더(KR/US)."""
from __future__ import annotations

from .base import DataSource, SourceContext
from ..logging_setup import get_logger

log = get_logger("src.toss_info")


class TossInfoSource(DataSource):
    name = "toss_info"

    def fetch(self, ctx: SourceContext) -> dict:
        out: dict = {"fx": {}, "sessions": {}}
        if ctx.dry or ctx.client is None:
            out["fx"]["USDKRW"] = 1380.0
            out["sessions"] = {"KR": {"dry": True}, "US": {"dry": True}}
            return out
        try:
            r = ctx.client.get_exchange_rate("USD", "KRW")
            out["fx"]["USDKRW"] = float(r.get("rate"))
        except Exception as e:
            log.warning("환율 조회 실패: %s", e)
        for country in ("KR", "US"):
            try:
                out["sessions"][country] = ctx.client.get_market_calendar(country)
            except Exception as e:
                log.warning("%s 캘린더 조회 실패: %s", country, e)
        return out

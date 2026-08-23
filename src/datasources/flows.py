"""국내 수급: 외국인/기관/개인 순매수 (무료, 네이버). market_state.flows 에 저장.

KR 에서 가장 중요한 시그널 중 하나. 종목별 최근 거래일의 순매수 수량과 외국인 지분율.
"""
from __future__ import annotations

import time

import requests

from .base import DataSource, SourceContext
from ..logging_setup import get_logger

log = get_logger("src.flows")

_UA = {"User-Agent": "Mozilla/5.0 argus"}
NAVER_TREND = "https://m.stock.naver.com/api/stock/{code}/trend"


def _num(s) -> int | None:
    try:
        return int(str(s).replace(",", "").replace("+", "").strip())
    except (ValueError, AttributeError):
        return None


class FlowsSource(DataSource):
    name = "flows"

    def __init__(self, symbols: list[str], spacing_sec: float = 0.15):
        self.symbols = symbols
        self.spacing = spacing_sec

    def fetch(self, ctx: SourceContext) -> dict:
        if ctx.dry:
            return {"flows": {s: {"foreign_net": 0, "inst_net": 0, "indiv_net": 0}
                              for s in self.symbols}}
        out: dict[str, dict] = {}
        for i, sym in enumerate(self.symbols):
            try:
                r = requests.get(NAVER_TREND.format(code=sym), headers=_UA, timeout=12)
                rows = r.json()
                if rows:
                    streak = 0
                    for d in rows:
                        fn = _num(d.get("foreignerPureBuyQuant"))
                        if fn is not None and fn < 0:
                            streak += 1
                        else:
                            break
                    d = rows[0]
                    out[sym] = {
                        "date": d.get("bizdate"),
                        "foreign_net": _num(d.get("foreignerPureBuyQuant")),
                        "inst_net": _num(d.get("organPureBuyQuant")),
                        "indiv_net": _num(d.get("individualPureBuyQuant")),
                        "foreign_hold_ratio": d.get("foreignerHoldRatio"),
                        "foreign_net_streak": streak,
                    }
            except Exception as e:
                log.warning("[%s] 수급 조회 실패: %s", sym, e)
            if self.spacing and i < len(self.symbols) - 1:
                time.sleep(self.spacing)
        log.info("수급 %d종목", len(out))
        return {"flows": out}

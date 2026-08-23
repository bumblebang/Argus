"""지수·원자재·크립토·환율 레벨 (무료, Yahoo). market_state.markets 에 저장.

각 심볼의 최근가와 전일 대비 변동률을 담는다. 위험선호/매크로 국면 파악용.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import requests

from .base import DataSource, SourceContext
from ..logging_setup import get_logger

log = get_logger("src.markets")

_UA = {"User-Agent": "Mozilla/5.0 argus"}
YF = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def _day(epoch) -> date:
    return datetime.fromtimestamp(float(epoch), timezone.utc).date()


def _closes_with_time(res: dict) -> list[tuple[int, float]]:
    """일봉 응답 → [(epoch, close), ...] 오름차순. 마지막 거래일이 비면 meta 로 메운다.

    Yahoo 는 최근 거래일 봉을 빼거나 close 를 null 로 주는 때가 있다. None 만 걸러
    closes[-1]/-2 를 쓰면 창이 하루 밀려 '전일 대비'가 전전일이 된다. 값은 그럴듯해서
    국면 숫자가 틀린 채로 뇌에 들어간다.

    meta.regularMarketTime 의 거래일이 마지막 봉보다 뒤면 meta.regularMarketPrice 로
    한 점을 보충한다.
    """
    q = (res.get("indicators") or {}).get("quote") or [{}]
    closes = q[0].get("close") or []
    stamps = res.get("timestamp") or []
    pairs = [(int(t), float(c)) for t, c in zip(stamps, closes) if c is not None]
    if not pairs and closes:
        # 응답에 timestamp 가 없다(Yahoo 스키마 변화 등) → 날짜는 포기하되 값은 살린다.
        # markets 슬롯이 통째로 비면 뇌가 지수 등락을 아예 못 보므로 예전 동작을 바닥으로 둔다.
        return [(None, float(c)) for c in closes if c is not None]
    meta = res.get("meta") or {}
    mt, mp = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if mt is not None and mp is not None:
        try:
            if not pairs or _day(mt) > _day(pairs[-1][0]):
                pairs.append((int(mt), float(mp)))
        except (TypeError, ValueError, OSError):
            pass
    return pairs


class MarketsSource(DataSource):
    name = "markets"

    def __init__(self, symbols: dict[str, str]):
        self.symbols = symbols  # {"KOSPI": "^KS11", "Gold": "GC=F", "BTC": "BTC-USD", ...}

    def fetch(self, ctx: SourceContext) -> dict:
        if ctx.dry:
            return {"markets": {n: {"last": 100.0, "chg_1d": 0.0} for n in self.symbols}}
        out: dict[str, dict] = {}
        for name, sym in self.symbols.items():
            try:
                r = requests.get(YF.format(symbol=sym.replace("^", "%5E")),
                                 params={"range": "5d", "interval": "1d"},
                                 headers=_UA, timeout=12)
                res = r.json()["chart"]["result"][0]
                pairs = _closes_with_time(res)
                if not pairs:
                    continue
                (last_t, last), prev = pairs[-1], (pairs[-2][1] if len(pairs) > 1 else None)
                row = {"last": round(last, 2),
                       "chg_1d": round(last / prev - 1, 4) if prev else 0.0}
                if last_t is not None:
                    # 어느 거래일 기준인지 남긴다 — 소비자(뇌·공개 브리핑)가 낡은 값을
                    # 최신으로 오해하지 않게. 하루 밀림은 조용히 생긴다.
                    row["asof"] = _day(last_t).isoformat()
                out[name] = row
            except Exception as e:
                log.warning("[%s/%s] 시세 조회 실패: %s", name, sym, e)
        log.info("markets %d개", len(out))
        return {"markets": out}

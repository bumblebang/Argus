"""외국인 한도·소진율 → market_state.foreign_exhaustion.

MDCSTAT03701 전종목 스냅샷에서 후보 심볼만 추출. D-1/D-2 확정 특성.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from .base import DataSource, SourceContext
from .krx_client import MARKET_ID, bld_for, cache_get, cache_put, connect, num, ymd
from ..logging_setup import get_logger

log = get_logger("src.foreign_exhaustion")

EXHAUST_WATCH_PCT = 80.0  # 한도소진율 ≥ 이 값이면 watch


def parse_exhaustion_row(row: dict) -> dict | None:
    if not isinstance(row, dict):
        return None
    sym = (row.get("ISU_SRT_CD") or row.get("ISU_CD") or row.get("isuSrtCd") or "")
    sym = str(sym).strip()
    if len(sym) > 6:
        sym = sym[-6:]
    if not sym:
        return None
    hold = num(row.get("FORN_HD_QTY") or row.get("FORN_HD_SHR"))
    ratio = num(row.get("FORN_SHR_RT") or row.get("FORN_HD_RT"))
    exh = num(row.get("FORN_LMT_EXHST_RT") or row.get("EXHST_RT"))
    lim = num(row.get("FORN_ORD_LMT_QTY"))
    listed = num(row.get("LIST_SHRS"))
    out = {"symbol": sym, "source": "krx",
           "hold_qty": hold, "hold_ratio": ratio,
           "limit_qty": lim, "listed_shares": listed,
           "exhaustion_pct": exh,
           "watch": bool(exh is not None and exh >= EXHAUST_WATCH_PCT)}
    return out


class ForeignExhaustionSource(DataSource):
    name = "foreign_exhaustion"

    def __init__(self, symbols: list[str], *, spacing_sec: float = 0.25,
                 user: str | None = None, password: str | None = None,
                 watch_pct: float = EXHAUST_WATCH_PCT):
        self.symbols = set(symbols or [])
        self.spacing = spacing_sec
        self.user = user
        self.password = password
        self.watch_pct = watch_pct

    def fetch(self, ctx: SourceContext) -> dict:
        if ctx.dry:
            return {"foreign_exhaustion": {
                "005930": {"hold_ratio": 55.0, "exhaustion_pct": 55.0,
                           "watch": False, "source": "dry"},
                "source": "dry", "asof": "dry",
            }}
        if not self.symbols:
            return {"foreign_exhaustion": {}}
        day = ymd()
        cached = cache_get("foreign_exhaustion", day)
        if isinstance(cached, dict) and cached.get("_map"):
            picked = {s: cached["_map"][s] for s in self.symbols if s in cached["_map"]}
            if picked:
                picked["source"] = "krx_cache"
                picked["asof"] = cached.get("asof")
                return {"foreign_exhaustion": picked}

        client = connect(user=self.user, password=self.password,
                         spacing_sec=self.spacing)
        if client is None:
            return {"foreign_exhaustion": {}}
        bld = bld_for("foreign_exhaustion_all") or "dbms/MDC/STAT/standard/MDCSTAT03701"
        full_map: dict = {}
        for mid in ("STK", "KSQ"):
            rows = client.get_rows(
                bld, searchType="1", mktId=mid, trdDd=day, isuLmtRto="0",
                share="1", money="1",
            )
            for r in rows:
                p = parse_exhaustion_row(r)
                if p:
                    full_map[p["symbol"]] = {k: v for k, v in p.items() if k != "symbol"}
        if full_map:
            cache_put("foreign_exhaustion", day, {
                "_map": full_map,
                "asof": datetime.now(timezone.utc).isoformat(),
            })
        out = {s: full_map[s] for s in self.symbols if s in full_map}
        out["source"] = "krx"
        out["asof"] = datetime.now(timezone.utc).isoformat()
        out["note"] = "외국인 보유·한도는 장개시 기준 D-1/D-2 확정치"
        watches = [s for s, v in out.items()
                   if isinstance(v, dict) and v.get("watch")]
        log.info("foreign_exhaustion: %d종목 watch=%s", len(out) - 3, watches[:8])
        return {"foreign_exhaustion": out}


__all__ = ["ForeignExhaustionSource", "parse_exhaustion_row", "EXHAUST_WATCH_PCT"]

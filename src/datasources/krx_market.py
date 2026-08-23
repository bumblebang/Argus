"""VKOSPI·전종목 브레드스·지수 구성종목 (P2)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .base import DataSource, SourceContext
from .breadth import label_of
from .krx_client import bld_for, cache_get, cache_put, connect, num, ymd, ymd_dash
from ..logging_setup import get_logger

log = get_logger("src.krx_market")


def parse_vkospi_row(row: dict) -> dict | None:
    if not isinstance(row, dict):
        return None
    d = ymd_dash(row.get("TRD_DD") or row.get("trdDd"))
    close = (num(row.get("CLSPRC_IDX")) or num(row.get("CLOSE_PRC"))
             or num(row.get("TDD_CLSPRC")) or num(row.get("PRC")))
    if close is None:
        return None
    return {"date": d, "close": close}


class VkospiSource(DataSource):
    """VKOSPI 최신치 → market_state.vkospi (웹 로그인 경로; fear_kr inputs 폴백)."""
    name = "vkospi"

    def __init__(self, *, spacing_sec: float = 0.25,
                 user: str | None = None, password: str | None = None):
        self.spacing = spacing_sec
        self.user = user
        self.password = password

    def fetch(self, ctx: SourceContext) -> dict:
        if ctx.dry:
            return {"vkospi": {"close": 18.5, "date": "2026-01-01", "source": "dry"}}
        day = ymd()
        cached = cache_get("vkospi", day)
        if isinstance(cached, dict) and cached.get("close") is not None:
            return {"vkospi": cached}
        client = connect(user=self.user, password=self.password,
                         spacing_sec=self.spacing)
        if client is None:
            return {"vkospi": {}}
        bld = bld_for("vkospi") or "dbms/MDC/STAT/standard/MDCSTAT01201"
        end = date.today()
        start = end - timedelta(days=30)
        rows = client.get_rows(
            bld,
            indTpCd="1", idxIndCd="300",
            strtDd=start.strftime("%Y%m%d"),
            endDd=end.strftime("%Y%m%d"),
            # pykrx issue #266 파라미터 변형
            idxCd2="300",
        )
        best = None
        best_d = ""
        for r in rows:
            p = parse_vkospi_row(r)
            if not p:
                continue
            d = p.get("date") or ""
            if d >= best_d:
                best_d, best = d, p
        if not best:
            log.info("vkospi: 조회 실패/빈 — fear_kr 는 기존 대리 유지")
            return {"vkospi": {}}
        out = {**best, "source": "krx",
               "asof": datetime.now(timezone.utc).isoformat()}
        cache_put("vkospi", day, out)
        log.info("vkospi: close=%s date=%s", out.get("close"), out.get("date"))
        return {"vkospi": out}


class KrxBreadthSource(DataSource):
    """전종목 등락 1회 스냅샷으로 KR regime 보강(상승종목 비율).

    MA20 브레드스와 스케일이 다름 — regime.KR 에 breadth_up_pct / source 추가.
    기존 breadth_above_ma20 은 덮지 않는다.
    """
    name = "krx_breadth"

    def __init__(self, *, spacing_sec: float = 0.25,
                 user: str | None = None, password: str | None = None):
        self.spacing = spacing_sec
        self.user = user
        self.password = password

    def fetch(self, ctx: SourceContext) -> dict:
        if ctx.dry:
            return {"regime": {"KR": {
                "breadth_up_pct": 0.45, "n_up": 450, "n_all": 1000,
                "source_krx_breadth": "dry",
            }}}
        day = ymd()
        cached = cache_get("krx_breadth", day)
        if isinstance(cached, dict) and "breadth_up_pct" in cached:
            return {"regime": {"KR": cached}}
        client = connect(user=self.user, password=self.password,
                         spacing_sec=self.spacing)
        if client is None:
            return {}
        bld = bld_for("ohlcv_all") or "dbms/MDC/STAT/standard/MDCSTAT01501"
        up = down = flat = 0
        for mid in ("STK", "KSQ"):
            rows = client.get_rows(
                bld, mktId=mid, trdDd=day, share="1", money="1",
            )
            for r in rows:
                fluc = r.get("FLUC_TP_CD") or r.get("FLUC_TP")
                # 1=상승 2=하락 3=보합 (관례)
                if str(fluc) == "1":
                    up += 1
                elif str(fluc) == "2":
                    down += 1
                else:
                    chg = num(r.get("CMPPREVDD_PRC") or r.get("FLUC_RT"))
                    if chg is None:
                        flat += 1
                    elif chg > 0:
                        up += 1
                    elif chg < 0:
                        down += 1
                    else:
                        flat += 1
        n = up + down + flat
        if n < 50:
            log.info("krx_breadth: 표본 부족 n=%s", n)
            return {}
        pct = up / n
        slot = {
            "breadth_up_pct": round(pct, 4),
            "n_up": up, "n_down": down, "n_flat": flat, "n_all": n,
            "label_up": label_of(pct),
            "source_krx_breadth": "krx",
            "asof": datetime.now(timezone.utc).isoformat(),
        }
        cache_put("krx_breadth", day, slot)
        log.info("krx_breadth: up_pct=%.3f n=%d", pct, n)
        return {"regime": {"KR": slot}}


class IndexConstituentsSource(DataSource):
    """대표 지수 구성종목 → market_state.index_constituents."""
    name = "index_constituents"

    # (label, indTpCd/group, idxIndCd) — KOSPI200 관례
    DEFAULT_INDICES = (
        ("KOSPI200", "1", "028"),
        ("KOSDAQ150", "2", "203"),
    )

    def __init__(self, indices=None, *, spacing_sec: float = 0.25,
                 user: str | None = None, password: str | None = None):
        self.indices = indices or self.DEFAULT_INDICES
        self.spacing = spacing_sec
        self.user = user
        self.password = password

    def fetch(self, ctx: SourceContext) -> dict:
        if ctx.dry:
            return {"index_constituents": {
                "KOSPI200": ["005930", "000660"],
                "source": "dry",
            }}
        day = ymd()
        cached = cache_get("index_constituents", day)
        if isinstance(cached, dict) and len(cached) > 1:
            return {"index_constituents": cached}
        client = connect(user=self.user, password=self.password,
                         spacing_sec=self.spacing)
        if client is None:
            return {"index_constituents": {}}
        bld = bld_for("index_constituents") or "dbms/MDC/STAT/standard/MDCSTAT00601"
        out: dict = {"source": "krx",
                     "asof": datetime.now(timezone.utc).isoformat()}
        for label, group, idx in self.indices:
            rows = client.get_rows(
                bld, trdDd=day, indTpCd=group, idxIndCd=idx,
                indIdx=group, indIdx2=idx,
            )
            syms = []
            for r in rows:
                s = r.get("ISU_SRT_CD") or r.get("ISU_CD") or ""
                s = str(s).strip()
                if len(s) > 6:
                    s = s[-6:]
                if s and s not in syms:
                    syms.append(s)
            if syms:
                out[label] = syms
        if len(out) <= 2:
            return {"index_constituents": {}}
        cache_put("index_constituents", day, out)
        log.info("index_constituents: %s",
                 {k: len(v) for k, v in out.items() if isinstance(v, list)})
        return {"index_constituents": out}


__all__ = ["VkospiSource", "KrxBreadthSource", "IndexConstituentsSource",
           "parse_vkospi_row"]

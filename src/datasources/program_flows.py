"""프로그램매매 순매수 → market_state.program_flows.

bld MDCSTAT02601 (구 02501 은 블록거래 화면으로 바뀜).
일별: strtDd=endDd=해당일, ITM_TP_NM=차익/비차익/전체.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from .base import DataSource, SourceContext
from .krx_client import bld_for, connect, num, ymd_dash
from ..logging_setup import get_logger

log = get_logger("src.program_flows")

_BLD = "dbms/MDC/STAT/standard/MDCSTAT02601"
_LOOKBACK = 14


def parse_program_rows(rows: list[dict], *, asof: str | None = None) -> dict | None:
    """MDCSTAT02601 3행(차익/비차익/전체) → {date, arb_net, nonarb_net, total_net}."""
    if not rows:
        return None
    arb = nonarb = total = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = str(row.get("ITM_TP_NM") or "").strip()
        val = num(row.get("NETBID_TRDVAL"))
        if label == "차익":
            arb = val
        elif label == "비차익":
            nonarb = val
        elif label == "전체":
            total = val
    if total is None and arb is not None and nonarb is not None:
        total = arb + nonarb
    if arb is None and nonarb is None and total is None:
        return None
    return {"date": asof, "arb_net": arb, "nonarb_net": nonarb, "total_net": total}


def parse_program_row(row: dict) -> dict | None:
    """하위호환: 단일 행(구 스키마)도 허용."""
    if not isinstance(row, dict):
        return None
    if row.get("ITM_TP_NM"):
        return parse_program_rows([row])
    d = ymd_dash(row.get("TRD_DD") or row.get("trdDd") or row.get("DATE"))
    arb = (num(row.get("ARB_NETBID_TRDVAL")) or num(row.get("ARB_TRDVAL"))
           or num(row.get("CHAIG_NTVAL")) or num(row.get("TRDVAL1")))
    nonarb = (num(row.get("NONARB_NETBID_TRDVAL")) or num(row.get("NON_ARB_TRDVAL"))
              or num(row.get("BCHAIG_NTVAL")) or num(row.get("TRDVAL2")))
    total = (num(row.get("NETBID_TRDVAL")) or num(row.get("TOT_NTVAL"))
             or num(row.get("TRDVAL3")))
    if total is None and arb is not None and nonarb is not None:
        total = arb + nonarb
    if d is None and total is None and arb is None:
        return None
    return {"date": d, "arb_net": arb, "nonarb_net": nonarb, "total_net": total}


def _latest_day_rows(client, bld: str, mkt_id: str,
                     lookback: int = _LOOKBACK) -> tuple[str | None, list[dict]]:
    """최근 lookback 일 중 데이터가 있는 마지막 거래일 행."""
    for lag in range(lookback):
        d = date.today() - timedelta(days=lag)
        ds = d.strftime("%Y%m%d")
        rows = client.get_rows(bld, strtDd=ds, endDd=ds, mktId=mkt_id)
        if rows:
            return f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}", rows
    return None, []


class ProgramFlowsSource(DataSource):
    name = "program_flows"

    def __init__(self, *, spacing_sec: float = 0.25,
                 user: str | None = None, password: str | None = None):
        self.spacing = spacing_sec
        self.user = user
        self.password = password

    def fetch(self, ctx: SourceContext) -> dict:
        if ctx.dry:
            return {"program_flows": {
                "KOSPI": {"date": "2026-01-01", "arb_net": 1e10, "nonarb_net": -5e9,
                          "total_net": 5e9},
                "KOSDAQ": {"date": "2026-01-01", "arb_net": 0.0, "nonarb_net": 1e9,
                           "total_net": 1e9},
                "source": "dry", "asof": "dry",
            }}
        client = connect(user=self.user, password=self.password,
                         spacing_sec=self.spacing)
        if client is None:
            return {"program_flows": {}}
        bld = bld_for("program_trading") or _BLD
        out: dict = {"source": "krx",
                     "asof": datetime.now(timezone.utc).isoformat()}
        for mkt, mid in (("KOSPI", "STK"), ("KOSDAQ", "KSQ")):
            asof, rows = _latest_day_rows(client, bld, mid)
            parsed = parse_program_rows(rows, asof=asof)
            if parsed:
                out[mkt] = parsed
        if "KOSPI" not in out and "KOSDAQ" not in out:
            log.info("program_flows: 빈 결과(bld/휴장) — 스킵")
            return {"program_flows": {}}
        log.info("program_flows: KOSPI total=%s KOSDAQ total=%s",
                 (out.get("KOSPI") or {}).get("total_net"),
                 (out.get("KOSDAQ") or {}).get("total_net"))
        return {"program_flows": out}


__all__ = ["ProgramFlowsSource", "parse_program_row", "parse_program_rows"]

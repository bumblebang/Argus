"""KRX 투자자별 수급 — Naver flows/flows_market 을 보강(우선 병합).

시장: MDCSTAT02202 일별추이(순매수·거래대금).
종목: MDCSTAT02302. 실패 시 빈 dict → 기존 Naver 소스 유지.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from pathlib import Path

from .base import DataSource, SourceContext
from .flows_market import (enrich_from_history, update_history, _load_history,
                           _save_history, _HISTORY)
from .krx_client import bld_for, connect, num, ymd_dash
from ..logging_setup import get_logger

log = get_logger("src.krx_flows")

# MDCSTAT02202 askBid=3(순매수) trdVolVal=2(대금) 컬럼 관례
# TRDVAL1=기관합계 TRDVAL2=기타법인 TRDVAL3=개인 TRDVAL4=외국인합계 (연도별 변동)
_MKT_KEYS = {
    "inst_net": ("TRDVAL1", "INVST_TP_NM_INST", "기관합계"),
    "indiv_net": ("TRDVAL3", "TRDVAL_INDIV"),
    "foreign_net": ("TRDVAL4", "TRDVAL_FORN", "외국인합계"),
}


def parse_market_investor_row(row: dict) -> dict | None:
    if not isinstance(row, dict):
        return None
    d = ymd_dash(row.get("TRD_DD") or row.get("trdDd"))
    foreign = num(row.get("TRDVAL4")) or num(row.get("TRDVAL_4"))
    inst = num(row.get("TRDVAL1")) or num(row.get("TRDVAL_1"))
    indiv = num(row.get("TRDVAL3")) or num(row.get("TRDVAL_3"))
    # 상세뷰(12유형)면 외국인 컬럼이 뒤로 밀림 — 합계 키 후보
    if foreign is None:
        foreign = num(row.get("TRDVAL11")) or num(row.get("FORN_NTVAL"))
    if d is None or foreign is None:
        return None
    return {"date": d.replace("-", "") if d and "-" in d else (d or "").replace("-", ""),
            "foreign_net": foreign, "inst_net": inst, "indiv_net": indiv,
            "_date_iso": d}


def _normalize_bizdate(row: dict) -> dict:
    """flows_market history 가 bizdate YYYYMMDD 를 쓰므로 맞춤."""
    out = {
        "date": (row.get("date") or "").replace("-", "")[:8],
        "foreign_net": row.get("foreign_net"),
        "inst_net": row.get("inst_net"),
        "indiv_net": row.get("indiv_net"),
    }
    return out


def parse_ticker_investor_row(row: dict) -> dict | None:
    """종목 일별 투자자 순매수(수량 우선)."""
    if not isinstance(row, dict):
        return None
    d = ymd_dash(row.get("TRD_DD") or row.get("trdDd"))
    foreign = (num(row.get("TRDVAL4")) or num(row.get("FRGN_NTVAL"))
               or num(row.get("외국인합계")))
    inst = num(row.get("TRDVAL1")) or num(row.get("ORG_NTVAL"))
    indiv = num(row.get("TRDVAL3")) or num(row.get("INDIV_NTVAL"))
    if d is None and foreign is None:
        return None
    return {
        "date": (d or "").replace("-", "")[:8],
        "foreign_net": int(foreign) if foreign is not None else None,
        "inst_net": int(inst) if inst is not None else None,
        "indiv_net": int(indiv) if indiv is not None else None,
        "source": "krx",
    }


class KrxFlowsMarketSource(DataSource):
    """시장 수급 — 성공 시 Naver 결과를 덮어쓴다(build 순서상 뒤에 두면 됨)."""
    name = "krx_flows_market"

    def __init__(self, *, spacing_sec: float = 0.25,
                 user: str | None = None, password: str | None = None,
                 history_path=None):
        self.spacing = spacing_sec
        self.user = user
        self.password = password
        self.history_path = history_path

    def fetch(self, ctx: SourceContext) -> dict:
        if ctx.dry:
            return {"flows_market": {
                "KOSPI": {"date": "20260101", "foreign_net": 5000.0, "inst_net": -2000.0,
                          "indiv_net": -3000.0, "foreign_net_3d": 12000.0,
                          "foreign_net_p90": 4000.0},
                "KOSDAQ": {"date": "20260101", "foreign_net": 800.0},
                "source": "krx", "asof": "dry", "nxt_sum": True,
            }}
        client = connect(user=self.user, password=self.password,
                         spacing_sec=self.spacing)
        if client is None:
            return {}
        bld = bld_for("investor_market_daily") or "dbms/MDC/STAT/standard/MDCSTAT02202"
        end = date.today()
        start = end - timedelta(days=25)
        hist = _load_history(Path(self.history_path) if self.history_path else _HISTORY)
        out: dict = {}
        for mkt, mid in (("KOSPI", "STK"), ("KOSDAQ", "KSQ")):
            rows = client.get_rows(
                bld,
                strtDd=start.strftime("%Y%m%d"),
                endDd=end.strftime("%Y%m%d"),
                mktId=mid,
                etf="", etn="", elw="",
                trdVolVal="2", askBid="3",
                share="1", money="1",
            )
            parsed_days = []
            for r in rows:
                p = parse_market_investor_row(r)
                if p:
                    parsed_days.append(_normalize_bizdate(p))
            if not parsed_days:
                continue
            parsed_days.sort(key=lambda x: x.get("date") or "")
            for day in parsed_days:
                hist = update_history(hist, mkt, day)
            latest = parsed_days[-1]
            out[mkt] = enrich_from_history(latest, hist.get(mkt) or [])
        if not out:
            log.info("krx_flows_market: 빈 결과 — Naver 유지")
            return {}
        try:
            hp = Path(self.history_path) if self.history_path else _HISTORY
            _save_history(hp, hist)
        except OSError as e:
            log.warning("krx flows_market history 저장 실패: %s", e)
        out["source"] = "krx"
        out["nxt_sum"] = True
        out["asof"] = datetime.now(timezone.utc).isoformat()
        log.info("krx_flows_market: KOSPI=%s KOSDAQ=%s",
                 (out.get("KOSPI") or {}).get("foreign_net"),
                 (out.get("KOSDAQ") or {}).get("foreign_net"))
        return {"flows_market": out}


class KrxFlowsSource(DataSource):
    """종목 수급 — 심볼별 최신 1행. 성공한 심볼만 병합."""
    name = "krx_flows"

    def __init__(self, symbols: list[str], *, spacing_sec: float = 0.25,
                 user: str | None = None, password: str | None = None):
        self.symbols = list(symbols or [])
        self.spacing = spacing_sec
        self.user = user
        self.password = password

    def fetch(self, ctx: SourceContext) -> dict:
        if ctx.dry:
            return {"flows": {s: {"date": "20260101", "foreign_net": 0,
                                  "inst_net": 0, "indiv_net": 0, "source": "krx"}
                              for s in self.symbols}}
        client = connect(user=self.user, password=self.password,
                         spacing_sec=self.spacing)
        if client is None or not self.symbols:
            return {}
        bld = bld_for("investor_ticker_daily") or "dbms/MDC/STAT/standard/MDCSTAT02302"
        end = date.today()
        start = end - timedelta(days=10)
        out: dict = {}
        for sym in self.symbols:
            rows = client.get_rows(
                bld,
                strtDd=start.strftime("%Y%m%d"),
                endDd=end.strftime("%Y%m%d"),
                isuCd=sym,  # 일부 화면은 ISIN — short code 도 허용되는 경우 많음
                isuSrtCd=sym,
                trdVolVal="1", askBid="3",
                inqTpCd="2", detailView="0",
                share="1", money="1",
            )
            best = None
            best_d = ""
            for r in rows:
                p = parse_ticker_investor_row(r)
                if not p:
                    continue
                d = p.get("date") or ""
                if d >= best_d:
                    best_d, best = d, p
            if best and best.get("foreign_net") is not None:
                out[sym] = best
        if not out:
            return {}
        log.info("krx_flows: %d/%d", len(out), len(self.symbols))
        return {"flows": out}


__all__ = ["KrxFlowsMarketSource", "KrxFlowsSource",
           "parse_market_investor_row", "parse_ticker_investor_row"]

"""종목별 신용·공매도 포지셔닝 + 시장 공매도 집계.

소스:
- 종목 잔고: MDCSTAT30501 (T+2, BAL_QTY/BAL_RTO) — 전종목 1회 후 심볼 필터
- 종목 일별 공매도거래(폴백): MDCSTAT30001 — isuCd=ISIN 필요
- 시장 투자자별: MDCSTAT30301
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .base import DataSource, SourceContext
from .krx_client import bld_for, connect, num, ymd_dash
from ..logging_setup import get_logger

log = get_logger("src.positioning")

_HISTORY = Path(__file__).resolve().parents[2] / "data" / "positioning_history.json"
SPIKE_PCT = 0.15
_BAL_BLD = "dbms/MDC/STAT/srt/MDCSTAT30501"
_SHORT_BLD = "dbms/MDC/STAT/srt/MDCSTAT30001"
_ISIN_BLD = "dbms/MDC/STAT/standard/MDCSTAT01501"


def parse_krx_short_row(row: dict) -> dict:
    """KRX output 행 → 표준 positioning 필드(있는 것만)."""
    short_bal = (num(row.get("BAL_QTY"))
                 or num(row.get("STR_CONST_VAL1"))
                 or num(row.get("SRTSLL_NTPOS_QTY"))
                 or num(row.get("SHORT_BAL"))
                 or num(row.get("CVSRTSELL_TRDVOL")))
    short_amt = (num(row.get("BAL_AMT"))
                 or num(row.get("STR_CONST_VAL2"))
                 or num(row.get("SRTSLL_NTPOS_VAL"))
                 or num(row.get("CVSRTSELL_TRDVAL")))
    date_s = ymd_dash(row.get("TRD_DD") or row.get("trdDd") or row.get("DATE")
                      or row.get("RPT_DUTY_OCCR_DD") or row.get("_asof"))
    out: dict = {"source": "krx", "asof": date_s}
    if short_bal is not None:
        out["short_balance"] = short_bal
    if short_amt is not None:
        out["short_value"] = short_amt
    ratio = (num(row.get("BAL_RTO")) or num(row.get("SRTSLL_NTPOS_RT"))
             or num(row.get("SHORT_RATIO")))
    if ratio is not None:
        out["short_ratio"] = ratio
    credit_bal = (num(row.get("CRD_BAL")) or num(row.get("CREDIT_BAL"))
                  or num(row.get("CRDTR_BAL_QTY")) or num(row.get("CRD_BAL_QTY"))
                  or num(row.get("LOAN_BAL")))
    credit_ratio = (num(row.get("CRD_RT")) or num(row.get("CREDIT_RATIO"))
                    or num(row.get("CRDTR_BAL_RT")))
    if credit_bal is not None:
        out["credit_balance"] = credit_bal
    if credit_ratio is not None:
        out["credit_ratio"] = credit_ratio
    return out


def detect_spike(curr: dict, prev: dict | None,
                 threshold: float = SPIKE_PCT) -> bool:
    if not prev or not curr:
        return False
    for key in ("short_balance", "credit_balance", "short_ratio", "credit_ratio"):
        a, b = curr.get(key), prev.get(key)
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            continue
        if b == 0:
            if a != 0:
                return True
            continue
        if abs(a - b) / abs(b) >= threshold:
            return True
    return False


def parse_short_market_row(row: dict) -> dict | None:
    """투자자별 공매도(MDCSTAT30301) 1행 → {date, inst, indiv, foreign, total}."""
    if not isinstance(row, dict):
        return None
    d = ymd_dash(row.get("TRD_DD") or row.get("trdDd"))
    inst = num(row.get("STR_CONST_VAL1"))
    indiv = num(row.get("STR_CONST_VAL2"))
    foreign = num(row.get("STR_CONST_VAL3"))
    other = num(row.get("STR_CONST_VAL4"))
    total = num(row.get("STR_CONST_VAL5"))
    if d is None and total is None and foreign is None:
        return None
    return {"date": d, "inst": inst, "indiv": indiv, "foreign": foreign,
            "other": other, "total": total}


def _load_hist(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_hist(path: Path, hist: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _latest_balance_map(client, mkt_tp: str, lookback: int = 10
                        ) -> tuple[str | None, dict[str, dict]]:
    """30501 전종목 → {ISU_CD: row}. T+2 라 최근 휴일 스킵."""
    bld = bld_for("short_잔고_전종목") or _BAL_BLD
    for lag in range(lookback):
        d = date.today() - timedelta(days=lag)
        ds = d.strftime("%Y%m%d")
        rows = client.get_rows(bld, trdDd=ds, mktTpCd=mkt_tp)
        if not rows:
            continue
        by_sym = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            sym = str(r.get("ISU_CD") or r.get("ISU_SRT_CD") or "").strip()
            if sym:
                r = dict(r)
                r["_asof"] = f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}"
                by_sym[sym] = r
        if by_sym:
            return f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}", by_sym
    return None, {}


def _isin_map(client) -> dict[str, str]:
    """단축코드 → ISIN (01501). 실패 시 빈 dict."""
    bld = bld_for("ohlcv_all") or _ISIN_BLD
    out: dict[str, str] = {}
    for mid in ("STK", "KSQ"):
        for lag in range(5):
            d = (date.today() - timedelta(days=lag)).strftime("%Y%m%d")
            rows = client.get_rows(bld, mktId=mid, trdDd=d)
            if not rows:
                continue
            for r in rows:
                srt = str(r.get("ISU_SRT_CD") or "").strip()
                isin = str(r.get("ISU_CD") or "").strip()
                if srt and isin.startswith("KR"):
                    out[srt] = isin
            break
    return out


class PositioningSource(DataSource):
    name = "positioning"

    def __init__(self, symbols: list[str], *,
                 history_path: Path | None = None,
                 spacing_sec: float = 0.25,
                 user: str | None = None,
                 password: str | None = None,
                 spike_pct: float = SPIKE_PCT,
                 fetch_market: bool = True):
        self.symbols = list(symbols or [])
        self.history_path = Path(history_path) if history_path else _HISTORY
        self.spacing = spacing_sec
        self.user = user
        self.password = password
        self.spike_pct = spike_pct
        self.fetch_market = fetch_market

    def fetch(self, ctx: SourceContext) -> dict:
        if ctx.dry:
            return {
                "positioning": {
                    "005930": {"short_balance": 1000.0, "short_ratio": 1.2,
                               "credit_balance": 5000.0, "credit_ratio": 3.0,
                               "spike": False, "source": "dry", "asof": "2026-01-01"},
                },
                "short_market": {
                    "KOSPI": {"date": "2026-01-01", "foreign": 1e9, "total": 2e9},
                    "source": "dry",
                },
            }
        client = connect(user=self.user, password=self.password,
                         spacing_sec=self.spacing)
        if client is None:
            log.info("KRX_USER/KRX_PASS 없음 또는 로그인 실패 → positioning 스킵")
            return {"positioning": {}, "short_market": {}}

        hist = _load_hist(self.history_path)
        out: dict[str, dict] = {}
        want = set(self.symbols)

        # 1) 잔고 스냅샷(전종목) — 심볼당 API 호출 없음
        bal_maps: dict[str, dict] = {}
        for tp in ("1", "2"):
            asof, mp = _latest_balance_map(client, tp)
            if mp:
                bal_maps.update(mp)
                log.info("positioning 잔고 mktTp=%s asof=%s n=%d", tp, asof, len(mp))

        for sym in want:
            row = bal_maps.get(sym)
            if not row:
                continue
            parsed = parse_krx_short_row(row)
            if "short_balance" not in parsed and "short_ratio" not in parsed:
                continue
            prev = hist.get(sym)
            parsed["spike"] = detect_spike(parsed, prev, self.spike_pct)
            parsed["asof"] = parsed.get("asof") or datetime.now(timezone.utc).isoformat()
            out[sym] = parsed

        # 2) 잔고에 없는 심볼 → 30001 일별 공매도거래(ISIN)
        missing = [s for s in want if s not in out]
        if missing:
            isins = _isin_map(client)
            bld_short = bld_for("short_종합") or _SHORT_BLD
            end = date.today()
            start = end - timedelta(days=20)
            for sym in missing:
                isin = isins.get(sym)
                if not isin:
                    continue
                rows = client.get_rows(
                    bld_short,
                    isuCd=isin,
                    strtDd=start.strftime("%Y%m%d"),
                    endDd=end.strftime("%Y%m%d"),
                )
                if not rows:
                    continue
                best, best_d = None, ""
                for cand in rows:
                    if not isinstance(cand, dict):
                        continue
                    d = str(cand.get("TRD_DD") or "")
                    if d >= best_d:
                        best_d, best = d, cand
                if not best:
                    continue
                parsed = parse_krx_short_row(best)
                if "short_balance" not in parsed and "short_ratio" not in parsed:
                    continue
                prev = hist.get(sym)
                parsed["spike"] = detect_spike(parsed, prev, self.spike_pct)
                parsed["asof"] = parsed.get("asof") or ymd_dash(best_d)
                out[sym] = parsed

        for sym, parsed in out.items():
            hist[sym] = {k: parsed[k] for k in
                         ("short_balance", "short_ratio", "credit_balance",
                          "credit_ratio", "short_value") if k in parsed}
        try:
            _save_hist(self.history_path, hist)
        except OSError as e:
            log.warning("positioning history 저장 실패: %s", e)

        short_market: dict = {}
        if self.fetch_market:
            short_market = self._fetch_short_market(client)
        log.info("positioning: %d/%d 종목 short_market=%s",
                 len(out), len(self.symbols), bool(short_market))
        return {"positioning": out, "short_market": short_market}

    def _fetch_short_market(self, client) -> dict:
        bld = bld_for("short_투자자별") or "dbms/MDC/STAT/srt/MDCSTAT30301"
        end = date.today()
        start = end - timedelta(days=10)
        out: dict = {"source": "krx", "asof": datetime.now(timezone.utc).isoformat()}
        for name, tp in (("KOSPI", "1"), ("KOSDAQ", "2")):
            rows = client.get_rows(
                bld,
                strtDd=start.strftime("%Y%m%d"),
                endDd=end.strftime("%Y%m%d"),
                inqCondTpCd="2",
                mktTpCd=tp,
            )
            if not rows:
                continue
            best = None
            best_d = ""
            for r in rows:
                parsed = parse_short_market_row(r)
                if not parsed or not parsed.get("date"):
                    continue
                if parsed["date"] >= best_d:
                    best_d, best = parsed["date"], parsed
            if best:
                out[name] = best
        return out if len(out) > 2 else {}


__all__ = ["PositioningSource", "parse_krx_short_row", "detect_spike",
           "parse_short_market_row", "SPIKE_PCT"]

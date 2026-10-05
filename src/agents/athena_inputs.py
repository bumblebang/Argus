"""Athena 종목 입력 보강 — 재무·수급·공매도 (라이브·리플레이 공용).

배경(10-05): market_state 의 재무·수급 슬롯은 고정 base_universe 앞 10종목만 채운다
(fundamentals_max). Athena 유니버스(거래대금 상위 ~100)는 91% 가 재무·수급 없이
차트만 보고 판정됐다. 프롬프트·컨텍스트는 이 슬롯을 전제로 설계돼 있다.

모든 조회는 asof(판단일, 시장 현지 날짜)를 받아 그 날 **이전**에 공개된 데이터만 쓴다 —
라이브는 오늘, 리플레이는 과거 날짜. 실패는 None(도시에 생성은 계속).

- KR 재무: 밸류 스캔의 DART 캐시(value_financials_cache) — API 호출 없음.
  연간 사업보고서는 결산 후 90일(3월 말) 안에 나오므로 4/1 이전이면 전전년도까지만.
  시총은 랭킹 캐시 시총을 그 날 주가로 환산(발행주식 수 불변 근사).
- KR 수급: KRX 종목별 투자자 순매수(금액, ISIN 필요) — asof 전날까지 최근 20거래일.
- KR 공매도: KRX 전종목 잔고(T+2 공시) — asof 3일 전부터 거꾸로.
- US 재무: Finnhub 현재값(라이브 전용 — 과거 시점 재현 불가라 리플레이에선 안 씀).
"""
from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger

log = get_logger("agents.athena_inputs")

ISIN_CACHE = "krx_isin_map.json"
ISIN_MAX_AGE_SEC = 7 * 86400
FLOW_LOOKBACK_DAYS = 35          # 달력일 — 20거래일 확보
SHORT_PUBLISH_LAG_DAYS = 3       # T+2 공시 + 여유


def public_fiscal_years(asof: date) -> tuple[int, int]:
    """asof 시점에 사업보고서가 공개됐을 최신 회계연도와 그 전년."""
    y = asof.year - 1 if asof >= date(asof.year, 4, 1) else asof.year - 2
    return (y, y - 1)


def _eok(v: float | None) -> float | None:
    return round(v / 1e8, 1) if v is not None else None


def summarize_flows(rows: list[dict], *, asof: date) -> dict | None:
    """일별 투자자 순매수(원) → 최근 5·20거래일 합(억원)과 외국인 연속 방향."""
    days = sorted((r for r in rows if r.get("date") and r["date"] < asof.strftime("%Y%m%d")),
                  key=lambda r: r["date"], reverse=True)[:20]
    if not days:
        return None

    def total(key: str, n: int) -> float | None:
        vals = [r.get(key) for r in days[:n] if r.get(key) is not None]
        return _eok(sum(vals)) if vals else None

    streak = 0
    first = days[0].get("foreign_net")
    if first:
        sign = 1 if first > 0 else -1
        for r in days:
            v = r.get("foreign_net")
            if v is None or v == 0 or (v > 0) != (sign > 0):
                break
            streak += sign
    return {
        "unit": "억원(순매수 금액)",
        "date": days[0]["date"],
        "foreign_5d": total("foreign_net", 5), "foreign_20d": total("foreign_net", 20),
        "inst_5d": total("inst_net", 5), "inst_20d": total("inst_net", 20),
        "indiv_5d": total("indiv_net", 5), "indiv_20d": total("indiv_net", 20),
        "foreign_streak_days": streak,
        "source": "krx",
    }


class AthenaInputs:
    """종목 입력 보강기. 데이터 원천은 주입 가능(테스트·리플레이)."""

    def __init__(self, data_dir: Path | str, *, krx_client: Any = None,
                 fin_cache: dict | None = None, corp_map: dict | None = None,
                 mcap: dict[str, tuple[float, float]] | None = None,
                 isin_map: dict[str, str] | None = None,
                 finnhub_key: str | None = None):
        self.data_dir = Path(data_dir)
        self.krx = krx_client
        self.fin_cache = fin_cache or {}
        self.corp_map = corp_map or {}
        self.mcap = mcap or {}
        self.isin_map = isin_map or {}
        self.finnhub_key = finnhub_key
        self._short_maps: dict[tuple[str, str], tuple[str | None, dict]] = {}

    # ── 생성 ──────────────────────────────────────────────────────
    @classmethod
    def load(cls, data_dir: Path | str, *, connect_krx: bool = True) -> "AthenaInputs":
        d = Path(data_dir)

        def _json(name: str) -> dict:
            try:
                v = json.loads((d / name).read_text(encoding="utf-8"))
                return v if isinstance(v, dict) else {}
            except (OSError, ValueError):
                return {}

        mcap: dict[str, tuple[float, float]] = {}
        for row in ((_json("ranking_cache.json").get("KR") or {}).get("rows") or []):
            try:
                if row.get("market_cap") and row.get("price"):
                    mcap[str(row["symbol"])] = (float(row["market_cap"]), float(row["price"]))
            except (TypeError, ValueError, KeyError):
                continue
        krx = None
        if connect_krx:
            try:
                from ..datasources.krx_client import connect
                krx = connect()
            except Exception as e:
                log.warning("KRX 연결 실패(수급·공매도 생략): %s", e)
        inst = cls(d, krx_client=krx, fin_cache=_json("value_financials_cache.json"),
                   corp_map=_json("dart_corpcode.json"), mcap=mcap,
                   finnhub_key=(os.getenv("FINNHUB_API_KEY") or "").strip() or None)
        inst.isin_map = inst._load_isin_map()
        return inst

    def _load_isin_map(self) -> dict[str, str]:
        p = self.data_dir / ISIN_CACHE
        try:
            if p.exists() and time.time() - p.stat().st_mtime < ISIN_MAX_AGE_SEC:
                return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if self.krx is None:
            return {}
        try:
            body = self.krx.get_json("dbms/comm/finder/finder_stkisu",
                                     mktsel="ALL", searchText="", typeNo="0") or {}
            m = {str(r["short_code"]): str(r["full_code"]) for r in body.get("block1") or []
                 if r.get("short_code") and r.get("full_code")}
            if m:
                p.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
            return m
        except Exception as e:
            log.warning("KRX ISIN 목록 실패: %s", e)
            return {}

    # ── 슬롯별 ────────────────────────────────────────────────────
    def kr_fundamentals(self, symbol: str, *, price: float | None, asof: date) -> dict | None:
        """DART 캐시 → 밸류에이션·건전성·성장. 캐시만 읽는다(DART 미접촉)."""
        ref = self.mcap.get(symbol)
        if not ref or not price:
            return None
        mcap_now, ref_price = ref
        market_cap = mcap_now * float(price) / ref_price
        from ..value_scan import _kr_fundamentals
        try:
            out = _kr_fundamentals("", self.corp_map, symbol, market_cap,
                                   json.loads(json.dumps(self.fin_cache)),
                                   fetch_fn=lambda *a, **k: None,
                                   years=public_fiscal_years(asof))
        except Exception as e:
            log.debug("[%s] KR 재무 계산 실패: %s", symbol, e)
            return None
        if out:
            out["market_cap_eok"] = round(market_cap / 1e8)
            for k in ("operating_cf", "investing_cf", "financing_cf", "capex", "fcf"):
                if k in out:                            # 원 → 억원(컨텍스트 가독성)
                    out[f"{k}_eok"] = _eok(out.pop(k))
            out["source"] = "dart_annual"
        return out

    def kr_flows(self, symbol: str, *, asof: date) -> dict | None:
        isin = self.isin_map.get(symbol)
        if self.krx is None or not isin:
            return None
        from ..datasources.krx_client import bld_for
        from ..datasources.krx_flows import parse_ticker_investor_row
        bld = bld_for("investor_ticker_daily") or "dbms/MDC/STAT/standard/MDCSTAT02302"
        try:
            rows = self.krx.get_rows(
                bld, strtDd=(asof - timedelta(days=FLOW_LOOKBACK_DAYS)).strftime("%Y%m%d"),
                endDd=(asof - timedelta(days=1)).strftime("%Y%m%d"), isuCd=isin,
                trdVolVal="2", askBid="3", inqTpCd="2", detailView="1",
                share="1", money="1")
        except Exception as e:
            log.debug("[%s] KRX 수급 실패: %s", symbol, e)
            return None
        parsed = [p for p in (parse_ticker_investor_row(r) for r in rows or []) if p]
        return summarize_flows(parsed, asof=asof)

    def kr_short(self, symbol: str, *, asof: date) -> dict | None:
        """공매도 잔고 비율 + 약 20일 전 대비 변화."""
        if self.krx is None:
            return None
        from ..datasources.positioning import _latest_balance_map, parse_krx_short_row
        isin = self.isin_map.get(symbol)

        def lookup(end: date) -> dict | None:
            for tp in ("1", "2"):                       # 유가·코스닥
                key = (tp, end.isoformat())
                if key not in self._short_maps:
                    try:
                        self._short_maps[key] = _latest_balance_map(self.krx, tp, end=end)
                    except Exception as e:
                        log.debug("KRX 공매도 잔고 실패(%s): %s", tp, e)
                        self._short_maps[key] = (None, {})
                by = self._short_maps[key][1]
                row = by.get(symbol) or (by.get(isin) if isin else None)
                if row:
                    return parse_krx_short_row(row)
            return None

        cur = lookup(asof - timedelta(days=SHORT_PUBLISH_LAG_DAYS))
        if not cur or cur.get("short_ratio") is None:
            return None
        prev = lookup(asof - timedelta(days=SHORT_PUBLISH_LAG_DAYS + 28))
        out = {"asof": cur.get("asof"), "short_ratio_pct": cur.get("short_ratio"),
               "source": "krx"}
        if prev and prev.get("short_ratio") is not None:
            out["short_ratio_chg_4w_pp"] = round(cur["short_ratio"] - prev["short_ratio"], 2)
        return out

    def us_fundamentals(self, symbol: str) -> dict | None:
        if not self.finnhub_key:
            return None
        from ..datasources.finnhub import fetch_basic_financials
        return fetch_basic_financials(self.finnhub_key, symbol)

    # ── 묶음 ──────────────────────────────────────────────────────
    def for_symbol(self, symbol: str, market: str, *, price: float | None, asof: date,
                   live: bool = True) -> dict:
        """{fundamentals, flows, positioning} 중 구한 것만. live=False 면 과거 재현 불가 슬롯 생략."""
        out: dict = {}
        if str(market).upper() == "KR":
            for key, fn in (("fundamentals", lambda: self.kr_fundamentals(
                                symbol, price=price, asof=asof)),
                            ("flows", lambda: self.kr_flows(symbol, asof=asof)),
                            ("positioning", lambda: self.kr_short(symbol, asof=asof))):
                v = fn()
                if v:
                    out[key] = v
        elif live:
            v = self.us_fundamentals(symbol)
            if v:
                out["fundamentals"] = v
        return out


def fill_missing(ctx: dict, extra: dict) -> list[str]:
    """ctx 의 비어 있는 슬롯만 extra 로 채운다(market_state 에 있던 값은 그대로). 채운 키 목록."""
    filled = []
    for k, v in (extra or {}).items():
        if v and not ctx.get(k):
            ctx[k] = v
            filled.append(k)
    return filled


def input_flags(ctx: dict) -> dict:
    """도시에 evidence 에 남길 입력 구성 — 데이터 유무별 성적 비교용."""
    return {"fundamentals": bool(ctx.get("fundamentals")), "flows": bool(ctx.get("flows")),
            "positioning": bool(ctx.get("positioning")),
            "news": len(ctx.get("news") or [])}

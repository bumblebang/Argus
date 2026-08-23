"""SEC EDGAR 미국 재무 소스 (무료, 키 불필요).

티커 -> CIK 매핑 후 companyconcept API 로 최근 연간(10-K/FY) 매출·순이익을 가져와
순이익률을 계산한다. SEC 는 식별 가능한 User-Agent 헤더를 요구한다(없으면 403).
토스 API 와 무관하므로 토스 쿨다운과 별개로 동작한다.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import requests

from .base import DataSource, SourceContext
from ..logging_setup import get_logger

log = get_logger("src.edgar")

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{concept}.json"
REVENUE_CONCEPTS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"]
# EDGAR 표준 연간 프레임만(분기 제외): "CY2024" O, "CY2024Q1" X
_ANNUAL_FRAME = re.compile(r"^CY(\d{4})$")


class EdgarSource(DataSource):
    name = "edgar"

    def __init__(self, tickers: list[str], user_agent: str,
                 cache_dir: str | Path = "data", spacing_sec: float = 0.2):
        self.tickers = tickers
        self.headers = {"User-Agent": user_agent}
        self.cache = Path(cache_dir) / "sec_tickers.json"
        self.spacing = spacing_sec

    def _cik_map(self) -> dict[str, int]:
        if self.cache.exists():
            raw = json.loads(self.cache.read_text(encoding="utf-8"))
        else:
            r = requests.get(TICKERS_URL, headers=self.headers, timeout=20)
            r.raise_for_status()
            raw = r.json()
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            self.cache.write_text(json.dumps(raw), encoding="utf-8")
        return {row["ticker"].upper(): row["cik_str"] for row in raw.values()}

    def _annual_by_year(self, cik: int, concept: str) -> dict[int, float]:
        """표준 연간 프레임(CYxxxx)별 값. 개념 간 같은 연도로 정렬하기 위함."""
        url = CONCEPT_URL.format(cik=cik, concept=concept)
        r = requests.get(url, headers=self.headers, timeout=20)
        if r.status_code != 200:
            return {}
        out: dict[int, float] = {}
        for u in r.json().get("units", {}).get("USD", []):
            m = _ANNUAL_FRAME.match(u.get("frame", "") or "")
            if m:
                out[int(m.group(1))] = float(u["val"])
        return out

    def fetch(self, ctx: SourceContext) -> dict:
        if ctx.dry:
            return {"fundamentals": {t: {"revenue": 1.0e11, "net_income": 2.0e10,
                                         "net_margin": 0.2} for t in self.tickers}}
        out: dict[str, dict] = {}
        try:
            cikmap = self._cik_map()
        except Exception as e:
            log.warning("SEC 티커맵 조회 실패: %s", e)
            return {"fundamentals": {}}

        for i, t in enumerate(self.tickers):
            cik = cikmap.get(t.upper())
            if not cik:
                continue
            try:
                rev_by_year: dict[int, float] = {}
                for c in REVENUE_CONCEPTS:
                    for yr, v in self._annual_by_year(cik, c).items():
                        rev_by_year.setdefault(yr, v)  # 먼저 잡힌 개념 우선
                ni_by_year = self._annual_by_year(cik, "NetIncomeLoss")

                # 매출·순이익이 모두 있는 가장 최근 연도로 정렬
                common = sorted(set(rev_by_year) & set(ni_by_year))
                if common:
                    fy = common[-1]
                    revenue, net_income = rev_by_year[fy], ni_by_year[fy]
                    margin = net_income / revenue if revenue else None
                    out[t] = {"fiscal_year": fy, "revenue": revenue, "net_income": net_income,
                              "net_margin": round(margin, 4) if margin is not None else None}
                    log.info("[%s] FY%d 매출 %.3g 순이익 %.3g 순이익률 %s", t, fy,
                             revenue, net_income, out[t]["net_margin"])
                else:
                    out[t] = {"fiscal_year": None, "revenue": None, "net_income": None,
                              "net_margin": None}
                    log.warning("[%s] 공통 연간 데이터 없음", t)
            except Exception as e:
                log.warning("[%s] EDGAR 조회 실패: %s", t, e)
            if self.spacing and i < len(self.tickers) - 1:
                time.sleep(self.spacing)
        return {"fundamentals": out}

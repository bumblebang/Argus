"""SEC EDGAR 미국 재무·필링 소스 (무료, 키 불필요).

티커 -> CIK 매핑 후 companyconcept API 로 최근 연간(10-K/FY) 매출·순이익을 가져와
순이익률을 계산한다. 워처용 submissions 폴링도 같은 CIK 맵·User-Agent 를 쓴다.
SEC 는 식별 가능한 User-Agent 헤더를 요구한다(없으면 403).
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
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
REVENUE_CONCEPTS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"]
# EDGAR 표준 연간 프레임만(분기 제외): "CY2024" O, "CY2024Q1" X
_ANNUAL_FRAME = re.compile(r"^CY(\d{4})$")

# 워처 감시 폼 — 10-K/10-Q·Form 3/4/5 제외(재무 배치·실적워처·인사이더 노이즈).
WATCH_FORMS = frozenset({"8-K", "8-K/A", "6-K"})
# 8-K Item 중 매매 판단에 쓸 만한 것만. 7.01(FD)·9.01(Exhibit)만이면 무시.
MATERIAL_ITEMS = frozenset({
    "1.01", "1.02", "1.03",
    "2.01", "2.03", "2.04", "2.05", "2.06",
    "3.01", "3.02", "3.03",
    "4.01", "4.02",
    "5.01", "5.02", "5.03",
    "8.01",
})
NOISE_ONLY_ITEMS = frozenset({"7.01", "9.01"})


def load_cik_map(user_agent: str, cache_dir: str | Path = "data") -> dict[str, int]:
    """티커→CIK. 캐시 파일이 있으면 네트워크 없이 쓴다."""
    cache = Path(cache_dir) / "sec_tickers.json"
    headers = {"User-Agent": user_agent}
    if cache.exists():
        raw = json.loads(cache.read_text(encoding="utf-8"))
    else:
        r = requests.get(TICKERS_URL, headers=headers, timeout=20)
        r.raise_for_status()
        raw = r.json()
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(raw), encoding="utf-8")
    return {row["ticker"].upper(): row["cik_str"] for row in raw.values()}


def parse_items(items: str | list | None) -> list[str]:
    """submissions `items` 필드 → ['1.01', '2.01', …]. 빈 문자열/None → []."""
    if items is None:
        return []
    if isinstance(items, list):
        raw = ",".join(str(x) for x in items)
    else:
        raw = str(items)
    out: list[str] = []
    for part in raw.replace(";", ",").split(","):
        tok = part.strip()
        if tok:
            out.append(tok)
    return out


def is_material_filing(form: str | None, items: str | list | None
                       ) -> tuple[str | None, bool]:
    """중대 필링이면 (report_nm, empty_items), 아니면 (None, False).

    empty_items=True: items 가 비어 있는 8-K — 보유/armed 만 wake, 유니버스 큐는 스킵.
    6-K 는 항목 없이 중대로 본다. 7.01/9.01 만 있으면 무시.
    """
    f = (form or "").strip().upper()
    if f not in WATCH_FORMS:
        return None, False
    if f == "6-K":
        return "6-K", False
    parsed = parse_items(items)
    if not parsed:
        return f, True
    material = [i for i in parsed if i in MATERIAL_ITEMS]
    if material:
        return f"{f} Item {','.join(material)}", False
    # 노이즈 전용(또는 미등록 item만) → 무시
    if set(parsed) <= NOISE_ONLY_ITEMS:
        return None, False
    return None, False


def fetch_recent_filings(symbols: list[str], user_agent: str, *,
                         cache_dir: str | Path = "data",
                         spacing_sec: float = 0.15,
                         recent_limit: int = 25,
                         since_date: str | None = None) -> list[dict]:
    """보유/유니버스 심볼의 최근 8-K/6-K 목록(표준형).

    since_date(YYYY-MM-DD, 미국 현지) 이전 filing 은 스킵·조기 종료 — 심볼별 전체
    이력을 훑지 않는다(DART list.json 오늘치 1콜과 대칭). None 이면 필터 없음(하위호환).

    반환 항목: accession, symbol, form, items, filing_date, report_nm, corp_name,
    empty_items. 비중대(7.01/9.01만 등)는 넣지 않는다.
    """
    if not symbols:
        return []
    headers = {"User-Agent": user_agent}
    try:
        cikmap = load_cik_map(user_agent, cache_dir=cache_dir)
    except Exception as e:
        log.warning("SEC 티커맵 조회 실패: %s", e)
        return []

    out: list[dict] = []
    for i, sym in enumerate(symbols):
        ticker = str(sym).upper()
        cik = cikmap.get(ticker)
        if not cik:
            continue
        try:
            url = SUBMISSIONS_URL.format(cik=int(cik))
            r = requests.get(url, headers=headers, timeout=20)
            if r.status_code != 200:
                log.warning("[%s] submissions HTTP %s", ticker, r.status_code)
                continue
            data = r.json()
            corp = data.get("name") or ticker
            recent = (data.get("filings") or {}).get("recent") or {}
            forms = recent.get("form") or []
            accessions = recent.get("accessionNumber") or []
            dates = recent.get("filingDate") or []
            items_arr = recent.get("items") or []
            n = min(len(forms), len(accessions), len(dates))
            kept = 0
            for j in range(n):
                if kept >= recent_limit:
                    break
                fdate = dates[j] if j < len(dates) else ""
                if since_date and fdate and fdate < since_date:
                    break
                form = forms[j]
                items_raw = items_arr[j] if j < len(items_arr) else ""
                label, empty = is_material_filing(form, items_raw)
                if not label:
                    continue
                out.append({
                    "accession": accessions[j],
                    "symbol": ticker,
                    "form": form,
                    "items": parse_items(items_raw),
                    "filing_date": dates[j],
                    "report_nm": label,
                    "corp_name": corp,
                    "empty_items": empty,
                })
                kept += 1
        except Exception as e:
            log.warning("[%s] EDGAR submissions 실패: %s", ticker, e)
        if spacing_sec and i < len(symbols) - 1:
            time.sleep(spacing_sec)
    return out


class EdgarSource(DataSource):
    name = "edgar"

    def __init__(self, tickers: list[str], user_agent: str,
                 cache_dir: str | Path = "data", spacing_sec: float = 0.2):
        self.tickers = tickers
        self.headers = {"User-Agent": user_agent}
        self.cache = Path(cache_dir) / "sec_tickers.json"
        self.spacing = spacing_sec

    def _cik_map(self) -> dict[str, int]:
        return load_cik_map(self.headers["User-Agent"],
                            cache_dir=self.cache.parent)
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

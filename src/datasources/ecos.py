"""한국 매크로 소스 (ECOS, 한국은행 경제통계). market_state.macro_kr 에 저장.

FRED(macro, 미국)와 슬롯을 분리한다 — KR 금리·물가·고용·심리의 정본은 여기다.
100대 통계지표(KeyStatisticList) 한 방 호출로 최신값만 가져온다.
"""
from __future__ import annotations

from datetime import datetime, timezone

import requests

from .base import DataSource, SourceContext
from ..logging_setup import get_logger

log = get_logger("src.macro_kr")

ECOS_URL = "https://ecos.bok.or.kr/api/KeyStatisticList/{key}/json/kr/1/101"

# ECOS KEYSTAT_NAME(완전일치) -> 안정 영문 키. 부분일치는 쓰지 않는다(이름 변동 시
# 엉뚱한 지표가 조용히 들어오는 것보다 키가 빠지는 편이 낫다).
KEY_MAP = {
    # 시장금리
    "한국은행 기준금리": "bok_base_rate",
    "콜금리(익일물)": "call_overnight",
    "KORIBOR(3개월)": "koribor_3m",
    "CD수익률(91일)": "cd_91d",
    "통안증권수익률(364일)": "msb_364d",
    "국고채수익률(3년)": "kr_treasury_3y",
    "국고채수익률(5년)": "kr_treasury_5y",
    "회사채수익률(3년,AA-)": "corp_aa_3y",
    # 환율 (Yahoo fx/markets 와 구분해 _bok 접미사)
    "원/달러 환율(종가)": "usdkrw_bok",
    # 물가
    "소비자물가지수": "cpi_index",
    "농산물 및 석유류제외 소비자물가지수": "cpi_core_index",
    "생산자물가지수": "ppi_index",
    # 고용
    "실업률": "unemployment",
    "고용률": "employment_rate",
    # 심리
    "소비자심리지수": "consumer_sentiment",
    "전산업 기업심리지수실적": "bsi_all",
    "경제심리지수": "economic_sentiment",
    # 경기
    "동행지수순환변동치": "coincident_cycle",
    "선행지수순환변동치": "leading_cycle",
    # 대외
    "외환보유액": "fx_reserves",
    "경상수지": "current_account",
}


def _num(v) -> float | None:
    """ECOS DATA_VALUE 문자열 -> float. 결측('-', '', None)은 None."""
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_key_stat_rows(rows: list[dict]) -> dict:
    """KeyStatisticList.row[] -> {영문키: float} + source/asof/raw_n 메타."""
    out: dict = {}
    cycles: list[str] = []
    for row in rows or []:
        key = KEY_MAP.get((row.get("KEYSTAT_NAME") or "").strip())
        if not key or key in out:
            continue
        val = _num(row.get("DATA_VALUE"))
        if val is None:
            continue
        out[key] = val
        cycle = (row.get("CYCLE") or "").strip()
        if cycle:
            cycles.append(cycle)
    out["source"] = "ecos"
    out["asof"] = max(cycles) if cycles else datetime.now(timezone.utc).isoformat()
    out["raw_n"] = len(out) - 2
    return out


class EcosMacroSource(DataSource):
    name = "macro_kr"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def fetch(self, ctx: SourceContext) -> dict:
        if ctx.dry:
            return {"macro_kr": {"bok_base_rate": 2.5, "kr_treasury_3y": 2.6,
                                 "cd_91d": 2.7, "cpi_index": 117.0,
                                 "unemployment": 2.9, "consumer_sentiment": 100.0,
                                 "usdkrw_bok": 1380.0,
                                 "source": "ecos", "asof": "20260101", "raw_n": 7}}
        try:
            r = requests.get(ECOS_URL.format(key=self.api_key), timeout=15)
            r.raise_for_status()
            rows = (r.json().get("KeyStatisticList") or {}).get("row") or []
        except Exception as e:
            log.warning("ECOS 실패: %s", e)
            return {"macro_kr": {}}
        if not rows:
            log.warning("ECOS 응답에 row 없음")
            return {"macro_kr": {}}
        out = parse_key_stat_rows(rows)
        log.info("macro_kr: 기준금리=%s 국고3Y=%s CPI=%s (%d/%d 매핑)",
                 out.get("bok_base_rate"), out.get("kr_treasury_3y"),
                 out.get("cpi_index"), out["raw_n"], len(KEY_MAP))
        return {"macro_kr": out}

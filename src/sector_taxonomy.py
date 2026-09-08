"""섹터 분류 체계 — KRX 업종 / Finnhub industry / 레거시 라벨을 11섹터로 정규화.

섹터 집중도 감독(`risk.max_sector_pct`)이 의미를 가지려면 KR·US 가 **같은 축**의
버킷을 써야 한다. 자유문자열(`반도체`·`Technology`·`소프트웨어`)로 두면 같은 산업이
다른 버킷으로 흩어져 캡이 사실상 안 걸린다. 그래서 GICS 11섹터(한글 라벨)로 모은다.

정규화 소스 셋:
  - KRX 업종분류현황(MDCSTAT03901)의 `IDX_IND_NM` — KR 전종목.
  - Finnhub `/stock/profile2` 의 `finnhubIndustry` — US.
  - 레거시 자유문자열 — config.yaml 정적 universe·기존 data/universe.yaml 이월분.

모르는 값은 None 을 돌려준다(호출부가 '미분류'로 둘지 결정). ETF 는 11섹터 밖의
별도 버킷 — 지수 상품이라 산업 축에 얹으면 왜곡된다.
"""
from __future__ import annotations

import re

# ── 표준 버킷 ────────────────────────────────────────────────
ENERGY = "에너지"
MATERIALS = "소재"
INDUSTRIALS = "산업재"
CONSUMER_DISC = "경기소비재"
CONSUMER_STAPLE = "필수소비재"
HEALTHCARE = "헬스케어"
FINANCIALS = "금융"
INFO_TECH = "정보기술"
COMMUNICATION = "커뮤니케이션"
UTILITIES = "유틸리티"
REAL_ESTATE = "부동산"
ETF = "ETF"

SECTORS: tuple[str, ...] = (
    ENERGY, MATERIALS, INDUSTRIALS, CONSUMER_DISC, CONSUMER_STAPLE, HEALTHCARE,
    FINANCIALS, INFO_TECH, COMMUNICATION, UTILITIES, REAL_ESTATE, ETF,
)

UNCLASSIFIED = "미분류"

# ── KRX 업종분류현황 IDX_IND_NM → 11섹터 ─────────────────────
# KOSPI/KOSDAQ 합쳐 27종(2026-09 기준 전수 확인). 신설 업종은 매핑 누락 → None.
_KRX: dict[str, str] = {
    "화학": MATERIALS,
    "금속": MATERIALS,
    "비금속": MATERIALS,
    "종이·목재": MATERIALS,
    "기계·장비": INDUSTRIALS,
    "건설": INDUSTRIALS,
    "운송·창고": INDUSTRIALS,
    "기타제조": INDUSTRIALS,
    "일반서비스": INDUSTRIALS,       # 광고·교육·렌탈 등 상업/전문 서비스
    "운송장비·부품": CONSUMER_DISC,  # 자동차·부품이 시총 지배(조선·항공도 포함)
    "유통": CONSUMER_DISC,
    "섬유·의류": CONSUMER_DISC,
    "음식료·담배": CONSUMER_STAPLE,
    "농업, 임업 및 어업": CONSUMER_STAPLE,
    "제약": HEALTHCARE,
    "의료·정밀기기": HEALTHCARE,
    "금융": FINANCIALS,
    "기타금융": FINANCIALS,
    "은행": FINANCIALS,
    "보험": FINANCIALS,
    "증권": FINANCIALS,
    "전기·전자": INFO_TECH,
    "IT 서비스": INFO_TECH,
    "통신": COMMUNICATION,
    "출판·매체복제": COMMUNICATION,
    "오락·문화": COMMUNICATION,      # 엔터·게임 — GICS Media & Entertainment
    "전기·가스": UTILITIES,
    "전기·가스·수도": UTILITIES,
    "부동산": REAL_ESTATE,
}

# ── Finnhub finnhubIndustry → 11섹터 ─────────────────────────
# 현재 US 유니버스에 실제로 나온 27종 + Finnhub 어휘 나머지(신규 편입 대비).
_FINNHUB: dict[str, str] = {
    "energy": ENERGY,
    "oil gas consumable fuels": ENERGY,
    "chemicals": MATERIALS,
    "metals mining": MATERIALS,
    "paper forest": MATERIALS,
    "packaging": MATERIALS,
    "constr mat": MATERIALS,
    "building": INDUSTRIALS,
    "construction": INDUSTRIALS,
    "machinery": INDUSTRIALS,
    "aerospace defense": INDUSTRIALS,
    "airlines": INDUSTRIALS,
    "marine": INDUSTRIALS,
    "road rail": INDUSTRIALS,
    "logistics transportation": INDUSTRIALS,
    "transportation infrastructure": INDUSTRIALS,
    "industrial conglomerates": INDUSTRIALS,
    "commercial services supplies": INDUSTRIALS,
    "professional services": INDUSTRIALS,
    "trading companies distributors": INDUSTRIALS,
    "distributors": INDUSTRIALS,
    "electrical equipment": INDUSTRIALS,
    "automobiles": CONSUMER_DISC,
    "auto components": CONSUMER_DISC,
    "retail": CONSUMER_DISC,
    "textiles apparel luxury goods": CONSUMER_DISC,
    "hotels restaurants leisure": CONSUMER_DISC,
    "leisure products": CONSUMER_DISC,
    "diversified consumer services": CONSUMER_DISC,
    "consumer products": CONSUMER_STAPLE,
    "household products": CONSUMER_STAPLE,
    "food products": CONSUMER_STAPLE,
    "beverages": CONSUMER_STAPLE,
    "tobacco": CONSUMER_STAPLE,
    "health care": HEALTHCARE,
    "pharmaceuticals": HEALTHCARE,
    "biotechnology": HEALTHCARE,
    "life sciences tools services": HEALTHCARE,
    "banking": FINANCIALS,
    "insurance": FINANCIALS,
    "financial services": FINANCIALS,
    "diversified financial services": FINANCIALS,
    "capital markets": FINANCIALS,
    "technology": INFO_TECH,
    "semiconductors": INFO_TECH,
    "software": INFO_TECH,
    "electronic equipment": INFO_TECH,
    "it services": INFO_TECH,
    "internet": INFO_TECH,
    "telecommunication": COMMUNICATION,
    "communications": COMMUNICATION,
    "wireless telecommunication services": COMMUNICATION,
    "media": COMMUNICATION,
    "entertainment": COMMUNICATION,
    "utilities": UTILITIES,
    "water utilities": UTILITIES,
    "real estate": REAL_ESTATE,
}

# ── 레거시 자유문자열(기존 config.yaml·universe.yaml 이월분) → 11섹터 ──
_LEGACY: dict[str, str] = {
    "반도체": INFO_TECH,
    "전자": INFO_TECH,
    "하드웨어": INFO_TECH,
    "소프트웨어": INFO_TECH,
    "it": INFO_TECH,
    "인터넷": INFO_TECH,
    "자동차": CONSUMER_DISC,
    "전력": UTILITIES,
    "헬스케어": HEALTHCARE,
    "바이오": HEALTHCARE,
    "제약": HEALTHCARE,
    "2차전지": INDUSTRIALS,
    "조선": INDUSTRIALS,
    "방산": INDUSTRIALS,
    "etf": ETF,
}

_PUNCT = re.compile(r"[^0-9a-z가-힣]+")


def _key(raw: str) -> str:
    """비교용 키 — 소문자화 + 구두점/공백 제거(Finnhub 표기 흔들림 흡수).

    'Hotels, Restaurants & Leisure' 와 'Hotels Restaurants & Leisure' 가 같은 키가
    되도록 한다. 한글은 가운뎃점(·)만 사라지므로 KRX 표기와 충돌하지 않는다.
    """
    return _PUNCT.sub(" ", str(raw).strip().lower()).strip()


_KRX_BY_KEY = {_key(k): v for k, v in _KRX.items()}
_FINNHUB_BY_KEY = {_key(k): v for k, v in _FINNHUB.items()}
_LEGACY_BY_KEY = {_key(k): v for k, v in _LEGACY.items()}
_CANON_BY_KEY = {_key(s): s for s in SECTORS}
_CANON_BY_KEY[_key(UNCLASSIFIED)] = UNCLASSIFIED


def normalize_sector(raw: str | None) -> str | None:
    """임의 섹터/업종 문자열 → 11섹터(+ETF) 표준 라벨. 모르면 None.

    이미 표준 라벨이면 그대로 통과시킨다(멱등) — 재정규화로 값이 바뀌지 않는다.
    """
    if not raw:
        return None
    k = _key(raw)
    if not k:
        return None
    for table in (_CANON_BY_KEY, _KRX_BY_KEY, _FINNHUB_BY_KEY, _LEGACY_BY_KEY):
        hit = table.get(k)
        if hit:
            return hit
    return None


__all__ = ["SECTORS", "UNCLASSIFIED", "normalize_sector", "ETF"]

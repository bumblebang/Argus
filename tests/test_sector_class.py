"""섹터 분류 — 11섹터 정규화 + KRX/Finnhub 수집·캐시. 실네트워크 금지(전부 주입).

섹터 집중도 감독(risk.max_sector_pct)은 sector 가 지정된 종목만 검사한다. 유니버스
216종목 중 13종목만 sector 가 있어 캡이 사실상 죽어 있던 게 이 모듈의 배경이다 —
여기서는 (1) KR/US 업종 문자열이 같은 축의 버킷으로 모이는지, (2) 캐시가 네트워크를
아끼면서도 실패분은 재시도하는지를 못박는다.
"""
from __future__ import annotations

import json

import src.datasources.sector_class as SC
from src.sector_taxonomy import SECTORS, normalize_sector


# ── 정규화 ────────────────────────────────────────────────────
def test_krx_industry_maps_to_canonical_sector():
    assert normalize_sector("전기·전자") == "정보기술"
    assert normalize_sector("IT 서비스") == "정보기술"
    assert normalize_sector("제약") == "헬스케어"
    assert normalize_sector("의료·정밀기기") == "헬스케어"
    assert normalize_sector("증권") == "금융"
    assert normalize_sector("농업, 임업 및 어업") == "필수소비재"


def test_finnhub_industry_maps_to_same_buckets_as_krx():
    """KR·US 가 같은 축을 써야 캡이 의미를 가진다 — 반도체는 양쪽 다 정보기술."""
    assert normalize_sector("Semiconductors") == normalize_sector("전기·전자")
    assert normalize_sector("Technology") == "정보기술"
    assert normalize_sector("Banking") == normalize_sector("은행")
    assert normalize_sector("Media") == "커뮤니케이션"
    assert normalize_sector("Energy") == "에너지"


def test_finnhub_punctuation_variants_normalize_alike():
    a = normalize_sector("Hotels, Restaurants & Leisure")
    b = normalize_sector("Hotels Restaurants  Leisure")
    assert a == b == "경기소비재"


def test_legacy_freeform_labels_are_folded():
    """옛 자유문자열(반도체/소프트웨어/Technology)이 한 버킷으로 모인다."""
    assert (normalize_sector("반도체") == normalize_sector("소프트웨어")
            == normalize_sector("Technology") == "정보기술")


def test_normalize_is_idempotent_on_canonical_labels():
    for s in SECTORS:
        assert normalize_sector(s) == s


def test_unknown_label_returns_none():
    assert normalize_sector("듣도보도못한업종") is None
    assert normalize_sector("") is None
    assert normalize_sector(None) is None


# ── KR 수집(벌크) ─────────────────────────────────────────────
class _FakeKRX:
    has_creds = True

    def __init__(self, rows_by_market):
        self.rows = rows_by_market
        self.calls = 0

    def get_rows(self, bld, **params):
        self.calls += 1
        return self.rows.get(params.get("mktId"), [])


def _krx(rows_stk=(), rows_ksq=()):
    return _FakeKRX({"STK": list(rows_stk), "KSQ": list(rows_ksq)})


def test_refresh_kr_bulk_fills_whole_table():
    """벌크 1회로 요청 밖 종목까지 캐시 — 다음 편입 종목이 곧바로 히트한다."""
    client = _krx([{"ISU_SRT_CD": "005930", "IDX_IND_NM": "전기·전자"},
                   {"ISU_SRT_CD": "051910", "IDX_IND_NM": "화학"}])
    cache: dict = {}
    hit = SC.refresh_kr(cache, ["005930"], now=1000.0, client=client)
    assert hit == 2
    assert SC.sector_for(cache, "KR", "005930") == "정보기술"
    assert SC.sector_for(cache, "KR", "051910") == "소재"      # 요청 안 했어도 캐시됨


def test_refresh_kr_marks_non_stock_codes_as_etf():
    """업종분류현황은 상장 주식 전수 — 거기 없는 코드는 ETF/ETN 이다."""
    client = _krx([{"ISU_SRT_CD": "005930", "IDX_IND_NM": "전기·전자"}])
    cache: dict = {}
    SC.refresh_kr(cache, ["005930", "232080"], now=1000.0, client=client)
    assert SC.sector_for(cache, "KR", "232080") == "ETF"


def test_refresh_kr_skips_network_when_cache_fresh():
    client = _krx([{"ISU_SRT_CD": "005930", "IDX_IND_NM": "전기·전자"}])
    cache: dict = {}
    SC.refresh_kr(cache, ["005930"], now=1000.0, client=client)
    before = client.calls
    assert SC.refresh_kr(cache, ["005930"], now=1000.0 + 3600, client=client) == 0
    assert client.calls == before


def test_refresh_kr_fetch_failure_leaves_cache_untouched():
    """조회 0건이면 fail-soft — 빈 값으로 캐시를 오염시키지 않는다."""
    cache = {"KR": {}}
    assert SC.refresh_kr(cache, ["005930"], now=1000.0, client=_krx()) == 0
    assert SC.sector_for(cache, "KR", "005930") is None


# ── US 수집(심볼별) ───────────────────────────────────────────
def test_refresh_us_fetches_only_stale_symbols(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(SC, "fetch_us_industry",
                        lambda sym, key, **kw: seen.append(sym) or "Semiconductors")
    cache: dict = {}
    SC.refresh_us(cache, ["NVDA"], now=1000.0, api_key="k", sleep=lambda s: None)
    SC.refresh_us(cache, ["NVDA", "AAPL"], now=1000.0, api_key="k",
                  sleep=lambda s: None)
    assert seen == ["NVDA", "AAPL"]                 # NVDA 재조회 없음
    assert SC.sector_for(cache, "US", "AAPL") == "정보기술"


def test_refresh_us_respects_max_fetch(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(SC, "fetch_us_industry",
                        lambda sym, key, **kw: seen.append(sym) or "Technology")
    cache: dict = {}
    SC.refresh_us(cache, ["A", "B", "C"], now=1.0, api_key="k", max_fetch=2,
                  sleep=lambda s: None)
    assert seen == ["A", "B"]


def test_refresh_us_without_key_is_noop(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.setattr(SC, "fetch_us_industry",
                        lambda *a, **k: pytest_fail("네트워크를 타면 안 된다"))
    cache: dict = {}
    assert SC.refresh_us(cache, ["NVDA"], now=1.0, sleep=lambda s: None) == 0


def pytest_fail(msg):                    # 헬퍼 — lambda 안에서 raise 하기 위해
    raise AssertionError(msg)


# ── 캐시 TTL ─────────────────────────────────────────────────
def test_unresolved_entry_retries_next_day(monkeypatch):
    """분류 실패는 30일이 아니라 하루 뒤 재시도 — 신규 상장이 한 달 미분류로 남지 않게."""
    calls: list[str] = []

    def _fetch(sym, key, **kw):
        calls.append(sym)
        return None if len(calls) == 1 else "Technology"

    monkeypatch.setattr(SC, "fetch_us_industry", _fetch)
    cache: dict = {}
    SC.refresh_us(cache, ["NEW"], now=0.0, api_key="k", sleep=lambda s: None)
    assert SC.sector_for(cache, "US", "NEW") is None
    SC.refresh_us(cache, ["NEW"], now=3600.0, api_key="k", sleep=lambda s: None)
    assert len(calls) == 1                          # 아직 MISS_TTL 안 지남
    SC.refresh_us(cache, ["NEW"], now=SC.MISS_TTL_SEC + 1, api_key="k",
                  sleep=lambda s: None)
    assert SC.sector_for(cache, "US", "NEW") == "정보기술"


def test_resolved_entry_survives_long_ttl(monkeypatch):
    monkeypatch.setattr(SC, "fetch_us_industry", lambda *a, **k: "Technology")
    cache: dict = {}
    SC.refresh_us(cache, ["NVDA"], now=0.0, api_key="k", sleep=lambda s: None)
    assert SC.refresh_us(cache, ["NVDA"], now=SC.TTL_SEC - 1, api_key="k",
                         sleep=lambda s: None) == 0


def test_cache_roundtrip(tmp_path):
    p = tmp_path / "sector_cache.json"
    cache = {"KR": {"005930": {"sector": "정보기술", "raw": "전기·전자", "fetched": 1.0}}}
    SC.save_cache(cache, p)
    assert json.loads(p.read_text(encoding="utf-8")) == cache
    assert SC.sector_for(SC.load_cache(p), "KR", "005930") == "정보기술"


def test_load_cache_tolerates_corrupt_file(tmp_path):
    p = tmp_path / "sector_cache.json"
    p.write_text("{not json", encoding="utf-8")
    assert SC.load_cache(p) == {}


def test_ensure_sectors_swallows_source_failure(monkeypatch, tmp_path):
    """한쪽 시장 수집이 터져도 나머지는 진행하고 예외를 밖으로 내지 않는다."""
    def _boom(*a, **k):
        raise RuntimeError("KRX down")

    monkeypatch.setattr(SC, "refresh_kr", _boom)
    monkeypatch.setattr(SC, "fetch_us_industry", lambda *a, **k: "Technology")
    cache = SC.ensure_sectors({"KR": ["005930"], "US": ["NVDA"]},
                              cache_path=tmp_path / "c.json", api_key="k")
    assert SC.sector_for(cache, "US", "NVDA") == "정보기술"
    assert SC.sector_for(cache, "KR", "005930") is None

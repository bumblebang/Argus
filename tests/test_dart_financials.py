"""DART fetch_financials(모듈 함수) — BS+IS 주요 계정 파싱(전부 mock, 네트워크 없음)."""
from __future__ import annotations

import src.datasources.dart as dart


class _Resp:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data


def _row(sj, fs, nm, amt):
    return {"sj_div": sj, "fs_div": fs, "account_nm": nm, "thstrm_amount": amt}


def test_여덟_계정_파싱_CFS_우선(monkeypatch):
    # CFS/OFS 를 섞고 순서도 뒤집어 — 각 값이 연결(CFS)로 채워지는지(순서 무관) 확인.
    # BS 는 유동/비유동을 섞어 정확 계정명 매칭(비유동 오탐 없음)까지 검증.
    rows = [
        _row("IS", "OFS", "매출액", "200,000,000,000,000"),        # 별도 먼저
        _row("IS", "CFS", "매출액", "300,000,000,000,000"),        # 연결이 덮어야
        _row("IS", "CFS", "매출총이익", "120,000,000,000,000"),    # '매출' 포함이나 첫매칭 아님
        _row("IS", "CFS", "영업이익", "50,000,000,000,000"),
        _row("IS", "CFS", "당기순이익(손실)", "34,000,000,000,000"),  # '당기순이익' 부분매칭
        _row("IS", "OFS", "당기순이익", "20,000,000,000,000"),
        _row("BS", "CFS", "유동자산", "180,000,000,000,000"),
        _row("BS", "CFS", "비유동자산", "260,000,000,000,000"),    # '유동자산' 오탐 금지
        _row("BS", "CFS", "자산총계", "440,000,000,000,000"),
        _row("BS", "CFS", "유동부채", "80,000,000,000,000"),
        _row("BS", "CFS", "비유동부채", "20,000,000,000,000"),     # '유동부채' 오탐 금지
        _row("BS", "CFS", "부채총계", "100,000,000,000,000"),
        _row("BS", "OFS", "자본총계", "200,000,000,000,000"),
        _row("BS", "CFS", "자본총계", "340,000,000,000,000"),      # 연결이 덮어야
    ]
    monkeypatch.setattr(dart.requests, "get",
                        lambda *a, **k: _Resp({"status": "000", "list": rows}))
    fin = dart.fetch_financials("KEY", "00126380", 2024)
    assert fin == {
        "fiscal_year": 2024, "revenue": 3.0e14, "operating_income": 5.0e13,
        "net_income": 3.4e13, "equity": 3.4e14, "total_assets": 4.4e14,
        "total_liabilities": 1.0e14, "current_assets": 1.8e14,
        "current_liabilities": 8.0e13,
    }


def test_연결없으면_별도로_채운다(monkeypatch):
    rows = [
        _row("BS", "OFS", "자본총계", "100"),
        _row("IS", "OFS", "매출액", "500"),
        _row("IS", "OFS", "영업이익", "80"),
        _row("IS", "OFS", "당기순이익", "50"),
    ]
    monkeypatch.setattr(dart.requests, "get",
                        lambda *a, **k: _Resp({"status": "000", "list": rows}))
    fin = dart.fetch_financials("KEY", "X", 2024)
    assert fin["equity"] == 100.0 and fin["revenue"] == 500.0
    assert fin["net_income"] == 50.0 and fin["operating_income"] == 80.0


def test_일부_계정_결손이면_해당_키만_None(monkeypatch):
    # 매출·자본총계만 오는 응답 — 나머지 계정은 None(하나라도 있으면 dict 반환).
    rows = [
        _row("IS", "CFS", "매출액", "500"),
        _row("BS", "CFS", "자본총계", "100"),
    ]
    monkeypatch.setattr(dart.requests, "get",
                        lambda *a, **k: _Resp({"status": "000", "list": rows}))
    fin = dart.fetch_financials("KEY", "X", 2024)
    assert fin["revenue"] == 500.0 and fin["equity"] == 100.0
    for k in ("operating_income", "net_income", "total_assets", "total_liabilities",
              "current_assets", "current_liabilities"):
        assert fin[k] is None


def test_status_비000_이면_None(monkeypatch):
    monkeypatch.setattr(dart.requests, "get",
                        lambda *a, **k: _Resp({"status": "013", "message": "no data"}))
    assert dart.fetch_financials("KEY", "X", 2025) is None


def test_http_비200_이면_None(monkeypatch):
    monkeypatch.setattr(dart.requests, "get",
                        lambda *a, **k: _Resp({"status": "000", "list": []}, status_code=500))
    assert dart.fetch_financials("KEY", "X", 2024) is None


def test_관심계정_모두_결측이면_None(monkeypatch):
    rows = [_row("BS", "CFS", "이익잉여금", "100")]        # 추적 대상 아닌 계정만
    monkeypatch.setattr(dart.requests, "get",
                        lambda *a, **k: _Resp({"status": "000", "list": rows}))
    assert dart.fetch_financials("KEY", "X", 2024) is None

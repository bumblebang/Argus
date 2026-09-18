"""DART fetch_cashflows — IFRS account_id 파싱(mock, 네트워크 없음)."""
from __future__ import annotations

import src.datasources.dart as dart


class _Resp:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data


def _cf(aid, amt, nm="x"):
    return {"sj_div": "CF", "account_id": aid, "account_nm": nm,
            "thstrm_amount": amt}


def test_CF_합계와_FCF_근사(monkeypatch):
    rows = [
        _cf("ifrs-full_CashFlowsFromUsedInOperatingActivities", "1000"),
        _cf("ifrs-full_CashFlowsFromUsedInInvestingActivities", "-400"),
        _cf("ifrs-full_CashFlowsFromUsedInFinancingActivities", "-200"),
        _cf("ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
            "300"),
        _cf("ifrs-full_PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities", "50"),
        {"sj_div": "BS", "account_id": "x", "thstrm_amount": "999"},  # 무시
    ]
    monkeypatch.setattr(dart.requests, "get",
                        lambda *a, **k: _Resp({"status": "000", "list": rows}))
    cf = dart.fetch_cashflows("KEY", "00126380", 2024)
    assert cf["operating_cf"] == 1000.0
    assert cf["investing_cf"] == -400.0
    assert cf["financing_cf"] == -200.0
    assert cf["capex"] == 350.0
    assert cf["fcf"] == 650.0
    assert cf["fiscal_year"] == 2024


def test_CFS_실패시_OFS(monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=30):
        calls.append(params.get("fs_div"))
        if params.get("fs_div") == "CFS":
            return _Resp({"status": "013", "message": "없음"})
        return _Resp({"status": "000", "list": [
            _cf("ifrs-full_CashFlowsFromUsedInOperatingActivities", "10"),
        ]})

    monkeypatch.setattr(dart.requests, "get", fake_get)
    cf = dart.fetch_cashflows("KEY", "X", 2024)
    assert calls == ["CFS", "OFS"]
    assert cf["operating_cf"] == 10.0
    assert cf["fcf"] is None  # capex 없음


def test_CF_없으면_None(monkeypatch):
    monkeypatch.setattr(dart.requests, "get",
                        lambda *a, **k: _Resp({"status": "000", "list": [
                            {"sj_div": "BS", "account_id": "a", "thstrm_amount": "1"},
                        ]}))
    assert dart.fetch_cashflows("KEY", "X", 2024) is None

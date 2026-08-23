"""매수 안전가드 데이터소스 테스트 — 순수 판정 헬퍼 + check_tradable 캐시/fail-open.

핵심 불변식:
- StockInfo: ACTIVE+STOCK 통과, DELISTED/SCHEDULED·ETF/ETN/DR/WARRANTS 차단.
- StockWarning: LIQUIDATION/WARNING/RISK/OVERHEATED 진행중 차단, VI_*·종료된 것 무시.
- check_tradable: 캐시 hit 시 client 미호출, 조회 예외는 fail-open(True).
"""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# TODAY(아래 문자열)와 같은 KST 날짜에 해당하는 epoch — check_tradable 의 _kst_today 판정용.
_NOW = datetime(2026, 7, 15, 12, 0, tzinfo=ZoneInfo("Asia/Seoul")).timestamp()

from src.datasources.stock_info import (
    _is_tradable_info, _active_warnings, check_tradable,
    INFO_TTL_SEC, WARN_TTL_SEC)


# ── (a) _is_tradable_info ─────────────────────────────────
def test_info_active_common_stock_passes():
    ok, reason = _is_tradable_info({"status": "ACTIVE", "securityType": "STOCK"})
    assert ok is True and reason == ""


def test_info_foreign_stock_reit_infra_pass():
    for st in ("FOREIGN_STOCK", "REIT", "INFRASTRUCTURE_FUND"):
        ok, _ = _is_tradable_info({"status": "ACTIVE", "securityType": st})
        assert ok is True, st


@pytest.mark.parametrize("status", ["SCHEDULED", "DELISTED"])
def test_info_inactive_status_blocked(status):
    ok, reason = _is_tradable_info({"status": status, "securityType": "STOCK"})
    assert ok is False and status in reason


@pytest.mark.parametrize("st", ["ETF", "FOREIGN_ETF", "ETN",
                                "DEPOSITARY_RECEIPT", "STOCK_WARRANTS"])
def test_info_blocked_security_types(st):
    ok, reason = _is_tradable_info({"status": "ACTIVE", "securityType": st})
    assert ok is False and st in reason


# ── (b) _active_warnings ──────────────────────────────────
TODAY = "2026-07-15"


@pytest.mark.parametrize("wt", ["LIQUIDATION_TRADING", "INVESTMENT_WARNING",
                                "INVESTMENT_RISK", "OVERHEATED"])
def test_warn_blocked_types_active(wt):
    w = [{"warningType": wt, "startDate": "2026-07-01", "endDate": None}]
    assert _active_warnings(w, TODAY) == [wt]


@pytest.mark.parametrize("wt", ["VI_STATIC_AND_DYNAMIC", "VI_STATIC",
                                "VI_DYNAMIC", "STOCK_WARRANTS"])
def test_warn_ignored_types(wt):
    w = [{"warningType": wt, "startDate": "2026-07-01", "endDate": None}]
    assert _active_warnings(w, TODAY) == []


def test_warn_ended_before_today_ignored():
    w = [{"warningType": "INVESTMENT_WARNING",
          "startDate": "2026-06-01", "endDate": "2026-07-10"}]
    assert _active_warnings(w, TODAY) == []


def test_warn_null_enddate_is_ongoing():
    w = [{"warningType": "INVESTMENT_RISK", "startDate": "2026-07-01", "endDate": None}]
    assert _active_warnings(w, TODAY) == ["INVESTMENT_RISK"]


def test_warn_null_startdate_treated_as_started():
    w = [{"warningType": "OVERHEATED", "startDate": None, "endDate": None}]
    assert _active_warnings(w, TODAY) == ["OVERHEATED"]


def test_warn_not_yet_started_ignored():
    w = [{"warningType": "OVERHEATED", "startDate": "2026-07-20", "endDate": None}]
    assert _active_warnings(w, TODAY) == []


def test_warn_endtoday_inclusive():
    w = [{"warningType": "LIQUIDATION_TRADING",
          "startDate": "2026-07-01", "endDate": "2026-07-15"}]
    assert _active_warnings(w, TODAY) == ["LIQUIDATION_TRADING"]


# ── (c) check_tradable — 캐시/fail-open ───────────────────
class _FakeClient:
    """호출 카운트 + 지정 응답/예외. get_stock_info/get_stock_warnings 만 구현."""
    def __init__(self, info=None, warnings=None, info_exc=None, warn_exc=None):
        self.info = info if info is not None else []
        self.warnings = warnings if warnings is not None else []
        self.info_exc = info_exc
        self.warn_exc = warn_exc
        self.info_calls = 0
        self.warn_calls = 0

    def get_stock_info(self, symbols):
        self.info_calls += 1
        if self.info_exc:
            raise self.info_exc
        return self.info

    def get_stock_warnings(self, symbol):
        self.warn_calls += 1
        if self.warn_exc:
            raise self.warn_exc
        return self.warnings


def test_check_cache_hit_skips_client(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    now = 1_000_000.0
    info_cache = {"005930": {"fetched": now,
                             "info": {"status": "ACTIVE", "securityType": "STOCK"}}}
    warn_cache = {"005930": {"fetched": now, "warnings": []}}
    client = _FakeClient()
    ok, reason = check_tradable("005930", "KR", client=client,
                                info_cache=info_cache, warn_cache=warn_cache, now=now)
    assert ok is True and reason == ""
    assert client.info_calls == 0 and client.warn_calls == 0    # 캐시로 client 미호출


def test_check_stale_cache_refetches(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    now = 1_000_000.0
    stale = now - INFO_TTL_SEC - 1
    info_cache = {"005930": {"fetched": stale,
                             "info": {"status": "ACTIVE", "securityType": "STOCK"}}}
    warn_cache = {"005930": {"fetched": now, "warnings": []}}
    client = _FakeClient(info=[{"symbol": "005930", "status": "ACTIVE",
                                "securityType": "STOCK"}])
    ok, _ = check_tradable("005930", "KR", client=client,
                           info_cache=info_cache, warn_cache=warn_cache, now=now)
    assert ok is True
    assert client.info_calls == 1                               # TTL 만료 → 재조회


def test_check_fetch_exception_fail_open(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    client = _FakeClient(info_exc=RuntimeError("network down"),
                         warn_exc=RuntimeError("network down"))
    ok, reason = check_tradable("005930", "KR", client=client,
                                info_cache={}, warn_cache={}, now=1_000_000.0)
    assert ok is True and reason == ""                          # fail-open


def test_check_blocks_etf_from_info(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    client = _FakeClient(info=[{"symbol": "069500", "status": "ACTIVE",
                                "securityType": "ETF"}])
    ok, reason = check_tradable("069500", "KR", client=client,
                                info_cache={}, warn_cache={}, now=1_000_000.0)
    assert ok is False and "ETF" in reason
    assert client.warn_calls == 0                               # info 차단 시 warnings 미조회


def test_check_blocks_active_warning(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    client = _FakeClient(
        info=[{"symbol": "005930", "status": "ACTIVE", "securityType": "STOCK"}],
        warnings=[{"warningType": "INVESTMENT_WARNING",
                   "startDate": "2026-07-01", "endDate": None}])
    ok, reason = check_tradable("005930", "KR", client=client,
                                info_cache={}, warn_cache={}, now=_NOW)
    assert ok is False and "INVESTMENT_WARNING" in reason


def test_check_us_symbol_empty_warnings_pass(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    client = _FakeClient(
        info=[{"symbol": "AAPL", "status": "ACTIVE", "securityType": "FOREIGN_STOCK"}],
        warnings=[])
    ok, reason = check_tradable("AAPL", "US", client=client,
                                info_cache={}, warn_cache={}, now=1_000_000.0)
    assert ok is True and reason == ""

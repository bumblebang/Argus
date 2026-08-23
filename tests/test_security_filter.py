"""security_filter — ETF 등 매수불가 조기 제외."""
from src.security_filter import (is_buy_ineligible, is_kr_etf_name,
                                 filter_universe, filter_candidates)


def test_kr_etf_name():
    assert is_kr_etf_name("TIGER 반도체TOP10")
    assert is_kr_etf_name("KODEX 200")
    assert not is_kr_etf_name("삼성전자")
    assert not is_kr_etf_name("")


def test_buy_ineligible_by_name():
    bad, reason = is_buy_ineligible("396500", "KR", "TIGER 반도체TOP10")
    assert bad and "ETF" in reason


def test_buy_ineligible_by_cache():
    cache = {"396500": {"fetched": 1, "info": {
        "securityType": "ETF", "status": "ACTIVE", "name": "TIGER"}}}
    bad, reason = is_buy_ineligible("396500", "KR", "", info_cache=cache)
    assert bad and reason == "부적격유형: ETF"
    ok, _ = is_buy_ineligible("005930", "KR", "삼성전자", info_cache={
        "005930": {"info": {"securityType": "STOCK", "status": "ACTIVE"}}})
    assert not ok


def test_filter_universe_drops_etf():
    uni = {"KR": [
        {"symbol": "005930", "name": "삼성전자"},
        {"symbol": "396500", "name": "TIGER 반도체TOP10"},
    ]}
    out = filter_universe(uni, log_drops=False)
    assert [x["symbol"] for x in out["KR"]] == ["005930"]


def test_filter_candidates():
    cands = [("KR", "069500", "KODEX 200"), ("KR", "005930", "삼성전자")]
    out = filter_candidates(cands)
    assert out == [("KR", "005930", "삼성전자")]

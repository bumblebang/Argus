"""markets.chg_1d 창 밀림 회귀 — '전일 대비'가 조용히 '전전일 대비'가 되던 버그.

Yahoo 가 최근 거래일 봉을 빼거나 close 를 null 로 주면 None 필터가 창을 하루 민다.
값이 그럴듯해 눈으로는 못 잡는다. 이 숫자는 뇌의 매매 판단에 직접 들어가므로 고정한다.
거래일 비교는 UTC 달력일이 아니라 market_hours.trading_date(시장 로컬).
"""
from src.datasources.markets import _closes_with_time
from src.market_hours import trading_date

D0, D1, D2 = 1785283200, 1785369600, 1785456000     # 07-29 / 07-30 / 07-31 (UTC 자정)
_MKT = "KR"


def _res(stamps, closes, meta_t=None, meta_px=None):
    meta = {}
    if meta_t is not None:
        meta["regularMarketTime"] = meta_t
    if meta_px is not None:
        meta["regularMarketPrice"] = meta_px
    return {"timestamp": list(stamps),
            "indicators": {"quote": [{"close": list(closes)}]},
            "meta": meta}


def test_normal_series_uses_last_two_closes():
    pairs = _closes_with_time(_res([D0, D1, D2], [5663.24, 5593.56, 6595.45]),
                              market=_MKT)
    assert pairs[-1] == (D2, 6595.45) and pairs[-2] == (D1, 5593.56)


def test_missing_last_bar_is_filled_from_meta():
    # 07-31 봉이 아예 없다 → meta 로 보충하지 않으면 07-30 이 '최신'이 된다(하루 밀림).
    pairs = _closes_with_time(_res([D0, D1], [5663.24, 5593.56],
                                   meta_t=D2 + 23400, meta_px=6595.45),
                              market=_MKT)
    assert pairs[-1][1] == 6595.45
    assert pairs[-2][1] == 5593.56                  # 전일 대비 = +17.91% 가 나온다


def test_null_last_close_is_filled_from_meta():
    pairs = _closes_with_time(_res([D0, D1, D2], [5663.24, 5593.56, None],
                                   meta_t=D2 + 23400, meta_px=6595.45),
                              market=_MKT)
    assert pairs[-1][1] == 6595.45 and pairs[-2][1] == 5593.56


def test_meta_of_same_day_does_not_duplicate():
    # 마지막 봉과 meta 가 같은 거래일이면 봉을 신뢰한다(장중 실시간가로 덮어쓰지 않는다).
    pairs = _closes_with_time(_res([D0, D1, D2], [5663.24, 5593.56, 6595.45],
                                   meta_t=D2 + 23400, meta_px=6600.0),
                              market=_MKT)
    assert len(pairs) == 3 and pairs[-1][1] == 6595.45


def test_broken_meta_is_ignored():
    for mt, mp in ((None, 100.0), (D2, None), ("쓰레기", 100.0)):
        pairs = _closes_with_time(_res([D1], [5593.56], meta_t=mt, meta_px=mp),
                                  market=_MKT)
        assert pairs[-1][1] == 5593.56              # 예외 없이 봉만 쓴다


def test_empty_response_is_empty():
    assert _closes_with_time({}) == []
    assert _closes_with_time(_res([], [])) == []


def test_trading_date_uses_market_local_not_utc():
    """UTC 자정 epoch 도 KST 거래일이면 그날 — last_bar_trading_date 와 같은 축."""
    assert trading_date("KR", D2) == "2026-07-31"
    # US: UTC 07-31 00:00 은 ET 전날 저녁 → 거래일 07-30
    assert trading_date("US", D2) == "2026-07-30"


def test_no_timestamps_falls_back_to_closes_only():
    """Yahoo 응답에 timestamp 가 없어도 markets 슬롯이 통째로 비면 안 된다(예전 동작이 바닥)."""
    pairs = _closes_with_time({"indicators": {"quote": [{"close": [2480.0, 2500.0]}]}})
    assert [c for _t, c in pairs] == [2480.0, 2500.0]
    assert all(t is None for t, _c in pairs)        # 날짜는 포기 → asof 를 안 붙인다

"""보유 표 통합 — 실계좌 스냅샷 + 봇 원장(전략·손절·목표)을 한 표로.

예전엔 상단 '실계좌 보유'와 '오늘' 탭 '보유 포지션'이 같은 보유를 두 번 보여줬고
운용계획(전략·손절·목표)은 아래에만 있었다. 합치면서 **어긋남을 삼키지 않는 것**이
계약이다 — 원장에만 있는 보유, 손절 미설정은 반드시 눈에 보여야 한다.
"""
from scripts.dashboard import _asset_html, _open_ledger, _today_html


def _d(items, positions, snap=True):
    d = {"now": 1785500000.0, "names": {}, "live_mode": True, "positions": positions,
         "pos_px": {}, "live_trades": [], "sentiment": {}, "fear_history": {},
         "hb": None, "hb_age": None, "kr_session": "closed", "us_session": "closed"}
    if snap:
        d["snapshot"] = {"ts": 1785499900.0, "items": items, "cash": {"KR": 100.0},
                         "market_value": {"KR": 0.0}, "total_purchase": {"KR": 0.0}}
    return d


_SNAP_A = {"symbol": "194700", "name": "노바렉스", "market": "KR", "qty": 7,
           "avg": 13680, "last": 14000, "value": 98000, "pnl": 2240, "pnl_rate": 0.0233}
_SNAP_B = {"symbol": "005930", "name": "삼성전자", "market": "KR", "qty": 1,
           "avg": 267500, "last": 259000, "value": 259000, "pnl": -8500,
           "pnl_rate": -0.0317}
_POS_A = {"symbol": "194700", "state": "open", "market": "KR", "qty": 7,
          "avg_price": 13680, "strategy": "value", "stop_price": 10957.48,
          "target_price": 15489.6}
_POS_ADOPTED = {"symbol": "005930", "state": "open", "market": "KR", "qty": 1,
                "avg_price": 267500, "strategy": None, "stop_price": None,
                "target_price": None}


def test_open_ledger_only_takes_open_state():
    pos = [_POS_A, {"symbol": "X", "state": "armed"}, {"symbol": "Y", "state": "closed"}]
    assert set(_open_ledger({"positions": pos})) == {"194700"}


def test_plan_columns_joined_onto_snapshot_row():
    h = _asset_html(_d([_SNAP_A], [_POS_A]))
    assert "보유 종목 (실계좌 + 봇 운용계획)" in h
    assert "value" in h and "10,957" in h and "15,490" in h    # 전략·손절·목표가 실렸다


def test_missing_stop_is_surfaced_not_silent():
    # 계좌 동기화로 채택된 보유는 코드 손절이 없다 — '–' 로 뭉개면 위험이 안 보인다.
    h = _asset_html(_d([_SNAP_B], [_POS_ADOPTED]))
    assert "미설정" in h and "freshwarn" in h
    assert "전략 미배정" in h


def test_ledger_only_position_is_warned():
    # 원장엔 열려 있는데 스냅샷엔 없다 = 스냅샷 지연이거나 유령. 조용히 숨기면 안 된다.
    h = _asset_html(_d([_SNAP_B], [_POS_A, _POS_ADOPTED]))
    assert "봇 원장에만 있는 보유" in h
    # 스냅샷에 있던 종목은 경고에 안 들어간다
    assert h.count("봇 원장에만 있는 보유") == 1


def test_no_ledger_only_warning_when_matched():
    h = _asset_html(_d([_SNAP_A, _SNAP_B], [_POS_A, _POS_ADOPTED]))
    assert "봇 원장에만 있는 보유" not in h
    assert "원장 없음" not in h


def test_snapshot_absent_falls_back_to_ledger():
    # 스냅샷이 없다고 보유가 통째로 사라지면 안 된다(합치기 전엔 '오늘' 탭이 받쳐줬다).
    h = _asset_html(_d([], [_POS_A], snap=False))
    assert "봇 원장 — 실계좌 스냅샷 대기중" in h
    assert "노바렉스" in h or "194700" in h
    assert "10,957" in h


def test_today_tab_no_longer_duplicates_holdings():
    h = _today_html(_d([_SNAP_A], [_POS_A]))
    assert "보유 포지션" not in h
    assert "진입대기" in h            # 보유가 아닌 것은 그대로 남는다

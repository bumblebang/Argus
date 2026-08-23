"""engine.store — SQLite 저장소 CRUD/상태기계 검증."""
from src.engine.store import Store


def test_log_event_and_snapshot(tmp_path):
    s = Store(tmp_path / "t.db")
    eid = s.log_event("trigger", "005930", {"reason": "drop -5%"})
    assert eid > 0
    sid = s.record_snapshot("005930", price=70000.0, payload={"rsi": 42})
    assert sid > 0


def test_record_snapshots_batch(tmp_path):
    s = Store(tmp_path / "t.db")
    n = s.record_snapshots([
        {"symbol": "A", "price": 1.0, "payload": {"x": 1}},
        {"symbol": "B", "price": 2.0},
    ])
    assert n == 2


def test_position_lifecycle(tmp_path):
    s = Store(tmp_path / "t.db")
    pid = s.open_position("005930", "KR", qty=10, avg_price=70000,
                          strategy="swing", thesis="반도체 반등", stop_price=68000)
    opens = s.get_open_positions()
    assert len(opens) == 1
    assert opens[0]["symbol"] == "005930"
    assert opens[0]["strategy"] == "swing"

    s.update_position(pid, stop_price=69000, meta={"trail": True})
    s.close_position(pid)
    assert s.get_open_positions() == []


def test_record_decision(tmp_path):
    s = Store(tmp_path / "t.db")
    did = s.record_decision("000660", "BUY", conviction=0.7, thesis="t",
                            verdict="vetoed", payload={"rule": "conviction"})
    assert did > 0


def test_armed_lifecycle(tmp_path):
    s = Store(tmp_path / "t.db")
    aid = s.arm_candidate("005930", "KR", strategy="rsi_reversion", thesis="데이트레",
                          meta={"horizon": "day", "params": {"period": 14}})
    assert aid > 0
    assert [r["symbol"] for r in s.get_armed()] == ["005930"]
    assert s.get_open_positions() == []               # armed 는 보유가 아니므로 제외
    # 진입 체결 -> open 승격, 진입가 기준 손절/목표 확정
    s.promote_armed(aid, qty=5, avg_price=70000, stop_price=68600, target_price=72100)
    assert s.get_armed() == []
    opens = s.get_open_positions()
    assert len(opens) == 1
    assert opens[0]["state"] == "open" and opens[0]["qty"] == 5
    assert opens[0]["stop_price"] == 68600 and opens[0]["target_price"] == 72100
    assert opens[0]["strategy"] == "rsi_reversion"    # 배정 전략 유지

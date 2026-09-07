"""value_ops — S1–S4 헬퍼 단위 테스트."""
from __future__ import annotations

from types import SimpleNamespace

from src.value_ops import (
    update_jaccard_state,
    annotate_watchlist_scores, apply_first_seen, apply_post_cycle_cooldown,
    effective_new_entries_cap, jaccard,
    load_cooldown, record_cooldown_event, truncate_buys_by_score, in_cooldown,
    COOLDOWN_PATH,
)


def test_jaccard():
    assert jaccard(["a", "b"], ["b", "c"]) == 1 / 3


def test_effective_new_cap_normal_ticket():
    # room 50만, ticket 30만 → 1
    assert effective_new_entries_cap(
        ceiling=2, remaining_slots=3, sleeve_room=500_000, min_ticket=300_000) == 1
    # room < ticket → 0 (1주 자투리로 열지 않음)
    assert effective_new_entries_cap(
        ceiling=2, remaining_slots=3, sleeve_room=50_000, min_ticket=300_000) == 0


def test_truncate_by_score():
    gated = [
        {"symbol": "A", "composite_value": 0.9},
        {"symbol": "B", "composite_value": 0.2},
        {"symbol": "C", "composite_value": 0.5},
    ]
    props = [
        SimpleNamespace(symbol="B", side="BUY"),
        SimpleNamespace(symbol="A", side="BUY"),
        SimpleNamespace(symbol="C", side="BUY"),
    ]
    kept, drops = truncate_buys_by_score(props, gated, cap=1)
    buy_kept = [p for p in kept if p.side == "BUY"]
    assert len(buy_kept) == 1 and buy_kept[0].symbol == "A"
    assert {d["symbol"] for d in drops} == {"B", "C"}
    assert drops[0]["llm_proposal_rank"] is not None


def test_first_seen_clear_after_n_days():
    now = 1_000_000.0
    e = apply_first_seen({}, stance="undervalued", now=now)
    assert e["first_seen_at"] == now
    e2 = apply_first_seen(e, stance="fair", now=now + 86400)
    assert e2.get("first_seen_at") == now
    assert "left_undervalued_at" in e2
    e3 = apply_first_seen(e2, stance="fair", now=now + 4 * 86400, clear_after_days=3)
    assert "first_seen_at" not in e3


def test_cooldown_streak():
    cd = {}
    now = 1_000.0
    for _ in range(3):
        record_cooldown_event(cd, "X", kind="llm_hold", now=now, hold_n=3, cool_days=5)
        now += 1
    assert in_cooldown(cd, "X", now)


def test_post_cycle_cooldown_whitelist(tmp_path, monkeypatch):
    path = tmp_path / "cd.json"
    monkeypatch.setattr("src.value_ops.COOLDOWN_PATH", path)
    now = 2_000.0
    apply_post_cycle_cooldown(
        [{"symbol": "A", "action": "BUY", "status": "vetoed"},
         {"symbol": "B", "action": "BUY", "status": "gate_rejected"},
         {"symbol": "C", "action": "BUY", "status": "filled"}],
        now=now, hold_n=1, cool_days=5)
    cd = load_cooldown(path)
    assert in_cooldown(cd, "A", now + 1)  # veto → 가산
    assert not in_cooldown(cd, "B", now + 1)  # 하드게이트 제외
    assert (cd.get("C") or {}).get("streak", 0) == 0


def test_annotate_us_mcap_busd_and_per_market():
    wl = {
        "005930": {
            "market": "KR", "stance": "undervalued", "first_seen_at": 1.0,
            "metrics": {"market_cap": 500e12, "drawdown_1y_pct": -40},
            "fundamentals": {"pb": 0.8, "pe_trailing": 8},
        },
        "AAPL": {
            "market": "US", "stance": "undervalued", "first_seen_at": 1.0,
            "metrics": {"drawdown_1y_pct": -35},
            "fundamentals": {
                "market_cap_busd": 3000, "pb": 5.0, "pe_trailing": 25},
        },
    }
    annotate_watchlist_scores(wl, now=1.0 + 86400, n_min=1)
    assert wl["AAPL"].get("composite_value") is not None
    assert wl["005930"].get("composite_value") is not None


def test_streak_는_오래되면_끊긴다():
    """의도는 '연속 3회'다 — 한 달에 한 번씩 걸린 종목이 유배되면 안 된다."""
    cd = {}
    now = 1_000_000.0
    for _ in range(2):
        record_cooldown_event(cd, "X", kind="llm_hold", now=now,
                              hold_n=3, cool_days=5, streak_ttl_days=5)
        now += 60
    assert cd["X"]["streak"] == 2
    now += 30 * 86400                       # 한 달 뒤 — 연속이 끊겼다
    record_cooldown_event(cd, "X", kind="llm_hold", now=now,
                          hold_n=3, cool_days=5, streak_ttl_days=5)
    assert cd["X"]["streak"] == 1
    assert not in_cooldown(cd, "X", now + 1)


def test_연속이면_그대로_유배():
    cd = {}
    now = 1_000_000.0
    for _ in range(3):
        record_cooldown_event(cd, "X", kind="llm_hold", now=now,
                              hold_n=3, cool_days=5, streak_ttl_days=5)
        now += 86400                        # 매일 연속
    assert in_cooldown(cd, "X", now)


def _wl_top(syms, mkt="KR"):
    return {s: {"market": mkt, "stance": "undervalued",
                "composite_value": 1.0 - i * 0.01} for i, s in enumerate(syms)}


def test_자카드_기준선은_하루단위(tmp_path):
    """스캔이 하루 2~3회라 매번 기준선을 갱신하면 '몇 시간 전 대비'가 된다."""
    p = tmp_path / "j.json"
    day1 = 1_800_000_000.0                      # KST 어느 날
    day2 = day1 + 86400
    update_jaccard_state(_wl_top(["A", "B", "C"]), market="KR", now=day1, path=p)
    # 같은 날 2차 스캔에서 명단이 완전히 바뀌어도 기준선은 그대로 → 변동이 잡힌다
    r2 = update_jaccard_state(_wl_top(["X", "Y", "Z"]), market="KR",
                              now=day1 + 3600, path=p)
    assert r2["jaccard"] == 0.0
    # 다음 날: 기준선이 '전일 마지막 스냅샷(X,Y,Z)'으로 승격
    r3 = update_jaccard_state(_wl_top(["X", "Y", "Z"]), market="KR", now=day2, path=p)
    assert r3["prev"] == ["X", "Y", "Z"] and r3["jaccard"] == 1.0

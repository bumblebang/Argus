"""09-28 #79 후속 — 멈춘 호가 판별(3)·데이 트랙 스위치(10)·청산 순서/트리거 기록(11)·그림자 채점(12)."""
import json
import time
from datetime import datetime, timedelta, timezone

from src.day_pool import active_day_pool, day_pool_active, day_track_enabled
from src.engine.execution import EntryExecutor
from src.engine.loop import WatchLoop
from src.engine.store import Store
from src.shadow_ledger import (book_row, exit_close_on_calendar, load_daily_series,
                               pick_history_csv, rescore_shadow_exits)
from tests.test_engine_loop import FakeGateway, _only_kr_open
from tests.test_price_guard import _Clock, _guard

KST = timezone(timedelta(hours=9))


# ── 3) 멈춘 호가는 증명될 때만 ──────────────────────────────────
def _wobble(g, clock, raws, secs):
    """틱마다 raws 를 번갈아 넣고 판정 목록을 돌려준다."""
    verdicts = []
    for i in range(secs):
        verdicts.append(g.filter("A", raws[i % len(raws)])[1])
        clock.t += 1
    return verdicts


def test_quote_without_timestamp_never_treated_as_frozen():
    """호가 시각이 없으면 멈춤을 증명 못 한다 — 흔들리는 오류가를 3분 뒤 채택하지 않는다."""
    clock = _Clock()
    g, _ = _guard((99.0, 101.0, None), clock=clock, confirm_sec=180, recheck_sec=5)
    g.filter("A", 100.0)
    verdicts = _wobble(g, clock, [70.0, 70.5], 400)
    assert set(verdicts) == {"rejected"}


def test_quote_values_moving_means_live_book():
    """호가 시각이 의심 전이어도 bid/ask 값이 바뀌었으면 살아 있는 호가 — 기각 유지."""
    clock = _Clock()
    frozen_at = clock.t - 600
    g, q = _guard((99.0, 101.0, frozen_at), clock=clock, confirm_sec=180, recheck_sec=5)
    g.filter("A", 100.0)
    _wobble(g, clock, [70.0, 70.5], 60)
    q.quote = (99.5, 101.5, frozen_at)                  # 값은 움직임(시각 필드만 낡음)
    verdicts = _wobble(g, clock, [70.0, 70.5], 300)
    assert set(verdicts) == {"rejected"}


def test_frozen_book_with_timestamp_still_accepts_real_move():
    clock = _Clock()
    frozen_at = clock.t - 600
    g, _ = _guard((99.0, 101.0, frozen_at), clock=clock, confirm_sec=180, recheck_sec=5)
    g.filter("A", 100.0)
    verdicts = _wobble(g, clock, [70.0, 69.0], 200)
    assert "stale_quote" in verdicts                    # confirm_sec 뒤 진짜 급락 채택


# ── 10) 데이 트랙 스위치 ───────────────────────────────────────
def test_day_track_defaults_off():
    assert day_track_enabled({}) is False
    assert day_track_enabled({"day_track": {"enabled": True}}) is True
    assert day_pool_active({"day_pool": {"enabled": True}}) is False


def test_stale_day_pool_not_merged_when_off(tmp_path):
    p = tmp_path / "day_pool.yaml"
    p.write_text("KR:\n- symbol: '005930'\n", encoding="utf-8")
    assert active_day_pool({"day_pool": {"enabled": True}}, p) == {}
    on = {"day_track": {"enabled": True}, "day_pool": {"enabled": True}}
    assert active_day_pool(on, p)["KR"][0]["symbol"] == "005930"


class _NoBroker:
    def position(self, sym):
        raise AssertionError("진입 평가까지 가면 안 된다")


def test_armed_day_disarmed_when_track_off(tmp_path):
    store = Store(tmp_path / "t.db")
    aid = store.arm_candidate("005930", "KR", strategy="volatility_breakout",
                              meta={"horizon": "day", "params": {}})
    ex = EntryExecutor(None, _NoBroker(), None, store, day_enabled=lambda: False)
    armed = dict(store.get_armed()[0])
    out = ex.evaluate(armed, "KR", 70_000)
    assert out["reason"] == "day_disabled"
    assert store.get_armed() == []
    row = store.conn.execute("SELECT exit_reason FROM positions WHERE id=?",
                             (aid,)).fetchone()
    assert row["exit_reason"] == "disarm:day_disabled"


def test_armed_swing_unaffected_by_day_switch(tmp_path):
    store = Store(tmp_path / "t.db")
    store.arm_candidate("005930", "KR", strategy="macd",
                        meta={"horizon": "swing", "params": {}})

    class _Held:
        def position(self, sym):
            return type("P", (), {"qty": 1.0})()

    ex = EntryExecutor(None, _Held(), None, store, day_enabled=False)
    out = ex.evaluate(dict(store.get_armed()[0]), "KR", 70_000)
    assert out["reason"] == "already_held"              # 해제 경로를 안 탔다
    assert len(store.get_armed()) == 1


# ── 11) 청산 우선순위·1틱 1주문·트리거 기록 ─────────────────────
def test_stop_hit_tried_before_target_and_only_once_per_tick(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 59_000})
    pos = {"symbol": "005930", "market": "KR", "qty": 10, "avg_price": 70_000,
           "stop_price": 60_000, "target_price": 50_000,     # 둘 다 성립(비정상 설정)
           "meta": '{"horizon": "swing"}'}
    tried = []

    def execu(sym, m, p, t):
        tried.append(t.kind)
        return False                                        # 거부

    loop = WatchLoop(gw, store, lambda: {"KR": {"positions": [pos], "candidates": []}},
                     executor=execu)
    loop.run_once()
    assert tried and tried[0] == "stop_hit" and len(tried) == 1


def test_event_triggers_not_throttled(tmp_path, monkeypatch):
    """급변(vol_spike) 같은 사건형 트리거는 발생마다 기록 — 쓰로틀은 상태형만."""
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    prices = {"005930": 70_000}
    gw = FakeGateway(prices)
    clock = {"t": 1_000_000.0}
    loop = WatchLoop(gw, store, lambda: {"KR": {"positions": [], "candidates": ["005930"]}},
                     now_fn=lambda: clock["t"])
    seq = [70_000, 75_000, 70_000, 75_000]
    for px in seq:
        prices["005930"] = px
        loop.run_once()
        clock["t"] += 1
    rows = store.conn.execute(
        "SELECT json_extract(payload,'$.kind') AS k FROM events WHERE kind='trigger'"
    ).fetchall()
    assert sum(1 for r in rows if r["k"] == "vol_spike") >= 2


# ── 12) 그림자 채점 ────────────────────────────────────────────
def _csv(path, rows):
    path.write_text("Date,Open,High,Low,Close,Volume\n"
                    + "".join(f"{d},{c},{c},{c},{c},1\n" for d, c in rows),
                    encoding="utf-8")


def test_pick_history_prefers_freshest_csv(tmp_path):
    hist = tmp_path / "history"
    hist.mkdir()
    _csv(hist / "X.KS_1d_5y.csv", [("2026-07-01", 10), ("2026-07-28", 11)])
    _csv(hist / "X.KS_1d_6mo.csv", [("2026-09-01", 12), ("2026-09-22", 13)])
    assert pick_history_csv(tmp_path, "X").name == "X.KS_1d_6mo.csv"


def test_exit_close_rejects_stale_series(tmp_path):
    hist = tmp_path / "history"
    hist.mkdir()
    _csv(hist / "X_1d_1y.csv", [("2026-07-01", 10), ("2026-07-28", 11)])
    series = load_daily_series(tmp_path, "X")
    entry = datetime(2026, 8, 4, 10, tzinfo=KST).timestamp()
    # 목표일 08-24 인데 CSV 는 07-28 에서 끝 — 예전엔 07-28 종가(진입 전!)를 청산가로 썼다.
    assert exit_close_on_calendar(series, entry, 20, market="KR") is None


def test_book_row_corrects_entry_price_against_snapshot(tmp_path):
    store = Store(tmp_path / "t.db")
    ts = time.time()
    store.record_snapshots([{"symbol": "005930", "price": 250_000.0, "payload": {}}])
    rid = book_row(store, cycle_ts=ts, cycle_ts_iso="", sleeve="brain", symbol="005930",
                   market="KR", block_status="no_dossier", block_reason="",
                   verifier_reason=None, concerns=[], conviction=0.7, horizon="swing",
                   target_weight=0.1, thesis="t", strategy=None, proposal=None,
                   entry_price=64_369.09)
    row = store.conn.execute("SELECT entry_price, meta FROM shadow_positions WHERE id=?",
                             (rid,)).fetchone()
    assert row["entry_price"] == 250_000.0
    meta = json.loads(row["meta"])
    assert meta["price_source"] == "snapshot" and meta["entry_price_rejected"] == 64_369.09


def test_rescore_shadow_exits_fixes_and_voids(tmp_path):
    store = Store(tmp_path / "t.db")
    hist = tmp_path / "history"
    hist.mkdir()
    _csv(hist / "GOOD_1d_1y.csv", [("2026-08-01", 100), ("2026-08-21", 120)])
    _csv(hist / "STALE_1d_1y.csv", [("2026-07-01", 100), ("2026-07-10", 90)])
    entry = datetime(2026, 8, 1, 10, tzinfo=KST).timestamp()
    ids = {}
    for sym in ("GOOD", "STALE"):
        rid = store.insert_shadow_position(
            cycle_ts=entry, sleeve="brain", symbol=sym, market="KR",
            block_status="vetoed", block_bucket="검증:LLM거부", block_reason="",
            concerns=[], conviction=0.7, horizon="swing", target_weight=0.1,
            thesis="", strategy=None, proposal_json=None, entry_price=100.0,
            entry_ts=entry, meta={"horizon_days": 20})
        store.score_shadow_position(rid, exit_price=90.0, exit_ts=entry + 20 * 86400,
                                    exit_reason="horizon_expired", ret_pct=-10.3)
        ids[sym] = rid
    dry = rescore_shadow_exits(store, data_dir=tmp_path, apply=False)
    assert dry["fixed"] == 1 and dry["voided"] == 1
    assert len(store.get_scored_shadow_positions()) == 2       # dry-run 은 무변경
    rescore_shadow_exits(store, data_dir=tmp_path)
    scored = {r["symbol"]: dict(r) for r in store.get_scored_shadow_positions()}
    assert set(scored) == {"GOOD"} and scored["GOOD"]["exit_price"] == 120.0

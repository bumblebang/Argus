"""11-27 점검 12건 회귀 테스트 — 항목 번호는 PR 본문과 같다."""
import json
import time

import pytest

import src.engine.loop as loopmod
from src.broker import Broker
from src.broker_sync import apply_reconcile_from_live, fetch_live_account_data
from src.engine.backoff import backoff_delay
from src.engine.execution import ExitExecutor
from src.engine.loop import WatchLoop, WatchConfig, TickResult
from src.engine.price_guard import PriceGuard
from src.engine.store import Store
from src.paper_account import PaperAccount, Position
from src.risk_gate import Order, RiskGate
from src.store_sync import RECONCILE_THESIS, adopt_live_position


# ── 공용 ─────────────────────────────────────────────────────────
def _gate(tmp_path):
    return RiskGate({"capital": {"KR": 10_000_000}, "max_position_pct": 1.0,
                     "max_positions": 5, "daily_loss_limit_pct": 0.5,
                     "kill_switch_file": str(tmp_path / "HALT")})


def _acct(tmp_path, cash=1_000_000):
    return PaperAccount(cash={"KR": cash}, fee_rate={"KR": 0.0},
                        slippage_bps={"KR": 0.0}, state_path=tmp_path / "a.json")


class _Client:
    def __init__(self, detail=None):
        self.detail = detail or {"status": "PENDING", "execution": {"filledQuantity": 0}}
        self.after_cancel = {"status": "CANCELED", "execution": {"filledQuantity": 0}}
        self.placed, self.canceled = [], []
        self.sellable = 100

    def get_sellable(self, seq, symbol):
        return {"sellableQuantity": self.sellable}

    def orderbook(self, symbol, market=None):
        return None

    def place_order(self, **kw):
        self.placed.append(kw)
        return {"orderId": f"O{len(self.placed)}"}

    def get_order(self, account_seq, order_id):
        if order_id in self.canceled:
            return self.after_cancel
        return self.detail

    def cancel_order(self, account_seq, order_id):
        self.canceled.append(order_id)
        return {"ok": True}


def _broker(tmp_path, client, **kw):
    store = Store(tmp_path / "t.db")
    b = Broker(account=_acct(tmp_path), gate=_gate(tmp_path), client=client,
               mode="live", account_seq="A1", live_markets=["KR"], store=store,
               reconcile_poll_attempts=1, reconcile_poll_sec=0.0, **kw)
    return store, b


class _Gw:
    def __init__(self, prices):
        self.prices = prices

    def poll_prices(self, symbols, record=True):
        return [{"symbol": s, "price": self.prices.get(s), "payload": {}} for s in symbols]

    def candles(self, symbol, interval="1m", count=20):
        return [{"close": 1}]


def _kr_open(monkeypatch):
    monkeypatch.setattr(loopmod, "current_session", lambda m, now=None:
                        "regular" if m == "KR" else "closed")
    monkeypatch.setattr(loopmod, "is_tradable", lambda m, allowed=None, now=None: m == "KR")


_POS = {"symbol": "005930", "market": "KR", "qty": 10, "avg_price": 70000,
        "stop_price": 69000, "target_price": None}


# ── 1) 백오프 오버플로 ────────────────────────────────────────────
def test_1_backoff_never_overflows():
    assert backoff_delay(10.0, 1, 30.0) == 10.0
    assert backoff_delay(10.0, 3, 1000.0) == 40.0
    assert backoff_delay(10.0, 1025, 30.0) == 30.0       # 예전: OverflowError
    assert backoff_delay(10.0, 10**6, 120.0) == 120.0


def test_1_exit_ready_after_1025_rejections(tmp_path):
    ex = ExitExecutor(broker=None, store=None, clock=lambda: 1e9)
    ex._retry["005930"] = (1025, 1e9 - 31)
    assert ex.ready("005930", "stop_hit") is True        # 예외 없이 판정


def test_1_ready_error_does_not_kill_tick_for_other_symbols(tmp_path, monkeypatch):
    _kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    other = {**_POS, "symbol": "000660"}
    exits = []

    class Ex:
        def ready(self, sym, kind):
            if sym == "005930":
                raise OverflowError("boom")
            return True

        def __call__(self, sym, m, p, t):
            exits.append(sym)
            return True

    loop = WatchLoop(_Gw({"005930": 68000, "000660": 68000}), store,
                     lambda: {"KR": {"positions": [_POS, other], "candidates": []}},
                     executor=Ex())
    res = loop.run_once()
    assert set(res.exits) == {"005930", "000660"} and sorted(exits) == ["000660", "005930"]


# ── 2) 취소 시점 부분체결 → 재발주 수량 차감 ─────────────────────────
def test_2_rebuy_subtracts_fill_at_cancel(tmp_path):
    client = _Client({"status": "FILLED", "execution": {
        "filledQuantity": 6, "averageFilledPrice": 71_000, "commission": 0, "tax": 0}})
    client.after_cancel = {"status": "CANCELED", "execution": {
        "filledQuantity": 4, "averageFilledPrice": 70_000, "commission": 0, "tax": 0}}
    store, broker = _broker(tmp_path, client, working_order_ttl_sec=600.0)
    store.upsert_working_order(order_id="WB1", symbol="005930", market="KR",
                               side="BUY", qty=10, price=70_000, status="PENDING")
    res = broker.execute(Order("005930", "KR", "BUY", 10, 71_000), "reprice")
    assert client.canceled == ["WB1"]
    assert client.placed and float(client.placed[-1]["qty"]) == 6.0
    assert res.ok


def test_2_rebuy_rejected_when_cancel_fill_covers_target(tmp_path):
    client = _Client()
    client.after_cancel = {"status": "CANCELED", "execution": {
        "filledQuantity": 10, "averageFilledPrice": 70_000, "commission": 0, "tax": 0}}
    store, broker = _broker(tmp_path, client, working_order_ttl_sec=600.0)
    store.upsert_working_order(order_id="WB1", symbol="005930", market="KR",
                               side="BUY", qty=10, price=70_000, status="PENDING")
    res = broker.execute(Order("005930", "KR", "BUY", 10, 71_000), "reprice")
    assert not res.ok and "부분체결" in (res.reject_reason or "")
    assert client.placed == []


# ── 3) 미체결 손절 SELL 이 매도가능 0 을 만들면 취소 후 재지정가 ───────────
def test_3_own_working_sell_released_on_sellable_zero(tmp_path):
    client = _Client({"status": "FILLED", "execution": {
        "filledQuantity": 1, "averageFilledPrice": 60_000, "commission": 0, "tax": 0}})
    store, broker = _broker(tmp_path, client, working_order_ttl_sec=600.0)
    broker.account.apply_fill("005930", "KR", "BUY", 1, 70_000, 0.0, "seed")
    store.upsert_working_order(order_id="WS1", symbol="005930", market="KR",
                               side="SELL", qty=1, price=65_000, status="PENDING")
    calls = {"n": 0}

    def sellable(seq, symbol):                 # 미체결 SELL 이 잡고 있으면 0, 취소 후 1
        calls["n"] += 1
        return {"sellableQuantity": 0 if "WS1" not in client.canceled else 1}
    client.get_sellable = sellable
    res = broker.execute(Order("005930", "KR", "SELL", 1, 60_000), "[exit] stop_hit",
                         exit_reason="stop_hit")
    assert client.canceled == ["WS1"]
    assert len(client.placed) == 1 and res.ok
    assert calls["n"] == 2


def test_3_sellable_zero_without_own_order_still_skips(tmp_path):
    client = _Client()
    client.sellable = 0
    _store, broker = _broker(tmp_path, client)
    res = broker.execute(Order("005930", "KR", "SELL", 1, 60_000), "x")
    assert not res.ok and broker.last_reject_reason == "매도가능 0"


# ── 4) 낡은/끊긴 호가가 손절을 무기한 보류하지 않는다 ───────────────────
def _stop_loop(tmp_path, monkeypatch, quote_fn, clock):
    _kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    gw = _Gw({"005930": 68000})
    gw.best_quote = quote_fn
    exits = []
    loop = WatchLoop(gw, store, lambda: {"KR": {"positions": [_POS], "candidates": []}},
                     executor=lambda s, m, p, t: exits.append(t.kind) or True,
                     now_fn=lambda: clock["t"])
    return loop, exits


def test_4_stale_quote_timestamp_does_not_hold_stop(tmp_path, monkeypatch):
    clock = {"t": 10_000.0}
    loop, exits = _stop_loop(tmp_path, monkeypatch,
                             lambda s: (71000.0, 71100.0, clock["t"] - 600), clock)
    assert loop.run_once().exits == ["005930"]


def test_4_stop_hold_has_a_ceiling(tmp_path, monkeypatch):
    clock = {"t": 10_000.0}
    loop, exits = _stop_loop(tmp_path, monkeypatch,
                             lambda s: (71000.0, 71100.0, clock["t"]), clock)
    assert loop.run_once().exits == []                   # 살아 있는 호가 → 보류
    clock["t"] += loopmod._STOP_CONFIRM_MAX_HOLD_SEC + 1
    assert loop.run_once().exits == ["005930"]           # 상한 경과 → 청산


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_4_guard_rejection_lapses_when_quote_dies():
    clock = _Clock()
    quote = {"q": (49.9, 50.1)}
    g = PriceGuard(lambda s: quote["q"], clock=clock, confirm_sec=60, recheck_sec=30)
    g.filter("A", 50.0)
    assert g.filter("A", 40.0)[1] == "rejected"          # 살아 있는 호가가 부정
    quote["q"] = None                                    # 호가 끊김
    clock.t += 31
    assert g.filter("A", 40.5)[1] == "rejected"          # 끊긴 직후엔 아직 유지
    verdicts = []
    for _ in range(4):
        clock.t += 31
        verdicts.append(g.filter("A", 40.0)[1])
    assert "timeout" in verdicts                         # 끊김 지속 → 채택(손절 가능)


def test_4_guard_frozen_quote_without_timestamp_has_deadline():
    clock = _Clock()
    g = PriceGuard(lambda s: (49.9, 50.1, None), clock=clock, confirm_sec=60,
                   recheck_sec=30)
    g.filter("A", 50.0)
    verdicts = []
    for i in range(12):                                  # 체결가 흔들림(2가지 이상)
        verdicts.append(g.filter("A", 40.0 + (i % 2) * 0.5)[1])
        clock.t += 31
    assert verdicts[0] == "rejected"
    assert "stale_quote" in verdicts                     # 5×confirm_sec 뒤 멈춘 호가로 채택


# ── 5) regular 빠진 세션표는 저장하지 않는다 ─────────────────────────
def test_5_session_table_without_regular_not_saved(tmp_path, monkeypatch):
    from src.datasources import market_calendar as mc
    path = tmp_path / "market_sessions.json"
    monkeypatch.setattr(mc, "CACHE_PATH", path)

    class C:
        calls = 0

        def get_market_calendar(self, country, date=None):
            C.calls += 1
            return {"today": {"date": "2026-11-27", "integrated": {
                "preMarket": {"startTime": "2026-11-27T08:00:00+09:00",
                              "endTime": "2026-11-27T09:00:00+09:00"}}}}
    mc.refresh_sessions(C(), ("KR",))
    assert C.calls == 2                                  # 1회 재조회
    assert not path.exists()                             # 저장 안 함 → is_open 폴백


# ── 6) 보유 종목 시세 누락은 하트비트에 드러난다 ─────────────────────────
def test_6_held_price_missing_flags_heartbeat(tmp_path, monkeypatch):
    _kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    hb = tmp_path / "hb.json"
    clock = {"t": 1000.0}
    loop = WatchLoop(_Gw({"000660": 100.0}), store,
                     lambda: {"KR": {"positions": [_POS], "candidates": ["000660"]}},
                     heartbeat_path=hb, now_fn=lambda: clock["t"],
                     sleep_fn=lambda s: None)
    res = loop.run_once()
    assert res.held_unpriced == []                       # 유예 안
    clock["t"] += loopmod._HELD_UNPRICED_ALERT_SEC + 1
    res = loop.run_once()
    assert res.held_unpriced == ["005930"]
    loop._beat(res)
    data = json.loads(hb.read_text(encoding="utf-8"))
    assert data["ok"] is False and data["held_unpriced"] == ["005930"]
    kinds = [r["kind"] for r in store.conn.execute("SELECT kind FROM events")]
    assert "held_price_missing" in kinds


# ── 7) 예외 없는 빈 보유 응답 ───────────────────────────────────────
def test_7_malformed_holdings_is_failure():
    class C:
        def get_buying_power(self, seq, m):
            return {"cashBuyingPower": "1000"}

        def get_holdings(self, seq):
            return None
    data = fetch_live_account_data(C(), "A1", markets=("KR",))
    assert data["holdings_ok"] is False


def test_7_unexplained_empty_holdings_needs_confirmation(tmp_path):
    store, acct = Store(tmp_path / "t.db"), _acct(tmp_path)
    acct.positions["005930"] = Position(symbol="005930", qty=10, avg_price=70_000)
    acct.symbol_market["005930"] = "KR"
    store.open_position("005930", "KR", 10, 70_000, thesis="tracked")
    data = {"cash": {"KR": 900_000}, "items": [], "holdings_ok": True}
    r1 = apply_reconcile_from_live(acct, store, data, markets=("KR",))
    assert r1["holdings_ok"] is False and acct.position("005930").is_open
    assert len(store.get_open_positions()) == 1
    # 중간에 보유가 다시 보이면 연속 카운트가 끊긴다
    ok = {"cash": {"KR": 900_000}, "holdings_ok": True, "items": [
        {"symbol": "005930", "quantity": "10", "averagePurchasePrice": "70000",
         "marketCountry": "KR"}]}
    apply_reconcile_from_live(acct, store, ok, markets=("KR",))
    r2 = apply_reconcile_from_live(acct, store, data, markets=("KR",))
    assert r2["holdings_ok"] is False and acct.position("005930").is_open


# ── 9) DB 쓰기 실패가 감시 루프를 끝내지 않는다 ─────────────────────────
def test_9_db_failure_in_tick_error_handler_does_not_kill_loop(tmp_path, monkeypatch):
    _kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")

    def boom(*a, **k):
        raise RuntimeError("database is locked")
    loop = WatchLoop(_Gw({"005930": 68000}), store,
                     lambda: {"KR": {"positions": [_POS], "candidates": []}},
                     sleep_fn=lambda s: None)
    monkeypatch.setattr(loop, "run_once", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    loop.run_forever(max_ticks=1)                        # loop_start 는 정상 기록
    store.log_event = boom                               # 이후 DB 전부 실패
    assert loop.run_forever(max_ticks=3) == 3


# ── 10) 지연 체결 존 진입도 즉시 체결과 같은 손절·목표 ──────────────────
def test_10_deferred_zone_fill_keeps_zone_levels(tmp_path):
    store = Store(tmp_path / "t.db")
    zone = {"low": 99.0, "high": 101.0, "invalidation": 96.0, "target": 115.0}
    store.arm_candidate("005930", "KR", strategy="breakout", thesis="t",
                        meta={"horizon": "swing", "entry_zone": zone})
    assert adopt_live_position(store, "005930", "KR", 3, 100.0,
                               source="reconcile_adopted",
                               thesis=RECONCILE_THESIS) == "promoted"
    row = store.get_open_positions()[0]
    assert (row["stop_price"], row["target_price"]) == (96.0, 115.0)   # 예전: 95/110


def test_10_deferred_signal_fill_unchanged(tmp_path):
    store = Store(tmp_path / "t.db")
    store.arm_candidate("005930", "KR", strategy="breakout", thesis="t",
                        meta={"horizon": "swing"})
    adopt_live_position(store, "005930", "KR", 3, 100.0,
                        source="reconcile_adopted", thesis=RECONCILE_THESIS)
    row = store.get_open_positions()[0]
    assert (row["stop_price"], row["target_price"]) == (95.0, 110.0)


# ── 11) 공개 페이지에 진입대기 thesis 원문 미발행 ────────────────────────
def test_11_public_armed_has_no_thesis(tmp_path):
    import sqlite3
    from scripts.public_page import _collect_positions
    con = sqlite3.connect(tmp_path / "p.db")
    con.row_factory = sqlite3.Row
    con.executescript(
        "create table positions (symbol text, market text, state text, strategy text,"
        " avg_price real, stop_price real, target_price real, meta text, thesis text,"
        " opened_at real);"
        "create table snapshots (ts real, symbol text, price real);")
    con.execute("insert into positions values ('005930','KR','armed','breakout',0,61000,"
                "78000,'{}','뇌 비공개 논거 원문',1.0)")
    _held, armed = _collect_positions(con.cursor())
    assert armed and "thesis" not in armed[0]
    assert "비공개 논거" not in json.dumps(armed, ensure_ascii=False)


# ── 12) --dry 는 운영 파일을 덮지 않는다 ────────────────────────────
def test_12_krx_alerts_dry_writes_only_dry_path(tmp_path):
    from src import paths
    from src.datasources.krx_alerts import KrxAlertsSource
    from src.datasources.base import SourceContext
    live = tmp_path / "data" / "krx_blocked_symbols.json"
    live.parent.mkdir(parents=True)
    live.write_text('{"symbols": ["123456"]}', encoding="utf-8")
    prev = paths.set_data_root(tmp_path)
    try:
        KrxAlertsSource(blocked_path=live).fetch(
            SourceContext(client=None, symbols_by_market={}, dry=True))
    finally:
        paths.set_data_root(prev)
    assert live.read_text(encoding="utf-8") == '{"symbols": ["123456"]}'
    assert (tmp_path / "data" / "dry" / "krx_blocked_symbols.json").exists()


def test_12_dry_output_path_is_isolated(tmp_path):
    from src import paths
    prev = paths.set_data_root(tmp_path)
    try:
        p = paths.dry_output_path("data/market_state.json")
    finally:
        paths.set_data_root(prev)
    assert p == tmp_path / "data" / "dry" / "market_state.json" and p.parent.is_dir()

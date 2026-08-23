"""engine.loop — 감시 루프 1틱/상시루프 검증 (fake gateway + 시장시계 monkeypatch)."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import src.engine.loop as loopmod
from src.engine.loop import WatchLoop, WatchConfig
from src.engine.store import Store

_KST = ZoneInfo("Asia/Seoul")


def _iso(epoch: float) -> str:
    """epoch -> ISO8601 문자열(토스 payload timestamp 형식 흉내)."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _kst_epoch(y, m, d, hh, mm) -> float:
    """KST 벽시계 시각 -> epoch (extra_wakes HH:MM 테스트용)."""
    return datetime(y, m, d, hh, mm, tzinfo=_KST).timestamp()


class FakeGateway:
    """poll_prices/candles 만 흉내. 가격은 생성자에서 주입."""
    def __init__(self, prices: dict, candles=None, payloads: dict | None = None):
        self.prices = prices                      # {symbol: price}
        self.payloads = payloads or {}             # {symbol: payload dict} — timestamp 등 원본
        self._candles = candles if candles is not None else [{"close": 1}]
        self.candle_calls = []

    def poll_prices(self, symbols, record=True):
        return [{"symbol": s, "price": self.prices.get(s),
                 "payload": self.payloads.get(s, {})} for s in symbols]

    def candles(self, symbol, interval="1m", count=20):
        self.candle_calls.append(symbol)
        return self._candles


def _sessions(monkeypatch, smap):
    """시장별 현재 세션을 주입({market: 세션명}, 없는 시장=closed). 거래 게이트/세션 판정 일괄."""
    monkeypatch.setattr(loopmod, "current_session",
                        lambda m, now=None: smap.get(m, "closed"))
    monkeypatch.setattr(loopmod, "is_tradable",
                        lambda m, allowed=None, now=None:
                        smap.get(m, "closed") in (allowed if allowed is not None else ("regular",)))


def _only_kr_open(monkeypatch):
    _sessions(monkeypatch, {"KR": "regular"})


def _all_closed(monkeypatch):
    _sessions(monkeypatch, {})


def test_run_once_closed_markets_polls_nothing(tmp_path, monkeypatch):
    _all_closed(monkeypatch)
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})
    loop = WatchLoop(gw, store, lambda: {"KR": {"positions": [], "candidates": ["005930"]}})
    res = loop.run_once()
    assert res.markets_open == [] and res.polled == 0 and res.triggers == []


def test_stop_hit_routes_to_code_exit_not_wake(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 68000})           # 손절가 69000 아래
    pos = {"symbol": "005930", "market": "KR", "qty": 10,
           "avg_price": 70000, "stop_price": 69000, "target_price": None}
    woke, exits = [], []
    def execu(sym, market, price, trig):
        exits.append((sym, market, trig.kind))
        return True
    loop = WatchLoop(
        gw, store,
        lambda: {"KR": {"positions": [pos], "candidates": []},
                 "US": {"positions": [], "candidates": ["AAPL"]}},
        on_wake=lambda why, trigs: woke.append((why, trigs)), executor=execu)
    res = loop.run_once()

    assert res.markets_open == ["KR"]             # US 는 닫혀 폴링 안 함
    assert res.polled == 1
    assert {t.kind for t in res.triggers} >= {"stop_hit"}
    assert exits == [("005930", "KR", "stop_hit")] and res.exits == ["005930"]
    assert res.woke is False and woke == []       # 코드청산은 뇌 안 깨움
    assert "005930" not in gw.candle_calls        # 청산했으니 정밀 폴링도 스킵
    logged = {r["kind"] for r in store.conn.execute("SELECT kind FROM events").fetchall()}
    assert "trigger" in logged                    # wake/precision 은 없음


def test_vol_spike_wakes_brain(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"AAPL": 100})
    woke = []
    loop = WatchLoop(gw, store, lambda: {"KR": {"positions": [], "candidates": ["AAPL"]}},
                     on_wake=lambda why, trigs: woke.append((why, trigs)),
                     config=WatchConfig(vol_spike_pct=0.03, vol_window=5))
    loop.run_once()                               # hist=[100]
    gw.prices["AAPL"] = 104                        # +4% -> vol_spike(watch)
    res = loop.run_once()
    assert res.woke and woke and woke[0][0] == "wake_triggers"
    assert any(t.kind == "vol_spike" for t in res.triggers)
    assert "AAPL" in gw.candle_calls              # 임박 -> 정밀 폴링


def test_precision_capped_per_tick(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    # 5종목 모두 손절히트(act) 유발 -> 정밀폴링은 상한(2)까지만.
    syms = [f"00{i}" for i in range(5)]
    gw = FakeGateway({s: 100 for s in syms})
    positions = [{"symbol": s, "market": "KR", "qty": 1, "avg_price": 200,
                  "stop_price": 150, "target_price": None} for s in syms]
    loop = WatchLoop(gw, store, lambda: {"KR": {"positions": positions, "candidates": []}},
                     config=WatchConfig(precision_per_tick=2))
    res = loop.run_once()
    assert len(res.precision) == 2
    assert len(gw.candle_calls) == 2


def test_volatility_trigger_over_ticks(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"AAPL": 100})
    loop = WatchLoop(gw, store, lambda: {"KR": {"positions": [], "candidates": ["AAPL"]}},
                     config=WatchConfig(vol_spike_pct=0.03, vol_window=5))
    loop.run_once()                               # hist=[100]
    gw.prices["AAPL"] = 104                        # +4%
    res = loop.run_once()                          # hist=[100,104] -> spike
    assert any(t.kind == "vol_spike" for t in res.triggers)


def test_strategy_runner_invoked_on_held_position(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})           # 손절/익절 미터치
    pos = {"symbol": "005930", "market": "KR", "qty": 3, "avg_price": 70000,
           "stop_price": 1, "target_price": 999999}
    calls = []

    class SR:
        def evaluate(self, p, m, price=None):
            calls.append((p["symbol"], m))
            return {"action": "hold", "executed": False}

    loop = WatchLoop(gw, store, lambda: {"KR": {"positions": [pos], "candidates": []}},
                     strategy_runner=SR())
    res = loop.run_once()
    assert calls == [("005930", "KR")]            # 보유분에 전략 실행기 호출됨
    assert res.exits == []


def test_entry_executor_invoked_on_armed(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})
    armed_pos = {"symbol": "005930", "market": "KR", "strategy": "rsi_reversion",
                 "qty": 0, "avg_price": 0, "meta": "{}"}
    calls = []

    class EE:
        def evaluate(self, a, m, price=None):
            calls.append((a["symbol"], m))
            return {"action": "buy", "executed": True}

    loop = WatchLoop(
        gw, store,
        lambda: {"KR": {"positions": [], "candidates": [], "armed": [armed_pos]}},
        entry_executor=EE())
    res = loop.run_once()
    assert calls == [("005930", "KR")]                # armed 에 진입 실행기 호출됨
    assert res.entries == ["005930"]
    assert res.polled == 1                             # armed 도 감시층 배치에 포함


def test_entry_capped_per_tick(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    syms = [f"00{i}" for i in range(5)]
    gw = FakeGateway({s: 100 for s in syms})
    armed = [{"symbol": s, "market": "KR", "strategy": "rsi_reversion",
              "qty": 0, "avg_price": 0, "meta": "{}"} for s in syms]
    calls = []

    class EE:
        def evaluate(self, a, m, price=None):
            calls.append(a["symbol"])
            return {"action": "hold", "executed": False}

    loop = WatchLoop(gw, store,
                     lambda: {"KR": {"positions": [], "candidates": [], "armed": armed}},
                     entry_executor=EE(), config=WatchConfig(entry_per_tick=2))
    loop.run_once()
    assert len(calls) == 2                             # 틱당 진입 판단 상한


def test_regime_flip_wakes_brain_once(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})            # 손절/익절 미터치
    pos = {"symbol": "005930", "market": "KR", "qty": 10, "avg_price": 70000,
           "stop_price": 1, "target_price": 999999, "meta": '{"entry_regime": "risk_on"}'}
    wl = {"KR": {"positions": [pos], "candidates": [], "armed": []},
          "_regime": {"KR": "risk_off"}}           # 진입 risk_on -> 현재 risk_off = 반전
    woke = []
    loop = WatchLoop(gw, store, lambda: wl,
                     on_wake=lambda why, trigs: woke.append(trigs))
    res = loop.run_once()
    assert any(t.kind == "regime_flip" for t in res.triggers)
    assert res.woke and len(woke) == 1             # 국면 반전 -> 뇌 각성
    res2 = loop.run_once()                          # 같은 국면 유지 -> dedup
    assert not any(t.kind == "regime_flip" for t in res2.triggers)
    assert len(woke) == 1


def test_build_watchlist_uses_passed_universe(tmp_path):
    """universe 파라미터로 넘긴 유니버스가 cfg.universe 대신 후보로 쓰인다(런타임 재독분)."""
    from src.config import load_config
    from src.engine.loop import build_watchlist
    store = Store(tmp_path / "t.db")
    wl = build_watchlist(load_config(), store, market_state_path=None,
                         universe={"KR": [{"symbol": "RUNTIME1"}, {"symbol": "RUNTIME2"}]})
    assert set(wl["KR"]["candidates"]) == {"RUNTIME1", "RUNTIME2"}


def test_build_watchlist_keeps_held_even_if_dropped_from_universe(tmp_path):
    """유니버스에서 빠진 심볼이라도 store 의 보유/armed 는 watchlist 에 계속 포함(회귀)."""
    from src.config import load_config
    from src.engine.loop import build_watchlist
    store = Store(tmp_path / "t.db")
    # 보유(open) + 진입대기(armed) 를 만들되, 넘기는 유니버스에는 둘 다 없음.
    store.open_position("HELD", "KR", qty=1, avg_price=100, strategy="s", thesis="t")
    store.arm_candidate("ARMED", "KR", strategy="rsi_reversion")
    wl = build_watchlist(load_config(), store, market_state_path=None,
                         universe={"KR": [{"symbol": "OTHER"}]})   # HELD/ARMED 빠짐
    assert [p["symbol"] for p in wl["KR"]["positions"]] == ["HELD"]   # 보유 유지
    assert [a["symbol"] for a in wl["KR"]["armed"]] == ["ARMED"]      # armed 유지
    assert wl["KR"]["candidates"] == ["OTHER"]                        # 후보는 새 유니버스


def test_build_watchlist_attaches_regime(tmp_path):
    import json as _j
    from src.config import load_config
    from src.engine.loop import build_watchlist
    store = Store(tmp_path / "t.db")
    msf = tmp_path / "ms.json"
    msf.write_text(_j.dumps({"regime": {"KR": {"label": "risk_off"},
                                        "US": {"label": "risk_on"}}}), encoding="utf-8")
    wl = build_watchlist(load_config(), store, market_state_path=msf)
    assert wl["_regime"] == {"KR": "risk_off", "US": "risk_on"}
    assert "KR" in wl and "_regime" not in ("KR", "US")   # 예약 키, 시장과 분리


def test_periodic_brain_wake(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})
    woke = []
    clock = [1000.0]
    loop = WatchLoop(gw, store, lambda: {"KR": {"positions": [], "candidates": ["005930"]}},
                     on_wake=lambda why, trigs: woke.append(why),
                     config=WatchConfig(brain_interval_sec=10),
                     now_fn=lambda: clock[0])
    loop.run_once()                          # t=1000, last=0 -> 즉시 1회(개장 첫 틱)
    assert woke == ["periodic"]
    clock[0] = 1005.0
    loop.run_once()                          # 5s < 10 -> 각성 없음
    assert woke == ["periodic"]
    clock[0] = 1011.0
    loop.run_once()                          # 11s >= 10 -> 재각성
    assert woke == ["periodic", "periodic"]


def test_no_periodic_wake_when_disabled(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})
    woke = []
    loop = WatchLoop(gw, store, lambda: {"KR": {"positions": [], "candidates": ["005930"]}},
                     on_wake=lambda why, trigs: woke.append(why),
                     config=WatchConfig(brain_interval_sec=0))   # 0 = 비활성
    loop.run_once(); loop.run_once()
    assert woke == []                        # 트리거 없으면 각성 없음


def test_live_price_passed_to_executors(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})
    pos = {"symbol": "005930", "market": "KR", "qty": 1, "avg_price": 70000,
           "stop_price": 1, "target_price": 999999}
    seen = []

    class SR:
        def evaluate(self, p, m, price=None):
            seen.append(price)
            return {"action": "hold", "executed": False}

    loop = WatchLoop(gw, store, lambda: {"KR": {"positions": [pos], "candidates": []}},
                     strategy_runner=SR())
    loop.run_once()
    assert seen == [70000]                    # 실시간가가 전략 실행기로 전달됨


def test_run_forever_respects_max_ticks_and_sleep(tmp_path, monkeypatch):
    _all_closed(monkeypatch)
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({})
    sleeps = []
    loop = WatchLoop(gw, store, lambda: {}, config=WatchConfig(idle_interval_sec=60),
                     sleep_fn=lambda s: sleeps.append(s))
    ticks = loop.run_forever(max_ticks=3)
    assert ticks == 3
    assert sleeps == [60, 60, 60]                  # 휴장 -> idle 간격
    kinds = {r["kind"] for r in store.conn.execute("SELECT kind FROM events").fetchall()}
    assert {"loop_start", "loop_stop"} <= kinds


# ── 종가 강제청산 (데이트레 오버나잇 금지) ─────────────────────────
def _near_end(monkeypatch, value=True):
    monkeypatch.setattr(loopmod, "near_session_end",
                        lambda market, minutes=5, now=None, **kw: value)


def test_session_end_flattens_day_position(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    _near_end(monkeypatch)
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000, "000660": 50000})
    day_pos = {"symbol": "005930", "market": "KR", "qty": 5, "avg_price": 69000,
               "stop_price": 1, "target_price": 999999, "meta": '{"horizon": "day"}'}
    swing_pos = {"symbol": "000660", "market": "KR", "qty": 3, "avg_price": 49000,
                 "stop_price": 1, "target_price": 999999, "meta": '{"horizon": "swing"}'}
    exits = []
    def execu(sym, market, price, trig):
        exits.append((sym, trig.kind, price))
        return True
    loop = WatchLoop(gw, store,
                     lambda: {"KR": {"positions": [day_pos, swing_pos], "candidates": []}},
                     executor=execu, config=WatchConfig(session_end_exit_min=5))
    res = loop.run_once()
    assert exits == [("005930", "session_end", 70000)]     # day 만 청산
    assert res.exits == ["005930"]                          # swing 은 유지
    kinds = {r["kind"] for r in store.conn.execute("SELECT kind FROM events").fetchall()}
    assert "trigger" in kinds


def test_session_end_disarms_day_armed(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    _near_end(monkeypatch)
    store = Store(tmp_path / "t.db")
    aid = store.arm_candidate("005930", "KR", strategy="rsi_reversion",
                              meta={"horizon": "day"})
    gw = FakeGateway({"005930": 70000})
    calls = []

    class EE:
        def evaluate(self, a, m, price=None):
            calls.append(a["symbol"])
            return {"action": "buy", "executed": True}

    armed_row = dict(store.get_armed()[0])
    loop = WatchLoop(gw, store,
                     lambda: {"KR": {"positions": [], "candidates": [], "armed": [armed_row]}},
                     entry_executor=EE(), config=WatchConfig(session_end_exit_min=5))
    loop.run_once()
    assert calls == []                                      # 마감 임박 → 신규 진입 안 함
    assert store.get_armed() == []                          # armed 해제(closed)
    kinds = {r["kind"] for r in store.conn.execute("SELECT kind FROM events").fetchall()}
    assert "disarm" in kinds


def test_session_end_keeps_swing_armed(tmp_path, monkeypatch):
    """회귀: 종가 강제청산은 day 만 대상 — horizon=swing 진입대기(존 가드)는 유지된다."""
    _only_kr_open(monkeypatch)
    _near_end(monkeypatch)
    store = Store(tmp_path / "t.db")
    store.arm_candidate("005930", "KR", strategy="rsi_reversion",
                        meta={"horizon": "swing",
                              "entry_zone": {"low": 100, "high": 110, "invalidation": 90}})
    gw = FakeGateway({"005930": 70000})
    calls = []

    class EE:
        def evaluate(self, a, m, price=None):
            calls.append(a["symbol"])
            return {"action": "hold", "executed": False}

    armed_row = dict(store.get_armed()[0])
    loop = WatchLoop(gw, store,
                     lambda: {"KR": {"positions": [], "candidates": [], "armed": [armed_row]}},
                     entry_executor=EE(), config=WatchConfig(session_end_exit_min=5))
    loop.run_once()
    assert store.get_armed()                                 # swing armed 는 해제 안 됨
    assert calls == ["005930"]                               # 마감창에도 진입 실행기 평가는 계속
    kinds = {r["kind"] for r in store.conn.execute("SELECT kind FROM events").fetchall()}
    assert "disarm" not in kinds                             # disarm 이벤트 없음


def test_session_end_inactive_when_disabled(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    _near_end(monkeypatch)                                  # 마감 임박이어도
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})
    day_pos = {"symbol": "005930", "market": "KR", "qty": 5, "avg_price": 69000,
               "stop_price": 1, "target_price": 999999, "meta": '{"horizon": "day"}'}
    exits = []
    loop = WatchLoop(gw, store, lambda: {"KR": {"positions": [day_pos], "candidates": []}},
                     executor=lambda *a: exits.append(a) or True,
                     config=WatchConfig(session_end_exit_min=0))   # 0 = 끔
    loop.run_once()
    assert exits == []


# ── per_tick 상한 라운드로빈 (기아 방지) ───────────────────────────
def test_strategy_rotation_covers_all_positions(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    syms = [f"SYM{i}" for i in range(5)]
    gw = FakeGateway({s: 100 for s in syms})
    positions = [{"symbol": s, "market": "KR", "qty": 1, "avg_price": 100,
                  "stop_price": 1, "target_price": 999999} for s in syms]
    calls = []

    class SR:
        def evaluate(self, p, m, price=None):
            calls.append(p["symbol"])
            return {"action": "hold", "executed": False}

    loop = WatchLoop(gw, store, lambda: {"KR": {"positions": positions, "candidates": []}},
                     strategy_runner=SR(), config=WatchConfig(strategy_per_tick=2))
    for _ in range(5):
        loop.run_once()
    assert set(calls) == set(syms)              # 상한(2) 밑이어도 전 종목이 차례로 평가됨


def test_entry_rotation_covers_all_armed(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    syms = [f"SYM{i}" for i in range(5)]
    gw = FakeGateway({s: 100 for s in syms})
    armed = [{"symbol": s, "market": "KR", "strategy": "rsi_reversion",
              "qty": 0, "avg_price": 0, "meta": "{}"} for s in syms]
    calls = []

    class EE:
        def evaluate(self, a, m, price=None):
            calls.append(a["symbol"])
            return {"action": "hold", "executed": False}

    loop = WatchLoop(gw, store,
                     lambda: {"KR": {"positions": [], "candidates": [], "armed": armed}},
                     entry_executor=EE(), config=WatchConfig(entry_per_tick=2))
    for _ in range(5):
        loop.run_once()
    assert set(calls) == set(syms)


# ── 실시간가 콜백 (계좌 마크 → 미실현 손익 게이트 연동) ─────────────
def test_on_prices_callback_receives_live_prices(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})
    seen = []
    loop = WatchLoop(gw, store, lambda: {"KR": {"positions": [], "candidates": ["005930"]}},
                     on_prices=lambda px: seen.append(dict(px)))
    loop.run_once()
    assert seen == [{"005930": 70000}]

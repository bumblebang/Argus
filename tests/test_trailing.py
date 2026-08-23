"""트레일링 스톱 — 목표가 도달 시 전량 청산 대신 이익 태우기(국면 연동, LLM 0콜) 검증.

설계: 목표가는 더는 청산가가 아니라 '트레일 활성화 지점'이다. 넘으면 손절가를 최고가 대비
trail_pct 아래로 끌어올리고(래칫, 절대 안 내림), 되돌리면 기존 stop_hit → ExitExecutor
경로가 그대로 전량 청산한다. test_engine_loop / test_engine_triggers 스타일(가짜 store·주입,
네트워크 0)을 따른다.
"""
import json

import pytest

import src.engine.loop as loopmod
from src.engine.loop import WatchLoop, WatchConfig
from src.engine.store import Store
from src.engine import triggers as T


# ── 공통 헬퍼 ──────────────────────────────────────────────────
def _sessions(monkeypatch, smap):
    monkeypatch.setattr(loopmod, "current_session",
                        lambda m, now=None: smap.get(m, "closed"))
    monkeypatch.setattr(loopmod, "is_tradable",
                        lambda m, allowed=None, now=None:
                        smap.get(m, "closed") in (allowed if allowed is not None else ("regular",)))


def _only_kr_open(monkeypatch):
    _sessions(monkeypatch, {"KR": "regular"})


def _trail_config() -> WatchConfig:
    """트레일링 활성 설정(risk_off 타이트·neutral 기본·risk_on 루즈, swing/position 대상)."""
    return WatchConfig(trailing={
        "enabled": True, "base_pct": 0.05,
        "regime_mult": {"risk_off": 0.6, "neutral": 1.0, "risk_on": 1.6},
        "horizons": ("swing", "position")})


class FakeGateway:
    """poll_prices/candles 만 흉내(test_engine_loop 와 동일)."""
    def __init__(self, prices: dict, candles=None):
        self.prices = prices
        self._candles = candles if candles is not None else [{"close": 1}]
        self.candle_calls = []

    def poll_prices(self, symbols, record=True):
        return [{"symbol": s, "price": self.prices.get(s), "payload": {}} for s in symbols]

    def candles(self, symbol, interval="1m", count=20):
        self.candle_calls.append(symbol)
        return self._candles


def _wl_from_store(store, regime=None):
    """매 틱 store 에서 보유를 재독분하는 watchlist_fn(프로덕션 build_watchlist 미러).

    트레일 상태가 디스크에 영속되면 다음 틱 재독분에 반영된다 — 재시작 지속성을 흉내낸다.
    """
    def fn():
        positions = [dict(r) for r in store.get_open_positions()]
        return {"KR": {"positions": positions, "candidates": [], "armed": []},
                "_regime": regime or {}}
    return fn


def _recorder():
    """청산 실행기 스텁 — 호출된 트리거 종류를 기록(진짜 ExitExecutor 는 별도 테스트)."""
    calls = []

    def execu(sym, market, price, trig):
        calls.append((sym, market, trig.kind, price))
        return True
    return execu, calls


def _event_kinds(store) -> set:
    return {r["kind"] for r in store.conn.execute("SELECT kind FROM events").fetchall()}


# ── 1) 순수함수: suppress_target ────────────────────────────────
def test_suppress_target_omits_target_hit_keeps_stop_hit():
    pos = {"symbol": "X", "stop_price": 90, "target_price": 110}
    # 억제 시 target_hit 미emit
    assert [t.kind for t in T.position_triggers(pos, 111, suppress_target=True)] == []
    # stop_hit 은 억제와 무관하게 정상
    assert [t.kind for t in T.position_triggers(pos, 89, suppress_target=True)] == ["stop_hit"]
    # 기본(억제 안 함): target_hit 정상(하위호환)
    assert [t.kind for t in T.position_triggers(pos, 111)] == ["target_hit"]


# ── 2) 목표가 도달 → 활성화(청산 아님) ──────────────────────────
def test_target_cross_activates_trail_no_exit(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    store.open_position("X", "KR", qty=1, avg_price=100, strategy="s", thesis="t",
                        target_price=110, stop_price=90, meta={"horizon": "swing"})
    gw = FakeGateway({"X": 111})
    execu, calls = _recorder()
    loop = WatchLoop(gw, store, _wl_from_store(store, {"KR": "neutral"}),
                     executor=execu, config=_trail_config())
    res = loop.run_once()

    assert calls == [] and res.exits == []             # 목표가에서 청산되지 않음
    row = store.get_open_positions()[0]
    meta = json.loads(row["meta"])
    assert meta["trail_active"] is True
    assert meta["trail_peak"] == 111
    assert row["stop_price"] == pytest.approx(105.45)  # 111×0.95, 90 에서 상향
    assert "trail_activated" in _event_kinds(store)


# ── 3) 상승 → peak·손절가 래칫업, 하락해도 안 내려감 ──────────────
def test_peak_and_stop_ratchet_up_then_hold(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    store.open_position("X", "KR", qty=1, avg_price=100, strategy="s", thesis="t",
                        target_price=110, stop_price=90, meta={"horizon": "swing"})
    gw = FakeGateway({"X": 111})
    loop = WatchLoop(gw, store, _wl_from_store(store, {"KR": "neutral"}),
                     config=_trail_config())
    loop.run_once()                                    # 활성화: peak 111, stop 105.45
    gw.prices["X"] = 120
    loop.run_once()                                    # 신고가: peak 120, stop 114
    row = store.get_open_positions()[0]
    assert json.loads(row["meta"])["trail_peak"] == 120
    assert row["stop_price"] == pytest.approx(114.0)   # 120×0.95

    gw.prices["X"] = 115                               # 하락(peak 밑, stop 위)
    loop.run_once()
    row = store.get_open_positions()[0]
    assert json.loads(row["meta"])["trail_peak"] == 120   # peak 안 내려감(불변식)
    assert row["stop_price"] == pytest.approx(114.0)      # stop 안 내려감(래칫)


# ── 4) 트레일링 스톱 아래로 되돌림 → stop_hit 전량 청산(기존 경로) ──
def test_pullback_below_trailing_stop_exits_via_stop_hit(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    store.open_position("X", "KR", qty=1, avg_price=100, strategy="s", thesis="t",
                        target_price=110, stop_price=90, meta={"horizon": "swing"})
    gw = FakeGateway({"X": 111})
    execu, calls = _recorder()
    loop = WatchLoop(gw, store, _wl_from_store(store, {"KR": "neutral"}),
                     executor=execu, config=_trail_config())
    loop.run_once()                                    # 활성화 stop 105.45
    gw.prices["X"] = 120
    loop.run_once()                                    # stop 114 로 래칫업
    assert calls == []                                 # 아직 청산 없음

    gw.prices["X"] = 113                               # 트레일링 스톱(114) 아래로 되돌림
    res = loop.run_once()
    assert [c[2] for c in calls] == ["stop_hit"]       # 기존 stop_hit 경로가 집행
    assert res.exits == ["X"]


# ── 5) day 포지션은 트레일 대상 아님 → 기존대로 target_hit 전량 청산 ──
def test_day_position_full_exit_at_target(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    store.open_position("X", "KR", qty=1, avg_price=100, strategy="s", thesis="t",
                        target_price=110, stop_price=90, meta={"horizon": "day"})
    gw = FakeGateway({"X": 111})
    execu, calls = _recorder()
    loop = WatchLoop(gw, store, _wl_from_store(store, {"KR": "neutral"}),
                     executor=execu, config=_trail_config())
    res = loop.run_once()
    assert [c[2] for c in calls] == ["target_hit"]     # day 는 하드 목표 유지
    assert res.exits == ["X"]
    assert "trail_active" not in json.loads(store.get_open_positions()[0]["meta"])


# ── 6) target_price 없는 포지션 → 트레일 미개입(예외 없음) ───────
def test_no_target_price_no_trail_no_error(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    store.open_position("X", "KR", qty=1, avg_price=100, strategy="s", thesis="t",
                        target_price=None, stop_price=90, meta={"horizon": "swing"})
    gw = FakeGateway({"X": 111})
    execu, calls = _recorder()
    loop = WatchLoop(gw, store, _wl_from_store(store, {"KR": "neutral"}),
                     executor=execu, config=_trail_config())
    res = loop.run_once()                              # 예외 없이 완주
    assert calls == [] and res.exits == []
    assert "trail_active" not in json.loads(store.get_open_positions()[0]["meta"] or "{}")


# ── 7) 국면별 trail_pct 배수 반영 ───────────────────────────────
def test_trail_pct_reflects_regime_multiplier(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    cases = {"risk_off": (0.03, 194.0), "neutral": (0.05, 190.0), "risk_on": (0.08, 184.0)}
    for regime, (exp_pct, exp_stop) in cases.items():
        store = Store(tmp_path / f"{regime}.db")
        store.open_position("X", "KR", qty=1, avg_price=100, strategy="s", thesis="t",
                            target_price=200, stop_price=100, meta={"horizon": "swing"})
        gw = FakeGateway({"X": 200})
        loop = WatchLoop(gw, store, _wl_from_store(store, {"KR": regime}),
                         config=_trail_config())
        assert loop._trail_pct("KR", {"KR": regime}) == pytest.approx(exp_pct)
        loop.run_once()                                 # 활성화(price==target)
        assert store.get_open_positions()[0]["stop_price"] == pytest.approx(exp_stop)


def test_trail_pct_default_mult_when_label_missing():
    """국면 라벨이 없거나 매핑에 없으면 배수 1.0(base_pct 그대로)."""
    loop = WatchLoop.__new__(WatchLoop)                 # 순수 계산만 — 완전 초기화 불필요
    loop.cfg = _trail_config()
    assert loop._trail_pct("KR", {}) == pytest.approx(0.05)             # 라벨 없음
    assert loop._trail_pct("KR", {"KR": "unknown"}) == pytest.approx(0.05)  # 매핑에 없음


# ── 8) 하위호환: 비활성이면 목표가 전량 청산·트레일 미기록 ─────────
def test_backward_compat_disabled_full_exit_at_target(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    store.open_position("X", "KR", qty=1, avg_price=100, strategy="value", thesis="t",
                        target_price=110, stop_price=90, meta={"horizon": "position"})
    gw = FakeGateway({"X": 111})
    execu, calls = _recorder()
    loop = WatchLoop(gw, store, _wl_from_store(store),
                     executor=execu, config=WatchConfig())  # trailing 기본 {} = 비활성
    res = loop.run_once()
    assert [c[2] for c in calls] == ["target_hit"]     # 기존과 100% 동일: 전량 청산
    assert res.exits == ["X"]
    assert "trail_active" not in json.loads(store.get_open_positions()[0]["meta"])
    assert "trail_activated" not in _event_kinds(store)


def test_from_config_no_block_or_disabled_yields_empty():
    assert WatchConfig.from_config({"watch": {}}).trailing == {}           # 블록 없음
    assert WatchConfig.from_config({"trailing": {"enabled": False}}).trailing == {}


def test_from_config_parses_trailing_block():
    cfg = WatchConfig.from_config({"trailing": {
        "enabled": True, "base_pct": 0.05,
        "regime_mult": {"risk_off": 0.6, "neutral": 1.0, "risk_on": 1.6},
        "horizons": ["swing", "position"]}})
    assert cfg.trailing["enabled"] is True
    assert cfg.trailing["base_pct"] == 0.05
    assert cfg.trailing["horizons"] == ("swing", "position")
    assert cfg.trailing["regime_mult"]["risk_on"] == 1.6


# ── 9) 손절가 래칫: 계산값이 기존보다 낮으면 기존 유지(내리지 않음) ──
def test_stop_ratchet_never_lowers_below_existing(tmp_path, monkeypatch):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    # 기존 손절가 108 이 트레일 계산값(111×0.95=105.45)보다 높다 → 유지되어야 한다
    store.open_position("X", "KR", qty=1, avg_price=100, strategy="s", thesis="t",
                        target_price=110, stop_price=108, meta={"horizon": "swing"})
    gw = FakeGateway({"X": 111})
    loop = WatchLoop(gw, store, _wl_from_store(store, {"KR": "neutral"}),
                     config=_trail_config())
    loop.run_once()
    row = store.get_open_positions()[0]
    assert row["stop_price"] == 108                    # 내리지 않음
    meta = json.loads(row["meta"])
    assert meta["trail_active"] is True and meta["trail_peak"] == 111


# ── 10) _portfolio() 가 활성 포지션에만 trail_active 를 싣는다 ────
def test_portfolio_carries_trail_active_only_when_active(tmp_path):
    from tests.test_pipeline import _runner
    store = Store(tmp_path / "t.db")
    r = _runner(tmp_path, store)                        # dry: swing BUY 체결 → 보유 1건
    r.run()

    pf0 = r._portfolio()
    assert pf0["positions"]
    assert "trail_active" not in pf0["positions"][0]    # 비활성엔 안 실림

    row = store.get_open_positions()[0]
    meta = json.loads(row["meta"]) if row["meta"] else {}
    meta.update({"trail_active": True, "trail_peak": 999999})
    store.update_position(row["id"], meta=meta)

    pf1 = r._portfolio()
    assert pf1["positions"][0]["trail_active"] is True  # 활성엔 실림

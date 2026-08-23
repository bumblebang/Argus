"""시간외 거래(프리마켓·애프터마켓) 확장 — 세션 게이트/뇌 각성/데이트레 가드/스프레드 가드.

핵심 불변식:
- is_tradable 은 인자 없으면 정규장 전용(하위호환). 세션 캐시가 없으면 폴백으로 정규장만.
- WatchConfig.trading_sessions 설정이 없으면 루프는 기존대로 정규장에서만 거래한다.
- 주기(정기) 뇌 각성은 brain_sessions 에 있는 세션에서만, 트리거(이벤트) 각성은 시간외에도 발화한다.
- 데이트레(day) 진입은 프리·애프터에서도 평가. 오버나잇은 오늘 마지막 허용 세션 종료 N분 전이 막는다.
- 시간외 + 넓은 스프레드면 라이브 주문 스킵. 정규장/호가 결측은 가드 미적용.

네트워크 0 — 가짜 게이트웨이/클라이언트 주입 + 세션 판정 monkeypatch.
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.market_hours as mh
import src.engine.loop as loopmod
from src.broker import Broker
from src.engine.loop import WatchLoop, WatchConfig
from src.engine.store import Store
from src.paper_account import PaperAccount
from src.risk_gate import RiskGate, Order

KST = ZoneInfo("Asia/Seoul")


def _kst(*args) -> float:
    return datetime(*args, tzinfo=KST).timestamp()


# ── 1) is_tradable 판정 ────────────────────────────────────────────────
def _write_cache(path, day=(2026, 7, 14)):
    """당일 취득된 KR 세션표(프리/정규/애프터) 캐시를 심는다."""
    y, m, d = day
    cache = {"KR": {"market": "KR", "date": f"{y:04d}-{m:02d}-{d:02d}",
                    "fetched": _kst(y, m, d, 7), "sessions": [
                        {"name": "premarket", "start": _kst(y, m, d, 8),
                         "end": _kst(y, m, d, 9)},
                        {"name": "regular", "start": _kst(y, m, d, 9),
                         "end": _kst(y, m, d, 15, 30)},
                        {"name": "aftermarket", "start": _kst(y, m, d, 15, 30),
                         "end": _kst(y, m, d, 20)},
                    ]}}
    path.write_text(json.dumps(cache), encoding="utf-8")


def test_is_tradable_기본은_정규장만(tmp_path, monkeypatch):
    path = tmp_path / "market_sessions.json"
    _write_cache(path)
    monkeypatch.setattr(mh, "_SESSIONS_CACHE", path)
    assert mh.is_tradable("KR", now=_kst(2026, 7, 14, 12)) is True        # 정규장
    assert mh.is_tradable("KR", now=_kst(2026, 7, 14, 8, 30)) is False    # 프리마켓
    assert mh.is_tradable("KR", now=_kst(2026, 7, 14, 17)) is False       # 애프터마켓
    assert mh.is_tradable("KR", now=_kst(2026, 7, 14, 21)) is False       # closed


def test_is_tradable_허용세션_지정(tmp_path, monkeypatch):
    path = tmp_path / "market_sessions.json"
    _write_cache(path)
    monkeypatch.setattr(mh, "_SESSIONS_CACHE", path)
    allowed = ("regular", "premarket", "aftermarket")
    assert mh.is_tradable("KR", allowed, _kst(2026, 7, 14, 17)) is True   # 애프터마켓 허용
    assert mh.is_tradable("KR", allowed, _kst(2026, 7, 14, 8, 30)) is True
    assert mh.is_tradable("KR", allowed, _kst(2026, 7, 14, 12)) is True
    # 휴장(closed)은 프리·애프터를 열어도 항상 False
    assert mh.is_tradable("KR", allowed, _kst(2026, 7, 14, 21)) is False
    assert mh.is_tradable("KR", allowed, _kst(2026, 7, 14, 7, 30)) is False


def test_is_tradable_캐시없으면_정규장으로_축소_안전실패(tmp_path, monkeypatch):
    """세션 캐시가 없으면 current_session 이 is_open 폴백 → 시간외를 허용해도 안 열린다."""
    monkeypatch.setattr(mh, "_SESSIONS_CACHE", tmp_path / "nope.json")
    allowed = ("regular", "premarket", "aftermarket")
    # 2026-07-14(화) 11:00 = 정규장 → 폴백도 regular
    assert mh.is_tradable("KR", allowed, _kst(2026, 7, 14, 11)) is True
    # 17:00 은 원래 애프터마켓이지만 캐시가 없으니 closed 로 축소(예외 없음)
    assert mh.is_tradable("KR", allowed, _kst(2026, 7, 14, 17)) is False
    assert mh.is_tradable("KR", now=_kst(2026, 7, 14, 17)) is False


def test_near_session_end_follows_last_allowed_session(tmp_path, monkeypatch):
    """프리+애프터를 열면 종점은 20:00. allowed=None 은 정규 15:30."""
    path = tmp_path / "market_sessions.json"
    _write_cache(path)
    monkeypatch.setattr(mh, "_SESSIONS_CACHE", path)
    allowed = ("regular", "premarket", "aftermarket")
    at_1526 = datetime(2026, 7, 14, 15, 26, tzinfo=KST)
    at_1956 = datetime(2026, 7, 14, 19, 56, tzinfo=KST)
    assert mh.near_session_end("KR", 5, at_1526, allowed=allowed) is False
    assert mh.near_session_end("KR", 5, at_1956, allowed=allowed) is True
    assert mh.near_session_end("KR", 5, at_1526) is True
    assert mh.near_session_end("KR", 5, at_1956) is False


# ── 2) 루프 거래 게이트 ────────────────────────────────────────────────
class FakeGateway:
    """poll_prices/candles 만 흉내(test_engine_loop 와 동일 스타일)."""
    def __init__(self, prices: dict, candles=None):
        self.prices = prices
        self._candles = candles if candles is not None else [{"close": 1}]
        self.candle_calls = []

    def poll_prices(self, symbols, record=True):
        return [{"symbol": s, "price": self.prices.get(s), "payload": {}} for s in symbols]

    def candles(self, symbol, interval="1m", count=20):
        self.candle_calls.append(symbol)
        return self._candles


def _sessions(monkeypatch, smap):
    """시장별 현재 세션 주입(없는 시장=closed). is_tradable 은 실물을 그대로 쓴다."""
    fake = lambda m, now=None: smap.get(m, "closed")   # noqa: E731
    monkeypatch.setattr(mh, "current_session", fake)   # is_tradable 내부가 참조
    monkeypatch.setattr(loopmod, "current_session", fake)


_EXT = {"KR": ("regular", "premarket", "aftermarket")}   # 시간외 허용 설정


def test_watchconfig_기본값은_정규장_전용(tmp_path):
    assert WatchConfig().trading_sessions == {"KR": ("regular",), "US": ("regular",)}
    # 설정 파일에 trading_sessions 블록이 없으면(하위호환) 기본값 그대로.
    c = WatchConfig.from_config({"watch": {"watch_interval_sec": 1}})
    assert c.trading_sessions == {"KR": ("regular",), "US": ("regular",)}


def test_watchconfig_세션목록_파싱():
    c = WatchConfig.from_config({"trading_sessions": {
        "KR": ["regular", "premarket", "aftermarket"],
        "US": ["regular", "aftermarket"]}})
    assert c.trading_sessions["KR"] == ("regular", "premarket", "aftermarket")
    assert c.trading_sessions["US"] == ("regular", "aftermarket")
    assert "daymarket" not in c.trading_sessions["US"]


def test_설정없으면_애프터마켓에_거래안함_하위호환(tmp_path, monkeypatch):
    """완료 판정 2: trading_sessions 없는 설정이면 루프 게이트는 정규장 전용."""
    _sessions(monkeypatch, {"KR": "aftermarket"})
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})
    loop = WatchLoop(gw, store, lambda: {"KR": {"positions": [], "candidates": ["005930"]}},
                     markets=("KR",), config=WatchConfig.from_config({"watch": {}}))
    res = loop.run_once()
    assert res.markets_open == [] and res.polled == 0


def test_애프터마켓_허용시_시장을_열고_폴링(tmp_path, monkeypatch):
    _sessions(monkeypatch, {"KR": "aftermarket"})
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})
    loop = WatchLoop(gw, store, lambda: {"KR": {"positions": [], "candidates": ["005930"]}},
                     markets=("KR",), config=WatchConfig(trading_sessions=_EXT))
    res = loop.run_once()
    assert res.markets_open == ["KR"] and res.polled == 1


def test_주기각성은_정규장에서만_발화(tmp_path, monkeypatch):
    """정기 각성이 시간외까지 열리면 claude 세션 한도가 터진다 — 정규장 전용."""
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})
    woke = []
    clock = [1000.0]
    loop = WatchLoop(gw, store, lambda: {"KR": {"positions": [], "candidates": ["005930"]}},
                     markets=("KR",), on_wake=lambda why, trigs: woke.append(why),
                     config=WatchConfig(brain_interval_sec=10, trading_sessions=_EXT),
                     now_fn=lambda: clock[0])
    _sessions(monkeypatch, {"KR": "aftermarket"})
    res = loop.run_once()
    assert res.markets_open == ["KR"]       # 거래는 열렸는데
    assert woke == [] and res.woke is False  # 주기 각성은 안 함

    _sessions(monkeypatch, {"KR": "regular"})
    clock[0] = 1020.0
    res = loop.run_once()
    assert woke == ["periodic"] and res.woke is True


def test_트리거각성은_애프터마켓에도_발화(tmp_path, monkeypatch):
    """이벤트(급변) 각성은 세션 무관 — 실적·급변 대응이 시간외 확장의 목적."""
    _sessions(monkeypatch, {"KR": "aftermarket"})
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 100})
    woke = []
    loop = WatchLoop(gw, store, lambda: {"KR": {"positions": [], "candidates": ["005930"]}},
                     markets=("KR",), on_wake=lambda why, trigs: woke.append(why),
                     config=WatchConfig(vol_spike_pct=0.03, vol_window=5,
                                        brain_interval_sec=0, trading_sessions=_EXT))
    loop.run_once()                       # hist=[100]
    gw.prices["005930"] = 104             # +4% → vol_spike
    res = loop.run_once()
    assert res.woke is True and woke == ["wake_triggers"]
    assert any(t.kind == "vol_spike" for t in res.triggers)


class _EE:
    """진입 실행기 스텁 — 평가 호출만 기록."""
    def __init__(self):
        self.calls = []

    def evaluate(self, armed, market, price=None):
        self.calls.append(armed["symbol"])
        return {"action": "hold", "executed": False}


def _armed_row(store, horizon):
    store.arm_candidate("005930", "KR", strategy="rsi_reversion", meta={"horizon": horizon})
    return dict(store.get_armed()[0])


def test_데이트레_신규진입은_시간외에도_평가(tmp_path, monkeypatch):
    """day armed 도 프리·애프터에서 진입 평가. 오버나잇은 세션 종료 flatten 이 담당."""
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})
    row = _armed_row(store, "day")
    ee = _EE()
    loop = WatchLoop(gw, store,
                     lambda: {"KR": {"positions": [], "candidates": [], "armed": [row]}},
                     markets=("KR",), entry_executor=ee,
                     config=WatchConfig(trading_sessions=_EXT))
    _sessions(monkeypatch, {"KR": "aftermarket"})
    loop.run_once()
    assert ee.calls == ["005930"]
    assert store.get_armed()                    # 평가만, 스텁은 미체결이라 armed 유지

    _sessions(monkeypatch, {"KR": "premarket"})
    loop.run_once()
    assert ee.calls == ["005930", "005930"]


def test_스윙_진입은_시간외에도_평가(tmp_path, monkeypatch):
    """swing/position 진입은 시간외에도 정상 동작."""
    _sessions(monkeypatch, {"KR": "aftermarket"})
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})
    row = _armed_row(store, "swing")
    ee = _EE()
    loop = WatchLoop(gw, store,
                     lambda: {"KR": {"positions": [], "candidates": [], "armed": [row]}},
                     markets=("KR",), entry_executor=ee,
                     config=WatchConfig(trading_sessions=_EXT))
    loop.run_once()
    assert ee.calls == ["005930"]


def test_청산은_애프터마켓에도_정상동작(tmp_path, monkeypatch):
    """손절(코드 청산=SELL)과 보유 전략 평가는 시간외에도 살아 있어야 한다."""
    _sessions(monkeypatch, {"KR": "aftermarket"})
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 68000, "000660": 50000})
    stop_pos = {"symbol": "005930", "market": "KR", "qty": 10, "avg_price": 70000,
                "stop_price": 69000, "target_price": None}
    hold_pos = {"symbol": "000660", "market": "KR", "qty": 3, "avg_price": 49000,
                "stop_price": 1, "target_price": 999999}
    exits, managed = [], []

    def execu(sym, market, price, trig):
        exits.append((sym, trig.kind))
        return True

    class SR:
        def evaluate(self, pos, market, price=None):
            managed.append(pos["symbol"])
            return {"action": "hold", "executed": False}

    loop = WatchLoop(gw, store,
                     lambda: {"KR": {"positions": [stop_pos, hold_pos], "candidates": []}},
                     markets=("KR",), executor=execu, strategy_runner=SR(),
                     config=WatchConfig(trading_sessions=_EXT))
    res = loop.run_once()
    assert exits == [("005930", "stop_hit")] and res.exits == ["005930"]
    assert managed == ["000660"]                # 보유 관리(전략 청산 평가)도 계속


# ── 3) 브로커 시간외 스프레드 가드 ────────────────────────────────────
class _MockClient:
    """orderbook/place_order/get_order 스텁(호가북 None = 조회 실패)."""
    def __init__(self, orderbook=None, raise_orderbook=False):
        self._orderbook = orderbook
        self._raise = raise_orderbook
        self.calls = []

    def orderbook(self, symbol):
        if self._raise:
            raise RuntimeError("호가 조회 실패")
        return self._orderbook

    def place_order(self, **kw):
        self.calls.append(kw)
        return {"orderId": "O1"}

    def get_order(self, account_seq, order_id):
        return {"status": "FILLED",
                "execution": {"filledQuantity": "1",
                              "averageFilledPrice": str(self.calls[-1]["price"]),
                              "commission": "0", "tax": "0"}}

    def get_sellable(self, account_seq, symbol):
        return None


def _live_broker(tmp_path, client, store=None, max_spread=0.02):
    acct = PaperAccount(cash={"KR": 1_000_000, "US": 0}, state_path=tmp_path / "pa.json")
    gate = RiskGate({"capital": {"KR": 1_000_000}, "max_position_pct": 0.5,
                     "max_positions": 5, "daily_loss_limit_pct": 0.05,
                     "max_order_notional": {"KR": 500_000},
                     "kill_switch_file": str(tmp_path / "HALT")})
    return Broker(account=acct, gate=gate, client=client, mode="live", account_seq=1,
                  live_markets=["KR"], store=store, max_spread_pct_extended=max_spread,
                  reconcile_poll_attempts=1, reconcile_poll_sec=0.0)


def _book(bid, ask):
    return {"asks": [{"price": str(ask), "volume": "100"}],
            "bids": [{"price": str(bid), "volume": "100"}]}


def _session(monkeypatch, name):
    import src.broker as bmod
    monkeypatch.setattr(bmod, "current_session", lambda m, now=None: name)


def test_시간외_넓은_스프레드_주문스킵(tmp_path, monkeypatch):
    _session(monkeypatch, "aftermarket")
    store = Store(tmp_path / "t.db")
    client = _MockClient(orderbook=_book(69000, 71000))     # 스프레드 ≈ 2.86% > 2%
    b = _live_broker(tmp_path, client, store=store)
    ok = b.execute(Order("005930", "KR", "BUY", 1, 70000.0), "test")
    assert not ok
    assert client.calls == []                                # 실주문 미발사
    assert b.account.position("005930").qty == 0             # 원장 무변
    evs = store.recent_events("wide_spread_skip", 0)
    assert len(evs) == 1
    p = json.loads(evs[0]["payload"])
    assert p["session"] == "aftermarket" and p["bid"] == 69000 and p["ask"] == 71000
    assert 0.028 < p["spread_pct"] < 0.029


def test_시간외_좁은_스프레드_통과(tmp_path, monkeypatch):
    _session(monkeypatch, "aftermarket")
    store = Store(tmp_path / "t.db")
    client = _MockClient(orderbook=_book(69900, 70000))      # 스프레드 ≈ 0.14%
    b = _live_broker(tmp_path, client, store=store)
    ok = b.execute(Order("005930", "KR", "BUY", 1, 70000.0), "test")
    assert ok
    assert client.calls and client.calls[0]["price"] == 70000.0
    assert store.recent_events("wide_spread_skip", 0) == []


def test_정규장은_넓은_스프레드여도_통과(tmp_path, monkeypatch):
    """가드는 시간외 전용 — 정규장에서는 절대 발동하지 않는다."""
    _session(monkeypatch, "regular")
    store = Store(tmp_path / "t.db")
    client = _MockClient(orderbook=_book(69000, 71000))      # 시간외였다면 스킵될 폭
    b = _live_broker(tmp_path, client, store=store)
    ok = b.execute(Order("005930", "KR", "BUY", 1, 70000.0), "test")
    assert ok
    assert store.recent_events("wide_spread_skip", 0) == []


def test_호가북_결측이면_가드_미적용_통과(tmp_path, monkeypatch):
    """스프레드를 계산할 수 없으면 통과(가드 오작동으로 정상 주문을 막는 게 더 나쁘다)."""
    _session(monkeypatch, "aftermarket")
    store = Store(tmp_path / "t.db")
    # (a) 조회 예외
    b = _live_broker(tmp_path, _MockClient(raise_orderbook=True), store=store)
    assert b.execute(Order("005930", "KR", "BUY", 1, 70000.0), "test")
    # (b) 한쪽 호가만 존재(bids 없음) → 스프레드 계산 불가
    sub = tmp_path / "b2"
    sub.mkdir()
    client = _MockClient(orderbook={"asks": [{"price": "70000", "volume": "10"}], "bids": []})
    b2 = _live_broker(sub, client, store=store)
    assert b2.execute(Order("005930", "KR", "BUY", 1, 70000.0), "test")
    assert store.recent_events("wide_spread_skip", 0) == []

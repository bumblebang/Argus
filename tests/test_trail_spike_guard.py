"""트레일 래칫 이상 틱 가드 — 단일 스파이크 틱이 트레일을 켜거나 손절가를 올리지 못한다.

사고(2026-10-01 06:09:16 KST, US 애프터): PLTR lastPrice 186.49 → 223.5936(1체결, ~6초간
매 폴링 같은 값) → 186.38. 그 한 틱으로 트레일이 켜져 손절가가 212.41(현재가 위)로 영속,
다음 틱부터 trail_stop 매도를 매 틱 시도했다.

설계: 래칫(활성화·peak 갱신)은 confirm_sec 창 최저가 + 서로 다른 체결 confirm_prints 건 이상일
때만. 손절 하향 돌파는 루프에서 지연시키지 않고 게이트웨이 PriceGuard(호가 교차확인)에 맡긴다.
"""
import json

import pytest

from src.engine.gateway import TossGateway
from src.engine.loop import WatchConfig, WatchLoop
from src.engine.price_guard import PriceGuard
from src.engine.store import Store
from tests.test_trailing import _event_kinds, _only_kr_open, _recorder, _wl_from_store


def _cfg(confirm_sec: float = 60.0, confirm_prints: int = 2) -> WatchConfig:
    return WatchConfig(trailing={
        "enabled": True, "base_pct": 0.05, "regime_mult": {},
        "horizons": ("swing", "position"),
        "confirm_sec": confirm_sec, "confirm_prints": confirm_prints})


class _Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class _TsGateway:
    """lastPrice + 체결시각(payload.timestamp) 을 돌려주는 가짜 게이트웨이."""
    def __init__(self):
        self.price: float | None = None
        self.ts: str | None = None

    def set(self, price: float, ts: str) -> None:
        self.price, self.ts = price, ts

    def poll_prices(self, symbols, record=True):
        return [{"symbol": s, "price": self.price,
                 "payload": {"lastPrice": str(self.price), "timestamp": self.ts}}
                for s in symbols]

    def candles(self, symbol, interval="1m", count=20):
        return [{"close": 1}]


def _setup(tmp_path, monkeypatch, *, target, stop, cfg=None, meta=None):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    store.open_position("X", "KR", qty=1, avg_price=100, strategy="s", thesis="t",
                        target_price=target, stop_price=stop,
                        meta=meta or {"horizon": "swing"})
    gw = _TsGateway()
    clock = _Clock()
    execu, calls = _recorder()
    loop = WatchLoop(gw, store, _wl_from_store(store, {"KR": "neutral"}),
                     executor=execu, config=cfg or _cfg(), now_fn=clock)
    return store, gw, clock, loop, calls


def _tick(loop, gw, clock, price, ts, dt=1.5):
    gw.set(price, ts)
    loop.run_once()
    clock.t += dt


def _row(store):
    row = store.get_open_positions()[0]
    return row, json.loads(row["meta"] or "{}")


# ── 1) 사고 재현: 단일 스파이크 → 트레일 미활성·손절가 불변·매도 없음 ─────────
def test_pltr_single_spike_does_not_activate_trail(tmp_path, monkeypatch):
    store, gw, clock, loop, calls = _setup(tmp_path, monkeypatch,
                                           target=205.78, stop=177.72)
    for i in range(50):                                  # 75초간 186 대 정상 체결
        _tick(loop, gw, clock, 186.49 + (i % 3) * 0.01, f"t{i // 4}")
    for _ in range(5):                                   # 1체결 스파이크가 ~6초간 반복 노출
        _tick(loop, gw, clock, 223.5936, "spike")
    for i in range(30):                                  # 정상 복귀
        _tick(loop, gw, clock, 186.38, f"after{i // 4}")

    row, meta = _row(store)
    assert "trail_active" not in meta and "trail_peak" not in meta
    assert row["stop_price"] == pytest.approx(177.72)    # 손절가 불변
    assert calls == []                                   # trail_stop/stop_hit 매도 없음
    assert "trail_activated" not in _event_kinds(store)


# ── 2) 지속 상승 → 정상 활성(확인가 기준, 손절가는 현재가 아래) ─────────────
def test_sustained_rise_activates_trail_with_confirmed_peak(tmp_path, monkeypatch):
    store, gw, clock, loop, calls = _setup(tmp_path, monkeypatch, target=110, stop=90)
    for i in range(10):
        _tick(loop, gw, clock, 105, f"a{i}", dt=10)      # 목표가 아래 100초
    for i in range(4):
        _tick(loop, gw, clock, 112 + i, f"b{i}", dt=15)  # 112→115, 45초 경과: 창에 105 남음
    _, meta = _row(store)
    assert "trail_active" not in meta                    # 아직 60초 지속 안 됨

    for i in range(3):
        _tick(loop, gw, clock, 115, f"c{i}", dt=15)      # 목표가 위 60초+ 지속
    row, meta = _row(store)
    assert meta["trail_active"] is True
    assert 110 <= meta["trail_peak"] <= 115              # 창 최저가(스파이크 아님)
    assert row["stop_price"] == pytest.approx(meta["trail_peak"] * 0.95)
    assert row["stop_price"] < 115                       # 불변식: 손절가 < 현재가
    assert calls == []
    ev = store.conn.execute(
        "SELECT payload FROM events WHERE kind='trail_activated'").fetchone()
    assert json.loads(ev["payload"])["last"] == 115      # 원시가도 기록


# ── 3) 활성 중 단일 스파이크 → peak·손절가 불변, 지속 신고가 → 래칫업 ────────
def test_active_trail_ignores_spike_but_ratchets_on_sustained_high(tmp_path, monkeypatch):
    store, gw, clock, loop, calls = _setup(
        tmp_path, monkeypatch, target=110, stop=114,
        meta={"horizon": "swing", "trail_active": True, "trail_peak": 120})
    for i in range(50):
        _tick(loop, gw, clock, 119, f"a{i // 4}")
    _tick(loop, gw, clock, 140, "spike")                 # +17.6% 1체결
    _tick(loop, gw, clock, 140, "spike")
    _tick(loop, gw, clock, 119, "back")
    row, meta = _row(store)
    assert meta["trail_peak"] == 120 and row["stop_price"] == pytest.approx(114)

    for i in range(50):                                  # 125 로 75초 지속
        _tick(loop, gw, clock, 125, f"h{i // 4}")
    row, meta = _row(store)
    assert meta["trail_peak"] == 125
    assert row["stop_price"] == pytest.approx(125 * 0.95)
    assert calls == []


# ── 4) 한 체결에 고정된 오류가(같은 timestamp) → 오래 지속돼도 미활성 ──────────
def test_stuck_single_print_above_target_never_confirms(tmp_path, monkeypatch):
    store, gw, clock, loop, calls = _setup(tmp_path, monkeypatch, target=110, stop=90)
    _tick(loop, gw, clock, 100, "a", dt=10)
    for _ in range(40):                                  # 120 이 같은 체결로 10분 고정
        _tick(loop, gw, clock, 120, "stuck", dt=15)
    _, meta = _row(store)
    assert "trail_active" not in meta


# ── 5) 재시작 직후(창 미충족) → 첫 틱으로 활성화하지 않음 ──────────────────
def test_no_activation_until_window_covered(tmp_path, monkeypatch):
    store, gw, clock, loop, calls = _setup(tmp_path, monkeypatch, target=110, stop=90)
    _tick(loop, gw, clock, 111, "a", dt=30)
    _tick(loop, gw, clock, 111.5, "b", dt=30)
    _, meta = _row(store)
    assert "trail_active" not in meta                    # 30초 — 창 미충족
    _tick(loop, gw, clock, 112, "c")                     # 60초 — 창 충족
    _, meta = _row(store)
    assert meta["trail_active"] is True and meta["trail_peak"] == 111


# ── 6) 설정 파싱: 기본값 켜짐, 0 이면 구 동작 ────────────────────────────
def test_from_config_trail_confirm_defaults_and_override():
    cfg = WatchConfig.from_config({"trailing": {"enabled": True}})
    assert cfg.trailing["confirm_sec"] == 60.0 and cfg.trailing["confirm_prints"] == 2
    cfg = WatchConfig.from_config({"trailing": {"enabled": True, "confirm_sec": 0,
                                                "confirm_prints": "bad"}})
    assert cfg.trailing["confirm_sec"] == 0.0 and cfg.trailing["confirm_prints"] == 2


def test_confirm_disabled_keeps_legacy_immediate_activation(tmp_path, monkeypatch):
    store, gw, clock, loop, calls = _setup(tmp_path, monkeypatch, target=110, stop=90,
                                           cfg=_cfg(confirm_sec=0))
    _tick(loop, gw, clock, 111, "a")
    _, meta = _row(store)
    assert meta["trail_active"] is True and meta["trail_peak"] == 111


# ── 7) 손절 하향 돌파: 루프는 지연하지 않는다(손절 지연 < 오매도 방지는 PriceGuard 몫) ──
def test_loop_does_not_delay_stop_hit(tmp_path, monkeypatch):
    store, gw, clock, loop, calls = _setup(tmp_path, monkeypatch, target=110, stop=95)
    _tick(loop, gw, clock, 100, "a")
    _tick(loop, gw, clock, 94, "b")                      # 가드 통과가가 손절 아래 → 즉시
    assert [c[2] for c in calls] == ["stop_hit"]


class _BookClient:
    """/prices 시퀀스 + 고정 호가(교차확인용) 가짜 토스 클라이언트."""
    def __init__(self, book=None):
        self.price = None
        self.ts = None
        self.book = book

    def get_prices(self, symbols):
        return [{"symbol": s, "lastPrice": str(self.price), "timestamp": self.ts}
                for s in symbols]

    def _request(self, name, params=None, **kw):
        assert name == "orderbook"
        if self.book is None:
            raise RuntimeError("호가 없음")
        return self.book


def _guarded(tmp_path, monkeypatch, *, target, stop, book):
    _only_kr_open(monkeypatch)
    store = Store(tmp_path / "t.db")
    store.open_position("X", "KR", qty=1, avg_price=100, strategy="s", thesis="t",
                        target_price=target, stop_price=stop, meta={"horizon": "swing"})
    client = _BookClient(book)
    gw = TossGateway(client, store=store)
    gw.price_guard = PriceGuard(gw.best_quote)
    execu, calls = _recorder()
    loop = WatchLoop(gw, store, _wl_from_store(store, {"KR": "neutral"}),
                     executor=execu, config=_cfg())
    return store, client, loop, calls


def _book(bid, ask):
    return {"bids": [{"price": str(bid), "volume": "10"}],
            "asks": [{"price": str(ask), "volume": "10"}]}


def test_guarded_single_down_spike_does_not_stop_out(tmp_path, monkeypatch):
    """호가가 부정하는 단발 급락 틱 → 가드가 중간값으로 대체 → stop_hit 없음."""
    store, client, loop, calls = _guarded(tmp_path, monkeypatch, target=130, stop=95,
                                          book=_book(99.9, 100.1))
    client.price, client.ts = 100, "a"
    loop.run_once()
    client.price, client.ts = 80, "spike"                # -20% 오류 체결
    loop.run_once()
    client.price, client.ts = 100, "b"
    loop.run_once()
    assert calls == []


def test_guarded_real_drop_stops_out_immediately(tmp_path, monkeypatch):
    """호가도 함께 내려간 진짜 급락 → 확인 즉시 stop_hit(추가 지연 없음)."""
    store, client, loop, calls = _guarded(tmp_path, monkeypatch, target=130, stop=95,
                                          book=_book(79.9, 80.1))
    client.price, client.ts = 100, "a"
    loop.run_once()
    client.price, client.ts = 80, "drop"
    loop.run_once()
    assert [c[2] for c in calls] == ["stop_hit"]


def test_guarded_up_spike_without_quote_does_not_activate(tmp_path, monkeypatch):
    """호가를 못 구해도(가드 held) 단발 상승 틱은 트레일을 켜지 못한다 — 두 겹 방어."""
    store, client, loop, calls = _guarded(tmp_path, monkeypatch, target=110, stop=90,
                                          book=None)
    client.price, client.ts = 100, "a"
    loop.run_once()
    client.price, client.ts = 120, "spike"
    loop.run_once()
    loop.run_once()
    client.price, client.ts = 100, "b"
    loop.run_once()
    _, meta = _row(store)
    assert "trail_active" not in meta
    assert calls == []

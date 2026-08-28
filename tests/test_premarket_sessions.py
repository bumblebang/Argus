"""프리장 개방 · 시간외 유동성 필터 · 지정시각 각성.

프리장에 가격이 형성되고 정규장 시가가 그 가격을 이어받는 종목이 있다.
정규장에서만 깨면 이 구간을 통째로 놓친다. 반대로 프리장 체결이 없는 종목
(체결 timestamp 가 전일에 멈춤)은 신규진입 후보로 삼으면 안 된다.
"""
from src.engine.loop import WatchLoop, WatchConfig
from src.engine.store import Store

from tests.test_engine_loop import FakeGateway, _sessions, _all_closed, _iso, _kst_epoch


def _wl_kr(*symbols):
    return lambda: {"KR": {"positions": [], "candidates": list(symbols), "armed": []}}


# ── 1. brain_sessions: 정기각성이 도는 세션 ──────────────────────────────
def test_brain_sessions_opens_aftermarket_periodic_wake(tmp_path, monkeypatch):
    """brain_sessions 에 aftermarket 이 있으면 애프터에서도 정기각성이 발화한다."""
    _sessions(monkeypatch, {"KR": "aftermarket"})
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})
    cfg = WatchConfig(brain_interval_sec=1,
                      trading_sessions={"KR": ("regular", "premarket", "aftermarket")},
                      brain_sessions={"KR": ("premarket", "regular", "aftermarket")})
    woke = []
    loop = WatchLoop(gw, store, _wl_kr("005930"), markets=("KR",), config=cfg,
                     on_wake=lambda why, trigs: woke.append(why))
    res = loop.run_once()
    assert res.markets_open == ["KR"]
    assert woke == ["periodic"]


def test_brain_sessions_opens_premarket_periodic_wake(tmp_path, monkeypatch):
    """brain_sessions 에 premarket 이 있으면 프리장에서도 정기각성이 발화한다."""
    _sessions(monkeypatch, {"KR": "premarket"})
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})
    cfg = WatchConfig(brain_interval_sec=1,
                      trading_sessions={"KR": ("regular", "premarket")},
                      brain_sessions={"KR": ("premarket", "regular")})
    woke = []
    loop = WatchLoop(gw, store, _wl_kr("005930"), markets=("KR",), config=cfg,
                     on_wake=lambda why, trigs: woke.append(why))
    res = loop.run_once()
    assert res.markets_open == ["KR"]
    assert woke == ["periodic"]


def test_brain_sessions_default_keeps_premarket_asleep(tmp_path, monkeypatch):
    """기본값(정규장 전용)이면 프리장에 폴링·청산은 돌아도 정기각성은 안 뜬다(하위호환)."""
    _sessions(monkeypatch, {"KR": "premarket"})
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})
    cfg = WatchConfig(brain_interval_sec=1,
                      trading_sessions={"KR": ("regular", "premarket")})   # brain_sessions 기본
    woke = []
    loop = WatchLoop(gw, store, _wl_kr("005930"), markets=("KR",), config=cfg,
                     on_wake=lambda why, trigs: woke.append(why))
    res = loop.run_once()
    assert res.markets_open == ["KR"]      # 거래 게이트는 열려 있고
    assert woke == []                      # 정기각성만 잠긴다


# ── 2. stale_quote_sec: 시간외 체결정지 종목을 신규진입 후보에서 제외 ──────
def test_stale_quote_marks_illiquid_in_extended_session(tmp_path, monkeypatch):
    """시간외에서 체결이 멈춘 종목만 illiquid. 신선/키없음/파싱불가는 fail-open."""
    _sessions(monkeypatch, {"KR": "premarket"})
    store = Store(tmp_path / "t.db")
    now = 1_784_674_800.0                        # 2026-07-22 08:00 KST
    gw = FakeGateway({"005930": 70000, "005935": 60000, "036930": 50000, "080220": 40000},
                     payloads={"005930": {"timestamp": _iso(now - 5)},        # 방금 체결
                               "005935": {"timestamp": _iso(now - 50_000)},   # 전일에 멈춤
                               "036930": {},                                  # 키 없음
                               "080220": {"timestamp": "not-a-date"}})        # 파싱 불가
    cfg = WatchConfig(stale_quote_sec=600,
                      trading_sessions={"KR": ("regular", "premarket")})
    loop = WatchLoop(gw, store, _wl_kr("005930", "005935", "036930", "080220"),
                     markets=("KR",), config=cfg, now_fn=lambda: now)
    loop.run_once()
    assert loop.illiquid_snapshot() == {"005935"}


def test_stale_quote_not_applied_in_regular_session(tmp_path, monkeypatch):
    """정규장에는 적용 안 함 — 거래 뜸한 종목을 잘못 배제하지 않는다."""
    _sessions(monkeypatch, {"KR": "regular"})
    store = Store(tmp_path / "t.db")
    now = 1_784_678_400.0                        # 2026-07-22 09:00 KST
    gw = FakeGateway({"005935": 60000},
                     payloads={"005935": {"timestamp": _iso(now - 50_000)}})
    cfg = WatchConfig(stale_quote_sec=600)
    loop = WatchLoop(gw, store, _wl_kr("005935"), markets=("KR",), config=cfg,
                     now_fn=lambda: now)
    loop.run_once()
    assert loop.illiquid_snapshot() == set()


def test_stale_quote_disabled_by_default(tmp_path, monkeypatch):
    """stale_quote_sec=0(기본)이면 판정 자체를 안 한다(하위호환)."""
    _sessions(monkeypatch, {"KR": "premarket"})
    store = Store(tmp_path / "t.db")
    now = 1_784_674_800.0
    gw = FakeGateway({"005935": 60000},
                     payloads={"005935": {"timestamp": _iso(now - 50_000)}})
    cfg = WatchConfig(trading_sessions={"KR": ("regular", "premarket")})
    loop = WatchLoop(gw, store, _wl_kr("005935"), markets=("KR",), config=cfg,
                     now_fn=lambda: now)
    loop.run_once()
    assert loop.illiquid_snapshot() == set()


def test_illiquid_released_when_session_turns_regular(tmp_path, monkeypatch):
    """프리장에 막혔던 종목은 09:00 개장과 함께 자동 해제된다."""
    store = Store(tmp_path / "t.db")
    now = [1_784_674_800.0]
    gw = FakeGateway({"005935": 60000},
                     payloads={"005935": {"timestamp": _iso(now[0] - 50_000)}})
    cfg = WatchConfig(stale_quote_sec=600,
                      trading_sessions={"KR": ("regular", "premarket")})
    loop = WatchLoop(gw, store, _wl_kr("005935"), markets=("KR",), config=cfg,
                     now_fn=lambda: now[0])
    _sessions(monkeypatch, {"KR": "premarket"})
    loop.run_once()
    assert loop.illiquid_snapshot() == {"005935"}

    _sessions(monkeypatch, {"KR": "regular"})    # 개장
    now[0] += 3600
    loop.run_once()
    assert loop.illiquid_snapshot() == set()


# ── 3. 세션 경계 가격이력 리셋 (헛 vol_spike 각성 제거) ──────────────────
def test_session_change_resets_price_history(tmp_path, monkeypatch):
    """세션 경계에서 가격 이력을 비우지 않으면, 프리장에 안 돌던 종목의 시가 점프가
    vol_spike 각성으로 한꺼번에 터진다. 이력을 비우면 새 창이 찰 때까지 발화하지 않는다.
    """
    store = Store(tmp_path / "t.db")
    prices = {"005935": 100.0}
    gw = FakeGateway(prices)
    cfg = WatchConfig(vol_window=12, vol_spike_pct=0.02,
                      trading_sessions={"KR": ("regular", "premarket")})
    woke = []
    loop = WatchLoop(gw, store, _wl_kr("005935"), markets=("KR",), config=cfg,
                     on_wake=lambda why, trigs: woke.append(why))

    _sessions(monkeypatch, {"KR": "premarket"})
    for _ in range(3):                           # 프리장에서 이력 축적(가격 고정)
        loop.run_once()
    assert list(loop._hist["005935"]) == [100.0, 100.0, 100.0]

    _sessions(monkeypatch, {"KR": "regular"})
    prices["005935"] = 106.0                     # 개장 갭 +6%
    loop.run_once()
    assert list(loop._hist["005935"]) == [106.0]  # 이력이 비워지고 새로 시작
    assert woke == []                             # 헛각성 없음


def test_same_session_keeps_history(tmp_path, monkeypatch):
    """세션이 그대로면 이력은 유지된다 — 진짜 급변은 여전히 잡아야 한다."""
    _sessions(monkeypatch, {"KR": "regular"})
    store = Store(tmp_path / "t.db")
    prices = {"005935": 100.0}
    gw = FakeGateway(prices)
    cfg = WatchConfig(vol_window=12, vol_spike_pct=0.02)
    woke = []
    loop = WatchLoop(gw, store, _wl_kr("005935"), markets=("KR",), config=cfg,
                     on_wake=lambda why, trigs: woke.append(why))
    loop.run_once()
    prices["005935"] = 106.0
    loop.run_once()
    assert list(loop._hist["005935"]) == [100.0, 106.0]
    assert woke == ["wake_triggers"]              # 같은 세션 안 급변은 정상 각성


# ── 4. extra_wakes: 지정 시각 1회 각성(19:00 애프터장 점검) ──────────────
def test_extra_wake_fires_once_per_trading_day(tmp_path, monkeypatch):
    """19:00 각성은 그 거래일에 1회만. 다음 거래일엔 다시 발화."""
    _sessions(monkeypatch, {"KR": "aftermarket"})
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})
    cfg = WatchConfig(trading_sessions={"KR": ("regular", "aftermarket")},
                      extra_wakes={"KR": ("19:00",)})
    now = [_kst_epoch(2026, 7, 22, 18, 59)]
    woke = []
    loop = WatchLoop(gw, store, _wl_kr("005930"), markets=("KR",), config=cfg,
                     on_wake=lambda why, trigs: woke.append(why), now_fn=lambda: now[0])

    loop.run_once()
    assert woke == []                            # 아직 19:00 전

    now[0] = _kst_epoch(2026, 7, 22, 19, 0)
    loop.run_once()
    assert woke == ["extra"]

    now[0] = _kst_epoch(2026, 7, 22, 19, 30)
    loop.run_once()
    assert woke == ["extra"]                     # 같은 거래일 재발화 없음

    now[0] = _kst_epoch(2026, 7, 23, 19, 0)      # 다음 거래일
    loop.run_once()
    assert woke == ["extra", "extra"]

    payloads = [r["payload"] for r in store.conn.execute(
        "SELECT payload FROM events WHERE kind='wake'").fetchall()]
    assert any("extra" in p and "19:00" in p for p in payloads)


def test_extra_wake_silent_when_market_closed(tmp_path, monkeypatch):
    """휴장이면 open_markets 가 비어 지정시각 각성도 안 뜬다(주말·공휴일 방어)."""
    _all_closed(monkeypatch)
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})
    cfg = WatchConfig(trading_sessions={"KR": ("regular", "aftermarket")},
                      extra_wakes={"KR": ("19:00",)})
    woke = []
    loop = WatchLoop(gw, store, _wl_kr("005930"), markets=("KR",), config=cfg,
                     on_wake=lambda why, trigs: woke.append(why),
                     now_fn=lambda: _kst_epoch(2026, 7, 25, 19, 30))
    loop.run_once()
    assert woke == []


def test_extra_0800_fires_even_if_athena_just_woke(tmp_path, monkeypatch):
    """07:30 훅이 last_brain_wake 를 찍어도 08:00 extra 는 별도 1회다."""
    _sessions(monkeypatch, {"KR": "premarket"})
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})
    cfg = WatchConfig(brain_interval_sec=3600,
                      trading_sessions={"KR": ("regular", "premarket")},
                      brain_sessions={"KR": ("premarket", "regular")},
                      extra_wakes={"KR": ("08:00",)},
                      extra_wake_state_path=str(tmp_path / "ew.json"),
                      extra_wake_grace_sec=0)
    now = [_kst_epoch(2026, 7, 22, 8, 0)]
    woke = []
    loop = WatchLoop(gw, store, _wl_kr("005930"), markets=("KR",), config=cfg,
                     on_wake=lambda why, trigs: woke.append(why), now_fn=lambda: now[0])
    loop._last_brain_wake = now[0] - 1800   # 07:30 훅 30분 전
    loop.run_once()
    assert woke == ["extra"]


def test_extra_wake_bad_time_format_ignored(tmp_path, monkeypatch):
    """설정 오타는 경고만 — 데몬이 죽거나 각성이 폭주하지 않는다."""
    _sessions(monkeypatch, {"KR": "aftermarket"})
    store = Store(tmp_path / "t.db")
    gw = FakeGateway({"005930": 70000})
    cfg = WatchConfig(trading_sessions={"KR": ("regular", "aftermarket")},
                      extra_wakes={"KR": ("19시",)})
    woke = []
    loop = WatchLoop(gw, store, _wl_kr("005930"), markets=("KR",), config=cfg,
                     on_wake=lambda why, trigs: woke.append(why),
                     now_fn=lambda: _kst_epoch(2026, 7, 22, 19, 30))
    loop.run_once()
    assert woke == []


# ── 5. 설정 파싱 ─────────────────────────────────────────────────────────
def test_watch_config_reads_new_blocks():
    """brain_sessions/extra_wakes/stale_quote_sec 파싱 + 없을 때 하위호환 기본값."""
    cfg = WatchConfig.from_config({
        "watch": {"stale_quote_sec": 600,
                  "brain_sessions": {"KR": ["premarket", "regular"], "US": ["regular"]},
                  "extra_wakes": {"KR": ["19:00"]}}})
    assert cfg.stale_quote_sec == 600
    assert cfg.brain_sessions == {"KR": ("premarket", "regular"), "US": ("regular",)}
    assert cfg.extra_wakes == {"KR": ("19:00",)}

    bare = WatchConfig.from_config({"watch": {}})
    assert bare.stale_quote_sec == 0.0
    assert bare.brain_sessions == {"KR": ("regular",), "US": ("regular",)}
    assert bare.extra_wakes == {}


def test_example_config_yaml_brain_budget_schedule():
    """공개 example: extra 벽시계 스케줄, brain_interval=0, US premarket."""
    import yaml
    from pathlib import Path
    raw = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text(encoding="utf-8"))
    cfg = WatchConfig.from_config(raw)
    assert cfg.brain_sessions["KR"] == ("premarket", "regular", "aftermarket")
    assert cfg.brain_sessions["US"] == ("premarket", "regular")
    assert cfg.brain_interval_sec == 0
    assert cfg.extra_wakes.get("KR") == (
        "08:00", "09:00", "11:00", "13:00", "15:15", "15:20", "19:50")
    assert cfg.extra_wakes.get("US") == ("17:00", "22:30", "00:30", "02:30", "04:30")
    assert cfg.extra_wake_grace_sec == 120
    assert cfg.extra_wake_window_min == 5
    assert "brain_wake_request" in (cfg.wake_request_path or "")
    assert cfg.stale_quote_sec == 600
    assert set(cfg.trading_sessions["KR"]) >= {"regular", "premarket", "aftermarket"}
    athena_kr = (raw.get("athena") or {}).get("windows") or {}
    stops = [w.get("stop") for w in (athena_kr.get("KR") or []) if isinstance(w, dict)]
    assert "07:30" in stops
    assert (raw.get("day_pool") or {}).get("type") == "MARKET_TRADING_AMOUNT"
    assert (raw.get("screener") or {}).get("rolling", {}).get("movers_enabled") is False

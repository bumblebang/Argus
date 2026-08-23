"""실시간 유니버스 브레드스 — MA20 캐시 산출 → 루프 계산 → 슬라이스 채택/폴백.

지수 2개 프록시(0%/50%/100% 계단)를 유니버스 전 종목 실시간 브레드스로 대체하는 경로 검증.
네트워크 0: 가짜 게이트웨이·가짜 MA20 캐시·fetch_history 몽키패치만 쓴다.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import src.engine.loop as loopmod
import src.live_slice as ls
from src.datasources import breadth as bmod
from src.datasources.breadth import label_of
from src.engine.loop import WatchLoop, ma20_reader
from src.engine.store import Store
from src.live_slice import build_fast_slice

ROOT = Path(__file__).resolve().parent.parent


# ── 공통 가짜 ──────────────────────────────────────────────────────
class FakeGateway:
    """poll_prices/candles 만 흉내. 가격은 생성자에서 주입(없는 심볼은 None=폴링 실패)."""
    def __init__(self, prices: dict):
        self.prices = prices

    def poll_prices(self, symbols, record=True):
        return [{"symbol": s, "price": self.prices.get(s), "payload": {}} for s in symbols]

    def candles(self, symbol, interval="1m", count=20):
        return [{"close": 1}]


def _sessions(monkeypatch, smap):
    """시장별 현재 세션 주입({market: 세션명}, 없는 시장=closed)."""
    monkeypatch.setattr(loopmod, "current_session",
                        lambda m, now=None: smap.get(m, "closed"))
    monkeypatch.setattr(loopmod, "is_tradable",
                        lambda m, allowed=None, now=None:
                        smap.get(m, "closed") in (allowed if allowed is not None else ("regular",)))


def _ma20(**rows) -> dict:
    """{심볼: ma20값} -> ma20.json 의 symbols 블록 형태."""
    return {s: {"ma20": v, "close": v, "n": 400, "market": "KR"} for s, v in rows.items()}


def _loop(tmp_path, prices, ma20, candidates=None, markets=("KR",), wl=None):
    store = Store(tmp_path / "t.db")
    gw = FakeGateway(prices)
    wl = wl if wl is not None else {
        "KR": {"positions": [], "armed": [],
               "candidates": candidates if candidates is not None else list(prices)}}
    return WatchLoop(gw, store, lambda: wl, markets=markets, ma20_fn=lambda: ma20)


def _write_ma20(path: Path, symbols: dict, age_days: float = 0.0) -> Path:
    asof = datetime.now(timezone.utc) - timedelta(days=age_days)
    path.write_text(json.dumps({"asof": asof.isoformat(), "symbols": symbols},
                               ensure_ascii=False), encoding="utf-8")
    return path


# ── 1. baserate MA20 산출 ─────────────────────────────────────────
def _import_baserate_script():
    """scripts/baserate.py 를 임포트 경로에 넣고 로드(scripts/screen.py 테스트와 같은 방식)."""
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "scripts"))
    import importlib
    return importlib.import_module("baserate")


def _df(closes) -> pd.DataFrame:
    c = pd.Series(closes, dtype=float)
    return pd.DataFrame({"time": pd.date_range("2024-01-01", periods=len(c)),
                         "open": c, "high": c * 1.01, "low": c * 0.99,
                         "close": c, "volume": 1000})


def test_ma20_산출은_20봉이상만_포함한다():
    br = _import_baserate_script()
    row = br.ma20_row(_df(np.linspace(100, 130, 25)), "KR")
    assert row is not None
    # MA20 = 마지막 20봉 평균, close = 마지막 종가, n = 봉 수
    closes = np.linspace(100, 130, 25)
    assert row["ma20"] == round(float(closes[-20:].mean()), 4)
    assert row["close"] == round(float(closes[-1]), 4)
    assert row["n"] == 25 and row["market"] == "KR"
    # 20봉 미만은 계산 불가 -> 캐시에서 제외(None)
    assert br.ma20_row(_df([100.0] * 19), "KR") is None
    assert br.ma20_row(_df([100.0] * 20), "KR") is not None      # 경계 포함


# ── 2~4. 루프의 실시간 브레드스 계산 ───────────────────────────────
def test_루프가_실시간가와_MA20캐시로_브레드스를_계산한다(tmp_path, monkeypatch):
    _sessions(monkeypatch, {"KR": "regular"})
    loop = _loop(tmp_path, {"A": 110.0, "B": 120.0, "C": 90.0},
                 _ma20(A=100.0, B=100.0, C=100.0))
    loop.run_once()
    snap = loop.breadth_snapshot()
    assert snap["KR"]["n"] == 3
    assert snap["KR"]["breadth_above_ma20"] == 0.667      # 3종목 중 2종목이 20일선 위
    assert snap["KR"]["label"] == "risk_on"
    assert snap["KR"]["source"] == "universe_live" and snap["KR"]["ts"] > 0


def test_MA20캐시에_없는_종목은_분모에서_제외된다(tmp_path, monkeypatch):
    _sessions(monkeypatch, {"KR": "regular"})
    # D 는 실시간가는 있지만 MA20 캐시에 없다(신규 편입 등) -> 분모에서 빠져 n=3 유지.
    loop = _loop(tmp_path, {"A": 110.0, "B": 120.0, "C": 90.0, "D": 500.0},
                 _ma20(A=100.0, B=100.0, C=100.0))
    loop.run_once()
    snap = loop.breadth_snapshot()
    assert snap["KR"]["n"] == 3 and snap["KR"]["breadth_above_ma20"] == 0.667


def test_실시간가_없는_종목은_분모에서_제외된다(tmp_path, monkeypatch):
    _sessions(monkeypatch, {"KR": "regular"})
    # E 는 MA20 은 있으나 폴링 실패(가격 None) -> 분모에서 빠진다(없는 걸 0 으로 세지 않음).
    loop = _loop(tmp_path, {"A": 110.0, "B": 120.0},
                 _ma20(A=100.0, B=100.0, E=100.0),
                 candidates=["A", "B", "E"])
    loop.run_once()
    snap = loop.breadth_snapshot()
    assert snap["KR"]["n"] == 2 and snap["KR"]["breadth_above_ma20"] == 1.0
    assert snap["KR"]["label"] == "risk_on"


def test_MA20캐시가_비면_브레드스가_비활성이다(tmp_path, monkeypatch):
    _sessions(monkeypatch, {"KR": "regular"})
    loop = _loop(tmp_path, {"A": 110.0, "B": 90.0}, {})
    loop.run_once()
    assert loop.breadth_snapshot() == {}     # 계산 불가 -> 소비자는 지수 프록시로 폴백


# ── 6. MA20 캐시 리더: 없음 / 낡음 / 깨짐 ─────────────────────────
def test_ma20_리더는_없음_낡음_깨짐을_빈dict로_처리한다(tmp_path):
    missing = ma20_reader(path=tmp_path / "없다.json")
    assert missing() == {}                                    # 파일 없음

    stale_p = _write_ma20(tmp_path / "stale.json", _ma20(A=100.0), age_days=6)
    assert ma20_reader(path=stale_p)() == {}                  # 5일 초과 -> 사용 안 함

    fresh_p = _write_ma20(tmp_path / "fresh.json", _ma20(A=100.0), age_days=4)
    assert set(ma20_reader(path=fresh_p)()) == {"A"}          # 5일 이내 -> 정상 사용

    broken = tmp_path / "broken.json"
    broken.write_text("{깨진 json", encoding="utf-8")
    assert ma20_reader(path=broken)() == {}                   # 파싱 실패

    no_asof = tmp_path / "no_asof.json"
    no_asof.write_text(json.dumps({"symbols": _ma20(A=100.0)}), encoding="utf-8")
    assert ma20_reader(path=no_asof)() == {}                  # 신선도 증명 불가 -> 보수적


def test_ma20_리더는_파일이_안바뀌면_다시_읽지_않는다(tmp_path):
    p = _write_ma20(tmp_path / "ma20.json", _ma20(A=100.0))
    reads = {"n": 0}
    orig = Path.read_text

    def counting(self, *a, **k):
        if self == p:
            reads["n"] += 1
        return orig(self, *a, **k)

    Path.read_text = counting
    try:
        read = ma20_reader(path=p)
        for _ in range(5):
            assert set(read()) == {"A"}
        assert reads["n"] == 1                    # mtime 캐시 — 첫 호출만 디스크 파싱
    finally:
        Path.read_text = orig


def test_ma20_파일이_없어도_루프는_죽지_않는다(tmp_path, monkeypatch):
    _sessions(monkeypatch, {"KR": "regular"})
    store = Store(tmp_path / "t.db")
    loop = WatchLoop(FakeGateway({"A": 110.0}), store,
                     lambda: {"KR": {"positions": [], "armed": [], "candidates": ["A"]}},
                     markets=("KR",), ma20_fn=ma20_reader(path=tmp_path / "없다.json"))
    res = loop.run_once()                          # 예외 없이 정상 틱
    assert res.polled == 1 and loop.breadth_snapshot() == {}


# ── 9. 닫힌 시장은 스냅샷에서 제외 ────────────────────────────────
def test_닫힌_시장은_스냅샷에서_빠진다(tmp_path, monkeypatch):
    wl = {"KR": {"positions": [], "armed": [], "candidates": ["A", "B"]},
          "US": {"positions": [], "armed": [], "candidates": ["AAPL"]}}
    ma20 = {"A": {"ma20": 100.0}, "B": {"ma20": 100.0}, "AAPL": {"ma20": 100.0}}
    prices = {"A": 110.0, "B": 120.0, "AAPL": 200.0}

    _sessions(monkeypatch, {"KR": "regular"})      # US 는 닫힘
    loop = _loop(tmp_path, prices, ma20, markets=("KR", "US"), wl=wl)
    loop.run_once()
    assert set(loop.breadth_snapshot()) == {"KR"}  # 닫힌 US 는 아예 없음

    _sessions(monkeypatch, {})                     # 전 시장 휴장
    loop.run_once()
    assert loop.breadth_snapshot() == {}           # 낡은 값을 남기지 않는다(프록시 폴백)


# ── 10. 라벨 임계값 단일 정의 ─────────────────────────────────────
def test_라벨_임계값이_breadth모듈과_동일하다(tmp_path, monkeypatch):
    assert bmod._label is label_of                 # 기존 이름은 별칭(정의는 한 곳)
    assert label_of(0.6) == "risk_on" and label_of(0.61) == "risk_on"
    assert label_of(0.4) == "risk_off" and label_of(0.39) == "risk_off"
    assert label_of(0.5) == "neutral" and label_of(0.59) == "neutral"
    # 루프가 계산한 라벨도 같은 함수를 탄다: 5종목 중 3종목 위 = 0.6 -> risk_on(경계)
    _sessions(monkeypatch, {"KR": "regular"})
    loop = _loop(tmp_path, {"A": 110.0, "B": 110.0, "C": 110.0, "D": 90.0, "E": 90.0},
                 _ma20(A=100.0, B=100.0, C=100.0, D=100.0, E=100.0))
    loop.run_once()
    snap = loop.breadth_snapshot()
    assert snap["KR"]["breadth_above_ma20"] == 0.6 and snap["KR"]["label"] == "risk_on"


# ── live_slice: 채택 / 폴백 ───────────────────────────────────────
def _cfg(min_n=None):
    ms = {"regime_symbols_yahoo": {"KR": ["^KS11", "^KQ11"], "US": ["^GSPC", "^IXIC"]},
          "sentiment_symbols": {"vix": "^VIX"},
          "markets_symbols": {"KOSPI": "^KS11"},
          "request_spacing_sec": 0.0}
    if min_n is not None:
        ms["breadth_min_n"] = min_n
    import types
    return types.SimpleNamespace(raw={"market_state": ms})


class _FakeResp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200

    def json(self):
        return self._p


def _patch_yahoo(monkeypatch, calls: list):
    """regime 캔들(fetch_history)·sentiment·markets 를 전부 가짜로. calls 에 캔들 호출 기록."""
    def _candles(sym, *a, **k):
        calls.append(sym)
        close = np.linspace(100, 130, 30)        # 상승 -> 프록시는 risk_on
        return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                             "close": close, "volume": [1000] * 30})

    monkeypatch.setattr(ls, "fetch_history", _candles)
    import src.datasources.sentiment as smod
    monkeypatch.setattr(smod, "_yahoo_last", lambda sym: 18.0)
    import src.datasources.markets as mmod
    monkeypatch.setattr(mmod.requests, "get",
                        lambda *a, **k: _FakeResp({"chart": {"result": [
                            {"indicators": {"quote": [{"close": [2480.0, 2500.0]}]}}]}}))


def _snap(**markets) -> dict:
    """breadth_snapshot 형태 스냅샷 조립. 값은 (pct, n)."""
    return {m: {"breadth_above_ma20": pct, "n": n, "label": label_of(pct),
                "source": "universe_live", "ts": 1.0} for m, (pct, n) in markets.items()}


def test_전시장_채택시_Yahoo_지수조회가_0회다(monkeypatch):
    calls: list = []
    _patch_yahoo(monkeypatch, calls)
    out = build_fast_slice(_cfg(),
                           breadth_fn=lambda: _snap(KR=(0.16, 19), US=(0.33, 24)))
    assert calls == []                                   # 지수 캔들 한 번도 안 부름
    assert out["regime"]["KR"] == {"breadth_above_ma20": 0.16, "n": 19,
                                   "label": "risk_off", "source": "universe_live", "ts": 1.0}
    assert out["regime"]["US"]["breadth_above_ma20"] == 0.33
    assert out["sentiment"]["vix"] == 18.0               # 나머지 슬롯은 정상 조립


def test_표본_부족_시장만_지수프록시로_폴백한다(monkeypatch):
    calls: list = []
    _patch_yahoo(monkeypatch, calls)
    # KR 은 n=3 (< min_n=10) -> 채택 안 함, US 는 n=24 -> 채택.
    out = build_fast_slice(_cfg(min_n=10),
                           breadth_fn=lambda: _snap(KR=(0.33, 3), US=(0.5, 24)))
    assert calls == ["^KS11", "^KQ11"]                   # KR 만 지수 조회
    assert out["regime"]["KR"]["source"] == "index_proxy" and out["regime"]["KR"]["n"] == 2
    assert out["regime"]["US"]["source"] == "universe_live" and out["regime"]["US"]["n"] == 24


def test_min_n_설정을_따른다(monkeypatch):
    calls: list = []
    _patch_yahoo(monkeypatch, calls)
    # 같은 스냅샷(n=3)이라도 min_n 을 3 으로 낮추면 채택된다.
    out = build_fast_slice(_cfg(min_n=3), breadth_fn=lambda: _snap(KR=(0.33, 3), US=(0.5, 24)))
    assert calls == [] and out["regime"]["KR"]["source"] == "universe_live"


def test_breadth_fn_예외는_전부_프록시로_폴백한다(monkeypatch):
    calls: list = []
    _patch_yahoo(monkeypatch, calls)

    def boom():
        raise RuntimeError("루프 미기동")

    out = build_fast_slice(_cfg(), breadth_fn=boom)      # 죽지 않는다
    assert calls == ["^KS11", "^KQ11", "^GSPC", "^IXIC"]
    assert {m: v["source"] for m, v in out["regime"].items()} == {
        "KR": "index_proxy", "US": "index_proxy"}


def test_빈_스냅샷은_전부_프록시로_폴백한다(monkeypatch):
    calls: list = []
    _patch_yahoo(monkeypatch, calls)
    out = build_fast_slice(_cfg(), breadth_fn=lambda: {})
    assert calls == ["^KS11", "^KQ11", "^GSPC", "^IXIC"]
    assert out["regime"]["KR"]["label"] == "risk_on"


def test_breadth_fn_없으면_기존_지수프록시_동작과_같다(monkeypatch):
    """하위호환: breadth_fn 미지정 = 기존 동작(전 시장 지수 프록시, 같은 호출·같은 값)."""
    calls: list = []
    _patch_yahoo(monkeypatch, calls)
    out = build_fast_slice(_cfg())
    # 조회 심볼·순서 동일
    assert calls == ["^KS11", "^KQ11", "^GSPC", "^IXIC"]
    # 브레드스 값/표본수/라벨 동일 — 출처 태그(source)만 추가된다.
    for m in ("KR", "US"):
        v = dict(out["regime"][m])
        assert v.pop("source") == "index_proxy"
        assert v == {"breadth_above_ma20": 1.0, "n": 2, "label": "risk_on"}
    assert set(out) >= {"regime", "sentiment", "markets", "flows_market"}


# ── 루프 → 슬라이스 통합(실측 시나리오: KR 16% / US 33%) ────────────
def test_루프_브레드스가_슬라이스_regime으로_흘러간다(tmp_path, monkeypatch):
    calls: list = []
    _patch_yahoo(monkeypatch, calls)
    _sessions(monkeypatch, {"KR": "regular"})
    # KR 19종목 중 3종목만 20일선 위 = 15.8% (실측 시나리오)
    syms = [f"S{i:02d}" for i in range(19)]
    prices = {s: (110.0 if i < 3 else 90.0) for i, s in enumerate(syms)}
    ma20 = {s: {"ma20": 100.0, "market": "KR"} for s in syms}
    loop = _loop(tmp_path, prices, ma20, candidates=syms)
    loop.run_once()

    out = build_fast_slice(_cfg(), breadth_fn=loop.breadth_snapshot)
    assert out["regime"]["KR"] == {**loop.breadth_snapshot()["KR"]}
    assert out["regime"]["KR"]["breadth_above_ma20"] == 0.158
    assert out["regime"]["KR"]["n"] == 19 and out["regime"]["KR"]["label"] == "risk_off"
    assert calls == ["^GSPC", "^IXIC"]        # KR 은 실시간 채택 -> US 만 프록시 조회

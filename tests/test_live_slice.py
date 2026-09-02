"""장중 빠른 슬라이스 — 조립(regime/sentiment/markets)·부분 merge·원자적 저장 테스트.

실제 네트워크 금지: fetch_history(regime 캔들)와 Yahoo HTTP(sentiment/markets)를 전부
monkeypatch 로 가짜 데이터 주입. regime 라벨은 주입 캔들 기반 브레드스로 계산됨을 검증.
"""
import json
import types

import numpy as np
import pandas as pd

import src.live_slice as ls
from src.live_slice import build_fast_slice, apply_fast_slice
from src.market_state import MarketState


def _cfg(regime_symbols=None, sentiment=None, markets=None):
    raw = {"market_state": {
        "regime_symbols": regime_symbols or {"KR": ["069500", "229200"]},
        "sentiment_symbols": sentiment or {"vix": "^VIX"},
        "markets_symbols": markets or {"KOSPI": "^KS11"},
        "request_spacing_sec": 0.0,   # 테스트는 sleep 없이
    }}
    return types.SimpleNamespace(raw=raw)


def _rising(*_a, **_k):
    close = np.linspace(100, 130, 30)  # 상승 -> MA20 상회 -> risk_on
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": [1000] * 30})


def _falling(*_a, **_k):
    close = np.linspace(130, 100, 30)  # 하락 -> risk_off
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": [1000] * 30})


class _FakeResp:
    def __init__(self, payload):
        self._p = payload
        self.status_code = 200

    def json(self):
        return self._p


def _patch_yahoo(monkeypatch, candle_fn=_rising, vix=18.0, last=2500.0, prev=2480.0):
    # regime 캔들: fetch_history 를 통째로 가짜로.
    monkeypatch.setattr(ls, "fetch_history", candle_fn)
    # sentiment: _yahoo_last 를 가짜로(HTTP 안 탐).
    import src.datasources.sentiment as smod
    monkeypatch.setattr(smod, "_yahoo_last", lambda sym: vix)
    # markets: requests.get 을 가짜로(5d/1d 응답 형태).
    import src.datasources.markets as mmod
    payload = {"chart": {"result": [{"indicators": {"quote": [
        {"close": [prev, last]}]}}]}}
    monkeypatch.setattr(mmod.requests, "get", lambda *a, **k: _FakeResp(payload))


def test_build_fast_slice_shape_and_regime(monkeypatch):
    _patch_yahoo(monkeypatch, candle_fn=_rising)
    out = build_fast_slice(_cfg())
    assert set(out) >= {"regime", "sentiment", "markets", "flows_market"}
    assert out["regime"]["KR"]["label"] == "risk_on"   # 주입 상승 캔들 -> 브레드스 100%
    assert out["sentiment"]["vix"] == 18.0
    assert out["sentiment"]["vix_label"] == "normal"
    assert out["markets"]["KOSPI"]["last"] == 2500.0


def test_build_fast_slice_regime_risk_off(monkeypatch):
    _patch_yahoo(monkeypatch, candle_fn=_falling)
    out = build_fast_slice(_cfg())
    assert out["regime"]["KR"]["label"] == "risk_off"


def test_build_fast_slice_no_network_toss_untouched(monkeypatch):
    # ctx.client=None 로 호출되므로 브레드스가 토스를 만지면 AttributeError 로 터진다.
    # 여기서 정상 조립되면 토스 무접촉이 증명됨.
    _patch_yahoo(monkeypatch)
    out = build_fast_slice(_cfg())
    assert "regime" in out and out["regime"]["KR"]["n"] == 2


def test_apply_fast_slice_preserves_other_slots(tmp_path):
    path = tmp_path / "market_state.json"
    # 기존 파일: 느린 슬롯(fundamentals/news/fx)이 채워져 있음.
    seed = MarketState()
    seed.merge({"fundamentals": {"AAPL": {"net_margin": 0.25}},
                "news": [{"title": "old headline"}],
                "fx": {"USDKRW": 1380.0},
                "regime": {"KR": {"label": "neutral"}},
                "markets": {"KOSPI": {"last": 1.0}}})
    seed.save(path)
    old_asof = MarketState.load(path).asof

    partial = {"regime": {"KR": {"label": "risk_on", "n": 2}},
               "sentiment": {"vix": 22.0, "vix_label": "elevated"},
               "markets": {"KOSPI": {"last": 2500.0}}}
    apply_fast_slice(partial, path)

    st = MarketState.load(path)
    # 빠른 슬롯은 갱신
    assert st.regime["KR"]["label"] == "risk_on"
    assert st.sentiment["vix"] == 22.0
    assert st.markets["KOSPI"]["last"] == 2500.0
    # 무관 슬롯은 보존
    assert st.fundamentals == {"AAPL": {"net_margin": 0.25}}
    assert st.news == [{"title": "old headline"}]
    assert st.fx == {"USDKRW": 1380.0}
    # asof 갱신됨
    assert st.asof != old_asof
    assert st.fast_asof == st.asof
    assert st.batch_asof == old_asof


def test_apply_fast_slice_creates_file_if_missing(tmp_path):
    path = tmp_path / "sub" / "market_state.json"   # 폴더도 없음
    partial = {"regime": {"US": {"label": "risk_on"}}, "sentiment": {}, "markets": {}}
    apply_fast_slice(partial, path)
    assert path.exists()
    st = MarketState.load(path)
    assert st.regime["US"]["label"] == "risk_on"


def test_apply_fast_slice_valid_json(tmp_path):
    path = tmp_path / "market_state.json"
    apply_fast_slice({"regime": {"KR": {"label": "neutral"}}, "sentiment": {},
                      "markets": {}}, path)
    # 저장 후 항상 유효 JSON (원자적 쓰기 → torn-read 없음)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["regime"]["KR"]["label"] == "neutral"
    assert "asof" in data


def _one_bar(*_a, **_k):
    # Yahoo 가 간헐적으로 1봉만 주는 상황(KR 지수 ETF 실측 이슈) — MA20 계산 불가.
    return pd.DataFrame({"open": [100.0], "high": [101.0], "low": [99.0],
                         "close": [100.0], "volume": [1000]})


def test_build_fast_slice_drops_market_on_insufficient_data(monkeypatch):
    # 데이터 부족(n=0)이면 regime 을 0.0 → risk_off 로 내보내지 말고 해당 시장을 제거해야
    # merge 가 직전 배치값을 보존한다(조회 실패가 risk_off 오판으로 둔갑 방지).
    _patch_yahoo(monkeypatch, candle_fn=_one_bar)
    out = build_fast_slice(_cfg())
    assert out["regime"] == {}                 # 모든 시장 데이터 부족 → regime 비움
    assert out["sentiment"]["vix"] == 18.0     # sentiment/markets 는 정상 조립


def test_apply_empty_regime_preserves_existing(tmp_path):
    # 빈 regime partial 은 기존(배치) regime 을 덮어쓰지 않는다(merge=dict update).
    path = tmp_path / "market_state.json"
    seed = MarketState()
    seed.merge({"regime": {"KR": {"label": "risk_on", "n": 2}}})
    seed.save(path)
    apply_fast_slice({"regime": {}, "sentiment": {"vix": 20.0}, "markets": {}}, path)
    st = MarketState.load(path)
    assert st.regime["KR"]["label"] == "risk_on"   # 배치값 보존
    assert st.sentiment["vix"] == 20.0


def test_build_then_apply_end_to_end(monkeypatch, tmp_path):
    _patch_yahoo(monkeypatch, candle_fn=_rising)
    path = tmp_path / "market_state.json"
    out = build_fast_slice(_cfg())
    apply_fast_slice(out, path)
    st = MarketState.load(path)
    assert st.regime["KR"]["label"] == "risk_on"
    assert st.markets["KOSPI"]["last"] == 2500.0

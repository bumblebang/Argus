"""공포지수 레이어 — CNN 어댑터/TTL 캐시/KR 대리지표/조립 + 후보 낙폭·안정화 피처.

실제 네트워크 금지: CNN JSON 은 fetch_fn 주입, 지수 캔들은 history_fn 주입/monkeypatch.
모듈 레벨 TTL 캐시가 테스트 간에 새지 않도록 매 테스트마다 리셋한다.
"""
import json
import types

import pytest

import src.datasources.fear_greed as fg
from src.datasources.fear_greed import (assess, fetch_cnn_fear_greed,
                                        index_drawdown_pct, index_stats,
                                        kr_fear_proxy, rating_of,
                                        apply_kr_rating, percentile_rank,
                                        load_kr_scores, summary_line)


@pytest.fixture(autouse=True)
def _reset_cache():
    fg._cache.update({"ts": 0.0, "value": None})
    yield
    fg._cache.update({"ts": 0.0, "value": None})


def _cnn_payload(score=38.9714285714286):
    """실제 CNN 응답과 같은 모양(하위지표가 최상위 키로 흩어져 있음)."""
    def _sub(s, r):
        return {"score": s, "rating": r, "timestamp": 1785459587000.0}
    return {
        "fear_and_greed": {"score": score, "rating": "fear",
                           "timestamp": "2026-07-31T00:59:47+00:00",
                           "previous_close": 38.9142857142857,
                           "previous_1_week": 41.3428571428571,
                           "previous_1_month": 29.971428571428568,
                           "previous_1_year": 63.714285714285715},
        "fear_and_greed_historical": _sub(38.97, "fear"),
        "market_momentum_sp500": _sub(30.8, "fear"),
        "market_momentum_sp125": _sub(30.8, "fear"),        # sp500 과 중복 → 제외 대상
        "stock_price_strength": _sub(30.6, "fear"),
        "stock_price_breadth": _sub(23.8, "extreme fear"),
        "put_call_options": _sub(30.2, "fear"),
        "market_volatility_vix": _sub(50, "neutral"),
        "market_volatility_vix_50": _sub(50, "neutral"),    # vix 와 중복 → 제외 대상
        "junk_bond_demand": _sub(68.8, "greed"),
        "safe_haven_demand": _sub(38.6, "fear"),
    }


# ── 라벨 경계값 ─────────────────────────────────────────────────────
def test_rating_of_boundaries():
    assert rating_of(24.9) == "extreme_fear"
    assert rating_of(25) == "fear"
    assert rating_of(44.9) == "fear"
    assert rating_of(45) == "neutral"
    assert rating_of(54.9) == "neutral"
    assert rating_of(55) == "greed"
    assert rating_of(74.9) == "greed"
    assert rating_of(75) == "extreme_greed"
    assert rating_of(0) == "extreme_fear" and rating_of(100) == "extreme_greed"


# ── CNN 파싱 ───────────────────────────────────────────────────────
def test_cnn_parse_shape():
    out = fetch_cnn_fear_greed(fetch_fn=_cnn_payload)
    assert out["score"] == 39.0 and out["rating"] == "fear"
    assert out["prev_close"] == 38.9 and out["prev_1w"] == 41.3
    assert out["prev_1m"] == 30.0 and out["prev_1y"] == 63.7
    assert out["asof"] == "2026-07-31T00:59:47+00:00"      # 원본 문자열 그대로
    assert out["market"] == "US" and out["source"] == "cnn"
    assert out["components"] == {
        "market_momentum_sp500": 30.8, "stock_price_strength": 30.6,
        "stock_price_breadth": 23.8, "put_call_options": 30.2,
        "market_volatility_vix": 50.0, "junk_bond_demand": 68.8,
        "safe_haven_demand": 38.6}
    # 중복/이력 키는 담지 않는다
    for k in ("market_momentum_sp125", "market_volatility_vix_50",
              "fear_and_greed_historical"):
        assert k not in out["components"]


def test_cnn_rating_is_normalized_not_copied():
    # CNN 원본 rating 문자열("extreme fear")이 아니라 우리 라벨을 쓴다.
    payload = _cnn_payload(score=12.0)
    payload["fear_and_greed"]["rating"] = "extreme fear"
    out = fetch_cnn_fear_greed(fetch_fn=lambda: payload)
    assert out["rating"] == "extreme_fear"


def test_cnn_missing_optional_keys_are_skipped():
    payload = {"fear_and_greed": {"score": 50.0}}          # prev/components/timestamp 없음
    out = fetch_cnn_fear_greed(fetch_fn=lambda: payload)
    assert out == {"score": 50.0, "rating": "neutral", "market": "US", "source": "cnn"}


# ── TTL 캐시 ───────────────────────────────────────────────────────
def test_ttl_cache_fetches_once():
    calls = []

    def _fn():
        calls.append(1)
        return _cnn_payload()

    a = fetch_cnn_fear_greed(ttl_sec=3600, fetch_fn=_fn)
    b = fetch_cnn_fear_greed(ttl_sec=3600, fetch_fn=_fn)
    assert len(calls) == 1                                  # 2회 호출 → 1회 조회
    assert a == b


def test_ttl_zero_refetches():
    calls = []

    def _fn():
        calls.append(1)
        return _cnn_payload()

    fetch_cnn_fear_greed(ttl_sec=0, fetch_fn=_fn)
    fetch_cnn_fear_greed(ttl_sec=0, fetch_fn=_fn)
    assert len(calls) == 2


# ── 조회 실패(fail-open) ────────────────────────────────────────────
def _boom():
    raise RuntimeError("네트워크 끊김")


def test_failure_returns_stale_cache():
    fetch_cnn_fear_greed(ttl_sec=0, fetch_fn=_cnn_payload)  # 캐시 적재
    out = fetch_cnn_fear_greed(ttl_sec=0, fetch_fn=_boom)
    assert out["stale"] is True and out["score"] == 39.0
    # 캐시 원본은 오염되지 않는다
    assert "stale" not in fg._cache["value"]


def test_failure_without_cache_returns_none():
    assert fetch_cnn_fear_greed(ttl_sec=0, fetch_fn=_boom) is None   # 예외 없음


def test_malformed_payload_returns_none():
    assert fetch_cnn_fear_greed(ttl_sec=0, fetch_fn=lambda: {"nope": 1}) is None
    assert fetch_cnn_fear_greed(ttl_sec=0, fetch_fn=lambda: None) is None


# ── 지수 낙폭 ───────────────────────────────────────────────────────
def _hist(closes):
    import pandas as pd
    return lambda sym, market: pd.DataFrame({"close": closes})


def test_index_drawdown_pct():
    closes = [100.0] * 19 + [80.0]                          # 최근 20봉 고점 100 → -20%
    assert index_drawdown_pct(history_fn=_hist(closes)) == -20.0


def test_index_drawdown_zero_at_high():
    closes = list(range(80, 100))                           # 마지막이 고점 → 0
    assert index_drawdown_pct(history_fn=_hist(closes)) == 0.0


def test_index_drawdown_needs_lookback_bars():
    assert index_drawdown_pct(history_fn=_hist([100.0] * 5)) is None
    import pandas as pd
    assert index_drawdown_pct(history_fn=lambda s, m: pd.DataFrame()) is None
    assert index_drawdown_pct(history_fn=lambda s, m: None) is None


def test_index_drawdown_swallows_exception():
    def boom(sym, market):
        raise RuntimeError("Yahoo 다운")
    assert index_drawdown_pct(history_fn=boom) is None


def test_index_stats_returns_both():
    closes = [100.0] * 15 + [100.0, 96.0, 92.0, 88.0, 80.0]   # 20봉, 5봉 전=100
    out = index_stats(history_fn=_hist(closes))
    assert out == {"drawdown_pct": -20.0, "ret_5d_pct": -20.0}


def test_index_stats_partial_when_short():
    # 6봉: lookback 20 미달 → drawdown 생략, ret_5d 는 계산 가능
    out = index_stats(history_fn=_hist([100.0] * 5 + [90.0]))
    assert out == {"ret_5d_pct": -10.0}
    assert index_stats(history_fn=lambda s, m: None) == {}


# ── KR 대리지표 ─────────────────────────────────────────────────────
def test_kr_proxy_all_three_components():
    # breadth 0.20→20.0(w.5), dd -6%→60.0(w.3), ret5d -5%→25.0(w.2)
    # score = 20*.5 + 60*.3 + 25*.2 = 33.0
    out = kr_fear_proxy({"breadth_above_ma20": 0.20, "n": 19}, -6.0, -5.0,
                        breadth_min_n=10)
    assert out["score"] == 33.0 and out["rating"] == "fear"
    assert out["components"] == {"breadth": 20.0, "drawdown": 60.0, "ret_5d": 25.0}
    assert out["inputs"] == {"breadth_above_ma20": 0.20, "n": 19,
                             "index_drawdown_pct": -6.0, "index_ret_5d_pct": -5.0}
    assert out["market"] == "KR" and out["source"] == "proxy_kr" and out["note"]
    assert out["incomplete"] is False and out["missing"] == []


def test_kr_proxy_thin_breadth_is_dropped_and_reweighted():
    # n=2 < min_n → breadth 제외, 남은 가중치 0.5 로 재정규화.
    # (60*.3 + 25*.2)/.5 = 46.0
    out = kr_fear_proxy({"breadth_above_ma20": 0.20, "n": 2}, -6.0, -5.0,
                        breadth_min_n=10)
    assert out["score"] == 46.0 and out["rating"] == "neutral"
    assert set(out["components"]) == {"drawdown", "ret_5d"}
    assert out["incomplete"] is True and out["missing"] == ["breadth"]
    assert "breadth_above_ma20" not in out["inputs"] and "n" not in out["inputs"]


def test_kr_proxy_single_component_is_the_score():
    out = kr_fear_proxy({"breadth_above_ma20": 0.10, "n": 19}, None, None)
    assert out["score"] == 10.0 and out["components"] == {"breadth": 10.0}
    assert out["incomplete"] is True
    assert set(out["missing"]) == {"drawdown", "ret_5d"}


def test_kr_proxy_clamps():
    # dd -30% → 0 (클램프), ret5d +20% → 100 (클램프). (0*.3 + 100*.2)/.5 = 40.0
    out = kr_fear_proxy({"n": 0}, -30.0, 20.0)
    assert out["components"] == {"drawdown": 0.0, "ret_5d": 100.0}
    assert out["score"] == 40.0


def test_kr_proxy_rebound_day_does_not_read_as_greed():
    # 폭락 뒤 반등일: 하루 +18% 여도 5일 기준으론 아직 마이너스 → 공포로 남아야 한다.
    # breadth 26.3(w.5) + dd -18.5%→0(w.3) + ret5d -2.4%→38.0(w.2) = 20.75
    out = kr_fear_proxy({"breadth_above_ma20": 0.263, "n": 19}, -18.5, -2.4)
    assert out["score"] == 20.8 and out["rating"] == "extreme_fear"


def test_kr_proxy_returns_none_when_empty():
    assert kr_fear_proxy(None, None, None) is None
    assert kr_fear_proxy({}, None, None) is None
    assert kr_fear_proxy({"breadth_above_ma20": 0.5, "n": 3}, None, None) is None  # 표본 부족


def test_percentile_rank_all_equal_is_fifty():
    assert percentile_rank(40.0, [40.0] * 20) == 50.0


def test_percentile_rank_low_is_near_zero():
    hist = list(range(20))  # 0..19
    assert percentile_rank(0.0, hist) == 2.5          # 0.5/20*100
    assert percentile_rank(19.0, hist) == 97.5


def test_apply_kr_rating_absolute_when_history_short():
    kr = {"score": 33.0, "rating": "fear"}
    apply_kr_rating(kr, [30.0] * 5, min_n=20)
    assert kr["rating_basis"] == "absolute" and kr["rating"] == "fear"
    assert "score_pct" not in kr


def test_apply_kr_rating_percentile_when_history_long():
    # 이력은 전부 50 근처, 현재 20 → 백분위 낮음 → extreme_fear. 원점수 20 은 fear 구간.
    kr = {"score": 20.0, "rating": "fear"}
    apply_kr_rating(kr, [50.0] * 20, min_n=20)
    assert kr["rating_basis"] == "percentile"
    assert kr["score"] == 20.0
    assert kr["score_pct"] == 0.0
    assert kr["rating"] == "extreme_fear"


def test_load_kr_scores_missing_file(tmp_path):
    assert load_kr_scores(tmp_path / "nope.json") == []


def test_assess_uses_history_for_kr_rating(tmp_path, monkeypatch):
    hist = tmp_path / "fh.json"
    hist.write_text(json.dumps({"kr": [[i, 50.0] for i in range(20)]}),
                    encoding="utf-8")
    _patch_sources(monkeypatch)
    out = assess(_STATE, _cfg(history_path=str(hist)))
    kr = out["fear_kr"]
    assert kr["score"] == 33.0
    assert kr["rating_basis"] == "percentile"
    assert kr["rating"] == "extreme_fear"          # 33 vs 이력 50 → 하위
    assert kr["incomplete"] is False


def test_summary_line_marks_incomplete_and_basis():
    s = summary_line({
        "fear_greed": {"score": 39.0, "rating": "fear"},
        "fear_kr": {"score": 46.0, "rating": "neutral", "incomplete": True,
                    "rating_basis": "absolute"},
    })
    assert "US=39.0(fear)" in s
    assert "KR=46.0(neutral/inc/abs)" in s
    s2 = summary_line({"fear_kr": {"score": 20.0, "rating": "extreme_fear",
                                   "rating_basis": "percentile"}})
    assert "KR=20.0(extreme_fear/pct)" in s2


# ── 조립 ───────────────────────────────────────────────────────────
def _cfg(**fear):
    # 운영 data/fear_history.json 이 있어도 등급이 퍼센타일로 바뀌지 않게 격리.
    fear.setdefault("history_path", "__no_fear_history__.json")
    # 테스트가 .env 의 KRX_API_KEY 로 실조회하지 않게.
    fear.setdefault("krx_enrich", False)
    return types.SimpleNamespace(
        raw={"market_state": {"breadth_min_n": 10, "fear_greed": fear}})


_STATE = {"regime": {"KR": {"breadth_above_ma20": 0.20, "n": 19}},
          "markets": {"KOSPI": {"last": 6595.45, "chg_1d": -0.015}}}


def _patch_sources(monkeypatch, cnn=True, dd=-6.0, ret5=-5.0, history=None):
    """assess 가 부르는 외부 경로(CNN·지수 통계·이력 적재)를 전부 주입.

    cnn=False → CNN 조회 실패. record_history 는 기본적으로 no-op 로 막는다(테스트가
    운영 data/fear_history.json 을 건드리면 안 된다). history 리스트를 주면 호출 인자를
    거기 기록한다.
    """
    us = ({"score": 39.0, "rating": "fear", "market": "US", "source": "cnn"}
          if cnn else None)
    stats = {k: v for k, v in (("drawdown_pct", dd), ("ret_5d_pct", ret5))
             if v is not None}
    monkeypatch.setattr(fg, "fetch_cnn_fear_greed", lambda **k: us)
    monkeypatch.setattr(fg, "index_stats", lambda **k: stats)
    monkeypatch.setattr(fg, "record_history",
                        lambda *a, **k: history.append((a, k)) if history is not None
                        else None)


def test_assess_disabled_returns_empty(monkeypatch):
    _patch_sources(monkeypatch)
    assert assess(_STATE, _cfg(enabled=False)) == {}


def test_assess_both_markets(monkeypatch):
    _patch_sources(monkeypatch)
    out = assess(_STATE, _cfg())
    assert out["fear_greed"]["score"] == 39.0
    assert out["fear_kr"]["score"] == 33.0                  # 위 손계산과 동일
    assert out["fear_kr"]["rating_basis"] == "absolute"     # 이력 파일 없음
    assert out["fear_kr"]["incomplete"] is False


def test_assess_omits_failed_side(monkeypatch):
    # CNN 실패해도 KR 은 나온다.
    _patch_sources(monkeypatch, cnn=False)
    out = assess(_STATE, _cfg())
    assert "fear_greed" not in out and out["fear_kr"]["score"] == 33.0


def test_assess_kr_proxy_off(monkeypatch):
    _patch_sources(monkeypatch)
    out = assess(_STATE, _cfg(kr_proxy=False))
    assert set(out) == {"fear_greed"}


def test_assess_all_failed_returns_empty(monkeypatch):
    _patch_sources(monkeypatch, cnn=False, dd=None, ret5=None)
    assert assess({}, _cfg()) == {}


def test_assess_never_raises(monkeypatch):
    _patch_sources(monkeypatch)
    for state in ({}, None, {"regime": None, "markets": None},
                  {"regime": {"KR": None}, "markets": {"KOSPI": None}},
                  {"regime": "쓰레기", "markets": 3}):
        assert isinstance(assess(state, _cfg()), dict)
    # cfg 가 None / 망가진 cfg 여도 안 터진다
    assert isinstance(assess(_STATE, None), dict)
    assert assess(_STATE, types.SimpleNamespace()) == {}


def test_assess_reuses_breadth_min_n_from_market_state(monkeypatch):
    _patch_sources(monkeypatch)
    cfg = types.SimpleNamespace(
        raw={"market_state": {"breadth_min_n": 25,
                              "fear_greed": {"history_path": "__no_fear_history__.json",
                                             "krx_enrich": False}}})
    out = assess(_STATE, cfg)                               # n=19 < 25 → breadth 제외
    assert set(out["fear_kr"]["components"]) == {"drawdown", "ret_5d"}
    assert out["fear_kr"]["incomplete"] is True
    assert out["fear_kr"]["missing"] == ["breadth"]


def test_assess_output_is_json_serializable(monkeypatch):
    _patch_sources(monkeypatch)
    json.dumps(assess(_STATE, _cfg()))


# ── 시계열 적재(대시보드 스파크라인 전용) ──────────────────────────
def _hist_payload(n=5, base=1_700_000_000_000):
    """CNN 응답에 1년치 일별 시계열(fear_and_greed_historical.data)을 붙인다."""
    p = _cnn_payload()
    p["fear_and_greed_historical"] = {
        "score": 38.97, "rating": "fear",
        "data": [{"x": base + i * 86_400_000, "y": 30.0 + i, "rating": "fear"}
                 for i in range(n)]}
    return p


def test_cnn_history_is_parsed_and_capped():
    out = fetch_cnn_fear_greed(fetch_fn=lambda: _hist_payload(n=200))
    h = out["_history"]
    assert len(h) == 120                                   # 최근 120 포인트만
    assert h == sorted(h, key=lambda p: p[0])              # 시간 오름차순
    assert h[0][0] == int((1_700_000_000_000 + 80 * 86_400_000) / 1000)
    assert h[-1][1] == 30.0 + 199                          # 마지막이 최신
    assert all(isinstance(t, int) for t, _ in h)


def test_cnn_history_absent_when_source_missing():
    # 기존 payload(historical 에 data 키 없음) → _history 자체가 없다.
    assert "_history" not in fetch_cnn_fear_greed(fetch_fn=_cnn_payload)


def test_record_history_replaces_us_and_appends_kr(tmp_path):
    p = tmp_path / "fear_history.json"
    fg.record_history([[100, 30.0], [200, 31.0]], 20.0, path=p, now_fn=lambda: 1000)
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["us"] == [[100, 30.0], [200, 31.0]]
    assert d["kr"] == [[1000, 20.0]]
    # us 는 통째 교체(CNN 이 권위), kr 은 간격을 넘겼으면 누적
    fg.record_history([[300, 40.0]], 25.0, path=p, min_gap_sec=1500,
                      now_fn=lambda: 1000 + 1500)
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d["us"] == [[300, 40.0]]
    assert d["kr"] == [[1000, 20.0], [2500, 25.0]]


def test_record_history_keeps_us_when_no_series(tmp_path):
    p = tmp_path / "f.json"
    fg.record_history([[100, 30.0]], None, path=p)
    fg.record_history(None, None, path=p)
    assert json.loads(p.read_text(encoding="utf-8"))["us"] == [[100, 30.0]]


def test_record_history_kr_respects_min_gap(tmp_path):
    p = tmp_path / "f.json"
    fg.record_history(None, 20.0, path=p, min_gap_sec=1500, now_fn=lambda: 1000)
    fg.record_history(None, 21.0, path=p, min_gap_sec=1500, now_fn=lambda: 2000)
    fg.record_history(None, 22.0, path=p, min_gap_sec=1500, now_fn=lambda: 2499)
    assert json.loads(p.read_text(encoding="utf-8"))["kr"] == [[1000, 20.0]]


def test_record_history_caps_from_front(tmp_path):
    p = tmp_path / "f.json"
    for i in range(6):
        fg.record_history(None, float(i), path=p, min_gap_sec=0, cap=3,
                          now_fn=lambda i=i: 1000 + i)
    kr = json.loads(p.read_text(encoding="utf-8"))["kr"]
    assert kr == [[1003, 3.0], [1004, 4.0], [1005, 5.0]]   # 앞에서 잘림


def test_record_history_recovers_from_corrupt_file(tmp_path):
    p = tmp_path / "f.json"
    p.write_text("{망가진", encoding="utf-8")
    fg.record_history([[100, 30.0]], 20.0, path=p, now_fn=lambda: 1000)
    d = json.loads(p.read_text(encoding="utf-8"))
    assert d == {"us": [[100, 30.0]], "kr": [[1000, 20.0]]}


def test_record_history_is_atomic_and_leaves_no_tmp(tmp_path):
    p = tmp_path / "f.json"
    fg.record_history([[100, 30.0]], 20.0, path=p, now_fn=lambda: 1000)
    assert not (tmp_path / "f.json.tmp").exists()
    assert list(tmp_path.iterdir()) == [p]


def test_record_history_never_raises(tmp_path):
    # 쓰레기 입력·못 쓰는 경로 어디서도 예외를 올리지 않는다(fail-open).
    fg.record_history("쓰레기", "쓰레기", path=tmp_path / "f.json")
    fg.record_history([[1, 2]], 3.0, path=tmp_path)          # 디렉터리 경로
    fg.record_history(None, None, path=tmp_path / "f.json")


def test_assess_does_not_leak_history_into_sentiment(monkeypatch, tmp_path):
    """회귀 고정: sentiment.fear_greed 에 _history 가 새면 뇌 프롬프트가 노이즈로 부푼다."""
    us = {"score": 39.0, "rating": "fear", "market": "US", "source": "cnn",
          "_history": [[100, 30.0], [200, 31.0]]}
    monkeypatch.setattr(fg, "fetch_cnn_fear_greed", lambda **k: us)
    monkeypatch.setattr(fg, "index_stats", lambda **k: {"drawdown_pct": -6.0})
    out = assess(_STATE, _cfg(history_path=str(tmp_path / "f.json")))
    assert "_history" not in out["fear_greed"]
    assert not any(k.startswith("_") for k in out["fear_greed"])
    assert us["_history"] == [[100, 30.0], [200, 31.0]]      # 캐시 원본은 안 건드림
    # 대신 별도 파일에 실린다
    d = json.loads((tmp_path / "f.json").read_text(encoding="utf-8"))
    assert d["us"] == [[100, 30.0], [200, 31.0]] and d["kr"]


def test_assess_history_can_be_disabled(monkeypatch, tmp_path):
    calls = []
    _patch_sources(monkeypatch, history=calls)
    assess(_STATE, _cfg(history=False))
    assert calls == []
    assess(_STATE, _cfg())
    assert len(calls) == 1 and calls[0][0][1] == 33.0        # kr 점수가 함께 넘어간다


# ── 후보 피처(낙폭·안정화) ──────────────────────────────────────────
def _candles(closes):
    return [{"time": i, "open": c, "high": c, "low": c, "close": c, "volume": 1000}
            for i, c in enumerate(closes)]


def _assemble_one(closes):
    from src.agents.features import assemble
    items = [{"symbol": "005930", "name": "삼성전자", "market": "KR"}]
    cands, _ = assemble(items, {}, lambda s, m: _candles(closes))
    return cands[0]


def test_features_drawdown_and_stabilizing_rising():
    feat = _assemble_one([100.0 + i for i in range(40)])     # 꾸준한 상승
    assert feat["drawdown_pct"] == 0.0                       # 마지막이 고점
    assert feat["drawdown_lookback"] == 40
    assert feat["stabilizing"]["ok"] is True
    assert feat["stabilizing"]["above_ma20"] is True
    assert feat["stabilizing"]["ret_20d_pct"] > 0
    assert feat["stabilizing"]["ret_5d_pct"] > 0
    assert feat["volume"] == 1000


def test_features_drawdown_and_stabilizing_falling():
    feat = _assemble_one([140.0 - i for i in range(40)])     # 꾸준한 하락
    assert feat["drawdown_pct"] < 0                          # 고점 대비 낙폭
    assert feat["stabilizing"]["ok"] is False
    assert feat["stabilizing"]["above_ma20"] is False
    assert feat["stabilizing"]["ret_20d_pct"] < 0


def test_features_drawdown_lookback_caps_at_60():
    feat = _assemble_one([100.0] * 90)
    assert feat["drawdown_lookback"] == 60 and feat["drawdown_pct"] == 0.0


def test_features_stabilizing_is_json_serializable():
    # numpy bool 이 새면 json.dumps 가 TypeError 로 터진다(뇌 입력이 통째로 실패).
    feat = _assemble_one([100.0 + i for i in range(40)])
    s = json.dumps(feat["stabilizing"])
    assert '"ok": true' in s
    assert type(feat["stabilizing"]["ok"]) is bool


# ── 프롬프트 회귀: 공포 로직이 지시문에 살아 있는지 ────────────────
# 이 레이어의 실제 동작은 코드가 아니라 프롬프트가 결정한다("공포에 팔라"를 뒤집는 게
# 목적). 나중 편집이 조용히 지워버리면 측정층만 남고 판단은 예전으로 돌아가므로 고정한다.
def test_decision_prompt_reframes_fear_as_opportunity():
    from src.agents.decision_agent import SYSTEM as DEC

    assert "공포는 매수 금지 신호가 아니다" in DEC
    assert "공포에 사고 탐욕에 파는 방향이 기본값이다" in DEC
    # 떨어지는 칼 방어(낙폭 + 안정화 동시 요구)가 함께 있어야 한다.
    assert "무차별 역행은 떨어지는 칼" in DEC
    assert "drawdown_pct" in DEC and "stabilizing" in DEC
    # 진입 자체를 봉인하던 옛 지시가 되살아나지 않았는지.
    assert "진입 자체를 봉인하지는 마라" in DEC
    assert "VIX 로 KR 공포를 판단하지 마라" in DEC
    assert "rating_basis=percentile" in DEC
    assert "incomplete=true" in DEC


def test_validation_rule3_is_two_sided():
    from src.agents.validation_agent import SYSTEM as VAL

    assert "공포 국면의 매수라는 이유만으로 거부하지 마라" in VAL
    assert "떨어지는 칼" in VAL and "stabilizing" in VAL
    assert "탐욕 추격" in VAL
    # 나머지 거부 규칙은 그대로 살아 있어야 한다.
    for rule in ("단일 지표 의존", "확신도 미달", "약세 스틸맨 부실"):
        assert rule in VAL
    assert "min_conviction 이 0 이면 이 규칙을 적용하지 마라" in VAL
    assert "fear_kr.incomplete" in VAL


def test_athena_prompt_allows_fear_bullish_with_lower_zone():
    from src.agents.athena import ATHENA_SYSTEM as ATH

    assert "진입존을 현재가 아래 지지선에 둔 bullish" in ATH
    assert "'많이 빠졌다'는 bullish 의" in ATH
    assert "안정화 조건을 evidence 에 명시하라" in ATH
    assert "rating_basis=percentile" in ATH


def test_athena_context_carries_sentiment():
    from src.agents.athena import build_research_context

    ms = {"regime": {"KR": {"label": "risk_off"}},
          "sentiment": {"vix": 17.1, "fear_kr": {"score": 20.8}}}
    ctx = build_research_context("005930", "삼성전자", "KR",
                                 history_df=None, market_state=ms)
    assert ctx["sentiment"]["fear_kr"]["score"] == 20.8

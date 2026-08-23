"""gem 스크린 유닛 테스트 — 프로파일 필터·유동성 플로어·edge 게이트·정렬/상한.

실네트워크 금지: discover_fn 과 fetch 를 gem_candidates 에 직접 주입한다(monkeypatch 불필요).
합성 캔들 3종(박스형/반등형/자유낙하형)으로 _profile 분기를 구동한다. edge 게이트는
회귀 전략 백테스트가 결정적으로 양의 edge·3거래 이상을 내기 어려우므로, 프로파일·유동성·
정렬 검증에서는 gem_screen._edge 를 고정값으로 monkeypatch 해 edge 로직과 분리하고,
edge 게이트 자체의 정확성(edge<=0·거래수<3 탈락)은 별도 테스트에서 _edge 를 심볼별로
monkeypatch 해 명시적으로 검증한다(프롬프트가 허용한 실용적 분리).
"""
import copy

import numpy as np
import pandas as pd
import pytest

import src.gem_screen as GS
from src.gem_screen import gem_candidates, _profile, _gems_cfg
from src.config import load_config


# ── 합성 캔들 헬퍼(결정적 시드) ────────────────────────────────────────────
def _mk(close, volume=5_000_000.0):
    """OHLCV DataFrame 조립. volume 을 낮추면 유동성 탈락 케이스 재현."""
    close = np.asarray(close, dtype=float)
    return pd.DataFrame({
        "open": np.concatenate([[close[0]], close[:-1]]),
        "high": close * 1.01, "low": close * 0.99, "close": close,
        "volume": np.full(len(close), float(volume)),
    })


def box_df(n=80, base=70000.0, seed=7, volume=5_000_000.0):
    """A 박스형: period-20 사인 횡보 → 60봉 모멘텀 ≈ 0(방향성 없음).

    사인 주기 20 이면 index -60 과 -1 이 정확히 3주기 떨어져 같은 위상 → mom_60 ≈ 0.
    진폭 8% 로 밴드를 오가되 유동성(거래대금)은 충분히 크게.
    """
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    close = base * (1 + 0.08 * np.sin(2 * np.pi * t / 20.0)) * (1 + rng.normal(0, 0.004, n))
    return _mk(close, volume)


def rebound_df(n=80, base=70000.0, volume=5_000_000.0):
    """B 반등형: 전반부 급락(고점 대비 ~-40%) → 후반 20봉 완만한 상승/횡보(자유낙하 아님).

    최근 20일 수익률 > knife_20d_min(-0.10) 이고 낙폭이 drawdown_range 안이며
    후반 변동성이 낮아(직선 상승) 자유낙하 가드를 통과한다.
    """
    peak = base
    first = np.linspace(peak, peak * 0.58, 55)          # -42% 급락
    second = np.linspace(peak * 0.58, peak * 0.63, 25)  # 완만한 회복(+~8%)
    return _mk(np.concatenate([first, second]), volume)


def freefall_df(n=80, base=70000.0, volume=5_000_000.0):
    """자유낙하형: 낙폭 구간은 유사하나 최근까지 계속 급락(ret_20 ≤ knife_20d_min).

    연속 직선 하락 → 최근 20일 수익률이 -0.10 밑 → 자유낙하 가드에서 배제(어느 프로파일도 아님).
    """
    peak = base
    close = np.linspace(peak, peak * 0.5, n)   # 고점 대비 -50%, 최근 20일도 하락 지속
    return _mk(close, volume)


def _fetch_from(mapping):
    """{symbol: DataFrame} 매핑을 fetch(symbol, market) 콜백으로."""
    def fetch(sym, market):
        return mapping[sym]
    return fetch


def _discover(*symbols):
    """discover_fn(cfg, pool, dry) → [(market, symbol, name)] (전부 KR)."""
    def fn(cfg, pool, dry):
        return [("KR", s, f"이름_{s}") for s in symbols][:pool]
    return fn


@pytest.fixture
def cfg():
    # 매 테스트 새 cfg(gems 오버라이드가 다른 테스트에 새지 않게).
    return load_config()


@pytest.fixture
def fixed_edge(monkeypatch):
    """_edge 를 고정 (전략, edge) 로 대체 — 프로파일/유동성/정렬 로직을 edge 와 분리."""
    monkeypatch.setattr(GS, "_edge", lambda df, min_trades: ("rsi_reversion", 0.10))


# ── 1) 박스형 통과(A 프로파일) ─────────────────────────────────────────────
def test_box_candidate_passes(cfg, fixed_edge):
    assert _profile(box_df(), _gems_cfg(cfg, "KR")) == "box"   # 프로파일 사전 확인
    out = gem_candidates(cfg, "KR", _fetch_from({"BOX": box_df()}),
                         discover_fn=_discover("BOX"))
    assert [g["symbol"] for g in out] == ["BOX"]
    assert out[0]["source"] == "gem" and out[0]["strategy"] == "rsi_reversion"


# ── 2) 반등형 통과(B 프로파일) ─────────────────────────────────────────────
def test_rebound_candidate_passes(cfg, fixed_edge):
    assert _profile(rebound_df(), _gems_cfg(cfg, "KR")) == "rebound"
    out = gem_candidates(cfg, "KR", _fetch_from({"REB": rebound_df()}),
                         discover_fn=_discover("REB"))
    assert [g["symbol"] for g in out] == ["REB"]
    assert out[0]["source"] == "gem"


# ── 3) 자유낙하형 배제(어느 프로파일도 아님) ───────────────────────────────
def test_freefall_candidate_excluded(cfg, fixed_edge):
    assert _profile(freefall_df(), _gems_cfg(cfg, "KR")) is None
    out = gem_candidates(cfg, "KR", _fetch_from({"FALL": freefall_df()}),
                         discover_fn=_discover("FALL"))
    assert out == []


# ── 4) 유동성 미달 배제(프로파일이 맞아도) ─────────────────────────────────
def test_low_turnover_excluded_even_if_profile_ok(cfg, fixed_edge):
    # 박스형(프로파일 통과)이지만 거래량이 작아 평균거래대금이 플로어(1e9) 미달.
    low = box_df(volume=100.0)
    assert _profile(low, _gems_cfg(cfg, "KR")) == "box"   # 프로파일 자체는 통과
    out = gem_candidates(cfg, "KR", _fetch_from({"LOW": low}),
                         discover_fn=_discover("LOW"))
    assert out == []


# ── 5) exclude 심볼은 발굴 단계에서 제외 ───────────────────────────────────
def test_exclude_symbols_dropped_at_discovery(cfg, fixed_edge):
    dfs = {"BOX": box_df(), "REB": rebound_df()}
    # BOX 는 이미 유니버스에 있음(exclude) → REB 만 남아야 함.
    out = gem_candidates(cfg, "KR", _fetch_from(dfs),
                         discover_fn=_discover("BOX", "REB"), exclude={"BOX"})
    assert [g["symbol"] for g in out] == ["REB"]


# ── 6) edge 게이트: edge<=0 또는 거래수<3 인 종목은 탈락 ────────────────────
def test_edge_gate_rejects_nonpositive_or_thin(cfg, monkeypatch):
    # 세 종목 모두 프로파일(박스형) 통과. _edge 만 심볼별로 제어.
    # 심볼 식별은 volume(상수, 사인 진폭과 무관)로 — 종가는 사인으로 겹쳐 식별 불가.
    dfs = {
        "WIN": box_df(volume=5_000_001.0),    # edge>0, 거래 충분 → 통과
        "FLAT": box_df(volume=5_000_002.0),   # edge<=0 → 탈락
        "THIN": box_df(volume=5_000_003.0),   # 거래수<3 → _edge 가 None → 탈락
    }

    def edge_by_volume(df, min_trades):
        v = int(df["volume"].iloc[0])
        if v == 5_000_001:                    # WIN
            return ("bollinger_reversion", 0.05)
        # FLAT(edge<=0)·THIN(거래수<3) 은 _edge 계약상 None(양의 edge·min_trades 미충족)
        return None

    monkeypatch.setattr(GS, "_edge", edge_by_volume)
    out = gem_candidates(cfg, "KR", _fetch_from(dfs),
                         discover_fn=_discover("WIN", "FLAT", "THIN"))
    assert [g["symbol"] for g in out] == ["WIN"]
    assert out[0]["strategy"] == "bollinger_reversion"


# ── 7) edge 내림차순 정렬 + count_per_market 상한 ──────────────────────────
def test_sorted_by_edge_and_capped_to_count(cfg, monkeypatch):
    # KR count=5. 후보 8개 전부 박스형 통과, edge 를 심볼별로 다르게 주고 상위 5개·정렬 확인.
    # 심볼 식별은 volume(상수)로 — edge = volume 하위 자리(0..7). 큰 edge 가 위로.
    syms = [f"G{i}" for i in range(8)]
    dfs = {s: box_df(volume=5_000_000.0 + i) for i, s in enumerate(syms)}

    def edge_by_volume(df, min_trades):
        idx = int(df["volume"].iloc[0]) - 5_000_000
        return ("rsi_reversion", float(idx))

    monkeypatch.setattr(GS, "_edge", edge_by_volume)
    out = gem_candidates(cfg, "KR", _fetch_from(dfs), discover_fn=_discover(*syms))
    assert len(out) == 5                       # count_per_market(KR)=5 상한
    # edge 큰 순 = index 큰 순 = G7,G6,G5,G4,G3
    assert [g["symbol"] for g in out] == ["G7", "G6", "G5", "G4", "G3"]


# ── 8) strategy 필드가 최고 edge 전략명과 일치 ─────────────────────────────
def test_strategy_field_matches_best_edge_strategy(cfg, monkeypatch):
    dfs = {"BOX": box_df()}
    monkeypatch.setattr(GS, "_edge", lambda df, mt: ("bollinger_reversion", 0.42))
    out = gem_candidates(cfg, "KR", _fetch_from(dfs), discover_fn=_discover("BOX"))
    assert out[0]["strategy"] == "bollinger_reversion"


# ── 보강: count_per_market 시장별 값(US=3) ─────────────────────────────────
def test_us_count_cap_is_three(cfg, monkeypatch):
    syms = [f"U{i}" for i in range(6)]
    dfs = {s: box_df(base=100.0 + i * 2.0, volume=5_000_000.0) for i, s in enumerate(syms)}

    def disc(cfg, pool, dry):
        return [("US", s, s) for s in syms][:pool]

    monkeypatch.setattr(GS, "_edge", lambda df, mt: ("rsi_reversion", 0.1))
    out = gem_candidates(cfg, "US", _fetch_from(dfs), discover_fn=disc)
    assert len(out) == 3                       # count_per_market(US)=3


# ── 보강: 짧은 히스토리(<60봉)는 스킵 ──────────────────────────────────────
def test_short_history_skipped(cfg, fixed_edge):
    short = _mk(np.full(40, 70000.0))          # 40봉 < 60 → _profile 이전 단계에서 스킵
    out = gem_candidates(cfg, "KR", _fetch_from({"SHORT": short}),
                         discover_fn=_discover("SHORT"))
    assert out == []


# ── 보강: fetch 예외는 심볼 단위 스킵(스크린 전체가 죽지 않음) ──────────────
def test_fetch_exception_skips_symbol(cfg, fixed_edge):
    good = box_df()

    def fetch(sym, market):
        if sym == "BAD":
            raise RuntimeError("yahoo 500")
        return good

    out = gem_candidates(cfg, "KR", fetch, discover_fn=_discover("BAD", "GOOD"))
    assert [g["symbol"] for g in out] == ["GOOD"]


# ── 보강: 발굴 예외는 빈 리스트 반환 ───────────────────────────────────────
def test_discover_exception_returns_empty(cfg, fixed_edge):
    def boom(cfg, pool, dry):
        raise RuntimeError("naver down")

    assert gem_candidates(cfg, "KR", _fetch_from({}), discover_fn=boom) == []

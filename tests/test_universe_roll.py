"""롤링 유니버스 순수 로직 — core_refresh(개장 전 리프레시)·mover_scan(장중 add-only).

발굴(discover_kr/us)·히스토리(_fetch_fn)·store·OUT 경로를 전부 주입/monkeypatch 한다 —
실네트워크 금지. OUT 은 tmp_path 로 리다이렉트해 production data/universe.yaml 격리.

무버는 도시에가 없으므로 뇌의 no_dossier 게이트(require_dossier)가 자동으로 무버를
데이트레(armed) 전용으로 강제한다 — 별도 게이트 코드가 없음을 여기서 상기한다(코드로
검증하는 대상은 pipeline/dossier 테스트이고, 여기선 layer 태깅만 검증).
"""
import numpy as np
import pandas as pd
import pytest
import yaml

import src.universe_roll as UR
from src.config import load_config


# ── 공통 픽스처: OUT 을 tmp 로, 발굴/히스토리를 합성으로 주입 ──────────────
def _synth_df(seed_base=70000.0, n=80, up=True):
    """스크리너 하드필터(KR min_price 2000·min_turnover 5e9)를 통과하는 합성 캔들.

    기본가 70000·거래량 5M → 거래대금 ~3.5e11. 미세 상승 추세(변동성/모멘텀 랭킹용). 낮은
    가격(seed_base<필터)을 주면 passes_filters 탈락 케이스 재현.
    """
    rng = np.random.default_rng(abs(hash((seed_base, n))) % (2**32))
    drift = 0.001 if up else -0.001
    close = seed_base * np.cumprod(1 + rng.normal(drift, 0.02, n))
    return pd.DataFrame({
        "open": np.concatenate([[close[0]], close[:-1]]),
        "high": close * 1.01, "low": close * 0.99, "close": close,
        "volume": np.full(n, 5_000_000),
    })


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture(autouse=True)
def _redirect_out(tmp_path, monkeypatch):
    """OUT 을 tmp 파일로 — 실 유니버스 파일 오염 방지."""
    monkeypatch.setattr(UR, "OUT", tmp_path / "universe.yaml")
    return tmp_path / "universe.yaml"


def _inject_discovery(monkeypatch, kr=None, us=None):
    """discover_kr/us 를 고정 후보로 대체하고, _DISCOVER 매핑도 갱신."""
    kr = kr if kr is not None else [("KR", f"KR{i}", f"이름{i}") for i in range(8)]
    us = us if us is not None else [("US", s, s) for s in ("AAA", "BBB", "CCC", "DDD")]
    dk = lambda cfg, count, dry: kr[:count]
    du = lambda cfg, count, dry: us[:count]
    monkeypatch.setattr(UR, "discover_kr", dk)
    monkeypatch.setattr(UR, "discover_us", du)
    monkeypatch.setattr(UR, "_DISCOVER", {"KR": dk, "US": du})


def _inject_fetch(monkeypatch, price=None, up=True):
    """_fetch_fn 이 항상 합성 캔들을 주도록 대체.

    price=None 이면 시장별 필터 통과가로 자동(KR 70000·US 100). 명시하면 그 값 고정
    (탈락 케이스 재현용).
    """
    def fetch(s, m):
        base = price if price is not None else (70000.0 if m == "KR" else 100.0)
        return _synth_df(base, up=up)
    monkeypatch.setattr(UR, "_fetch_fn", lambda cfg, dry: fetch)


# ── 가짜 store ─────────────────────────────────────────────────────────────
class _Row(dict):
    def __getitem__(self, k):
        return dict.get(self, k)


class FakeStore:
    def __init__(self, *, held=None, armed=None, dossiers=None):
        self._held = held or []       # list of {"symbol","market"}
        self._armed = armed or []
        self._dossiers = dossiers or {}   # symbol -> {"evidence": '{"stance":...}'}

    def get_open_positions(self):
        return [_Row(r) for r in self._held]

    def get_armed(self):
        return [_Row(r) for r in self._armed]

    def get_fresh_dossier(self, symbol, now=None):
        d = self._dossiers.get(symbol)
        return _Row(d) if d else None


# ── core_refresh ───────────────────────────────────────────────────────────
def test_core_refresh_writes_layer_and_added_at(cfg, monkeypatch, _redirect_out):
    _inject_discovery(monkeypatch)
    _inject_fetch(monkeypatch)
    out = UR.core_refresh(cfg, "KR")
    assert out is not None
    data = yaml.safe_load(_redirect_out.read_text(encoding="utf-8"))
    assert data["KR"], "KR 종목이 실려야 함"
    for it in data["KR"]:
        assert it["layer"] == "core"
        assert isinstance(it["added_at"], float)
        assert it.get("strategy")


def test_core_refresh_replaces_only_that_market(cfg, monkeypatch, _redirect_out):
    # 기존 파일에 US 가 있고 KR 만 리프레시 → US 보존, KR 교체.
    _redirect_out.write_text(yaml.safe_dump(
        {"KR": [{"symbol": "OLD_KR", "layer": "core"}],
         "US": [{"symbol": "KEEP_US", "layer": "core"}]}), encoding="utf-8")
    _inject_discovery(monkeypatch)
    _inject_fetch(monkeypatch)
    UR.core_refresh(cfg, "KR")
    data = yaml.safe_load(_redirect_out.read_text(encoding="utf-8"))
    assert data["US"] == [{"symbol": "KEEP_US", "layer": "core"}]   # 타 시장 그대로
    kr_syms = {it["symbol"] for it in data["KR"]}
    assert "OLD_KR" not in kr_syms                                  # KR 교체됨


def test_core_refresh_picks_scale_applied(cfg, monkeypatch, _redirect_out):
    # picks_scale US=1.5 → 각 전략 count*1.5 반올림. 후보를 넉넉히 줘서 상한이 안 걸리게.
    us = [("US", f"U{i}", f"U{i}") for i in range(60)]
    _inject_discovery(monkeypatch, us=us)
    _inject_fetch(monkeypatch)
    # 기본 picks 각 count=4 → US 는 round(4*1.5)=6 씩 3전략 = 18 목표.
    UR.core_refresh(cfg, "US")
    data = yaml.safe_load(_redirect_out.read_text(encoding="utf-8"))
    assert len(data["US"]) == 18, f"US scale 반영 실패: {len(data['US'])}"


def test_core_refresh_kr_no_scale(cfg, monkeypatch, _redirect_out):
    kr = [("KR", f"K{i}", f"K{i}") for i in range(60)]
    _inject_discovery(monkeypatch, kr=kr)
    _inject_fetch(monkeypatch)
    UR.core_refresh(cfg, "KR")
    data = yaml.safe_load(_redirect_out.read_text(encoding="utf-8"))
    assert len(data["KR"]) == 12, "KR scale=1.0 → 4*3=12"


def test_core_refresh_retains_held(cfg, monkeypatch, _redirect_out):
    # 보유 종목(HELD)이 새 스크린 리스트에 없어도 유지된다.
    _redirect_out.write_text(yaml.safe_dump(
        {"KR": [{"symbol": "HELD", "layer": "core", "strategy": "ma_crossover",
                 "added_at": 111.0}]}), encoding="utf-8")
    _inject_discovery(monkeypatch)      # 발굴엔 HELD 없음
    _inject_fetch(monkeypatch)
    store = FakeStore(held=[{"symbol": "HELD", "market": "KR"}])
    UR.core_refresh(cfg, "KR", store=store)
    data = yaml.safe_load(_redirect_out.read_text(encoding="utf-8"))
    syms = {it["symbol"] for it in data["KR"]}
    assert "HELD" in syms
    held = next(it for it in data["KR"] if it["symbol"] == "HELD")
    assert held["added_at"] == 111.0        # 기존 태그 보존
    assert held["layer"] == "core"


def test_core_refresh_retains_armed(cfg, monkeypatch, _redirect_out):
    _redirect_out.write_text(yaml.safe_dump(
        {"KR": [{"symbol": "ARMED", "layer": "mover"}]}), encoding="utf-8")
    _inject_discovery(monkeypatch)
    _inject_fetch(monkeypatch)
    store = FakeStore(armed=[{"symbol": "ARMED", "market": "KR"}])
    UR.core_refresh(cfg, "KR", store=store)
    data = yaml.safe_load(_redirect_out.read_text(encoding="utf-8"))
    assert "ARMED" in {it["symbol"] for it in data["KR"]}


def test_core_refresh_retains_bullish_dossier_nonheld(cfg, monkeypatch, _redirect_out):
    # 비보유라도 신선한 강세 도시에가 있으면 유지 — 연구 자본 보존(갭 가드 존 대기 후보가
    # 뇌 후보에서 사라지지 않게). 도시에 TTL 만료 시 자연 소멸.
    _inject_discovery(monkeypatch, kr=[("KR", "NEW1", "새종목")])
    _inject_fetch(monkeypatch)
    _redirect_out.write_text(yaml.safe_dump(
        {"KR": [{"symbol": "GEMX", "name": "도시에만", "strategy": "rsi_reversion",
                 "layer": "core", "added_at": 111.0}]}, allow_unicode=True),
        encoding="utf-8")
    store = FakeStore(dossiers={"GEMX": {"evidence": '{"stance": "bullish"}'}})
    assert UR.core_refresh(cfg, "KR", store=store) is not None
    data = yaml.safe_load(_redirect_out.read_text(encoding="utf-8"))
    syms = {it["symbol"] for it in data["KR"]}
    assert "GEMX" in syms and "NEW1" in syms
    gemx = next(it for it in data["KR"] if it["symbol"] == "GEMX")
    assert gemx["added_at"] == 111.0            # 기존 태그 보존


def test_core_refresh_drops_neutral_dossier_nonheld(cfg, monkeypatch, _redirect_out):
    # 중립(neutral) 도시에만 있는 비보유 종목은 보존 대상 아님(강세만 연구 자본으로 취급).
    _inject_discovery(monkeypatch, kr=[("KR", "NEW1", "새종목")])
    _inject_fetch(monkeypatch)
    _redirect_out.write_text(yaml.safe_dump(
        {"KR": [{"symbol": "MEHX", "name": "중립도시에", "strategy": "rsi_reversion",
                 "layer": "core", "added_at": 111.0}]}, allow_unicode=True),
        encoding="utf-8")
    store = FakeStore(dossiers={"MEHX": {"evidence": '{"stance": "neutral"}'}})
    assert UR.core_refresh(cfg, "KR", store=store) is not None
    data = yaml.safe_load(_redirect_out.read_text(encoding="utf-8"))
    assert "MEHX" not in {it["symbol"] for it in data["KR"]}


def test_core_refresh_retains_bullish_dossier_holding(cfg, monkeypatch, _redirect_out):
    # 강세(bullish) 도시에가 있는 '보유' 종목은 새 리스트에 없어도 유지.
    _redirect_out.write_text(yaml.safe_dump(
        {"KR": [{"symbol": "BULL", "layer": "core"}]}), encoding="utf-8")
    _inject_discovery(monkeypatch)
    _inject_fetch(monkeypatch)
    store = FakeStore(held=[{"symbol": "BULL", "market": "KR"}],
                      dossiers={"BULL": {"evidence": '{"stance": "bullish"}'}})
    UR.core_refresh(cfg, "KR", store=store)
    data = yaml.safe_load(_redirect_out.read_text(encoding="utf-8"))
    assert "BULL" in {it["symbol"] for it in data["KR"]}


def test_core_refresh_no_store_skips_retention(cfg, monkeypatch, _redirect_out):
    # store=None(수동 CLI) → 보존 생략: 기존 종목이 새 리스트에 없으면 사라진다.
    _redirect_out.write_text(yaml.safe_dump(
        {"KR": [{"symbol": "GONE", "layer": "core"}]}), encoding="utf-8")
    _inject_discovery(monkeypatch)
    _inject_fetch(monkeypatch)
    UR.core_refresh(cfg, "KR", store=None)
    data = yaml.safe_load(_redirect_out.read_text(encoding="utf-8"))
    assert "GONE" not in {it["symbol"] for it in data["KR"]}


def test_core_refresh_movers_vanish(cfg, monkeypatch, _redirect_out):
    # 그 시장의 기존 무버는 코어 리프레시 시 새 리스트에 안 실려 자동 소멸(보존 대상 아님).
    _redirect_out.write_text(yaml.safe_dump(
        {"KR": [{"symbol": "MOVER1", "layer": "mover"},
                {"symbol": "MOVER2", "layer": "mover"}]}), encoding="utf-8")
    _inject_discovery(monkeypatch)
    _inject_fetch(monkeypatch)
    store = FakeStore()      # 무버는 보유·armed·도시에 없음 → 보존 안 됨
    UR.core_refresh(cfg, "KR", store=store)
    data = yaml.safe_load(_redirect_out.read_text(encoding="utf-8"))
    syms = {it["symbol"] for it in data["KR"]}
    assert "MOVER1" not in syms and "MOVER2" not in syms


def test_core_refresh_zero_picks_keeps_existing(cfg, monkeypatch, _redirect_out):
    # 선정 0종목(전 후보 필터 탈락)이면 기존 파일을 건드리지 않고 None 반환.
    _redirect_out.write_text(yaml.safe_dump(
        {"KR": [{"symbol": "KEEP", "layer": "core"}]}), encoding="utf-8")
    _inject_discovery(monkeypatch)
    # min_price 를 매우 높게 → 모든 후보 필터 탈락(compute_metrics 는 통과, passes_filters 탈락).
    monkeypatch.setattr(UR, "_fetch_fn",
                        lambda cfg, dry: (lambda s, m: _synth_df(1.0)))   # 가격 ~1
    out = UR.core_refresh(cfg, "KR")
    assert out is None
    data = yaml.safe_load(_redirect_out.read_text(encoding="utf-8"))
    assert data["KR"] == [{"symbol": "KEEP", "layer": "core"}]      # 무변경


def test_core_refresh_no_candidates_keeps_existing(cfg, monkeypatch, _redirect_out):
    _redirect_out.write_text(yaml.safe_dump(
        {"KR": [{"symbol": "KEEP", "layer": "core"}]}), encoding="utf-8")
    _inject_discovery(monkeypatch, kr=[])   # 발굴 0
    _inject_fetch(monkeypatch)
    assert UR.core_refresh(cfg, "KR") is None
    data = yaml.safe_load(_redirect_out.read_text(encoding="utf-8"))
    assert data["KR"] == [{"symbol": "KEEP", "layer": "core"}]


def test_core_refresh_atomic_valid_yaml(cfg, monkeypatch, _redirect_out):
    # 쓰기 후 파일이 항상 유효 yaml(원자적 tmp+replace) — tmp 잔여물 없음.
    _inject_discovery(monkeypatch)
    _inject_fetch(monkeypatch)
    UR.core_refresh(cfg, "KR")
    # 유효 yaml 로 파싱되어야 함(반쪽 쓰기 아님).
    data = yaml.safe_load(_redirect_out.read_text(encoding="utf-8"))
    assert isinstance(data, dict) and data["KR"]
    assert not (_redirect_out.parent / "universe.yaml.tmp").exists()   # tmp 잔여 없음


# ── mover_scan ─────────────────────────────────────────────────────────────
def test_mover_scan_adds_only_new(cfg, monkeypatch, _redirect_out):
    # 랭킹 상위에 기존(EXIST) + 신규(NEW1,NEW2). 신규만 편입.
    _redirect_out.write_text(yaml.safe_dump(
        {"KR": [{"symbol": "EXIST", "layer": "core"}]}), encoding="utf-8")
    _inject_discovery(monkeypatch, kr=[("KR", "EXIST", "e"), ("KR", "NEW1", "n1"),
                                       ("KR", "NEW2", "n2")])
    _inject_fetch(monkeypatch)
    added = UR.mover_scan(cfg, "KR")
    assert set(added) == {"NEW1", "NEW2"}
    data = yaml.safe_load(_redirect_out.read_text(encoding="utf-8"))
    syms = [it["symbol"] for it in data["KR"]]
    assert syms[0] == "EXIST"                       # 기존 항목 그대로(add-only, 맨 앞 유지)
    for it in data["KR"]:
        if it["symbol"] in ("NEW1", "NEW2"):
            assert it["layer"] == "mover" and isinstance(it["added_at"], float)


def test_mover_scan_never_removes_existing(cfg, monkeypatch, _redirect_out):
    _redirect_out.write_text(yaml.safe_dump(
        {"KR": [{"symbol": "A", "layer": "core"}, {"symbol": "B", "layer": "mover"}]}),
        encoding="utf-8")
    _inject_discovery(monkeypatch, kr=[("KR", "NEW", "n")])
    _inject_fetch(monkeypatch)
    UR.mover_scan(cfg, "KR")
    data = yaml.safe_load(_redirect_out.read_text(encoding="utf-8"))
    syms = {it["symbol"] for it in data["KR"]}
    assert {"A", "B"}.issubset(syms)                # 기존 무제거


def test_mover_scan_per_scan_cap(cfg, monkeypatch, _redirect_out):
    # movers_per_scan=3(config 기본) → 신규 후보가 많아도 스캔당 3개만.
    _redirect_out.write_text(yaml.safe_dump({"KR": []}), encoding="utf-8")
    cands = [("KR", f"N{i}", f"n{i}") for i in range(10)]
    _inject_discovery(monkeypatch, kr=cands)
    _inject_fetch(monkeypatch)
    added = UR.mover_scan(cfg, "KR")
    assert len(added) == 3


def test_mover_scan_total_cap_per_market(cfg, monkeypatch, _redirect_out):
    # mover_max_per_market=6. 이미 무버 5개 → 이번 스캔은 1개만 편입(남은 슬롯).
    existing = [{"symbol": f"M{i}", "layer": "mover"} for i in range(5)]
    _redirect_out.write_text(yaml.safe_dump({"KR": existing}), encoding="utf-8")
    cands = [("KR", f"N{i}", f"n{i}") for i in range(10)]
    _inject_discovery(monkeypatch, kr=cands)
    _inject_fetch(monkeypatch)
    added = UR.mover_scan(cfg, "KR")
    assert len(added) == 1                          # 6-5=1 슬롯만
    data = yaml.safe_load(_redirect_out.read_text(encoding="utf-8"))
    assert sum(1 for it in data["KR"] if it["layer"] == "mover") == 6


def test_mover_scan_total_cap_full_noop(cfg, monkeypatch, _redirect_out):
    existing = [{"symbol": f"M{i}", "layer": "mover"} for i in range(6)]
    _redirect_out.write_text(yaml.safe_dump({"KR": existing}), encoding="utf-8")
    _inject_discovery(monkeypatch, kr=[("KR", "N", "n")])
    _inject_fetch(monkeypatch)
    assert UR.mover_scan(cfg, "KR") == []           # 상한 가득 → 무편입


def test_mover_scan_filter_rejects(cfg, monkeypatch, _redirect_out):
    # 하드필터(min_price) 탈락 종목은 편입 안 됨.
    _redirect_out.write_text(yaml.safe_dump({"KR": []}), encoding="utf-8")
    _inject_discovery(monkeypatch, kr=[("KR", "CHEAP", "c")])
    monkeypatch.setattr(UR, "_fetch_fn",
                        lambda cfg, dry: (lambda s, m: _synth_df(1.0)))   # 가격 ~1 < min_price
    assert UR.mover_scan(cfg, "KR") == []


def test_mover_scan_history_failure_skipped(cfg, monkeypatch, _redirect_out):
    # 히스토리 실패(예외) 심볼은 스킵(보수적), 성공분만 편입.
    _redirect_out.write_text(yaml.safe_dump({"KR": []}), encoding="utf-8")
    _inject_discovery(monkeypatch, kr=[("KR", "BAD", "b"), ("KR", "GOOD", "g")])

    def fetch(s, m):
        if s == "BAD":
            raise RuntimeError("yahoo 500")
        return _synth_df(70000.0)

    monkeypatch.setattr(UR, "_fetch_fn", lambda cfg, dry: fetch)
    added = UR.mover_scan(cfg, "KR")
    assert added == ["GOOD"]


def test_mover_scan_no_new_noop(cfg, monkeypatch, _redirect_out):
    _redirect_out.write_text(yaml.safe_dump(
        {"KR": [{"symbol": "A", "layer": "core"}]}), encoding="utf-8")
    _inject_discovery(monkeypatch, kr=[("KR", "A", "a")])   # 전부 기존
    _inject_fetch(monkeypatch)
    assert UR.mover_scan(cfg, "KR") == []


# ── core_refresh × gem 버킷 통합 ────────────────────────────────────────────
# gem 내부 로직(프로파일/edge)은 test_gem_screen.py 가 담당. 여기선 core_refresh 가
# gem_candidates 결과를 layer="core"·source="gem" 으로 append 하고, enabled=false·예외
# 상황을 올바르게 처리하는 통합 배선만 UR.gem_candidates 를 monkeypatch 해 검증한다.
def test_core_refresh_appends_gem_bucket(cfg, monkeypatch, _redirect_out):
    _inject_discovery(monkeypatch)
    _inject_fetch(monkeypatch)
    monkeypatch.setattr(UR, "gem_candidates", lambda *a, **k: [
        {"symbol": "GEM1", "name": "gem종목", "strategy": "rsi_reversion", "source": "gem"}])
    out = UR.core_refresh(cfg, "KR")
    assert out is not None
    data = yaml.safe_load(_redirect_out.read_text(encoding="utf-8"))
    gem = next((it for it in data["KR"] if it["symbol"] == "GEM1"), None)
    assert gem is not None, "gem 후보가 유니버스에 실려야 함"
    assert gem["layer"] == "core" and gem["source"] == "gem"
    assert isinstance(gem["added_at"], float)
    # 메인 선정과 심볼 중복 없음(방어적 중복 제거).
    syms = [it["symbol"] for it in data["KR"]]
    assert len(syms) == len(set(syms))


def test_core_refresh_gem_dedup_against_main(cfg, monkeypatch, _redirect_out):
    # gem 이 메인 선정과 겹치는 심볼을 돌려줘도 방어적으로 제거된다(중복 append 안 함).
    _inject_discovery(monkeypatch, kr=[("KR", "KR0", "이름0")])
    _inject_fetch(monkeypatch)
    captured = {}

    def fake_gem(cfg_, market, fetch, *, exclude=None, dry=False):
        captured["exclude"] = set(exclude or set())
        return [{"symbol": "KR0", "name": "이름0", "strategy": "rsi_reversion",
                 "source": "gem"}]     # 메인 선정 KR0 과 동일 → 제거 대상

    monkeypatch.setattr(UR, "gem_candidates", fake_gem)
    UR.core_refresh(cfg, "KR")
    data = yaml.safe_load(_redirect_out.read_text(encoding="utf-8"))
    assert "KR0" in captured["exclude"]                 # 메인 선정이 exclude 로 전달됨
    kr0 = [it for it in data["KR"] if it["symbol"] == "KR0"]
    assert len(kr0) == 1 and kr0[0].get("source") != "gem"   # 메인 선정본만(중복 gem 제거)


def test_core_refresh_gems_disabled_skips_gem(cfg, monkeypatch, _redirect_out):
    cfg.raw["screener"]["gems"]["enabled"] = False
    _inject_discovery(monkeypatch)
    _inject_fetch(monkeypatch)
    calls = {"n": 0}

    def spy(*a, **k):
        calls["n"] += 1
        return [{"symbol": "GEM1", "name": "g", "strategy": "rsi_reversion", "source": "gem"}]

    monkeypatch.setattr(UR, "gem_candidates", spy)
    UR.core_refresh(cfg, "KR")
    assert calls["n"] == 0                              # enabled=false → 호출 안 함
    data = yaml.safe_load(_redirect_out.read_text(encoding="utf-8"))
    assert "GEM1" not in {it["symbol"] for it in data["KR"]}
    assert not any(it.get("source") == "gem" for it in data["KR"])


def test_core_refresh_gem_exception_proceeds_without_gem(cfg, monkeypatch, _redirect_out):
    _inject_discovery(monkeypatch)
    _inject_fetch(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("gem 스크린 폭발")

    monkeypatch.setattr(UR, "gem_candidates", boom)
    out = UR.core_refresh(cfg, "KR")                    # 예외 없이 정상 완료
    assert out is not None
    data = yaml.safe_load(_redirect_out.read_text(encoding="utf-8"))
    assert data["KR"], "메인 선정 결과는 그대로 반영됨"
    assert not any(it.get("source") == "gem" for it in data["KR"])   # gem 없이 진행


def test_sort_core_by_turnover_reorders_without_dropping(monkeypatch, _redirect_out):
    _redirect_out.write_text(yaml.safe_dump({
        "KR": [{"symbol": "A", "layer": "core"},
               {"symbol": "B", "layer": "core"},
               {"symbol": "C", "layer": "core"}],
    }), encoding="utf-8")

    def cache(market):
        return [{"symbol": "C", "trading_value": 3},
                {"symbol": "A", "trading_value": 1}]

    monkeypatch.setattr("src.datasources.discovery._load_ranking_cache", cache)
    out = UR.sort_core_by_turnover("KR")
    assert [it["symbol"] for it in out["KR"]] == ["C", "A", "B"]  # 캐시 없는 B는 맨 뒤


def test_static_universe_without_operator_config(monkeypatch, tmp_path):
    """클론/CI 에는 config.yaml 이 없다. example 로 폴백해야 한다."""
    monkeypatch.setattr(UR, "default_config_path", lambda: tmp_path / "missing.yaml")
    static = UR._static_universe(None)
    assert any(it.get("symbol") == "005930" for it in (static.get("KR") or []))

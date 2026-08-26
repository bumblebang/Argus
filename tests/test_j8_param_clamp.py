"""J8 — 파라미터 클램프가 스키마 키만 먹고 NaN 은 통과하던 결함.

백로그 재현(수정 전 실패 조건):
  - validate_params('rsi_reversion', {stop_loss_pct: 0.99}) -> 0.99 통과
  - validate_params('volatility_breakout', {stop_loss_pct: nan}) -> nan 통과
  - entry_stop_target(10000, swing, {0.99}) -> stop=100
  - position_triggers(stop_price=nan, price=1) -> []  (손절 무발화)
  - check_price(1, {price: nan}) -> None
  - Proposal(params={stop_loss_pct: nan}) -> 수용
"""
import math

import pytest

from src.agents.schemas import DossierOutput, Proposal
from src.agents.wiring import entry_stop_target
from src.engine.triggers import position_triggers
from src.strategies import REGISTRY, validate_params
from src.thesis_watch import check_price, parse_invalidation_spec

NAN = float("nan")
INF = float("inf")


def _proposal(**kw):
    base = dict(symbol="005930", market="KR", side="BUY", conviction=0.7,
                target_weight=0.1, thesis="t")
    base.update(kw)
    return Proposal(**base)


# ── 스키마 밖 stop_loss_pct ─────────────────────────────────────
def test_stop_loss_pct_clamped_on_strategy_without_own_spec():
    """rsi_reversion 은 PARAMS 에 stop_loss_pct 가 없다 — COMMON_PARAMS 가 잡아야 한다."""
    assert "stop_loss_pct" not in {s.name for s in REGISTRY["rsi_reversion"].PARAMS}
    params, viol = validate_params("rsi_reversion", {"stop_loss_pct": 0.99})
    assert params["stop_loss_pct"] == 0.30          # 공통 상한
    assert any("stop_loss_pct" in v for v in viol)


def test_all_strategies_clamp_stop_loss_pct():
    for name in REGISTRY:
        params, _ = validate_params(name, {"stop_loss_pct": 0.99})
        assert params["stop_loss_pct"] <= 0.30, name


def test_strategy_own_spec_stays_tighter():
    """volatility_breakout 자체 상한 0.20 이 공통 0.30 보다 우선."""
    params, _ = validate_params("volatility_breakout", {"stop_loss_pct": 0.99})
    assert params["stop_loss_pct"] == 0.20


def test_absent_stop_loss_pct_is_not_injected():
    """부재 시 기본을 끼워 넣으면 보유기간별 폴백이 죽는다."""
    params, _ = validate_params("rsi_reversion", {})
    assert "stop_loss_pct" not in params


# ── NaN / Inf ──────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_non_finite_falls_back_to_default(bad):
    params, viol = validate_params("volatility_breakout", {"stop_loss_pct": bad})
    assert params["stop_loss_pct"] == 0.02          # 스펙 default
    assert any("stop_loss_pct" in v for v in viol)


def test_unknown_key_dropped_with_violation():
    params, viol = validate_params("rsi_reversion", {"보안관없는키": 1.0})
    assert "보안관없는키" not in params
    assert any("보안관없는키" in v for v in viol)


def test_passthrough_allowlist_survives():
    params, _ = validate_params("volatility_breakout",
                                {"candle_interval": "1m", "exit_at_session_end": True})
    assert params["candle_interval"] == "1m"
    assert params["exit_at_session_end"] is True


# ── entry_stop_target 최종 방어선 (validate 를 안 거친 store meta) ──
def test_entry_stop_target_rejects_out_of_range_pct():
    stop, _ = entry_stop_target(10_000, "swing", {"stop_loss_pct": 0.99})
    assert stop == 9_500.0                          # swing 기본 5%


def test_entry_stop_target_rejects_nan_pct():
    stop, target = entry_stop_target(10_000, "swing", {"stop_loss_pct": NAN,
                                                       "target_profit_pct": NAN})
    assert stop == 9_500.0 and target == 11_000.0
    assert math.isfinite(stop) and math.isfinite(target)


def test_entry_stop_target_keeps_valid_pct():
    stop, target = entry_stop_target(10_000, "swing", {"stop_loss_pct": 0.03,
                                                       "target_profit_pct": 0.08})
    assert (stop, target) == (9_700.0, 10_800.0)


# ── 트리거·무효화 (손절 무발화 재현) ─────────────────────────────
def test_nan_stop_does_not_silently_swallow_trigger():
    pos = {"symbol": "005930", "stop_price": NAN}
    trigs = position_triggers(pos, price=1.0)
    kinds = {t.kind for t in trigs}
    assert "stop_invalid" in kinds                  # 조용히 [] 가 아니라 신호를 낸다


def test_finite_stop_still_fires():
    pos = {"symbol": "005930", "stop_price": 100.0}
    kinds = {t.kind for t in position_triggers(pos, price=90.0)}
    assert kinds == {"stop_hit"}


def test_check_price_ignores_nan_limit():
    spec = parse_invalidation_spec({"thesis_invalidation": {"price": NAN}})
    assert "price" not in spec                      # 애초에 실리지 않는다
    assert check_price(1.0, {"price": NAN}, "005930") is None


def test_check_price_still_fires_on_valid_limit():
    hit = check_price(90.0, {"price": 100.0}, "005930")
    assert hit is not None and hit.kind == "price"


# ── 스키마 경계 ─────────────────────────────────────────────────
def test_proposal_drops_non_finite_params():
    p = _proposal(strategy="rsi_reversion", params={"stop_loss_pct": NAN, "period": 14})
    assert p.params == {"period": 14.0}


def test_proposal_drops_inf_params():
    p = _proposal(params={"x": INF})
    assert p.params == {}


def test_dossier_non_finite_levels_become_none():
    d = DossierOutput(stance="bullish", thesis="t", entry_low=NAN,
                      entry_high=1050, invalidation=NAN, target=1200, conviction=0.8)
    assert d.entry_low is None and d.invalidation is None
    assert d.entry_high == 1050 and d.target == 1200

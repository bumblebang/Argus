"""전략 레지스트리 + 파라미터 카탈로그/검증.

전략 = 파라미터화 템플릿(코드, 고정) + 그 파라미터(LLM 이 범위 내에서 선택).
카탈로그는 뇌가 "어떤 전략·어떤 파라미터를 쓸 수 있나"를 읽는 단일 출처이고,
validate_params 는 LLM 이 고른 값을 하드 바운드로 클램프한다(하드 가드).
"""
from __future__ import annotations

from ..logging_setup import get_logger
from .base import Strategy, Signal, Action, Position, ParamSpec
from .volatility_breakout import VolatilityBreakout
from .ma_crossover import MACrossover
from .rsi_reversion import RSIReversion
from .donchian_breakout import DonchianBreakout
from .momentum import Momentum
from .macd_cross import MACDCross
from .bollinger_reversion import BollingerReversion
from .bollinger_breakout import BollingerBreakout

log = get_logger("strategies")

REGISTRY: dict[str, type[Strategy]] = {
    s.name: s for s in (
        # 데이트레(day)
        VolatilityBreakout, BollingerBreakout,
        # 단기 스윙(swing)
        RSIReversion, MACDCross, BollingerReversion,
        # 중장기 추세(position)
        MACrossover, DonchianBreakout, Momentum,
    )
}


def build_strategy(name: str, params: dict) -> Strategy:
    if name not in REGISTRY:
        raise KeyError(f"알 수 없는 전략: {name}. 사용 가능: {list(REGISTRY)}")
    strat = REGISTRY[name](params)
    if strat.param_violations:        # LLM/설정이 범위 밖 값을 줬다면 클램프하고 기록
        log.warning("[%s] 파라미터 클램프: %s", name, "; ".join(strat.param_violations))
    return strat


def validate_params(name: str, params: dict) -> tuple[dict, list[str]]:
    """LLM 이 고른 파라미터를 인스턴스 생성 없이 클램프(검증 에이전트/사전점검용)."""
    if name not in REGISTRY:
        return {}, [f"알 수 없는 전략: {name}"]
    return REGISTRY[name].validate(params or {})


def strategy_catalog() -> list[dict]:
    """전체 전략 카탈로그(이름·설명·파라미터 범위). 뇌가 전략·파라미터 선택 시 입력."""
    return [cls.describe() for cls in REGISTRY.values()]


__all__ = ["Strategy", "Signal", "Action", "Position", "ParamSpec", "build_strategy",
           "validate_params", "strategy_catalog", "REGISTRY"]

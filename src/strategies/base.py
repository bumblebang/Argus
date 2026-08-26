"""전략 공통 인터페이스."""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

import pandas as pd


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class Position:
    symbol: str
    qty: float = 0.0
    avg_price: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.qty > 0


@dataclass
class Signal:
    action: Action
    reason: str
    # 0.0~1.0: 이 종목에 배정된 예산 중 사용할 비중. None 이면 리스크 기본값 사용.
    target_weight: float | None = None


@dataclass(frozen=True)
class ParamSpec:
    """전략 파라미터 1개의 명세 + 하드 바운드.

    LLM 은 이 범위 안에서만 값을 고를 수 있고, 코드가 clamp 로 강제한다(하드 가드).
    범위 밖/비정상값은 경계로 잘리고 위반 메시지를 남긴다 → 검증 에이전트가 참고.
    """
    name: str
    default: float
    min: float
    max: float
    kind: str = "float"     # "float" | "int"
    desc: str = ""

    def coerce(self, value) -> tuple[float, str | None]:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return self._cast(self.default), f"{self.name}: 비정상값 {value!r} → 기본 {self.default}"
        # NaN/Inf 는 min/max 비교가 둘 다 False 라 경계로 잘리지 않는다 — 먼저 거른다.
        if not math.isfinite(v):
            return self._cast(self.default), f"{self.name}: 비유한값 {value!r} → 기본 {self.default}"
        if v < self.min:
            return self._cast(self.min), f"{self.name}: {v} < 최소 {self.min} → {self.min}"
        if v > self.max:
            return self._cast(self.max), f"{self.name}: {v} > 최대 {self.max} → {self.max}"
        return self._cast(v), None

    def _cast(self, v: float):
        return int(round(v)) if self.kind == "int" else float(v)


class Strategy(ABC):
    name: str = "base"
    # 이 전략이 가장 잘 맞는 보유기간(분류): position=중장기 / swing=단기 / day=데이트레.
    # 뇌가 제안 horizon 에 맞는 전략을 고르는 힌트(카탈로그에 노출). 다른 주기서도 동작은 함.
    horizon: str = "swing"
    # 각 전략이 선언하는 파라미터 스키마(하드 바운드). LLM/카탈로그/검증의 단일 출처.
    PARAMS: tuple[ParamSpec, ...] = ()
    # 전 전략 공통 하드 바운드 — 전략이 PARAMS 에 선언하지 않아도 손절/익절 비율은
    # 여기서 클램프된다. wiring.entry_stop_target 이 전략 스키마와 무관하게 이 키를
    # 읽어 손절가를 만들기 때문(스키마 없는 전략에 0.99 를 실으면 사실상 손절 소멸).
    # PARAMS 에 같은 이름이 있으면 그쪽(더 좁은 범위)이 이긴다.
    # 값이 없을 때 기본을 끼워 넣지 않는다 — 부재 시 보유기간별 폴백이 살아야 한다.
    COMMON_PARAMS: tuple[ParamSpec, ...] = (
        ParamSpec("stop_loss_pct", 0.02, 0.005, 0.30, "float", "손절 비율"),
        ParamSpec("target_profit_pct", 0.03, 0.005, 0.50, "float", "익절 비율"),
    )
    # 스키마 밖이지만 통과시키는 비수치 운용 키. 이 목록에 없는 키는 버린다.
    PASSTHROUGH_KEYS: frozenset[str] = frozenset({
        "candle_interval", "exit_at_session_end",
    })

    def __init__(self, params: dict):
        clamped, violations = self.validate(params or {})
        self.params = clamped
        self.param_violations = violations

    # ── 파라미터 정식화: 클램프(하드 가드) + 카탈로그 ───────────
    @classmethod
    def validate(cls, params: dict) -> tuple[dict, list[str]]:
        """입력 파라미터를 스키마로 클램프. (정제된 params, 위반메시지목록) 반환.

        1) PARAMS: 항상 존재(없으면 default) + 범위 클램프.
        2) COMMON_PARAMS: 입력에 있을 때만 클램프(기본값 주입 안 함).
        3) PASSTHROUGH_KEYS: 그대로 통과.
        4) 나머지 스키마 밖 키: 버리고 위반 기록 — 클램프 없는 수치가 손절 경로로
           새어 들어가는 것을 막는다.
        """
        out: dict = {}
        violations: list[str] = []
        src = params or {}
        for spec in cls.PARAMS:
            val, viol = spec.coerce(src.get(spec.name, spec.default))
            out[spec.name] = val
            if viol:
                violations.append(viol)
        for spec in cls.COMMON_PARAMS:
            if spec.name in out or spec.name not in src:
                continue
            val, viol = spec.coerce(src[spec.name])
            out[spec.name] = val
            if viol:
                violations.append(viol)
        for k, v in src.items():
            if k in out:
                continue
            if k in cls.PASSTHROUGH_KEYS:
                out[k] = v
            else:
                violations.append(f"{k}: {cls.name} 스키마 밖 키 → 무시")
        out, cross = cls.cross_check(out)
        return out, violations + cross

    @classmethod
    def cross_check(cls, params: dict) -> tuple[dict, list[str]]:
        """파라미터 간 제약(예: short<long). 기본은 없음. 필요한 전략이 오버라이드."""
        return params, []

    @classmethod
    def describe(cls) -> dict:
        """카탈로그 1항목 — 뇌(LLM)가 읽고 전략·파라미터를 고를 때 쓴다."""
        doc = (cls.__doc__ or "").strip().splitlines()[0] if cls.__doc__ else ""
        return {"name": cls.name, "doc": doc, "horizon": cls.horizon,
                "params": [{"name": s.name, "default": s.default, "min": s.min,
                            "max": s.max, "kind": s.kind, "desc": s.desc}
                           for s in cls.PARAMS]}

    @property
    def candle_interval(self) -> str:
        return self.params.get("candle_interval", "1d")

    @property
    def min_candles(self) -> int:
        """이 전략이 신호를 내기 위해 필요한 최소 캔들 수."""
        return 2

    @abstractmethod
    def decide(self, candles: pd.DataFrame, position: Position) -> Signal:
        """candles: open/high/low/close/volume 컬럼, 시간 오름차순. 마지막행=최신."""
        ...

    @staticmethod
    def hold(reason: str = "조건 미충족") -> Signal:
        return Signal(Action.HOLD, reason)

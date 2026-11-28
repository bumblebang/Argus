"""청산 재시도 지수 백오프 — 오버플로 없는 공용 계산."""
from __future__ import annotations

# 지수 상한. 거부가 수천 번 쌓여도 2**streak 이 float 범위를 넘지 않게 한다
# (streak 1,025 에서 float·int 곱이 OverflowError → 매 틱 실패 → 다른 종목 손절까지 정지).
MAX_BACKOFF_EXP = 30


def backoff_delay(base: float, streak: int, cap: float) -> float:
    """min(base·2^(streak-1), cap). streak<=1 이면 base."""
    exp = min(max(0, int(streak) - 1), MAX_BACKOFF_EXP)
    return min(float(base) * float(1 << exp), float(cap))

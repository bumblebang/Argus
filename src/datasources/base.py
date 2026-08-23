"""데이터소스 공통 인터페이스.

각 소스는 fetch(ctx) 에서 MarketState 슬롯에 합쳐질 부분 dict 를 반환한다.
순수 인터페이스라 ctx.client 를 가짜로 주입해 테스트할 수 있다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SourceContext:
    client: object | None = None              # TossClient (또는 테스트용 가짜)
    symbols_by_market: dict = field(default_factory=dict)  # {"KR": [...], "US": [...]}
    dry: bool = False                         # True 면 외부호출 없이 합성/스킵
    spacing_sec: float = 0.25                 # 캔들 한도(5/s) 회피용 간격


class DataSource(ABC):
    name: str = "base"

    @abstractmethod
    def fetch(self, ctx: SourceContext) -> dict:
        ...

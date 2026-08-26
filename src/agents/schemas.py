"""에이전트 입출력 스키마 (구조화 출력용 Pydantic).

결정 에이전트 -> DecisionOutput, 검증 에이전트 -> ValidationOutput.
모든 매매 제안은 근거(thesis)와 확신도, 보유기간을 명시해야 한다.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Proposal(BaseModel):
    """한 종목에 대한 매매 제안."""
    symbol: str
    market: Literal["KR", "US"]
    side: Literal["BUY", "SELL", "HOLD"]
    conviction: float = Field(ge=0.0, le=1.0, description="확신도 0~1")
    horizon: Literal["day", "swing", "position"] = "swing"
    target_weight: float = Field(
        ge=0.0, le=1.0,
        description="스키마 호환용(사이징 미사용 — 코드가 base_position_pct 로 정함)")
    thesis: str = Field(description="이 판단의 핵심 근거 (시황·수급·재무·뉴스 종합)")
    key_risks: list[str] = Field(default_factory=list, description="이 판단이 틀릴 수 있는 위험요인")
    # 약세 스틸맨(BUY 필수) — key_risks 가 '위험 목록'이라면 이건 '내가 틀리는 하나의 완결된
    # 이야기'와 그 반박이다. 반박하지 못하면 BUY 대신 HOLD 하라는 게 이 필드의 목적.
    bear_case: str | None = Field(
        default=None, description="이 매수가 틀리는 가장 강한 약세 시나리오(데이터 수치 인용)")
    bear_rebuttal: str | None = Field(
        default=None, description="그 약세 시나리오를 왜 반박할 수 있는지(반박 불가면 BUY 금지)")
    # 전략 배정(BUY 시): strategies 카탈로그에서 이 종목에 쓸 템플릿을 고르고 파라미터를 제시.
    # 비우면 코드가 기본 배정(config.universe 매핑)으로 폴백. 범위 밖 값은 하드 가드가 클램프.
    strategy: str | None = Field(default=None, description="전략 템플릿명(strategies 카탈로그 중 택1)")
    params: dict[str, float] | None = Field(default=None, description="선택 전략의 파라미터(범위 내)")
    # P2 점예측: 도시레 target 이 invalidation 전에 먼저 도달할 확률 (0~1).
    # conviction 과 다르다 — conviction 은 코드가 덮어쓰는 매매 확신, 이건 경로 확률.
    p_target_before_stop: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="BUY+도시레 target/invalidation 있을 때만. HOLD/SELL·레벨 없으면 null")


class DecisionOutput(BaseModel):
    market_view: str = Field(description="현재 시장 국면에 대한 종합 판단 한두 문장")
    proposals: list[Proposal] = Field(default_factory=list)


class DossierOutput(BaseModel):
    """Athena 딥리서치 1종목 결론 — 반증 가능해야 한다(진입존·무효화가 필수).

    bullish 인데 레벨(entry/invalidation/target)이 없거나 순서가 어긋나면 코드가
    neutral 로 강등한다(자유 서술 금지 — 채점 불가능한 도시에는 도시에가 아니다).
    """
    stance: Literal["bullish", "neutral", "bearish"]
    thesis: str = Field(description="핵심 결론 2~4문장(왜 사/관망/피하나)")
    horizon: Literal["day", "swing", "position"] = "swing"
    entry_low: float | None = Field(default=None, description="진입 존 하단")
    entry_high: float | None = Field(default=None, description="진입 존 상단")
    invalidation: float | None = Field(default=None,
                                       description="무효화 가격(이 아래면 thesis 폐기=손절)")
    target: float | None = Field(default=None, description="목표가")
    conviction: float = Field(ge=0.0, le=1.0, description="확신도 0~1")
    evidence: list[str] = Field(default_factory=list,
                                description="증거 목록(재무추세/수급/기술적/베이스레이트/뉴스)")
    key_risks: list[str] = Field(default_factory=list, description="틀릴 수 있는 위험요인")


class ValueDossier(BaseModel):
    """저평가 스캔(밸류에이션) 1종목 결론 — '왜 싼가·해소 가능한가'가 핵심.

    밸류 트랩(만성 저PBR·지주사 디스카운트·사양산업·지배구조)을 명시 점검한 뒤
    stance 를 낸다. 매매 게이트가 아니라 지도(value_watchlist)용 — 소비는 후속.
    """
    stance: Literal["undervalued", "fair", "value_trap"]
    conviction: float = Field(ge=0.0, le=1.0, description="확신도 0~1")
    thesis: str = Field(description="왜 싸졌고 해소 가능한가 — 2~4문장")
    fair_low_pct: float | None = Field(default=None,
                                       description="적정가 밴드 하단(현재가 대비 %)")
    fair_high_pct: float | None = Field(default=None,
                                        description="적정가 밴드 상단(현재가 대비 %)")
    risks: list[str] = Field(default_factory=list, description="핵심 리스크(최대 3)")
    evidence: list[str] = Field(default_factory=list,
                                description="판단 근거(지식이 낡았을 가능성 명시 포함)")


class ValidationVerdict(BaseModel):
    """검증 에이전트의 종목별 판정."""
    symbol: str
    approved: bool
    reason: str = Field(description="승인/거부 사유")
    concerns: list[str] = Field(default_factory=list, description="발견한 우려사항(거부 규칙 위반 등)")


class ValidationOutput(BaseModel):
    verdicts: list[ValidationVerdict] = Field(default_factory=list)

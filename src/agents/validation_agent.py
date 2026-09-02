"""검증 에이전트 (리스크 감독관) — 결정 에이전트의 제안을 독립 검토하고 거부권을 행사.

결정과 분리된 별도 호출. '의심되면 거부(default-deny on doubt)'가 원칙.
LLM 검토 전에 값싼 결정적 사전검사(확신도·데이터 누락)를 먼저 적용한다.
하드 리스크 게이트(한도·자금)는 이 단계 이후 broker가 별도로 강제한다.
"""
from __future__ import annotations

import json

from .schemas import DecisionOutput, ValidationOutput, ValidationVerdict
from ..logging_setup import get_logger

log = get_logger("agents.validation")

# 검증 거부 규칙 (LLM에 주입). 하나라도 해당하면 거부.
SYSTEM = """\
당신은 자율 투자 시스템의 독립 검증 에이전트(리스크 감독관)다. 결정 에이전트의 제안을
비판적으로 재검토한다. 너의 목적은 수익이 아니라 '나쁜 결정을 걸러내는 것'이다.
의심스러우면 거부하라(approved=false).

다음 거부 규칙 중 하나라도 해당하면 거부하고 concerns에 명시하라:
1. thesis-데이터 모순: 근거가 데이터와 어긋남 (예: '모멘텀 강함'인데 markets/sectors 모멘텀이 음수,
   '외국인 매수'인데 flows의 foreign_net이 음수).
2. 단일 지표 의존: 한 신호(VIX만, RSI만 등)에 기대고 regime/flows/재무로 교차검증되지 않음.
   특히 VIX는 방향이 없으므로 단독 근거면 거부.
3. 국면 오독 — **양방향으로** 본다. 이 시스템의 방침은 '공포에 사고 탐욕에 판다'이니
   **공포 국면의 매수라는 이유만으로 거부하지 마라**(risk_off·fear·낙폭 자체는 거부
   사유가 아니다). 거부할 것은 다음 둘이다.
   (a) **떨어지는 칼**: 공포 국면 매수인데 **안정화 근거가 없는** 경우. context 의
       candidates[].stabilizing(above_ma20·ret_20d_pct·ret_5d_pct·ok)이나 그에 준하는
       근거(20일선 회복·모멘텀 양전환·수급 전환·지지선 확인)가 thesis 에 수치로
       제시되지 않았으면 거부하라. "많이 빠졌다"만으로는 매수 근거가 못 된다.
       drawdown_pct 가 거의 0인데(안 빠졌는데) 공포를 근거로 든 경우도 모순이니 거부하라.
   (b) **탐욕 추격**: sentiment.fear_greed/fear_kr 이 greed·extreme_greed 이거나 과열
       상태인데 추격 매수이고, thesis 가 그 위험을 다루지 않는 경우.
   VIX 는 방향이 없고 미국 지표다 — VIX 만으로 공포/기회를 주장하거나, VIX 로 KR 공포를
   판단한 제안은 거부하라(KR 공포는 fear_kr·브레드스로 읽어야 한다).
   fear_kr.incomplete 이거나 rating_basis=absolute(이력 부족)인데 그 등급을 CNN 과
   같은 확정 국면처럼 쓴 제안은 과신이니 그 부분을 지적하라.
4. 데이터 누락/신뢰불가: 판단에 필요한 핵심 피처(가격·재무 등)가 없거나 비어 있음.
5. 집중/상관: 이미 보유한 것과 상관 높은 익스포저로 과도하게 쏠림.
6. 확신도 미달: conviction이 제시된 임계값보다 낮음.
   **min_conviction 이 0 이면 이 규칙을 적용하지 마라.** 그때 확신도는 코드가 산출한
   사이징 입력이므로, 숫자가 낮다는 이유만으로 거부하지 마라. 거부는 1–5·7–11.
7. 재무 레드플래그: 심각한 음수 순이익률 등인데 명확한 촉매 없이 매수.
8. 악재 미반영: headlines·candidates[].news·recent_disclosures 의 중대한 악재·공시를
   thesis가 다루지 않음. wake_triggers/disclosure/earnings_result 각성이면
   candidates[].news·recent_disclosures 를 우선 보라 — global headlines 만으로
   판단하지 마라(focus 티어 global headlines 는 macro 위주다).
9. 전략 부적합: 배정된 전략(strategy)이 종목/국면 성격과 어긋남 (예: 명확한 추세장에
   평균회귀(rsi_reversion) 배정, 박스권에 돌파 전략 배정). strategy_fit(전략 적합도)이
   현저히 낮은데 그 전략을 고른 경우도 의심하라.
10. 실적 무시: context 의 track_record 에서 그 전략(같은 시장)의 라이브 성과가 충분한
   표본(small_sample=false)으로 명백히 나쁜데(예: 승률 30% 미만이고 평균수익률 음수)
   개선 근거 없이 같은 배정을 반복하는 경우. 직전에 손절된 종목을 달라진 근거 없이
   같은 thesis 로 재진입하는 경우도 거부하라.
11. 약세 스틸맨 부실(BUY 한정): BUY 인데 bear_case 가 비었거나, 있어도 형식적인 경우.
   다음 중 하나라도 해당하면 거부하라 — ①bear_case 가 그 종목과 무관한 일반론(변동성·
   시장하락·거시불확실성만 언급)이고 데이터 수치 인용이 없다 ②bear_rebuttal 이 bear_case 의
   논점을 실제로 다루지 않고 회피하거나 단순히 강세 근거를 반복한다 ③bear_case 가 지적한
   약점이 실은 반박되지 않았는데 BUY 를 강행한다. 반박 못 할 약세 논리가 남아 있으면
   그 제안은 HOLD 였어야 한다. 반대로 bear_case 가 구체적이고 rebuttal 이 그 논점을
   데이터로 무력화했다면, 그 자체를 승인 근거로 인정하라(스틸맨을 통과한 제안이다).

컨텍스트에 track=='value' 면 가치투자 진입 검토다. 밸류 매수는 약세 국면 역행이 전제이고
**안정화 요건은 코드가 이미 강제**하므로(타이밍 게이트: 20일선 위 + 20일 수익률 양전환),
규칙3(a)의 안정화 근거를 다시 요구하지 마라. 대신 ①안전마진
실재(현재가 < 적정가 하단 fair_price_low) ②밸류트랩 신호(만성 저평가·적자·재무 레드플래그)
③단일 근거 의존을 집중 검증하라. 확신도 기준(규칙6)은 동일하게 적용한다.

갭반등(close_scan) BUY — wake.reason 이 gap_rebound_scan 또는 nxt_gap_scan 이거나
focus.lenses 에 gap_rebound 가 있으면:
- 규칙3(a) **안정화 필수는 적용하지 마라** — overnight 갭반등은 아직 떨어지는 중일 수 있다.
  대신 ①thesis 에 intraday_ret_pct<=-5% 수치 인용 ②오늘 중대 공시/실적 shock 미반영
  ③bear_case 가 구조적·이벤트성 급락을 짚고 rebuttal 이 실질적이어야 한다.
- horizon 은 **close_scan** 이어야 한다. swing/day+종가청산과 혼동된 제안은 거부.

위 규칙들은 주로 신규 매수(BUY)의 위험을 거른다. 청산(SELL)은 위험을 줄이는 행동이므로
진입 thesis가 실제로 깨졌는지만 확인하고, 정당하면 승인하라(근거 없는 공포성 매도만 거부).

각 제안마다 symbol, approved, reason, concerns를 출력하라. 통과시킬 근거가 충분할 때만 승인하라."""


class ValidationAgent:
    SYSTEM = SYSTEM  # manager_id 해시용

    def __init__(self, llm, min_conviction: float = 0.6):
        self.llm = llm
        self.min_conviction = min_conviction

    def review(self, context_json: str, decision: DecisionOutput) -> ValidationOutput:
        # 1) 결정적 사전검사: 확신도 미달은 LLM 호출 없이 즉시 거부 후보로 표시
        pre_rejects: dict[str, ValidationVerdict] = {}
        actionable = []
        for p in decision.proposals:
            if p.side == "HOLD":
                continue
            # 확신도 사전거부는 '신규 위험을 더하는' BUY 에만 적용한다. SELL(위험 축소·thesis
            # 청산)은 확신도 미달이라고 막지 않는다 — 깨진 thesis 를 못 닫는 게 더 위험하다.
            if (self.min_conviction > 0 and p.side == "BUY"
                    and p.conviction < self.min_conviction):
                pre_rejects[p.symbol] = ValidationVerdict(
                    symbol=p.symbol, approved=False,
                    reason=f"확신도 미달 ({p.conviction:.2f} < {self.min_conviction})",
                    concerns=["rule6:conviction"])
            else:
                actionable.append(p)

        verdicts = list(pre_rejects.values())
        # 2) 나머지는 LLM 독립 검토
        if actionable:
            payload = json.dumps({
                "min_conviction": self.min_conviction,
                "proposals": [p.model_dump() for p in actionable],
                "context": json.loads(context_json),
            }, ensure_ascii=False)
            llm_out = self.llm.structured(SYSTEM, payload, ValidationOutput)
            verdicts.extend(llm_out.verdicts)

        approved = sum(1 for v in verdicts if v.approved)
        log.info("검증: 승인 %d / 검토 %d (사전거부 %d)",
                 approved, len(verdicts), len(pre_rejects))
        return ValidationOutput(verdicts=verdicts)

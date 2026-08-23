"""각성 트리거 — 상시 감시 루프가 '뇌(LLM)'를 깨울지 판단하는 규칙들.

전부 순수 함수다: (포지션/가격) -> Trigger 목록. 토스 호출도 LLM 호출도 없으니
단위테스트가 쉽다. M1 에서 트리거는 events 로 로깅되고, 'act' 등급이면 루프가
정밀층(캔들)을 켜고 각성 콜백을 호출한다(실제 뇌 연결은 M2).

urgency 3단계:
  info  — 기록만(시계열 축적).
  watch — 주의. 정밀 폴링 후보로 승격.
  act   — 즉각 대응 필요. 뇌 각성 트리거.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

URGENCY = {"info": 0, "watch": 1, "act": 2}


@dataclass
class Trigger:
    kind: str           # stop_hit / stop_approach / target_hit / vol_spike / limit_reached
    symbol: str
    urgency: str        # info / watch / act
    reason: str
    payload: dict = field(default_factory=dict)

    def as_event(self) -> dict:
        return {"kind": self.kind, "urgency": self.urgency,
                "reason": self.reason, **self.payload}


def position_triggers(pos: dict, price: float | None,
                      stop_buffer_pct: float = 0.005,
                      *, suppress_target: bool = False) -> list[Trigger]:
    """보유 포지션 1건 + 현재가 -> 손절/익절 트리거.

    pos: {symbol, qty, avg_price, stop_price, target_price, ...}
    stop_buffer_pct: 손절가에 이만큼 근접하면 '임박(watch)'으로 본다(기본 0.5%).
    suppress_target: True 면 target_hit 을 아예 내지 않는다(트레일링 대상 — 목표가는
      청산가가 아니라 트레일링 활성화 지점일 뿐이다. 되돌림 청산은 트레일링 스톱=stop_hit
      이 집행). 이 함수는 순수 유지: 대상 판정·상태 영속은 호출측(루프)이 한다.
    """
    if not pos or price is None:
        return []
    sym = pos.get("symbol")
    stop = pos.get("stop_price")
    target = pos.get("target_price")
    out: list[Trigger] = []
    if stop:
        if price <= stop:
            out.append(Trigger("stop_hit", sym, "act",
                               f"가격 {price} <= 손절가 {stop}",
                               {"price": price, "stop": stop}))
        elif price <= stop * (1 + stop_buffer_pct):
            out.append(Trigger("stop_approach", sym, "watch",
                               f"가격 {price} 손절가 {stop} 근접",
                               {"price": price, "stop": stop}))
    if target and price >= target and not suppress_target:
        out.append(Trigger("target_hit", sym, "act",
                           f"가격 {price} >= 목표가 {target}",
                           {"price": price, "target": target}))
    return out


def volatility_trigger(symbol: str, prices: Sequence[float],
                       spike_pct: float = 0.03) -> Trigger | None:
    """최근 가격 윈도(오래된→최신)에서 단기 급변 감지.

    윈도 시작값 대비 최신값이 spike_pct 이상(±) 움직이면 vol_spike(watch).
    윈도 크기는 호출측(루프)이 폴링주기로 정한다(예: 5초×12 ≈ 1분).
    """
    pts = [p for p in prices if p]
    if len(pts) < 2:
        return None
    ref, last = pts[0], pts[-1]
    if not ref:
        return None
    change = (last - ref) / ref
    if abs(change) >= spike_pct:
        return Trigger("vol_spike", symbol, "watch",
                       f"단기 변동 {change:+.2%}",
                       {"change": change, "from": ref, "to": last})
    return None


def limit_trigger(symbol: str, price: float | None, watch_level: float | None,
                  direction: str = "below") -> Trigger | None:
    """후보 관심가 도달: 가격이 watch_level 에 닿으면 act.

    direction='below' 면 price<=level(저가 매수 관심), 'above' 면 price>=level(돌파).
    M1 에선 관심가가 비어 있을 수 있어(아직 LLM 미배정) None 을 그냥 흘린다.
    """
    if price is None or not watch_level:
        return None
    hit = price <= watch_level if direction == "below" else price >= watch_level
    if not hit:
        return None
    return Trigger("limit_reached", symbol, "act",
                   f"가격 {price} {direction} 관심가 {watch_level}",
                   {"price": price, "level": watch_level, "dir": direction})


def session_end_trigger(symbol: str, market: str) -> Trigger:
    """데이트레(day) 포지션 종가 청산 — 장 마감 임박 시 코드가 즉시 전량 청산(오버나잇 금지)."""
    return Trigger("session_end", symbol, "act",
                   f"장 마감 임박 — 데이트레 종가 청산 ({market})", {"market": market})


_REGIME_OPPOSITE = {"risk_on": "risk_off", "risk_off": "risk_on"}


def regime_flip_trigger(symbol: str, entry_regime: str | None,
                        current_regime: str | None) -> Trigger | None:
    """진입 시점 국면이 현재와 정반대로 뒤집혔으면 act(뇌 각성).

    risk_on <-> risk_off 반전만 flip 으로 본다(neutral 경유는 제외). 코드가 청산하지 않고
    뇌를 깨워 thesis 를 재평가하게 한다 — 매수 thesis 가 국면 가정에 기대는 경우가 많으므로,
    국면이 뒤집히면 "그 thesis 가 아직 유효한가?"를 다시 묻는다.
    """
    if not entry_regime or not current_regime:
        return None
    if _REGIME_OPPOSITE.get(entry_regime) == current_regime:
        return Trigger("regime_flip", symbol, "act",
                       f"국면 반전 {entry_regime} -> {current_regime}",
                       {"entry_regime": entry_regime, "current_regime": current_regime})
    return None


def max_urgency(triggers: Sequence[Trigger]) -> str | None:
    """트리거 목록에서 최고 긴급도 등급 반환(없으면 None)."""
    if not triggers:
        return None
    return max(triggers, key=lambda t: URGENCY.get(t.urgency, 0)).urgency

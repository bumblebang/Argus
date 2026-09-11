"""청산 사유(exit_reason) 한 곳 정의 — 트리거 kind = 원장 exit_reason = 표시 라벨.

trail_stop 과 stop_hit 은 "가격이 스톱을 깼다"는 같은 경로지만 의미가 반대다.
트레일링 스톱은 목표가 돌파 후 고점 대비 되돌림에 **이익을 확정**한 청산이고,
stop_hit 은 진입 논거가 깨져 **손실을 끊은** 청산이다. 한 라벨로 뭉개면 +26% 청산이
'손절'로 읽히고, 회고(lessons)·최근거래(recent_trades)가 같은 칸에 들어가 뇌가
"직전에 손절당한 종목"으로 잘못 학습한다.

구 원장 호환: trail_stop 도입 전에 닫힌 행은 exit_reason='stop_hit' 이지만
meta.trail_active 가 남아 있다. 읽는 쪽에서 refine_exit_reason 으로 보정한다
(DB 마이그레이션 없이 과거 거래도 바르게 읽힌다).
"""
from __future__ import annotations

import json

STOP_HIT = "stop_hit"
TRAIL_STOP = "trail_stop"

EXIT_REASON_KO = {
    "stop_hit": "손절",
    "trail_stop": "트레일링 스톱",
    "target_hit": "목표가 도달",
    "session_end": "종가 청산",
    "time_stop": "시간손절",
    "brain": "뇌 판단",
    "partial_exit": "부분 청산",
    "exit": "청산",
}


def _meta_dict(meta) -> dict:
    """meta 는 store 행(JSON 문자열)에서도, 메모리 dict 에서도 온다."""
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            return {}
    return meta if isinstance(meta, dict) else {}


def refine_exit_reason(exit_reason, meta=None) -> str:
    """원장 exit_reason 을 포지션 meta 로 보정.

    트레일 활성 상태로 닫힌 stop_hit 은 트레일링 스톱이다. 그 외는 원문 그대로
    (분류를 새로 만들지 않는다 — 모르는 값은 건드리지 않고 흘린다).
    """
    er = str(exit_reason or "").strip()
    if er != STOP_HIT:
        return er
    return TRAIL_STOP if _meta_dict(meta).get("trail_active") else er


def exit_reason_ko(exit_reason, meta=None) -> str:
    """사람이 읽는 라벨. 매핑에 없으면 원문 그대로 보여준다(삼키지 않는다)."""
    er = refine_exit_reason(exit_reason, meta)
    if er.startswith("strategy:"):
        return f"전략신호 ({er.split(':', 1)[1]})"
    return EXIT_REASON_KO.get(er, er)

"""진입 근거(entry basis) — 청산 룰셋을 '왜 샀는가'에 묶는다.

같은 종목이라도 진입 논거가 다르면 청산 논거도 달라야 한다. 도시에 진입존 논거로
산 포지션을 전략 신호(데드크로스)로 파는 것은 **사는 판단자와 파는 판단자가 다른**
것이다 — 밸류 트랙의 `sell_block_fn`(cycle.py) 이 이미 쓰는 원칙을 스윙 트랙까지
확장한다.

  signal  전략 decide() 가 BUY 를 내서 진입(EntryExecutor.evaluate)
  zone    도시에 진입존 재진입(EntryExecutor._evaluate_zone)
  thesis  뇌 즉시 체결 — 도시에 진입존·RR·무효화 논거(cycle_runner)
  value   밸류 트랙(value_trade)
  orphan  체결 미러/기동 동기화로 생긴 행 — 진입 논거 미상

**전략 신호 청산이 켜지는 조건은 basis=signal 하나뿐이다**(대칭). 나머지는 신호가
떠도 팔지 않고 뇌를 깨워 논거를 재평가한다(loop 의 strategy_signal 트리거).
예외로 horizon 이 day/close_scan 이면 basis 와 무관하게 켠다 — 코드가 당일 안에
자기가 연 자리를 자기가 닫는 트랙이고 세션 종료 강제청산과 짝이기 때문이다.

전략 라벨(`positions.strategy`)과 파라미터는 그대로 둔다 — 손절폭 계산
(wiring.combine_stop_target)과 성과 측정(strategy_stats)이 그 값을 쓴다. 여기서
가르는 것은 **신호 청산의 집행 권한**뿐이다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

BASIS_SIGNAL = "signal"
BASIS_ZONE = "zone"
BASIS_THESIS = "thesis"
BASIS_VALUE = "value"
BASIS_ORPHAN = "orphan"

KNOWN_BASES = frozenset({BASIS_SIGNAL, BASIS_ZONE, BASIS_THESIS,
                         BASIS_VALUE, BASIS_ORPHAN})

# basis 와 무관하게 신호 청산을 켜는 보유기간(코드 소유 트랙).
SIGNAL_EXIT_HORIZONS = frozenset({"day", "close_scan"})


def meta_of(pos: dict | None) -> dict:
    meta = (pos or {}).get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            return {}
    return meta if isinstance(meta, dict) else {}


def basis_of(pos: dict | None) -> str:
    """포지션의 진입 근거. meta.entry_basis 스탬프가 권위, 없으면 추론(구 행 호환).

    추론은 **보수적**이다 — 판정이 안 서면 thesis 로 본다(신호 청산 OFF). 진입 논거를
    모르는 포지션을 배정 라벨만 보고 파는 것이 바로 이 모듈이 막으려는 사고다.
    """
    meta = meta_of(pos)
    raw = meta.get("entry_basis")
    if isinstance(raw, str) and raw.strip().lower() in KNOWN_BASES:
        return raw.strip().lower()

    source = str(meta.get("source") or "").strip().lower()
    if source == "value" or str((pos or {}).get("strategy") or "").lower() == "value":
        return BASIS_VALUE
    if source == "fill_mirror":
        return BASIS_ORPHAN
    if meta.get("entry_zone"):
        return BASIS_ZONE
    return BASIS_THESIS


def horizon_of(pos: dict | None) -> str:
    """meta.horizon 우선, 없으면 전략 클래스 horizon — exit_policy 와 같은 판정을 쓴다."""
    from .exit_policy import horizon_of as _hz
    return str(_hz(pos or {}) or "").strip().lower()


def signal_exit_allowed(pos: dict | None) -> tuple[bool, str]:
    """(전략 신호로 청산해도 되는가, basis). 안 되면 호출측이 뇌 각성으로 강등."""
    basis = basis_of(pos)
    if basis == BASIS_SIGNAL:
        return True, basis
    if horizon_of(pos) in SIGNAL_EXIT_HORIZONS:
        return True, basis
    return False, basis


# ── 신호 청산 정책(config 최상위 strategy_exit 블록) ────────────────
@dataclass(frozen=True)
class SignalExitConfig:
    """전략 신호 청산의 공통 가드.

    bind_to_entry_basis: basis=signal(+day/close_scan) 에만 신호 청산 허용.
      False 면 구 동작(라벨만 있으면 청산) — 롤백 스위치.
    min_hold_sec: 진입 후 이 시간 안에는 **신호** 청산 금지. 손절/목표(ExitExecutor)와
      세션 종료 청산은 이 가드와 무관하게 즉시 작동한다.
    closed_bar_only: 크로스형 전략(closed_bar_signal=True)의 신호는 **확정봉**으로만
      판정한다 — 미완성 당봉에 실시간가를 덮으면 장중 흔들림만으로 크로스가 켜졌다 꺼진다.
    wake_cooldown_sec: 강등된 신호로 뇌를 깨우는 최소 간격(종목·전략 단위).
    """
    bind_to_entry_basis: bool = True
    min_hold_sec: float = 60.0
    closed_bar_only: bool = True
    wake_cooldown_sec: float = 1800.0


def parse_signal_exit(raw: dict | None) -> SignalExitConfig:
    """config 최상위 strategy_exit 블록 → 정규화. 블록이 없으면 기본값(전부 켬)."""
    block = (raw or {}).get("strategy_exit") or {}
    if not isinstance(block, dict):
        return SignalExitConfig()
    d = SignalExitConfig()

    def _num(key: str, default: float) -> float:
        try:
            v = float(block[key])
        except (KeyError, TypeError, ValueError):
            return default
        return v if v >= 0 else default

    return SignalExitConfig(
        bind_to_entry_basis=bool(block.get("bind_to_entry_basis",
                                           d.bind_to_entry_basis)),
        min_hold_sec=_num("min_hold_sec", d.min_hold_sec),
        closed_bar_only=bool(block.get("closed_bar_only", d.closed_bar_only)),
        wake_cooldown_sec=_num("wake_cooldown_sec", d.wake_cooldown_sec),
    )

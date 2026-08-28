"""extra_wakes 발화 dedup 영속 — 재기동 시 08:00·과거 슬롯 중복 각성 방지.

인메모리 _extra_fired 만 쓰면 재기동 때 거래일 dedup 이 리셋되어, 이미 지난 HH:MM
슬롯이 한꺼번에 다시 발화한다(08-28 11:53 실측). 디스크에 (market, HH:MM)→거래일
을 남긴다.

catch-up 억제: target 시각 이후 extra_wake_window_min 분까지만 발화. 그 창을 넘긴
슬롯은 그날 스킵(데몬 다운 구간 — 다음 슬롯까지).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .. import paths as _paths
from ..logging_setup import get_logger

log = get_logger("engine.extra_wake_state")

_KST = ZoneInfo("Asia/Seoul")
DEFAULT_PATH = "data/state/extra_wake_fired.json"
_MAX_ENTRIES = 512


def _state_path(path: str | Path | None) -> Path:
    if path:
        return Path(path) if Path(path).is_absolute() else _paths.ROOT / path
    return _paths.resolve("extra_wake_state", configured=DEFAULT_PATH)


def _key(market: str, hhmm: str) -> str:
    return f"{market}|{hhmm}"


def load_fired(path: str | Path | None = None) -> dict[tuple[str, str], str]:
    """파일 → {(market, HH:MM): trading_day_iso}."""
    p = _state_path(path)
    if not p.is_file():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as e:
        log.warning("extra_wake 상태 로드 실패(빈 dict): %s", e)
        return {}
    fired = raw.get("fired") if isinstance(raw, dict) else None
    if not isinstance(fired, dict):
        return {}
    out: dict[tuple[str, str], str] = {}
    for k, day in fired.items():
        if not isinstance(k, str) or not day:
            continue
        parts = k.split("|", 1)
        if len(parts) != 2:
            continue
        out[(parts[0], parts[1])] = str(day)
    return out


def save_fired(mapping: dict[tuple[str, str], str], *,
               path: str | Path | None = None,
               now_fn: Callable[[], float] = time.time) -> None:
    """원자적 저장. 오래된 항목은 상한 초과 시 잘라낸다."""
    p = _state_path(path)
    trimmed = dict(list(mapping.items())[-_MAX_ENTRIES:])
    payload = {
        "fired": {_key(m, h): d for (m, h), d in trimmed.items()},
        "updated_ts": now_fn(),
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as e:
        log.warning("extra_wake 상태 저장 실패(무시): %s", e)


def minutes_since_hhmm(now_kst: datetime, hhmm: str) -> int | None:
    """now 가 target HH:MM 이후면 경과 분(정수). 아직 전이면 None."""
    try:
        target = datetime.strptime(hhmm, "%H:%M").time()
    except ValueError:
        return None
    now_t = now_kst.time()
    if now_t < target:
        return None
    now_m = now_kst.hour * 60 + now_kst.minute
    tgt_m = target.hour * 60 + target.minute
    return now_m - tgt_m


# KR 갭반등 close_scan 슬롯 — 나머지 extra_wakes 는 reason=extra 유지.
_GAP_SCAN_REASONS: dict[str, str] = {
    "15:20": "gap_rebound_scan",
    "19:50": "nxt_gap_scan",
}

# 풀만 갱신(뇌 미각성) — 15:15 에 gap_decline_pool refresh.
_POOL_ONLY_REASONS: frozenset[str] = frozenset({"gap_pool_refresh"})

_POOL_REFRESH_SLOTS: dict[str, str] = {
    "15:15": "gap_pool_refresh",
}


def reason_for_extra(market: str, hhmm: str) -> str:
    """지정 시각 → wake reason. KR 갭반등 슬롯만 전용 reason."""
    if market == "KR":
        if hhmm in _POOL_REFRESH_SLOTS:
            return _POOL_REFRESH_SLOTS[hhmm]
        return _GAP_SCAN_REASONS.get(hhmm, "extra")
    return "extra"


def is_pool_only_reason(reason: str) -> bool:
    """True 면 뇌 각성 없이 풀 refresh 콜백만."""
    return str(reason or "") in _POOL_ONLY_REASONS


def should_fire_extra(*, market: str, hhmm: str, trading_day: str,
                      fired: dict[tuple[str, str], str],
                      now_ts: float,
                      grace_until: float,
                      window_min: float) -> bool:
    """이번 틱에 extra 를 발화할지."""
    if grace_until > 0 and now_ts < grace_until:
        return False
    key = (market, hhmm)
    if fired.get(key) == trading_day:
        return False
    now_kst = datetime.fromtimestamp(now_ts, tz=_KST)
    elapsed = minutes_since_hhmm(now_kst, hhmm)
    if elapsed is None:
        return False
    if window_min > 0 and elapsed > int(window_min):
        return False
    return True

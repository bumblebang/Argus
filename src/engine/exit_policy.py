"""공통 청산 정책 — 전략 decide() 와 무관한 코드 바닥(빠른손).

v1: horizon별 최대 보유일(time_stop). day 는 watch.session_end 가 담당.
value 는 뇌 time_stop 컨텍스트 유지 — 코드 강제 청산 제외.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import triggers as T

_DEFAULT_MAX_DAYS: dict[str, int] = {"swing": 20, "position": 120}


@dataclass(frozen=True)
class ExitPolicyConfig:
    enabled: bool = False
    max_days: dict[str, int] = field(default_factory=dict)
    exclude_strategies: frozenset[str] = field(default_factory=frozenset)


def parse_exit_policy(raw: dict | None) -> ExitPolicyConfig:
    """config 최상위 exit_policy 블록 → 정규화. 없거나 enabled=false 면 비활성."""
    block = (raw or {}).get("exit_policy") or {}
    if not isinstance(block, dict) or not block.get("enabled"):
        return ExitPolicyConfig(enabled=False, max_days={}, exclude_strategies=frozenset())
    ts = block.get("time_stop") or {}
    if not isinstance(ts, dict) or not ts.get("enabled", True):
        return ExitPolicyConfig(enabled=False, max_days={}, exclude_strategies=frozenset())

    by_hz = ts.get("by_horizon") or {}
    max_days: dict[str, int] = {}
    for hz in ("swing", "position"):
        sub = by_hz.get(hz) if isinstance(by_hz, dict) else None
        if isinstance(sub, dict) and sub.get("max_days") is not None:
            try:
                max_days[hz] = max(0, int(sub["max_days"]))
            except (TypeError, ValueError):
                max_days[hz] = _DEFAULT_MAX_DAYS[hz]
        else:
            max_days[hz] = _DEFAULT_MAX_DAYS[hz]

    excl = ts.get("exclude_strategy", ["value"])
    if isinstance(excl, str):
        excl = [excl]
    exclude = frozenset(str(s) for s in (excl or []) if s)

    return ExitPolicyConfig(enabled=True, max_days=max_days, exclude_strategies=exclude)


def _meta_get(pos: dict, key: str) -> Any:
    meta = pos.get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            return None
    return meta.get(key) if isinstance(meta, dict) else None


def horizon_of(pos: dict) -> str | None:
    """meta.horizon 우선. 없으면 전략 클래스 horizon."""
    h = _meta_get(pos, "horizon")
    if h:
        return str(h).strip().lower()
    name = pos.get("strategy")
    if not name:
        return None
    try:
        from ..strategies import REGISTRY
        cls = REGISTRY.get(name)
        if cls is not None:
            return str(getattr(cls, "horizon", "") or "").lower() or None
    except ImportError:
        pass
    return None


def days_held(pos: dict, now: float) -> float | None:
    opened = pos.get("opened_at")
    if opened is None:
        return None
    try:
        return (float(now) - float(opened)) / 86400.0
    except (TypeError, ValueError):
        return None


def time_stop_trigger(pos: dict, *, cfg: ExitPolicyConfig,
                      now: float) -> T.Trigger | None:
    """보유일이 horizon 상한을 넘으면 act 청산 트리거."""
    if not cfg.enabled:
        return None
    sym = pos.get("symbol")
    if not sym or float(pos.get("qty") or 0) <= 0:
        return None
    strat = pos.get("strategy")
    if strat and strat in cfg.exclude_strategies:
        return None
    hz = horizon_of(pos)
    if not hz or hz in ("day", "close_scan"):
        return None
    max_d = cfg.max_days.get(hz, 0)
    if max_d <= 0:
        return None
    held = days_held(pos, now)
    if held is None or held < max_d:
        return None
    return T.Trigger(
        "time_stop", sym, "act",
        f"시간손절 보유 {held:.1f}일 >= {max_d}일 ({hz})",
        {"days_held": round(held, 2), "max_days": max_d, "horizon": hz},
    )


def close_scan_exit_trigger(pos: dict, *, market: str = "KR", now: float,
                            sessions: tuple[str, ...] | None = None) -> T.Trigger | None:
    """close_scan(갭반등) 익일 세션 개시 후 청산."""
    if horizon_of(pos) != "close_scan":
        return None
    sym = pos.get("symbol")
    if not sym or float(pos.get("qty") or 0) <= 0:
        return None
    opened = pos.get("opened_at")
    if opened is None:
        return None
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from ..market_hours import market_day, is_tradable, current_session

    kst = ZoneInfo("Asia/Seoul")
    now_ts = float(now)
    now_dt = datetime.fromtimestamp(now_ts, tz=kst)
    opened_dt = datetime.fromtimestamp(float(opened), tz=kst)
    if market_day(market, now_dt) <= market_day(market, opened_dt):
        return None
    # is_tradable/current_session 은 epoch(float)만 받는다 — datetime 넘기면 TypeError.
    if not is_tradable(market, sessions, now_ts):
        return None
    sess = current_session(market, now_ts)
    if sess not in ("premarket", "regular"):
        return None
    return T.Trigger(
        "close_scan_exit", sym, "act",
        "close_scan 익일 청산(갭반등 1박)",
        {"horizon": "close_scan", "session": sess},
    )


def thesis_inval_trigger(pos: dict, *, price: float | None, now: float,
                         flow_streak: int | None = None) -> T.Trigger | None:
    """thesis 무효화(price/flow/time) — 코드 감사. hits 있으면 act 청산."""
    from ..thesis_watch import audit_position
    hits = audit_position(pos, price=price, now=now, flow_streak=flow_streak)
    if not hits:
        return None
    sym = pos.get("symbol")
    if not sym:
        return None
    kinds = ",".join(h.kind for h in hits)
    detail = "; ".join(h.detail for h in hits)
    return T.Trigger(
        "thesis_invalidation", sym, "act",
        f"thesis 무효화[{kinds}] {detail}",
        {"kinds": [h.kind for h in hits], "details": [h.detail for h in hits]},
    )

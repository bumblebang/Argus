"""판단 단위 라벨 — 전방 수익 · 정책수익 · 도시에 타깃 선도달.

v1 채점 단위는 체결이 아니라 (사이클, 후보) 판단이다.
HOLD 정책수익 = 0. BUY 만 전방수익을 가져간다.
포트 복리·순차 리플레이는 하지 않는다.

P2: Proposal.p_target_before_stop (선택) vs 도시레 target/invalidation 으로 적립한
`target_hit_before_stop` 이진 라벨 — Brier/log-loss 로 proper scoring.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..market_hours import _SESSIONS
from ..shadow_ledger import (KST, exit_close_on_calendar, horizon_calendar_days,
                             load_daily_series)

MIN_N = 20


def symbol_market(symbol: str) -> str:
    """KR 6자리 vs 그 외 US."""
    s = str(symbol or "")
    return "KR" if s.isdigit() and len(s) == 6 else "US"


def asof_local_date(asof: datetime, market: str) -> date:
    """판단일을 해당 시장 타임존 날짜로."""
    tzname = _SESSIONS.get(market, ("Asia/Seoul",))[0]
    return asof.astimezone(ZoneInfo(tzname)).date()


def parse_asof(ts) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    s = str(ts).strip()
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s[:32])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=KST)
        except ValueError:
            return None


def close_on_or_before(series: list[tuple[datetime, float]],
                       asof: datetime, *, market: str = "KR") -> float | None:
    if not series:
        return None
    asof_d = asof_local_date(asof, market)
    best = None
    for d, c in series:
        if d.date() <= asof_d:
            best = c
        else:
            break
    return best


def forward_return(data_dir: Path | str, symbol: str, asof, *,
                   horizon: str = "swing", cfg: dict | None = None
                   ) -> dict[str, Any]:
    """판단일 종가 대비 horizon 종가 수익률(소수). 데이터 없으면 ret=None."""
    data_dir = Path(data_dir)
    dt = parse_asof(asof)
    mkt = symbol_market(symbol)
    days = horizon_calendar_days(horizon, cfg)
    series = load_daily_series(data_dir, symbol)
    if dt is None or not series:
        return {"fwd_ret": None, "entry": None, "exit": None,
                "horizon_days": days, "symbol": symbol}
    entry = close_on_or_before(series, dt, market=mkt)
    entry_ts = dt.timestamp()
    exit_px = exit_close_on_calendar(series, entry_ts, days, market=mkt)
    ret = None
    if entry and entry > 0 and exit_px is not None:
        ret = exit_px / entry - 1.0
    return {"fwd_ret": ret, "entry": entry, "exit": exit_px,
            "horizon_days": days, "symbol": symbol}


def policy_return(side: str, fwd_ret: float | None) -> float | None:
    """1[side==BUY] * fwd_ret. HOLD/SELL = 0 (전방수익이 있을 때)."""
    if fwd_ret is None:
        return None
    if (side or "HOLD").upper() == "BUY":
        return float(fwd_ret)
    return 0.0


def _load_ohlc(data_dir: Path, symbol: str) -> list[tuple[datetime, float, float, float]]:
    """(dt, high, low, close). 컬럼 부족하면 close 만 복제."""
    series = load_daily_series(data_dir, symbol)
    candidates = sorted(Path(data_dir).glob(f"history/{symbol}.KS_1d_*.csv"))
    if not candidates:
        candidates = sorted(Path(data_dir).glob(f"history/{symbol}_1d_*.csv"))
    if not candidates:
        return [(d, c, c, c) for d, c in series]
    path = candidates[-1]
    rows: list[tuple[datetime, float, float, float]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if i == 0 and ("Date" in line or "date" in line):
            continue
        parts = line.split(",")
        if len(parts) < 5:
            continue
        try:
            d = datetime.fromisoformat(parts[0][:10]).replace(tzinfo=KST)
            close = float(parts[4]) if len(parts) >= 5 else float(parts[-1])
            high = float(parts[2]) if len(parts) >= 4 else close
            low = float(parts[3]) if len(parts) >= 5 else close
            rows.append((d, high, low, close))
        except ValueError:
            continue
    return rows or [(d, c, c, c) for d, c in series]


def target_hit_before_stop(data_dir: Path | str, symbol: str, asof, *,
                           target: float | None, invalidation: float | None,
                           horizon: str = "swing",
                           cfg: dict | None = None) -> dict[str, Any]:
    """도시레 타깃이 무효화보다 먼저 터치되면 True.

    같은 봉에서 둘 다 터치되면 순서 불명이라 None.
    horizon 내 미도달이면 False (라벨은 적립, 확률 필드는 스키마에 없음).
    """
    out: dict[str, Any] = {
        "target_hit_before_stop": None, "resolved": False, "reason": "no_levels",
    }
    if target is None or invalidation is None:
        return out
    try:
        tgt, inv = float(target), float(invalidation)
    except (TypeError, ValueError):
        return out
    dt = parse_asof(asof)
    if dt is None:
        out["reason"] = "no_asof"
        return out
    mkt = symbol_market(symbol)
    days = horizon_calendar_days(horizon, cfg)
    bars = _load_ohlc(Path(data_dir), symbol)
    if not bars:
        out["reason"] = "no_bars"
        return out
    start = asof_local_date(dt, mkt)
    end = start + timedelta(days=days)
    for d, high, low, _c in bars:
        day = d.date()
        if day <= start:
            continue
        if day > end:
            break
        hit_tgt = high >= tgt
        hit_stp = low <= inv
        if hit_tgt and hit_stp:
            out.update(reason="same_bar_ambiguous", resolved=False)
            return out
        if hit_stp:
            out.update(target_hit_before_stop=False, resolved=True, reason="stop_first")
            return out
        if hit_tgt:
            out.update(target_hit_before_stop=True, resolved=True, reason="target_first")
            return out
    out.update(target_hit_before_stop=False, resolved=True, reason="neither_by_horizon")
    return out


def _as01(y: bool | int | float | None) -> float | None:
    if y is None:
        return None
    return 1.0 if y else 0.0


def brier_score(pairs: list[tuple[float, bool | int | float]]) -> float | None:
    """(p, y) 쌍의 Brier. y 는 0/1 이진 라벨."""
    vals: list[tuple[float, float]] = []
    for p, y in pairs:
        yi = _as01(y)
        if yi is None:
            continue
        vals.append((float(p), yi))
    if not vals:
        return None
    return sum((p - y) ** 2 for p, y in vals) / len(vals)


def log_loss(pairs: list[tuple[float, bool | int | float]], *, eps: float = 1e-15) -> float | None:
    """(p, y) 쌍의 log-loss. p 는 [0,1] 클램프."""
    vals: list[tuple[float, float]] = []
    for p, y in pairs:
        yi = _as01(y)
        if yi is None:
            continue
        pf = min(1.0 - eps, max(eps, float(p)))
        vals.append((pf, yi))
    if not vals:
        return None
    return -sum(y * math.log(p) + (1.0 - y) * math.log(1.0 - p) for p, y in vals) / len(vals)

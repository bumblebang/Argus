"""thesis 무효화 타입 확장 — price | flow | time.

가격 무효화만으로는 서사(수급·시간) 사망을 못 잡는다.
포지션 meta.thesis_invalidation 에 조건을 심고, 감시 루프가 감사한다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .logging_setup import get_logger

log = get_logger("thesis_watch")


@dataclass(frozen=True)
class InvalHit:
    kind: str          # price | flow | time
    symbol: str
    detail: str


def parse_invalidation_spec(meta: dict | None) -> dict:
    """meta.thesis_invalidation → 정규화.

    예:
      {"price": 95000,                    # 기존 invalidation 가와 동일 역할
       "flow": {"foreign_net_days": 3, "sign": -1},  # 3거래일 연속 외국인 순매도
       "time": {"max_days": 20}}          # horizon 과 별도 thesis 시한
    """
    raw = (meta or {}).get("thesis_invalidation") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    if raw.get("price") is not None:
        try:
            out["price"] = float(raw["price"])
        except (TypeError, ValueError):
            pass
    fl = raw.get("flow")
    if isinstance(fl, dict):
        out["flow"] = {
            "foreign_net_days": int(fl.get("foreign_net_days") or 0),
            "sign": int(fl.get("sign") or -1),  # -1=순매도 연속
        }
    tm = raw.get("time")
    if isinstance(tm, dict) and tm.get("max_days") is not None:
        try:
            out["time"] = {"max_days": max(1, int(tm["max_days"]))}
        except (TypeError, ValueError):
            pass
    return out


def check_price(price: float | None, spec: dict, symbol: str) -> InvalHit | None:
    lim = spec.get("price")
    if lim is None or price is None:
        return None
    if price < lim:
        return InvalHit("price", symbol, f"가격 {price:g} < 무효화 {lim:g}")
    return None


def check_time(opened_at: float | None, now: float, spec: dict,
               symbol: str) -> InvalHit | None:
    tm = spec.get("time")
    if not tm or opened_at is None:
        return None
    held = (now - float(opened_at)) / 86400.0
    if held >= tm["max_days"]:
        return InvalHit("time", symbol,
                        f"thesis 시한 {held:.1f}일 >= {tm['max_days']}일")
    return None


def check_flow(flow_streak: int | None, spec: dict, symbol: str) -> InvalHit | None:
    """flow_streak: 순매도(sign=-1) 연속 일수. 호출측이 계산해 넘긴다."""
    fl = spec.get("flow")
    if not fl or not fl.get("foreign_net_days"):
        return None
    need = fl["foreign_net_days"]
    if flow_streak is not None and flow_streak >= need:
        return InvalHit("flow", symbol,
                        f"외국인 순매도 {flow_streak}일 >= {need}일")
    return None


def audit_position(pos: dict, *, price: float | None, now: float,
                   flow_streak: int | None = None) -> list[InvalHit]:
    """열린 포지션 1건 감사. hits 가 있으면 thesis 사망 후보."""
    meta = pos.get("meta")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            meta = {}
    if not isinstance(meta, dict):
        meta = {}
    # 레거시: stop/dossier invalidation 만 있으면 price 스펙으로 승격
    spec = parse_invalidation_spec(meta)
    if "price" not in spec and pos.get("stop_price"):
        try:
            spec["price"] = float(pos["stop_price"])
        except (TypeError, ValueError):
            pass
    if not spec:
        return []
    sym = str(pos.get("symbol") or "")
    hits = []
    for fn, args in (
        (check_price, (price, spec, sym)),
        (check_time, (pos.get("opened_at"), now, spec, sym)),
        (check_flow, (flow_streak, spec, sym)),
    ):
        h = fn(*args)
        if h:
            hits.append(h)
    return hits


def default_spec_from_dossier(dossier: dict | None, horizon: str | None) -> dict:
    """도시에서 thesis_invalidation 시드. Athena/뇌가 안 넣어도 가격+시간 기본."""
    out: dict[str, Any] = {}
    if dossier and dossier.get("invalidation") is not None:
        out["price"] = float(dossier["invalidation"])
    hz = (horizon or "swing").lower()
    max_days = {"day": 2, "swing": 20, "position": 120}.get(hz, 20)
    out["time"] = {"max_days": max_days}
    out["flow"] = {"foreign_net_days": 3, "sign": -1}
    return out


def flow_streak_from_market_state(ms: dict | None, symbol: str) -> int | None:
    """market_state.flows[symbol].foreign_net_streak — 없으면 None."""
    if not ms or not symbol:
        return None
    flows = (ms.get("flows") or {}).get(symbol)
    if not isinstance(flows, dict):
        return None
    v = flows.get("foreign_net_streak")
    if v is None:
        return None
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return None

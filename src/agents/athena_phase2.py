"""Athena Phase 2 — 존 근접 우선순위·이벤트 큐·레벨-only 갱신.

2-2: covered 종목을 존 근접(in→below→above→unknown) 순으로 재소환.
2-1: 갭·무효화 임박 감지 → athena_queue 이벤트.
2-3: 신선 bullish + 소폭 가격변동 시 레벨-only LLM 갱신.
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterable

from ..logging_setup import get_logger
from .schemas import DossierLevelOutput, DossierOutput

log = get_logger("agents.athena_phase2")

ATHENA_QUEUE_KIND = "athena_queue"

ZONE_RANK = {"in": 0, "below": 1, "above": 2, "unknown": 3}


def _row_dict(row) -> dict | None:
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    try:
        return dict(row)
    except (TypeError, ValueError):
        return None


def phase2_cfg(cfg) -> dict[str, Any]:
    """athena.phase2 설정 + 기본값."""
    raw = getattr(cfg, "raw", cfg) if not isinstance(cfg, dict) else cfg
    if not isinstance(raw, dict):
        raw = {}
    p2 = (raw.get("athena") or {}).get("phase2") or {}
    return {
        "enabled": bool(p2.get("enabled", True)),
        "gap_pct": float(p2.get("gap_pct", 4.0)),
        "invalidation_near_pct": float(p2.get("invalidation_near_pct", 0.02)),
        "level_only_max_move_pct": float(p2.get("level_only_max_move_pct", 0.05)),
        "queue_cooldown_hours": float(p2.get("queue_cooldown_hours", 6)),
        "queue_since_hours": float(p2.get("queue_since_hours", 24)),
    }


def zone_loc(price: float | None, lo, hi) -> str | None:
    if price is None or lo is None or hi is None:
        return None
    try:
        px, lo_f, hi_f = float(price), float(lo), float(hi)
    except (TypeError, ValueError):
        return None
    if px < lo_f:
        return "below"
    if px > hi_f:
        return "above"
    return "in"


def zone_rank(loc: str | None) -> int:
    return ZONE_RANK.get(loc or "unknown", 3)


def prices_from_market_state(ms: dict | None,
                             symbols: Iterable[str] | None = None) -> dict[str, float]:
    """market_state 에서 종목별 현재가 추출(candidates·fundamentals·flows·positioning)."""
    if not ms:
        return {}
    want = set(symbols) if symbols is not None else None
    out: dict[str, float] = {}

    def _put(sym: str | None, val) -> None:
        if not sym or (want is not None and sym not in want):
            return
        if sym in out:
            return
        try:
            px = float(val)
            if px > 0:
                out[str(sym)] = px
        except (TypeError, ValueError):
            pass

    for c in ms.get("candidates") or []:
        if not isinstance(c, dict):
            continue
        sym = c.get("symbol")
        for k in ("price", "last", "close"):
            if c.get(k) is not None:
                _put(sym, c[k])
                break

    for bucket in ("fundamentals", "flows", "positioning"):
        for sym, row in (ms.get(bucket) or {}).items():
            if not isinstance(row, dict):
                continue
            for k in ("price", "last", "close"):
                if row.get(k) is not None:
                    _put(str(sym), row[k])
                    break
    return out


def sort_covered_by_zone(symbols: list[str], store, prices: dict[str, float],
                         covered_at: dict[str, float]) -> list[str]:
    """커버된 종목: 존 근접 우선, 동순위는 오래된 도시에 먼저."""

    def _key(sym: str) -> tuple[int, float]:
        row = _row_dict(store.get_fresh_dossier(sym))
        lo = hi = None
        if row:
            lo, hi = row.get("entry_low"), row.get("entry_high")
        loc = zone_loc(prices.get(sym), lo, hi)
        return zone_rank(loc), float(covered_at.get(sym, 0))

    return sorted(symbols, key=_key)


def _athena_queued(store, market: str, since_hours: float = 24.0) -> list[str]:
    """athena_queue(route=queue) 종목 — held 다음 우선 재소환."""
    mkt = str(market or "").upper()
    out: list[str] = []
    try:
        rows = store.recent_events(
            ATHENA_QUEUE_KIND, time.time() - since_hours * 3600, limit=80)
        for r in rows:
            p = json.loads(r["payload"]) if r["payload"] else {}
            if p.get("route") != "queue":
                continue
            sym = r["symbol"]
            if not sym:
                continue
            pm = str(p.get("market") or "").upper()
            if pm:
                if pm != mkt:
                    continue
            else:
                looks_kr = str(sym).isdigit() and len(str(sym)) == 6
                if mkt == "KR" and not looks_kr:
                    continue
                if mkt == "US" and looks_kr:
                    continue
            out.append(sym)
    except Exception as e:
        log.warning("athena_queue 조회 실패(무시): %s", e)
    return list(dict.fromkeys(out))


def enqueue_athena(store, symbol: str, market: str, reason: str, **extra) -> None:
    store.log_event(ATHENA_QUEUE_KIND, symbol,
                    {"route": "queue", "market": market, "reason": reason, **extra})


def was_recently_queued(store, symbol: str, *, reason: str | None = None,
                        hours: float = 6.0) -> bool:
    try:
        rows = store.recent_events(
            ATHENA_QUEUE_KIND, time.time() - hours * 3600, limit=30)
    except Exception:
        return False
    for r in rows:
        if r["symbol"] != symbol:
            continue
        try:
            p = json.loads(r["payload"]) if r["payload"] else {}
        except (TypeError, ValueError):
            p = {}
        if p.get("route") != "queue":
            continue
        if reason is None or p.get("reason") == reason:
            return True
    return False


def scan_athena_triggers(store, p2cfg: dict, live_prices: dict[str, float], *,
                         market_state: dict | None = None,
                         ma20: dict | None = None,
                         symbols: Iterable[str] | None = None,
                         now: float | None = None) -> int:
    """감시 루프 훅 — 갭·무효화 임박 시 athena_queue 등록. 등록 건수 반환."""
    if not p2cfg.get("enabled", True):
        return 0
    now = now or time.time()
    gap_thr = float(p2cfg.get("gap_pct", 4.0))
    inv_near = float(p2cfg.get("invalidation_near_pct", 0.02))
    cooldown = float(p2cfg.get("queue_cooldown_hours", 6))
    ms_prices = prices_from_market_state(market_state)
    ma20 = ma20 or {}
    syms = list(symbols) if symbols is not None else list(live_prices.keys())
    enqueued = 0

    for sym in syms:
        price = live_prices.get(sym)
        if price is None or price <= 0:
            continue
        market = "KR" if str(sym).isdigit() and len(str(sym)) == 6 else "US"

        prev = (ma20.get(sym) or {}).get("close")
        if prev is None:
            prev = ms_prices.get(sym)
        if prev and prev > 0:
            gap = abs((price - float(prev)) / float(prev) * 100)
            if gap >= gap_thr and not was_recently_queued(
                    store, sym, reason="gap", hours=cooldown):
                enqueue_athena(store, sym, market, "gap",
                               gap_pct=round(gap, 2), price=price)
                enqueued += 1
                continue

        row = _row_dict(store.get_fresh_dossier(sym, now=now))
        if not row:
            continue
        inv = row.get("invalidation")
        if inv is None:
            continue
        try:
            inv_f = float(inv)
        except (TypeError, ValueError):
            continue
        if inv_f <= 0:
            continue
        dist = (price - inv_f) / price
        if 0 <= dist <= inv_near and not was_recently_queued(
                store, sym, reason="invalidation_near", hours=cooldown):
            enqueue_athena(store, sym, market, "invalidation_near",
                           dist_pct=round(dist * 100, 2), price=price,
                           invalidation=inv_f)
            enqueued += 1
    return enqueued


def _parse_evidence(row: dict) -> dict:
    ev_raw = row.get("evidence")
    if isinstance(ev_raw, str):
        try:
            return json.loads(ev_raw) if ev_raw else {}
        except (TypeError, ValueError):
            return {}
    return dict(ev_raw) if isinstance(ev_raw, dict) else {}


def dossier_ref_price(row: dict) -> float | None:
    """도시에 작성 시점 기준가 — evidence.ref_price 우선, 없으면 존 중앙."""
    ev = _parse_evidence(row)
    rp = ev.get("ref_price")
    if rp is not None:
        try:
            v = float(rp)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    lo, hi = row.get("entry_low"), row.get("entry_high")
    try:
        if lo is not None and hi is not None:
            lo_f, hi_f = float(lo), float(hi)
            if lo_f > 0 and hi_f > 0:
                return (lo_f + hi_f) / 2
    except (TypeError, ValueError):
        pass
    for k in ("invalidation", "target"):
        v = row.get(k)
        if v is not None:
            try:
                vf = float(v)
                if vf > 0:
                    return vf
            except (TypeError, ValueError):
                pass
    return None


def _dossier_stance(row) -> str:
    row = _row_dict(row) or {}
    ev = _parse_evidence(row)
    if ev.get("stance"):
        return str(ev["stance"]).lower()
    return ""


def should_level_only(store, sym: str, price: float | None, p2cfg: dict,
                      *, now: float | None = None) -> tuple[bool, dict | None]:
    """신선 bullish 도시에 + 가격 변동이 작으면 레벨-only 갱신 후보."""
    if not p2cfg.get("enabled", True) or price is None or price <= 0:
        return False, None
    row = _row_dict(store.get_fresh_dossier(sym, now=now))
    if not row:
        return False, None
    if _dossier_stance(row) != "bullish":
        return False, None
    ref = dossier_ref_price(row)
    if ref is None or ref <= 0:
        return False, None
    move = abs(price - ref) / ref
    max_move = float(p2cfg.get("level_only_max_move_pct", 0.05))
    if move > max_move:
        return False, None
    return True, dict(row)


def merge_level_refresh(prev_row: dict, level: DossierLevelOutput) -> DossierOutput:
    """레벨-only 결과를 기존 도시에와 병합 — thesis·evidence 유지."""
    ev_raw = prev_row.get("evidence")
    if isinstance(ev_raw, str):
        try:
            ev = json.loads(ev_raw) if ev_raw else {}
        except (TypeError, ValueError):
            ev = {}
    elif isinstance(ev_raw, dict):
        ev = dict(ev_raw)
    else:
        ev = {}
    thesis = str(prev_row.get("thesis") or "")
    if level.level_note:
        thesis = f"{thesis} [레벨갱신] {level.level_note}".strip()
    return DossierOutput(
        stance=level.stance,
        thesis=thesis,
        horizon=ev.get("horizon") or "swing",
        entry_low=level.entry_low,
        entry_high=level.entry_high,
        invalidation=level.invalidation,
        target=level.target,
        conviction=level.conviction,
        evidence=list(ev.get("evidence") or []),
        key_risks=list(ev.get("key_risks") or []),
    )


ATHENA_LEVEL_SYSTEM = """\
당신은 Athena의 레벨 갱신 모드다. 기존 도시에(thesis·증거)는 유지하고, 현재가·
기술적 요약 변화에 맞춰 진입존·무효화·목표·확신도만 조정한다.

원칙:
- existing_dossier 의 thesis·evidence 를 전제로 한다. 논리가 깨졌으면 stance 를
  neutral/bearish 로, 살아 있으면 bullish 유지.
- bullish 면 4개 레벨 필수: invalidation < entry_low <= entry_high < target.
  손익비 1.5 미만이면 bullish 재고.
- level_note 에 레벨을 왜 조정했는지 한 줄(없으면 null).
- 데이터에 없는 사실을 지어내지 마라."""


def build_level_context(base_ctx: dict, prev_row: dict) -> dict:
    """레벨-only LLM 입력 — 기존 도시에 + 축약 컨텍스트."""
    ev_raw = prev_row.get("evidence")
    if isinstance(ev_raw, str):
        try:
            ev = json.loads(ev_raw) if ev_raw else {}
        except (TypeError, ValueError):
            ev = {}
    else:
        ev = dict(ev_raw) if isinstance(ev_raw, dict) else {}
    return {
        "mode": "level_refresh",
        "symbol": base_ctx.get("symbol"),
        "market": base_ctx.get("market"),
        "technical": base_ctx.get("technical"),
        "existing_dossier": {
            "stance": ev.get("stance"),
            "thesis": prev_row.get("thesis"),
            "entry_low": prev_row.get("entry_low"),
            "entry_high": prev_row.get("entry_high"),
            "invalidation": prev_row.get("invalidation"),
            "target": prev_row.get("target"),
            "conviction": prev_row.get("conviction"),
            "horizon": ev.get("horizon"),
        },
    }

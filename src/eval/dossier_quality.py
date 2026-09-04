"""도시어(Athena) 품질 리포트 — Tier 0 측정.

stance 분포·커버리지·존 위치·(선택) target-before-stop 라벨을 한 번에 집계한다.
프롬프트/가중치 변경 전·후 비교용. 승격 판단용이 아니다.
"""
from __future__ import annotations

import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .labels import MIN_N, target_hit_before_stop
from ..agents.athena_phase2 import resolve_symbol_prices, zone_loc
from ..config import ROOT


def dossier_stance(row: dict) -> str:
    """도시어 행에서 stance — evidence JSON 또는 top-level."""
    st = row.get("stance")
    if st:
        return str(st).lower()
    ev = row.get("evidence")
    if isinstance(ev, str):
        try:
            ev = json.loads(ev) if ev else {}
        except (TypeError, ValueError):
            ev = {}
    if isinstance(ev, dict):
        return str(ev.get("stance") or "").lower()
    return ""


def _load_market_state(path: Path | None) -> dict:
    if not path or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _universe_symbols(cfg: dict | None) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {"KR": set(), "US": set()}
    uni = (cfg or {}).get("universe") or {}
    for mkt, items in uni.items():
        key = str(mkt).upper()
        if key not in out:
            out[key] = set()
        for it in items or []:
            if isinstance(it, dict) and it.get("symbol"):
                out[key].add(str(it["symbol"]))
    return out


def _sanitize_downgraded(row: dict) -> bool:
    ev = row.get("evidence")
    if isinstance(ev, str):
        try:
            ev = json.loads(ev) if ev else {}
        except (TypeError, ValueError):
            return False
    if not isinstance(ev, dict):
        return False
    notes = ev.get("sanitize_notes") or []
    return bool(notes)


def summarize_dossiers(
    store,
    *,
    cfg: dict | None = None,
    data_dir: Path | str | None = None,
    market_state_path: Path | str | None = None,
    now: float | None = None,
    label_days: float = 60.0,
) -> dict[str, Any]:
    """신선 도시어(심볼별 최신·미만료) 품질 집계."""
    now = now or time.time()
    rows = [dict(r) for r in store.list_fresh_dossiers(now=now)]
    data_path = Path(data_dir) if data_dir else (ROOT / "data")
    ms_path = Path(market_state_path) if market_state_path else None
    ms = _load_market_state(ms_path)
    syms = [str(r.get("symbol") or "") for r in rows if r.get("symbol")]
    acct_path = data_path / "account_snapshot.json"
    prices, price_meta = resolve_symbol_prices(
        syms,
        market_state=ms,
        store=store,
        data_dir=data_path,
        account_snapshot_path=acct_path if acct_path.is_file() else None,
    )
    # 리포트 JSON 이 커지지 않게 심볼별 sources 맵은 빼고 카운트만
    price_meta_out = {k: v for k, v in price_meta.items() if k != "sources"}
    uni = _universe_symbols(cfg)

    stance_ct = {"bullish": 0, "neutral": 0, "bearish": 0, "unknown": 0}
    zone_ct = {"in": 0, "below": 0, "above": 0, "unknown": 0}
    rr_vals: list[float] = []
    age_hours: list[float] = []
    sanitized = 0
    bullish_levels = 0
    bullish_n_zone = 0

    for r in rows:
        st = dossier_stance(r) or "unknown"
        if st not in stance_ct:
            st = "unknown"
        stance_ct[st] += 1
        age_hours.append(max(0.0, (now - float(r.get("created_at") or now)) / 3600))
        if _sanitize_downgraded(r):
            sanitized += 1
        if st == "bullish":
            levels = (r.get("entry_low"), r.get("entry_high"),
                      r.get("invalidation"), r.get("target"))
            if all(v is not None for v in levels):
                bullish_levels += 1
            rr = r.get("rr")
            if rr is not None:
                try:
                    rr_vals.append(float(rr))
                except (TypeError, ValueError):
                    pass
            sym = str(r.get("symbol") or "")
            loc = zone_loc(prices.get(sym), r.get("entry_low"), r.get("entry_high"))
            zone_ct[loc or "unknown"] += 1
            bullish_n_zone += 1

    fresh_syms = {str(r.get("symbol")) for r in rows if r.get("symbol")}
    coverage: dict[str, Any] = {}
    for mkt, usyms in uni.items():
        if not usyms:
            continue
        covered = fresh_syms & usyms
        coverage[mkt] = {
            "universe": len(usyms),
            "fresh": len(covered),
            "pct": round(len(covered) / len(usyms), 3) if usyms else None,
            "uncovered": len(usyms - covered),
        }

    outcomes = _label_outcomes(store, data_dir=data_path, cfg=cfg,
                               since=now - label_days * 86400, now=now)

    n = len(rows)
    bullish_n = stance_ct["bullish"]
    zone_unknown_rate = (
        round(zone_ct["unknown"] / bullish_n_zone, 3) if bullish_n_zone else None)
    return {
        "asof": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "fresh_count": n,
        "stance": stance_ct,
        "bullish_pct": round(bullish_n / n, 3) if n else None,
        "bullish_with_levels": bullish_levels,
        "sanitize_downgraded_latest": sanitized,
        "age_hours": _percentiles(age_hours),
        "rr_bullish": _rr_stats(rr_vals),
        "zone_bullish": zone_ct,
        "zone_unknown_rate": zone_unknown_rate,
        "price_coverage": price_meta_out,
        "coverage": coverage,
        "outcomes": outcomes,
        "note": ("Tier 0 도시어 품질. stance 라벨 평가는 outcomes 참고. "
                 "프롬프트 승격 근거로 단독 쓰지 말 것(min_n). "
                 "zone_unknown_rate 높으면 가격 센서 실패(승격 아님)."),
    }


def _percentiles(vals: list[float]) -> dict[str, float | None]:
    if not vals:
        return {"p50": None, "p90": None, "max": None}
    s = sorted(vals)
    def pct(p: float) -> float:
        i = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
        return round(s[i], 2)
    return {"p50": pct(0.5), "p90": pct(0.9), "max": round(max(s), 2)}


def _rr_stats(vals: list[float]) -> dict[str, Any]:
    if not vals:
        return {"n": 0, "mean": None, "median": None, "below_1_5": 0}
    below = sum(1 for v in vals if v < 1.5)
    return {
        "n": len(vals),
        "mean": round(statistics.mean(vals), 2),
        "median": round(statistics.median(vals), 2),
        "below_1_5": below,
    }


def _label_outcomes(
    store,
    *,
    data_dir: Path | str | None,
    cfg: dict | None,
    since: float,
    now: float,
) -> dict[str, Any]:
    """최근 생성 bullish 도시어의 target-before-stop 라벨(가능할 때만)."""
    if not data_dir:
        return {"n": 0, "status": "no_data_dir", "by_stance": {},
                "skipped": {}}
    data_dir = Path(data_dir)
    with store._lock:
        hist = store.conn.execute(
            "SELECT symbol, created_at, target, invalidation, evidence "
            "FROM dossiers WHERE created_at >= ? AND created_at <= ? "
            "ORDER BY created_at DESC",
            (since, now)).fetchall()
    by_stance: dict[str, dict[str, Any]] = {}
    skipped: dict[str, int] = {}
    for row in hist:
        r = dict(row)
        st = dossier_stance(r)
        if st != "bullish":
            skipped["not_bullish"] = skipped.get("not_bullish", 0) + 1
            continue
        tgt, inv = r.get("target"), r.get("invalidation")
        if tgt is None or inv is None:
            skipped["no_levels"] = skipped.get("no_levels", 0) + 1
            continue
        hz = "swing"
        ev = r.get("evidence")
        if isinstance(ev, str):
            try:
                ev = json.loads(ev) if ev else {}
            except (TypeError, ValueError):
                ev = {}
        if isinstance(ev, dict) and ev.get("horizon"):
            hz = str(ev["horizon"])
        hit = target_hit_before_stop(
            data_dir, str(r["symbol"]),
            datetime.fromtimestamp(float(r["created_at"]), tz=timezone.utc),
            target=tgt, invalidation=inv, horizon=hz, cfg=cfg)
        lab = hit.get("target_hit_before_stop")
        reason = str(hit.get("reason") or "unresolved")
        if lab is None:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        bucket = by_stance.setdefault(st, {"n": 0, "target_first": 0})
        bucket["n"] += 1
        if lab is True:
            bucket["target_first"] += 1
    total_n = sum(b["n"] for b in by_stance.values())
    for b in by_stance.values():
        b["target_first_rate"] = round(b["target_first"] / b["n"], 3) if b["n"] else None
    status = "scored" if total_n >= MIN_N else "shadow_only"
    return {
        "n": total_n,
        "min_n": MIN_N,
        "status": status,
        "window_days": round((now - since) / 86400, 1),
        "by_stance": by_stance,
        "skipped": skipped,
    }

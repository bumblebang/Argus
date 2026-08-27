"""판단 단위 채점 — 저널 액션 vs 라벨 vs 널 (LLM 콜 0).

포트 복리 없음. min_n 미달이면 shadow_only. 승격 불가.
"""
from __future__ import annotations

import gzip
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ..eval_protocol import can_promote, shadow_only_label
from ..shadow_ledger import parse_ts
from .archive import load_context, parse_context
from .labels import MIN_N, forward_return, policy_return, target_hit_before_stop
from .labels import brier_score, log_loss
from .null_manager import eligible_candidates, null_cash, null_random_gated


def _parse_min_date(s: str | date | None) -> date | None:
    if s is None:
        return None
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    text = str(s)[:10]
    return date.fromisoformat(text)


def _rec_date(rec: dict) -> date | None:
    ts = parse_ts(rec.get("ts"))
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def _side_map(rec: dict, candidates: list[dict]) -> dict[str, str]:
    """제안 없는 후보 = HOLD."""
    sides = {str(c.get("symbol")): "HOLD" for c in candidates if c.get("symbol")}
    for p in rec.get("proposals") or []:
        if not isinstance(p, dict):
            continue
        sym = p.get("symbol")
        if not sym:
            continue
        sides[str(sym)] = str(p.get("side") or "HOLD").upper()
    return sides


def _horizon_of(rec: dict, symbol: str) -> str:
    for p in rec.get("proposals") or []:
        if isinstance(p, dict) and p.get("symbol") == symbol:
            return str(p.get("horizon") or "swing")
    return "swing"


def _p_target_of(rec: dict, symbol: str) -> float | None:
    for p in rec.get("proposals") or []:
        if isinstance(p, dict) and p.get("symbol") == symbol:
            val = p.get("p_target_before_stop")
            if val is None:
                return None
            try:
                return float(val)
            except (TypeError, ValueError):
                return None
    return None


def _n_buy(sides: dict[str, str]) -> int:
    return sum(1 for s in sides.values() if s == "BUY")


def score_journal(*, journal_path: Path | str, data_dir: Path | str,
                  cfg: dict | None = None,
                  min_date: str | date | None = None,
                  min_n: int = MIN_N,
                  require_dossier: bool = True) -> dict[str, Any]:
    """score-live: 아카이브+저널만으로 현재 매니저 vs 널 채점."""
    journal_path = Path(journal_path)
    data_dir = Path(data_dir)
    cutoff = _parse_min_date(min_date)
    rows: list[dict] = []
    skipped_no_ctx = 0
    skipped_date = 0
    if not journal_path.exists():
        return _summary(rows, min_n=min_n, skipped_no_ctx=0, skipped_date=0)

    with journal_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            d = _rec_date(rec)
            if cutoff and d is not None and d < cutoff:
                skipped_date += 1
                continue
            ref = rec.get("context_ref")
            if not ref:
                skipped_no_ctx += 1
                continue
            try:
                raw = load_context(journal_path, ref,
                                   expected_sha256=rec.get("context_sha256"))
            except (OSError, ValueError, gzip.BadGzipFile):
                skipped_no_ctx += 1
                continue
            ctx = parse_context(raw)
            cands = [c for c in (ctx.get("candidates") or ctx.get("universe") or [])
                     if isinstance(c, dict)]
            live = _side_map(rec, cands)
            cycle_ts = parse_ts(rec.get("ts")) or 0.0
            n_buy = _n_buy(live)
            cash = null_cash(cands)
            gated = null_random_gated(ctx, cycle_ts=cycle_ts, n_buy=n_buy,
                                      require_dossier=require_dossier)
            elig_syms = {str(c.get("symbol")) for c in
                         eligible_candidates(ctx, require_dossier=require_dossier)}
            asof = (ctx.get("asof") or rec.get("ts"))
            epoch = ((rec.get("manager") or {}).get("epoch"))
            for c in cands:
                sym = str(c.get("symbol") or "")
                if not sym:
                    continue
                hz = _horizon_of(rec, sym)
                lab = forward_return(data_dir, sym, asof, horizon=hz, cfg=cfg)
                dossier = c.get("dossier") if isinstance(c.get("dossier"), dict) else {}
                hit = target_hit_before_stop(
                    data_dir, sym, asof, target=dossier.get("target"),
                    invalidation=dossier.get("invalidation"),
                    horizon=hz, cfg=cfg)
                live_side = live.get(sym, "HOLD")
                p_tgt = _p_target_of(rec, sym)
                rows.append({
                    "ts": rec.get("ts"),
                    "symbol": sym,
                    "horizon": hz,
                    "epoch": epoch,
                    "live_side": live_side,
                    "null_cash_side": cash.get(sym, "HOLD"),
                    "null_gated_side": gated.get(sym, "HOLD"),
                    "in_eligible_pool": sym in elig_syms,
                    "fwd_ret": lab["fwd_ret"],
                    "live_policy": policy_return(live_side, lab["fwd_ret"]),
                    "null_cash_policy": policy_return(cash.get(sym, "HOLD"), lab["fwd_ret"]),
                    "null_gated_policy": policy_return(gated.get(sym, "HOLD"), lab["fwd_ret"]),
                    "target_hit_before_stop": hit.get("target_hit_before_stop"),
                    "hit_reason": hit.get("reason"),
                    "p_target_before_stop": p_tgt,
                })
    return _summary(rows, min_n=min_n, skipped_no_ctx=skipped_no_ctx,
                    skipped_date=skipped_date)


def _mean(xs: list[float | None]) -> float | None:
    vals = [x for x in xs if x is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _summary(rows: list[dict], *, min_n: int, skipped_no_ctx: int,
             skipped_date: int) -> dict[str, Any]:
    scored = [r for r in rows if r.get("fwd_ret") is not None]
    n = len(scored)
    live_m = _mean([r["live_policy"] for r in scored])
    cash_m = _mean([r["null_cash_policy"] for r in scored])
    gated_m = _mean([r["null_gated_policy"] for r in scored])
    delta = None if live_m is None or gated_m is None else live_m - gated_m
    same = [r for r in scored if r.get("in_eligible_pool")]
    other = [r for r in scored if not r.get("in_eligible_pool")]

    def _delta(subset: list[dict]) -> float | None:
        lm = _mean([r["live_policy"] for r in subset])
        gm = _mean([r["null_gated_policy"] for r in subset])
        if lm is None or gm is None:
            return None
        return lm - gm
    status = "shadow_only" if n < min_n else "scored"
    promote_ok, promote_why = can_promote(
        change="replay_score", evidence_n=n)
    if n < min_n:
        promote_ok, promote_why = False, shadow_only_label({"n": n})
    by_side: dict[str, int] = {}
    for r in rows:
        s = r.get("live_side") or "?"
        by_side[s] = by_side.get(s, 0) + 1
    proper_pairs = [
        (r["p_target_before_stop"], r["target_hit_before_stop"])
        for r in scored
        if r.get("p_target_before_stop") is not None
        and r.get("target_hit_before_stop") is not None
    ]
    n_proper = len(proper_pairs)
    proper_status = "shadow_only" if n_proper < min_n else "scored"
    brier = logloss = None
    if proper_pairs:
        br = brier_score(proper_pairs)
        ll = log_loss(proper_pairs)
        brier = round(br, 4) if br is not None else None
        logloss = round(ll, 4) if ll is not None else None
    return {
        "n": n,
        "n_rows": len(rows),
        "min_n": min_n,
        "status": status,
        "mean_live_policy": live_m,
        "mean_null_cash_policy": cash_m,
        "mean_null_gated_policy": gated_m,
        "delta_vs_gated": delta,
        "delta_decomp": {
            "same_pool": _delta(same),
            "gate_diff": _delta(other),
            "n_same_pool": len(same),
            "n_gate_diff": len(other),
            "note": ("same_pool=라이브와 같은 bullish 풀에서의 Δ. "
                     "gate_diff=풀 불일치(stance/존). 합산 Δ로 스킬 주장 금지."),
        },
        "by_side": by_side,
        "skipped_no_ctx": skipped_no_ctx,
        "skipped_date": skipped_date,
        "proper_score": {
            "n": n_proper,
            "min_n": min_n,
            "status": proper_status,
            "brier": brier,
            "log_loss": logloss,
            "note": ("p_target_before_stop vs target_hit_before_stop. "
                     "conviction Brier 와 혼동 금지. min_n 미달=shadow_only."),
        },
        "can_promote": False,
        "promote_ok": promote_ok,
        "promote_why": promote_why,
        "note": ("판단 단위 채점. 포트 복리 없음. 널 대비 전에 스킬 주장 금지. "
                 "리플레이 Δ 로 메인/슬리브 승격 금지."),
        "rows": rows,
    }

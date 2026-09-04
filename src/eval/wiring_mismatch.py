"""배선 mismatch 집계 — decisions.jsonl + context_archive.

LIVE_OBSERVATION §C: fit vs 배정 · horizon vs 카탈로그 · close_scan↔day 겹침 등.
같은 패턴이 ≥3건/2주 일 때만 배선 조정 안건. 승격·튜닝 근거로 쓰지 말 것.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from ..config import ROOT
from .archive import load_context, parse_context
from ..strategies import REGISTRY

DAY_STRATEGIES = frozenset({"volatility_breakout", "bollinger_breakout"})
TREND_STRATEGIES = frozenset({
    "momentum", "donchian_breakout", "ma_crossover", "macd",
})
REVERSION_STRATEGIES = frozenset({"rsi_reversion", "bollinger_reversion"})
GAP_POOLS = frozenset({"close_scan", "gap", "gap_rebound", "nxt_gap"})
DAY_POOLS = frozenset({"day", "discovery", "day_pool"})

DEFAULT_THRESHOLD = 3
DEFAULT_WINDOW_DAYS = 14


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _catalog_horizon(strategy: str | None) -> str | None:
    if not strategy:
        return None
    cls = REGISTRY.get(str(strategy))
    return getattr(cls, "horizon", None) if cls else None


def _cand_by_symbol(ctx: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for c in ctx.get("candidates") or []:
        if isinstance(c, dict) and c.get("symbol"):
            out[str(c["symbol"])] = c
    return out


def classify_buy(
    proposal: dict,
    candidate: dict | None,
    *,
    cycle_ts: str | None = None,
) -> list[dict[str, Any]]:
    """단일 BUY proposal → mismatch 이벤트 리스트(없으면 [])."""
    if str(proposal.get("side") or "").upper() != "BUY":
        return []
    cand = candidate or {}
    sym = str(proposal.get("symbol") or "")
    strat = proposal.get("strategy")
    hz = proposal.get("horizon")
    pool = str(cand.get("pool") or "").lower() or None
    mom = cand.get("momentum_20d")
    if mom is None:
        mom = cand.get("momentum")
    try:
        mom_f = float(mom) if mom is not None else None
    except (TypeError, ValueError):
        mom_f = None

    fit = cand.get("strategy_fit") if isinstance(cand.get("strategy_fit"), dict) else {}
    best = fit.get("best")
    thin = bool(fit.get("thin_sample"))

    hits: list[dict[str, Any]] = []

    def _hit(kind: str, **extra: Any) -> None:
        hits.append({
            "kind": kind,
            "symbol": sym,
            "market": proposal.get("market"),
            "strategy": strat,
            "horizon": hz,
            "pool": pool,
            "fit_best": best,
            "thin_sample": thin,
            "momentum_20d": mom_f,
            "ts": cycle_ts,
            **extra,
        })

    # 1) fit vs 배정 — best 가 있고 thin 아닐 때만
    if best and not thin and strat and str(best) != str(strat):
        _hit("fit_vs_assigned", detail=f"fit={best} assigned={strat}")

    # 2) horizon vs 카탈로그 — close_scan 은 갭반등 전용이라 카탈로그와 다를 수 있음
    cat_hz = _catalog_horizon(str(strat) if strat else None)
    if strat and hz and cat_hz and str(hz) not in ("close_scan",) and str(hz) != str(cat_hz):
        _hit("horizon_vs_catalog",
             detail=f"proposal_hz={hz} catalog_hz={cat_hz}")

    # 3) close_scan / gap 풀 + day 템플릿
    is_gap = (pool in GAP_POOLS) or (str(hz) == "close_scan")
    if is_gap and (str(strat) in DAY_STRATEGIES or str(hz) == "day"):
        _hit("close_scan_day_overlap",
             detail=f"pool={pool} strategy={strat} hz={hz}")

    # 4) day/discovery 풀인데 강한 하락 + 추세 전략 (발굴↑ vs 추격)
    if pool in DAY_POOLS and mom_f is not None and mom_f <= -0.15:
        if str(strat) in TREND_STRATEGIES:
            _hit("discovery_down_trend",
                 detail=f"mom20d={mom_f:.3f} strategy={strat}")

    # 5) day 풀 + 강한 상승 + 회귀 전략 (반대 편향)
    if pool in DAY_POOLS and mom_f is not None and mom_f >= 0.15:
        if str(strat) in REVERSION_STRATEGIES:
            _hit("discovery_up_reversion",
                 detail=f"mom20d={mom_f:.3f} strategy={strat}")

    return hits


def iter_journal_buys(
    journal_path: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Iterable[tuple[dict, dict, dict | None]]:
    """(cycle_rec, proposal, candidate|None) yield. candidate 는 아카이브에서."""
    if not journal_path.is_file():
        return
    # cache context per ref
    ctx_cache: dict[str, dict] = {}
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _parse_ts(rec.get("ts"))
        if since and ts and ts < since:
            continue
        if until and ts and ts > until:
            continue
        buys = [p for p in (rec.get("proposals") or [])
                if isinstance(p, dict) and str(p.get("side") or "").upper() == "BUY"]
        if not buys:
            continue
        by_sym: dict[str, dict] = {}
        ref = rec.get("context_ref")
        if ref:
            if ref not in ctx_cache:
                try:
                    raw = load_context(journal_path, str(ref),
                                       expected_sha256=rec.get("context_sha256"))
                    ctx_cache[ref] = parse_context(raw)
                except Exception:
                    ctx_cache[ref] = {}
            by_sym = _cand_by_symbol(ctx_cache[ref])
        for p in buys:
            yield rec, p, by_sym.get(str(p.get("symbol") or ""))


def summarize_wiring(
    journal_path: Path | str | None = None,
    *,
    window_days: float = DEFAULT_WINDOW_DAYS,
    threshold: int = DEFAULT_THRESHOLD,
    now: datetime | None = None,
    data_dir: Path | str | None = None,
) -> dict[str, Any]:
    """최근 window_days BUY 배선 mismatch 요약."""
    data = Path(data_dir) if data_dir else (ROOT / "data")
    journal = Path(journal_path) if journal_path else (data / "ledgers" / "decisions.jsonl")
    if not journal.is_file():
        alt = data / "decisions.jsonl"
        if alt.is_file():
            journal = alt
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    since = now - timedelta(days=float(window_days))

    buy_n = 0
    missing_context = 0
    thin_fit_skip = 0
    events: list[dict] = []
    by_kind: Counter[str] = Counter()

    for rec, prop, cand in iter_journal_buys(journal, since=since, until=now):
        buy_n += 1
        if cand is None and not rec.get("context_ref"):
            missing_context += 1
        fit = (cand or {}).get("strategy_fit") if isinstance((cand or {}).get("strategy_fit"), dict) else {}
        if fit.get("thin_sample") and fit.get("best") is None:
            thin_fit_skip += 1
        for hit in classify_buy(prop, cand, cycle_ts=rec.get("ts")):
            events.append(hit)
            by_kind[hit["kind"]] += 1

    flagged = {k: n for k, n in by_kind.items() if n >= int(threshold)}
    examples: dict[str, list] = defaultdict(list)
    for e in events:
        kind = e["kind"]
        if len(examples[kind]) < 5:
            examples[kind].append({
                "symbol": e.get("symbol"),
                "strategy": e.get("strategy"),
                "fit_best": e.get("fit_best"),
                "horizon": e.get("horizon"),
                "pool": e.get("pool"),
                "detail": e.get("detail"),
                "ts": e.get("ts"),
            })

    return {
        "asof": now.isoformat(),
        "window_days": float(window_days),
        "threshold": int(threshold),
        "journal": str(journal),
        "buy_n": buy_n,
        "missing_context": missing_context,
        "thin_fit_noted": thin_fit_skip,
        "mismatch_n": len(events),
        "by_kind": dict(by_kind),
        "flagged_kinds": flagged,
        "actionable": bool(flagged),
        "examples": dict(examples),
        "note": (
            f"같은 kind 가 {threshold}건/{window_days:g}일 이상이면 actionable. "
            "1~2건은 뇌 변동. 승격·파라미터 튜닝 근거 금지."
        ),
    }

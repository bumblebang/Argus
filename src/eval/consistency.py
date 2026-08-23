"""오프라인 일관성 — 동일 컨텍스트 N회 재결정. 라이브 앙상블 없음.

지표: 종목별 side 일치율, Fleiss' kappa, 국면(regime) 버킷.
temperature=0 은 리플레이 전용. 라이브 thinking=adaptive 는 유지.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable


SIDES = ("BUY", "HOLD", "SELL")


def _regime(context: dict) -> str:
    m = context.get("market") or {}
    r = m.get("regime")
    if isinstance(r, dict):
        return str(r.get("label") or r.get("name") or "unknown")
    if r:
        return str(r)
    return "unknown"


def fleiss_kappa(ratings: list[list[str]], categories: tuple[str, ...] = SIDES
                 ) -> float | None:
    """subjects × raters 범주 평점. 표본 없거나 전부 동일 범주면 1 또는 None."""
    if not ratings:
        return None
    n_subj = len(ratings)
    n_raters = len(ratings[0])
    if n_raters < 2 or n_subj < 1:
        return None
    k = len(categories)
    idx = {c: i for i, c in enumerate(categories)}
    p_j = [0.0] * k
    p_i = []
    for row in ratings:
        counts = [0] * k
        for lab in row:
            if lab in idx:
                counts[idx[lab]] += 1
        n = sum(counts) or n_raters
        p_i.append(sum(c * (c - 1) for c in counts) / (n * (n - 1)) if n > 1 else 0.0)
        for j, c in enumerate(counts):
            p_j[j] += c / (n_subj * n)
    p_bar = sum(p_i) / n_subj
    p_e = sum(p * p for p in p_j)
    if abs(1 - p_e) < 1e-12:
        return 1.0 if p_bar >= 1 - 1e-12 else 0.0
    return (p_bar - p_e) / (1 - p_e)


def consistency_report(context: dict, decide_fn: Callable[[str], Any], *,
                       n: int = 5,
                       context_json: str | None = None) -> dict[str, Any]:
    """decide_fn(context_json) -> DecisionOutput. N회 호출, 집행 없음."""
    import json
    blob = context_json if context_json is not None else json.dumps(
        context, ensure_ascii=False)
    runs: list[dict[str, str]] = []
    for _ in range(max(2, int(n))):
        dec = decide_fn(blob)
        sides = {p.symbol: str(p.side).upper() for p in getattr(dec, "proposals", [])}
        runs.append(sides)
    symbols = sorted({s for r in runs for s in r})
    per_symbol: dict[str, Any] = {}
    agree_n = 0
    ratings: list[list[str]] = []
    for sym in symbols:
        labs = [r.get(sym, "HOLD") for r in runs]
        ratings.append(labs)
        mode = max(set(labs), key=labs.count)
        agr = labs.count(mode) / len(labs)
        per_symbol[sym] = {"agreement": agr, "sides": labs, "mode": mode}
        if agr == 1.0:
            agree_n += 1
    overall = (agree_n / len(symbols)) if symbols else None
    return {
        "n_runs": len(runs),
        "n_symbols": len(symbols),
        "exact_agreement": overall,
        "fleiss_kappa": fleiss_kappa(ratings) if ratings else None,
        "regime": _regime(context),
        "per_symbol": per_symbol,
        "note": "오프라인 일관성. 라이브 다수결 앙상블 없음.",
    }


def bucket_by_regime(reports: list[dict]) -> dict[str, dict]:
    buckets: dict[str, list] = defaultdict(list)
    for r in reports:
        buckets[str(r.get("regime") or "unknown")].append(r.get("exact_agreement"))
    out = {}
    for k, xs in buckets.items():
        vals = [x for x in xs if x is not None]
        out[k] = {
            "n": len(xs),
            "mean_agreement": (sum(vals) / len(vals)) if vals else None,
        }
    return out

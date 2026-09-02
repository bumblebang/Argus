"""유니버스 전략 스코어 배치 — core_refresh 직후 1회, 뇌 scan pad·피처용.

wake 마다 100×8 백테스트를 돌리지 않고 data/strategy_scores.json 을 읽는다.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable

from .agents.tools import recommend_strategy
from .config import ROOT
from .logging_setup import get_logger

log = get_logger("src.strategy_scores")

OUT = ROOT / "data" / "strategy_scores.json"


def load_strategy_scores(path: Path | None = None) -> dict[str, dict]:
    """{symbol: {best, ranking}}. 없거나 깨지면 {}."""
    p = path or OUT
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            syms = data.get("symbols") if isinstance(data, dict) else data
            return syms if isinstance(syms, dict) else {}
    except (OSError, ValueError) as e:
        log.warning("strategy_scores 로드 실패: %s", e)
    return {}


def _atomic_write(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)


def _universe_symbols(universe: dict | None) -> set[str]:
    out: set[str] = set()
    for items in (universe or {}).values():
        for it in items or []:
            if isinstance(it, dict) and it.get("symbol"):
                out.add(str(it["symbol"]))
    return out


def refresh_strategy_scores(
    universe: dict,
    fetch_candles: Callable[[str, str], object],
    *,
    dry: bool = False,
    path: Path | None = None,
    now_fn: Callable[[], float] = time.time,
    prune_universe: dict | None = None,
) -> dict[str, dict]:
    """유니버스 전 종목 8전략 간이 백테스트 → JSON 저장. 반환: symbols dict.

    prune_universe 지정 시 해당 유니버스에 없는 심볼 스코어는 제거한다.
    """
    if dry:
        log.info("strategy_scores dry — 스킵")
        return {}
    from .runner import candles_to_df
    import pandas as pd

    out_path = path or OUT
    existing = load_strategy_scores(out_path)
    scores: dict[str, dict] = dict(existing)
    n_ok, n_fail = 0, 0
    for _market, items in (universe or {}).items():
        for it in items or []:
            if not isinstance(it, dict):
                continue
            sym = str(it.get("symbol") or "")
            mkt = str(it.get("market") or _market or "KR")
            if not sym:
                continue
            try:
                raw = fetch_candles(sym, mkt)
                if isinstance(raw, pd.DataFrame):
                    df = raw if not raw.empty else None
                else:
                    df = candles_to_df(raw) if raw else None
                if df is None or len(df) < 20:
                    n_fail += 1
                    continue
                rec = recommend_strategy(df)
                if rec.get("best"):
                    scores[sym] = {
                        "best": rec["best"],
                        "ranking": rec.get("ranking", [])[:3],
                    }
                    n_ok += 1
                else:
                    n_fail += 1
            except Exception as e:
                n_fail += 1
                log.debug("[%s] strategy_scores 실패: %s", sym, e)
    if prune_universe is not None:
        keep = _universe_symbols(prune_universe)
        before = len(scores)
        scores = {k: v for k, v in scores.items() if k in keep}
        if before > len(scores):
            log.info("strategy_scores prune %d→%d (유니버스 밖 제거)", before, len(scores))
    payload = {"asof": now_fn(), "symbols": scores}
    _atomic_write(payload, out_path)
    log.info("strategy_scores %d종목 저장 (실패/스킵 %d) -> %s", n_ok, n_fail, out_path)
    return scores


def pad_score(scores: dict[str, dict], symbol: str) -> float:
    """scan pad 정렬용 — ranking[0].return_pct (없으면 -inf)."""
    rec = scores.get(symbol) or {}
    ranking = rec.get("ranking") or []
    if not ranking:
        return float("-inf")
    try:
        return float(ranking[0].get("return_pct", float("-inf")))
    except (TypeError, ValueError):
        return float("-inf")

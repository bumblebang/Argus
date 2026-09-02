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
DEFAULT_MAX_AGE_HOURS = 36.0
MIN_TRADES_BEST = 3


def _read_payload(path: Path) -> dict | None:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
    except (OSError, ValueError) as e:
        log.warning("strategy_scores 로드 실패: %s", e)
    return None


def strategy_scores_asof(path: Path | None = None) -> float | None:
    """파일 asof(epoch 또는 iso). 없/깨짐 → None."""
    data = _read_payload(path or OUT)
    if not data:
        return None
    raw = data.get("asof")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def strategy_scores_stale(path: Path | None = None, *,
                          max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
                          now_fn: Callable[[], float] = time.time) -> bool:
    """True if missing, unreadable, or older than max_age_hours."""
    if max_age_hours <= 0:
        return False
    ts = strategy_scores_asof(path)
    if ts is None:
        return True
    return (now_fn() - ts) > max_age_hours * 3600


def load_strategy_scores(path: Path | None = None, *,
                         max_age_hours: float | None = DEFAULT_MAX_AGE_HOURS,
                         now_fn: Callable[[], float] = time.time) -> dict[str, dict]:
    """{symbol: {best, ranking}}. 없거나 깨지거나 stale 이면 {}."""
    p = path or OUT
    if max_age_hours is not None and strategy_scores_stale(
            p, max_age_hours=max_age_hours, now_fn=now_fn):
        log.info("strategy_scores stale (max_age=%sh) — load 스킵", max_age_hours)
        return {}
    data = _read_payload(p)
    if not data:
        return {}
    syms = data.get("symbols") if isinstance(data, dict) else data
    return syms if isinstance(syms, dict) else {}


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
    existing = load_strategy_scores(out_path, max_age_hours=None)
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
    """scan pad 정렬용 — ranking[0].return_pct (없으면 -inf).

    n_trades < MIN_TRADES_BEST 이면 pad 에서 제외(-inf).
    """
    rec = scores.get(symbol) or {}
    ranking = rec.get("ranking") or []
    if not ranking:
        return float("-inf")
    try:
        n = int(ranking[0].get("n_trades") or 0)
    except (TypeError, ValueError):
        n = 0
    if n < MIN_TRADES_BEST:
        return float("-inf")
    try:
        return float(ranking[0].get("return_pct", float("-inf")))
    except (TypeError, ValueError):
        return float("-inf")


def strategy_fit_brief(rec: dict | None, *, min_trades: int = MIN_TRADES_BEST) -> dict | None:
    """뇌 컨텍스트용 strategy_fit — best 는 min_trades 미만이면 null + thin_sample."""
    if not rec:
        return None
    ranking = list(rec.get("ranking") or [])[:3]
    best = rec.get("best")
    thin = True
    if ranking:
        try:
            thin = int(ranking[0].get("n_trades") or 0) < min_trades
        except (TypeError, ValueError):
            thin = True
    elif best:
        thin = False
    out: dict = {"ranking": ranking, "best": best if not thin else None}
    if thin:
        out["thin_sample"] = True
    return out

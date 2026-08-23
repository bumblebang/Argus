"""확신도 캘리브레이션 — 구간별 실현 승률 / Brier.

캘리브레이션이 입증되기 전(calibrated=False)에는 사이징 링크를 평평하게 둔다
(conviction_sizing 무시 = weight 그대로).
"""
from __future__ import annotations

import json
import math
from collections import defaultdict

from .logging_setup import get_logger

log = get_logger("calibration")

MIN_N = 20           # 전체 최소 표본
MIN_BIN = 5          # 구간별 최소
BINS = [(0.0, 0.4), (0.4, 0.55), (0.55, 0.7), (0.7, 0.85), (0.85, 1.01)]


def _bin_label(lo: float, hi: float) -> str:
    return f"{lo:.2f}-{hi:.2f}"


def _bin_of(c: float) -> str | None:
    for lo, hi in BINS:
        if lo <= c < hi:
            return _bin_label(lo, hi)
    return None


def conviction_calibration(store, since_days: float = 90) -> dict:
    """청산 포지션의 entry conviction vs 실현 승률/Brier.

    conviction 출처: positions.meta.conviction 또는 decisions 테이블 조인 없음
    (진입 시 meta 에 심어둔 값). 없으면 표본에서 제외.
    """
    import time
    since = time.time() - since_days * 86400
    buckets: dict[str, list[tuple[float, int]]] = defaultdict(list)
    # (conviction, win01)
    pairs: list[tuple[float, int]] = []

    for row in store.get_closed_positions(since=since):
        try:
            meta = json.loads(row["meta"]) if row["meta"] else {}
        except (ValueError, TypeError):
            meta = {}
        c = meta.get("conviction")
        if c is None:
            continue
        try:
            c = float(c)
        except (TypeError, ValueError):
            continue
        win = 1 if (row["pnl"] or 0) > 0 else 0
        pairs.append((c, win))
        lab = _bin_of(c)
        if lab:
            buckets[lab].append((c, win))

    by_bin = {}
    for lo, hi in BINS:
        lab = _bin_label(lo, hi)
        rows = buckets.get(lab) or []
        if not rows:
            by_bin[lab] = {"n": 0}
            continue
        wins = sum(w for _, w in rows)
        avg_c = sum(c for c, _ in rows) / len(rows)
        hit = wins / len(rows)
        by_bin[lab] = {
            "n": len(rows),
            "avg_conviction": round(avg_c, 3),
            "hit_rate": round(hit, 3),
            "small_sample": len(rows) < MIN_BIN,
        }

    brier = None
    if pairs:
        brier = round(sum((c - w) ** 2 for c, w in pairs) / len(pairs), 4)

    n = len(pairs)
    # 캘리브레이션 입증: 표본 충분 + 고확신 구간 hit >= 저확신 (단조 대략)
    calibrated = False
    if n >= MIN_N:
        rates = []
        for lo, hi in BINS:
            b = by_bin[_bin_label(lo, hi)]
            if b.get("n", 0) >= MIN_BIN:
                rates.append(b["hit_rate"])
        if len(rates) >= 2 and rates[-1] >= rates[0]:
            calibrated = True

    return {
        "note": ("확신도 캘리브레이션. calibrated=False 이면 사이징을 평평하게 "
                 "(conviction_sizing 비활성). 자동 가중치 갱신 없음."),
        "n": n,
        "brier": brier,
        "by_bin": by_bin,
        "calibrated": calibrated,
        "min_n": MIN_N,
    }


def sizing_enabled(store, *, configured: bool, since_days: float = 90) -> bool:
    """config 가 켜져 있어도 캘리브레이션 전엔 False."""
    if not configured:
        return False
    try:
        cal = conviction_calibration(store, since_days=since_days)
        if not cal.get("calibrated"):
            log.info("확신도 미캘리브레이션(n=%s) — 사이징 평평", cal.get("n"))
            return False
        return True
    except Exception as e:
        log.warning("캘리브레이션 조회 실패 → 사이징 평평: %s", e)
        return False

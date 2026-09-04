"""확신도 캘리브레이션 — 구간별 실현 승률 / Brier.

캘리브레이션이 입증되기 전(calibrated=False)에는 사이징 링크를 평평하게 둔다
(conviction_sizing 무시 = weight 그대로).
"""
from __future__ import annotations

import json
import math
from collections import defaultdict

from .eval.trade_defs import scored_trades
from .logging_setup import get_logger

log = get_logger("calibration")

MIN_N = 20           # 전체 최소 표본
MIN_BIN = 5          # 구간별 최소
MIN_VALID_BINS = 3   # 잠금 해제에 필요한 유효(n>=MIN_BIN) 구간 수
MIN_SLOPE = 0.05     # 최저↔최고 유효 구간 hit_rate 최소 상승폭(동점 해제 금지)
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
    n_scored = 0
    n_excluded_no_conviction = 0

    for trade in scored_trades(store, since=since):
        n_scored += 1
        try:
            meta = json.loads(trade["meta"]) if trade["meta"] else {}
        except (ValueError, TypeError):
            meta = {}
        c = meta.get("conviction")
        if c is None:
            n_excluded_no_conviction += 1
            continue
        try:
            c = float(c)
        except (TypeError, ValueError):
            n_excluded_no_conviction += 1
            continue
        win = 1 if (trade["pnl"] or 0) > 0 else 0
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
    rates = [by_bin[_bin_label(lo, hi)]["hit_rate"] for lo, hi in BINS
             if by_bin[_bin_label(lo, hi)].get("n", 0) >= MIN_BIN]
    calibrated, why = _calibration_verdict(n, rates)

    return {
        "note": ("확신도 캘리브레이션. calibrated=False 이면 사이징을 평평하게 "
                 "(conviction_sizing 비활성). 자동 가중치 갱신 없음."),
        "n": n,
        "n_scored_trades": n_scored,
        "n_excluded_no_conviction": n_excluded_no_conviction,
        "brier": brier,
        "by_bin": by_bin,
        "calibrated": calibrated,
        "calibration_reason": why,
        "valid_bins": len(rates),
        "min_n": MIN_N,
        "min_valid_bins": MIN_VALID_BINS,
        "min_slope": MIN_SLOPE,
    }


def _calibration_verdict(n: int, rates: list[float]) -> tuple[bool, str]:
    """확신도 사이징 잠금 해제 판정. (통과여부, 사유).

    양 끝만 비교하면 V자(중간 붕괴)도 통과하고, `>=` 는 동점만으로도 통과한다.
    확신도 배율은 실제 주문 크기를 바꾸므로, 노이즈로 열리지 않게 세 조건을 건다:
    표본 수, 유효 구간 수, 전 구간 비감소 + 최소 상승폭.

    Brier 는 게이트에 쓰지 않는다 — conviction 은 확률이 아니라 mis-specified
    (EVAL_PROTOCOL "점예측" 참고). 관측용으로만 반환한다.
    """
    if n < MIN_N:
        return False, f"표본 부족 (n={n} < {MIN_N})"
    if len(rates) < MIN_VALID_BINS:
        return False, f"유효 구간 부족 ({len(rates)} < {MIN_VALID_BINS})"
    for prev, cur in zip(rates, rates[1:]):
        if cur < prev:
            return False, f"구간 단조성 붕괴 ({prev} → {cur})"
    slope = rates[-1] - rates[0]
    if slope < MIN_SLOPE:
        return False, f"기울기 부족 ({slope:.3f} < {MIN_SLOPE})"
    return True, f"통과 (n={n}, 유효구간={len(rates)}, 기울기={slope:.3f})"


def sizing_enabled(store, *, configured: bool, since_days: float = 90) -> bool:
    """config 가 켜져 있어도 캘리브레이션 전엔 False."""
    if not configured:
        return False
    try:
        cal = conviction_calibration(store, since_days=since_days)
        if not cal.get("calibrated"):
            log.info("확신도 미캘리브레이션 — 사이징 평평: %s",
                     cal.get("calibration_reason") or f"n={cal.get('n')}")
            return False
        return True
    except Exception as e:
        log.warning("캘리브레이션 조회 실패 → 사이징 평평: %s", e)
        return False

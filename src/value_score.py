"""밸류 트랙 종합 Score — 순수 함수(네트워크/LLM 없음).

S0/S1: ValueFactor(시총분위 상대) + 낙폭 + QualityTilt, age_decay(first_seen_at).
정렬 SSOT 전환(S2) 전까지는 병기·테스트용.

**결측 ValueFactor = NEUTRAL_VALUE_FACTOR(0.5)** — 이 축의 스케일은 [0,1] 이고
0 은 "중립"이 아니라 **가장 비쌈**이다. 재무 결측을 0 으로 두면 최대 감점이 되어
사실상 탈락 사유가 되므로(설계 금지 사항), 중앙값 0.5 를 준다. quality_tilt 가
[-1,1]→0.5 로 정규화되는 것과 같은 규약.
분위별 유효 표본 < n_min 이면 시장 pooled 폴백, pooled도 미달이면 중립.
"""
from __future__ import annotations

import math
from typing import Iterable


DEFAULT_WEIGHTS = {"val": 0.50, "dd": 0.35, "q": 0.15}
# [0,1] 스케일의 중앙 = 중립. 0 은 "가장 비쌈"이므로 결측 기본값으로 쓰지 않는다.
NEUTRAL_VALUE_FACTOR = 0.5
# 밸류는 보유를 넉 달까지 끌고 간다 — 후보 신선도 반감기가 그보다 훨씬 짧으면
# "한 달 지난 논지는 아무리 좋아도 신규에 진다". 좀비 차단은 쿨다운이 직접 하고,
# 여기 감쇠는 동점자 정리 수준으로만 쓴다(바닥 있음).
DEFAULT_HALF_LIFE_DAYS = 30.0
DEFAULT_AGE_DECAY_FLOOR = 0.5
# 낙폭은 깊을수록 좋은 게 아니다 — 이 구간이 최고점, 더 깊으면 '떨어지는 칼'로 감점.
DEFAULT_DD_PEAK = (-50.0, -35.0)
DEFAULT_DD_DEEP_FLOOR = 0.2
DEFAULT_QUINTILE_N_MIN = 20


def _finite(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def age_decay(first_seen_at: float | None, now: float, *,
              half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
              floor: float = DEFAULT_AGE_DECAY_FLOOR) -> float:
    """first_seen_at 기준 지수 감쇠. 결측·미래 ts → 1.0(감쇠 없음).

    floor 아래로는 내려가지 않는다 — 오래된 후보를 뒤로 미루되 **배제하지는** 않는다.
    """
    if first_seen_at is None or half_life_days <= 0:
        return 1.0
    age_sec = float(now) - float(first_seen_at)
    if age_sec <= 0:
        return 1.0
    d = float(0.5 ** ((age_sec / 86400.0) / half_life_days))
    return max(float(floor), d)


def quality_tilt(fundamentals: dict | None) -> float:
    """ROE·부채·역성장 간단 틸트 ∈ [-1, 1]. 결측은 0 기여."""
    if not fundamentals:
        return 0.0
    score = 0.0
    n = 0
    debt = _finite(fundamentals.get("debt_ratio"))
    if debt is not None:
        n += 1
        if debt > 2.0:
            score -= 1.0
        elif debt > 1.0:
            score -= 0.4
        else:
            score += 0.3
    roe = _finite(fundamentals.get("roe"))
    if roe is not None:
        n += 1
        if roe < 0:
            score -= 0.8
        elif roe < 0.05:
            score -= 0.2
        else:
            score += min(0.8, roe)
    for gkey in ("revenue_growth", "op_income_growth", "net_income_growth"):
        g = _finite(fundamentals.get(gkey))
        if g is None:
            continue
        n += 1
        if g < -0.1:
            score -= 0.5
        elif g < 0:
            score -= 0.2
        elif g > 0.1:
            score += 0.3
    if n == 0:
        return 0.0
    return max(-1.0, min(1.0, score / n))


def drawdown_component(drawdown_1y_pct: float | None, *,
                       dd_floor: float = -70.0, dd_ceil: float = -25.0,
                       peak: tuple[float, float] = DEFAULT_DD_PEAK,
                       deep_floor: float = DEFAULT_DD_DEEP_FLOOR) -> float:
    """낙폭 %를 [0,1]로 — **역U자**. 밴드 밖·결측 → 0.

    peak 구간(기본 -50~-35%)이 1.0. 그보다 얕으면 기회가 작아 0 쪽으로,
    그보다 깊으면 '떨어지는 칼' 쪽이라 deep_floor 까지 감점한다. 예전처럼
    "깊을수록 높은 점수"면 컷라인(-70%) 바로 위 종목이 1등이 된다.
    """
    dd = _finite(drawdown_1y_pct)
    if dd is None:
        return 0.0
    if not (dd_floor <= dd <= dd_ceil):
        return 0.0
    lo, hi = float(min(peak)), float(max(peak))       # 예: -50, -35
    if lo <= dd <= hi:
        return 1.0
    if dd > hi:                                        # 얕은 쪽: hi→1, dd_ceil→0
        span = dd_ceil - hi
        if span <= 0:
            return 1.0
        return max(0.0, min(1.0, (dd_ceil - dd) / span))
    span = lo - dd_floor                               # 깊은 쪽: lo→1, dd_floor→deep_floor
    if span <= 0:
        return 1.0
    ratio = (lo - dd) / span
    return max(0.0, min(1.0, 1.0 - (1.0 - float(deep_floor)) * ratio))


def _cheapness_ranks(values: list[float | None]) -> list[float | None]:
    """낮을수록 싼 배수 → [0,1] 상대 점수(가장 쌈=1, 가장 비쌈=0). 결측은 None."""
    indexed = [(i, v) for i, v in enumerate(values) if v is not None and v > 0]
    out: list[float | None] = [None] * len(values)
    if len(indexed) < 2:
        for i, _ in indexed:
            out[i] = NEUTRAL_VALUE_FACTOR  # 단독 유효 → 비교 불가라 중립
        return out
    indexed.sort(key=lambda t: t[1])  # 낮은(싼) 값 먼저
    n = len(indexed)
    for rank, (i, _) in enumerate(indexed):
        # rank 0(가장 쌈) → 1.0, rank n-1 → 0.0
        out[i] = 1.0 - (rank / (n - 1))
    return out


def _assign_quintiles(mcaps: list[float | None]) -> list[int | None]:
    """시총 기준 0..4 분위(0=소형). 결측 시총 → None."""
    valid = [(i, m) for i, m in enumerate(mcaps) if m is not None and m > 0]
    out: list[int | None] = [None] * len(mcaps)
    if not valid:
        return out
    valid.sort(key=lambda t: t[1])
    n = len(valid)
    for order, (i, _) in enumerate(valid):
        # 균등 5분위
        q = min(4, int(order * 5 / n))
        out[i] = q
    return out


def value_factors_mcap_quintile(
    rows: list[dict],
    *,
    n_min: int = DEFAULT_QUINTILE_N_MIN,
    pb_key: str = "pb",
    pe_key: str = "pe_trailing",
    mcap_key: str = "market_cap",
) -> list[float]:
    """시총분위(또는 pooled 폴백) 상대 ValueFactor ∈ [0,1].

    실패·결측 → NEUTRAL_VALUE_FACTOR(0.5). 0 은 "가장 비쌈"이지 중립이 아니다.
    rows[i] 에 market_cap 과 pb/pe(또는 fundamentals 중첩) 가 있다고 가정.
    """
    n = len(rows)
    if n == 0:
        return []

    def _fund(r: dict) -> dict:
        f = r.get("fundamentals")
        return f if isinstance(f, dict) else r

    mcaps = [_finite(_fund(r).get(mcap_key) if mcap_key in _fund(r)
                     else r.get(mcap_key)) for r in rows]
    # pe/pb: fundamentals 우선
    pbs: list[float | None] = []
    pes: list[float | None] = []
    for r in rows:
        f = _fund(r)
        pbs.append(_finite(f.get(pb_key)))
        pes.append(_finite(f.get(pe_key)))

    quintiles = _assign_quintiles(mcaps)
    factors = [NEUTRAL_VALUE_FACTOR] * n

    def _valid_n(indices: list[int]) -> int:
        return sum(
            1 for i in indices
            if (pbs[i] is not None and pbs[i] > 0)
            or (pes[i] is not None and pes[i] > 0)
        )

    def _fill(indices: list[int]) -> bool:
        """n_min 충족 시 factors 채우고 True, 아니면 False(폴백 대상)."""
        if not indices or _valid_n(indices) < n_min:
            return False
        sub_pb = [pbs[i] for i in indices]
        sub_pe = [pes[i] for i in indices]
        rp = _cheapness_ranks(sub_pb)
        re = _cheapness_ranks(sub_pe)
        for j, i in enumerate(indices):
            parts = [x for x in (rp[j], re[j]) if x is not None]
            # 이 분위는 상대화됐지만 이 종목만 배수 결측 → 최하위가 아니라 중립.
            factors[i] = (sum(parts) / len(parts) if parts
                          else NEUTRAL_VALUE_FACTOR)
        return True

    by_q: dict[int, list[int]] = {q: [] for q in range(5)}
    no_q: list[int] = []
    for i, q in enumerate(quintiles):
        if q is None:
            no_q.append(i)
        else:
            by_q[q].append(i)

    weak_q: list[int] = []
    for idxs in by_q.values():
        if idxs and not _fill(idxs):
            weak_q.extend(idxs)

    need_pool = weak_q + no_q
    if need_pool:
        _fill(need_pool)  # pooled도 미달이면 중립 유지

    return [round(max(0.0, min(1.0, f)), 4) for f in factors]


def composite_scores(
    rows: Iterable[dict],
    *,
    now: float,
    weights: dict | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    age_decay_floor: float = DEFAULT_AGE_DECAY_FLOOR,
    n_min: int = DEFAULT_QUINTILE_N_MIN,
    dd_floor: float = -70.0,
    dd_ceil: float = -25.0,
    dd_peak: tuple[float, float] = DEFAULT_DD_PEAK,
    dd_deep_floor: float = DEFAULT_DD_DEEP_FLOOR,
) -> list[dict]:
    """각 row에 value_factor / quality / dd_component / age_decay / composite_value 부여.

    입력 row 키(권장): market_cap, fundamentals|{pb,pe_trailing,...},
    drawdown_1y_pct 또는 metrics.drawdown_1y_pct, first_seen_at.
    원본 dict 는 복사하지 않고 새 dict 리스트를 반환(입력 키 유지 + score 필드).
    """
    material = [dict(r) for r in rows]
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    wsum = float(w["val"] + w["dd"] + w["q"]) or 1.0
    vfs = value_factors_mcap_quintile(material, n_min=n_min)

    out: list[dict] = []
    for r, vf in zip(material, vfs):
        metrics = r.get("metrics") if isinstance(r.get("metrics"), dict) else {}
        dd = r.get("drawdown_1y_pct")
        if dd is None:
            dd = metrics.get("drawdown_1y_pct")
        fund = r.get("fundamentals") if isinstance(r.get("fundamentals"), dict) else r
        dd_c = drawdown_component(dd, dd_floor=dd_floor, dd_ceil=dd_ceil,
                                  peak=dd_peak, deep_floor=dd_deep_floor)
        q = quality_tilt(fund if isinstance(fund, dict) else None)
        decay = age_decay(r.get("first_seen_at"), now,
                          half_life_days=half_life_days, floor=age_decay_floor)
        raw = (w["val"] * vf + w["dd"] * dd_c + w["q"] * ((q + 1) / 2)) / wsum
        # quality_tilt [-1,1] → [0,1] 로 정규화해 가중합
        comp = round(raw * decay, 4)
        row = dict(r)
        row["value_factor"] = vf
        row["quality_tilt"] = round(q, 4)
        row["dd_component"] = round(dd_c, 4)
        row["age_decay"] = round(decay, 4)
        row["composite_value"] = comp
        out.append(row)
    return out


def quintile_coverage_report(
    rows: list[dict],
    *,
    n_min: int = DEFAULT_QUINTILE_N_MIN,
) -> dict:
    """분위별 유효(pb|pe) 표본 수 — S0 성공 기준 리포트용.

    ok=True 조건: rows 비어 있지 않고, 시총 배정된 종목이 있으며,
    n_total>0 인 분위는 모두 n_valid>=n_min.
    """
    empty = {"n_min": n_min, "quintiles": {str(q): {"n_total": 0, "n_valid": 0}
                                           for q in range(5)}, "ok": False}
    if not rows:
        return empty

    def _fund(r: dict) -> dict:
        f = r.get("fundamentals")
        return f if isinstance(f, dict) else r

    mcaps = []
    for r in rows:
        f = _fund(r)
        m = _finite(r.get("market_cap"))
        if m is None:
            m = _finite(f.get("market_cap"))
        mcaps.append(m)
    qs = _assign_quintiles(mcaps)
    buckets: dict[str, dict] = {str(q): {"n_total": 0, "n_valid": 0} for q in range(5)}
    n_assigned = 0
    for i, q in enumerate(qs):
        if q is None:
            continue
        n_assigned += 1
        b = buckets[str(q)]
        b["n_total"] += 1
        f = _fund(rows[i])
        pb, pe = _finite(f.get("pb")), _finite(f.get("pe_trailing"))
        if (pb is not None and pb > 0) or (pe is not None and pe > 0):
            b["n_valid"] += 1
    ok = (
        n_assigned > 0
        and all(b["n_valid"] >= n_min for b in buckets.values() if b["n_total"] > 0)
    )
    return {"n_min": n_min, "quintiles": buckets, "n_assigned": n_assigned, "ok": ok}

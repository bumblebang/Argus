"""널 매니저 — 동일 후보·동일 재현가능 게이트, LLM 없음.

null_cash: 전부 HOLD.
null_random_gated: 도시레/존/슬롯을 아카이브 스칼라로 재현한 통과 집합에서
sha256(cycle_ts|symbol) 순으로 k개 BUY (k=당시 라이브 BUY 수, 최소 0).
같은 시드면 항상 같은 픽.
"""
from __future__ import annotations

import hashlib
from typing import Any


def _hash_rank(cycle_ts: float | str, symbol: str) -> str:
    return hashlib.sha256(f"{cycle_ts}|{symbol}".encode("utf-8")).hexdigest()


def _dossier(c: dict) -> dict:
    d = c.get("dossier")
    return d if isinstance(d, dict) else {}


def _price(c: dict) -> float | None:
    for k in ("price", "last", "close"):
        v = c.get(k)
        if v is None:
            continue
        try:
            px = float(v)
        except (TypeError, ValueError):
            continue
        if px > 0:
            return px
    return None


def eligible_candidates(context: dict, *, require_dossier: bool = True
                        ) -> list[dict]:
    """아카이브 스칼라로 재현 가능한 코드 게이트만 적용.

    라이브 `_has_bullish_dossier` 와 같이 stance=='bullish' 만 통과시킨다.
    도시레가 있기만 하면 넣으면 라이브가 안 사는 중립/약세 종목을 널이 집어
    delta_vs_gated 가 스킬처럼 보인다.
    """
    out: list[dict] = []
    constraints = context.get("constraints") or {}
    for c in context.get("candidates") or context.get("universe") or []:
        if not isinstance(c, dict):
            continue
        sym = str(c.get("symbol") or "")
        if not sym:
            continue
        d = _dossier(c)
        if require_dossier:
            if not d or (d.get("stance") or "").lower() != "bullish":
                continue
        px = _price(c)
        lo, hi = d.get("entry_low"), d.get("entry_high")
        inv = d.get("invalidation")
        try:
            if px is not None and hi is not None and px > float(hi):
                continue  # 갭 위
            if px is not None and inv is not None and px < float(inv):
                continue  # 무효화 하회
            if px is not None and lo is not None and hi is not None:
                if not (float(lo) <= px <= float(hi)):
                    # 존 아래(회복 대기)도 게이트 밖 — 라이브 gap_armed 와 정렬
                    if lo is not None and px < float(lo):
                        continue
        except (TypeError, ValueError):
            pass
        out.append(c)
    max_pos = constraints.get("max_positions")
    if max_pos is not None:
        try:
            cap = int(max_pos)
            if cap >= 0:
                out = out[: max(cap * 4, cap)]  # 정렬 전 상한은 픽 단계에서
        except (TypeError, ValueError):
            pass
    return out


def null_cash(candidates: list[dict]) -> dict[str, str]:
    """전부 HOLD."""
    return {str(c.get("symbol")): "HOLD" for c in candidates if c.get("symbol")}


def null_random_gated(context: dict, *, cycle_ts: float | str,
                      n_buy: int,
                      require_dossier: bool = True) -> dict[str, str]:
    """통과 집합에서 해시 순 k개 BUY, 나머지 HOLD. n_buy<=0 이면 전부 HOLD."""
    all_cands = [c for c in (context.get("candidates") or context.get("universe") or [])
                 if isinstance(c, dict)]
    sides = null_cash(all_cands)
    if n_buy <= 0:
        return sides
    elig = eligible_candidates(context, require_dossier=require_dossier)
    constraints = context.get("constraints") or {}
    try:
        max_pos = int(constraints.get("max_positions") or n_buy)
    except (TypeError, ValueError):
        max_pos = n_buy
    k = max(0, min(int(n_buy), max_pos, len(elig)))
    ranked = sorted(elig, key=lambda c: _hash_rank(cycle_ts, str(c.get("symbol"))))
    for c in ranked[:k]:
        sides[str(c.get("symbol"))] = "BUY"
    return sides

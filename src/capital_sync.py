"""risk.capital 을 실계좌 equity 에 주기 동기화.

사이징/노출은 equity 기준이지만 capital 은 SoD·equity 산출 실패 시 폴백이다.
입출금 후에도 config 의 낡은 capital 이 남으면 폴백이 어긋나므로, 재대사 직후
게이트·RiskManager·cfg.raw 의 capital 을 실자산으로 맞춘다(config.yaml 은 건드리지 않음 —
재기동 직후 첫 재대사가 다시 맞춘다).
"""
from __future__ import annotations

from typing import Any

from .logging_setup import get_logger

log = get_logger("capital_sync")


def equity_by_market(account, markets: tuple[str, ...] | list[str]) -> dict[str, float]:
    """시장별 현금+보유평가(평균가). 양수만."""
    out: dict[str, float] = {}
    for m in markets:
        try:
            eq = float(account.equity(m))
        except Exception as e:
            log.warning("equity 산출 실패(%s): %s", m, e)
            continue
        if eq > 0:
            out[str(m).upper()] = eq
    return out


def _min_abs(cfg: dict, market: str) -> float:
    raw = cfg.get("min_change_abs", 0)
    if isinstance(raw, dict):
        try:
            return float(raw.get(market, raw.get("KR", 0)) or 0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def should_update(old: float, new: float, *, min_pct: float, min_abs: float) -> bool:
    """의미 있는 변화만. old<=0 이면 새 값이 양수일 때 갱신."""
    if new <= 0:
        return False
    if old <= 0:
        return True
    delta = abs(new - old)
    if delta < float(min_abs or 0):
        return False
    return delta / old >= float(min_pct or 0)


def apply_capital_sync(
    *,
    gate=None,
    risk=None,
    cfg_raw: dict | None = None,
    account,
    markets: tuple[str, ...] | list[str],
    sync_cfg: dict | None = None,
) -> dict[str, Any]:
    """account equity → gate/risk/cfg capital. 변경분·최종 capital 반환."""
    scfg = dict(sync_cfg or {})
    if scfg.get("enabled", True) is False:
        return {"enabled": False, "changed": {}, "capital": {}}

    min_pct = float(scfg.get("min_change_pct", 0.02) or 0)
    targets = equity_by_market(account, markets)
    if not targets:
        return {"enabled": True, "changed": {}, "capital": {}, "reason": "no_equity"}

    # 현재값(게이트 우선)
    current: dict[str, float] = {}
    if gate is not None and getattr(gate, "capital", None) is not None:
        current = {str(k).upper(): float(v or 0) for k, v in dict(gate.capital).items()}
    elif risk is not None and getattr(risk, "capital", None) is not None:
        current = {str(k).upper(): float(v or 0) for k, v in dict(risk.capital).items()}
    elif cfg_raw and isinstance(cfg_raw.get("risk"), dict):
        current = {str(k).upper(): float(v or 0)
                   for k, v in dict((cfg_raw["risk"] or {}).get("capital") or {}).items()}

    changed: dict[str, dict[str, float]] = {}
    merged = dict(current)
    for mkt, new_v in targets.items():
        old_v = float(merged.get(mkt, 0) or 0)
        if not should_update(old_v, new_v, min_pct=min_pct,
                             min_abs=_min_abs(scfg, mkt)):
            continue
        # 통화 단위 정수(원·센트 반올림) — 게이트 비교 노이즈 감소
        rounded = float(round(new_v))
        merged[mkt] = rounded
        changed[mkt] = {"old": old_v, "new": rounded}

    if not changed:
        return {"enabled": True, "changed": {}, "capital": merged}

    if gate is not None and getattr(gate, "capital", None) is not None:
        gate.capital = dict(merged)
    if risk is not None and getattr(risk, "capital", None) is not None:
        risk.capital = dict(merged)
    if cfg_raw is not None:
        risk_block = cfg_raw.setdefault("risk", {})
        if not isinstance(risk_block, dict):
            cfg_raw["risk"] = {"capital": dict(merged)}
        else:
            risk_block["capital"] = dict(merged)

    log.info("capital 동기화 — %s",
             ", ".join(f"{m}: {v['old']:.0f}→{v['new']:.0f}" for m, v in changed.items()))
    return {"enabled": True, "changed": changed, "capital": merged}

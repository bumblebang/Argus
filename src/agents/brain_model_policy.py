"""뇌 결정 LLM 티어 — wake reason/slot 기준 opus vs sonnet.

검증 LLM 은 별도(val_llm_factory)로 항상 상위 티어 유지.
"""
from __future__ import annotations

_DEFAULT_OPUS_REASONS = frozenset({"athena_done", "gap_rebound_scan", "nxt_gap_scan"})
_DEFAULT_OPUS_EXTRA_SLOTS = {"KR": ("08:00", "09:00"), "US": ("22:30",)}


def brain_decision_cfg(agents_cfg: dict | None) -> dict:
    raw = (agents_cfg or {}).get("brain_decision") or {}
    opus_reasons = raw.get("opus_reasons")
    if opus_reasons is None:
        reasons = set(_DEFAULT_OPUS_REASONS)
    else:
        reasons = {str(x) for x in opus_reasons}
    slots = raw.get("opus_extra_slots")
    if slots is None:
        extra = {m: list(v) for m, v in _DEFAULT_OPUS_EXTRA_SLOTS.items()}
    else:
        extra = {str(m): [str(h) for h in (lst or [])]
                 for m, lst in slots.items()}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "opus_model": raw.get("opus_model"),
        "sonnet_model": raw.get("sonnet_model"),
        "opus_reasons": reasons,
        "opus_extra_slots": extra,
    }


def _reason_parts(wake: dict | None) -> list[str]:
    reason = str((wake or {}).get("reason") or "").strip()
    if not reason:
        return []
    return [p.strip() for p in reason.replace("|", "+").split("+") if p.strip()]


def decision_tier(wake: dict | None, *, agents_cfg: dict | None = None) -> str:
    """'opus' | 'sonnet' — 결정 LLM 티어."""
    cfg = brain_decision_cfg(agents_cfg)
    if not cfg.get("enabled", True):
        return "opus"
    parts = _reason_parts(wake)
    if any(p in (cfg.get("opus_reasons") or set()) for p in parts):
        return "opus"
    reason = str((wake or {}).get("reason") or "")
    if reason == "extra" or "extra" in parts:
        at = str((wake or {}).get("at") or "").strip()
        market = str((wake or {}).get("market") or "KR").upper()
        if at and at in (cfg.get("opus_extra_slots") or {}).get(market, []):
            return "opus"
    return "sonnet"


def pick_decision_llm(wake: dict | None, opus_llm, sonnet_llm, *,
                      agents_cfg: dict | None = None):
    return opus_llm if decision_tier(wake, agents_cfg=agents_cfg) == "opus" else sonnet_llm

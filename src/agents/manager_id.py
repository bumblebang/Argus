"""매니저 정체성 — (model, prompt_hash, source, fallback) 스냅샷.

재량 패러다임에서 모델/프롬프트 변경은 매니저 교체와 같다.
결정마다 저널에 남기고 attribution 을 에포크별로 분리한다.
"""
from __future__ import annotations

import hashlib
from typing import Any


def prompt_hash(text: str) -> str:
    """시스템 프롬프트 내용 해시(앞 12자). 프롬프트 수정 = 새 매니저."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def llm_meta(llm: Any) -> dict:
    """LLM 클라이언트에서 직전 호출 메타 추출. 없는 필드는 생략."""
    out: dict = {}
    model = getattr(llm, "last_model", None) or getattr(llm, "model", None)
    if model:
        out["model"] = str(model)
    src = getattr(llm, "last_source", None)
    if src:
        out["source"] = str(src)
    if getattr(llm, "used_fallback", False):
        out["used_fallback"] = True
        fb = getattr(llm, "fallback_model", None)
        if fb:
            out["fallback_model"] = str(fb)
    return out


def manager_snapshot(*, decision_llm=None, validation_llm=None,
                     decision_prompt: str = "",
                     validation_prompt: str = "") -> dict:
    """사이클 1건의 매니저 정체성 블록."""
    dec = llm_meta(decision_llm) if decision_llm is not None else {}
    val = llm_meta(validation_llm) if validation_llm is not None else {}
    snap = {
        "decision": {
            **dec,
            "prompt_hash": prompt_hash(decision_prompt),
        },
        "validation": {
            **val,
            "prompt_hash": prompt_hash(validation_prompt),
        },
    }
    # 에포크 키: 결정 모델+프롬프트 (검증 폴백은 별도 집계)
    epoch = f"{snap['decision'].get('model') or '?'}@{snap['decision']['prompt_hash']}"
    if snap["decision"].get("used_fallback"):
        epoch += ":fallback"
    snap["epoch"] = epoch
    return snap


def legacy_manager_stamp(*, note: str = "pre-instrumentation") -> dict:
    """과거 저널/decisions 역채우기용 고정 에포크."""
    return {
        "decision": {"model": "legacy", "prompt_hash": "legacy000000",
                     "source": "backfill"},
        "validation": {"model": "legacy", "prompt_hash": "legacy000000",
                       "source": "backfill"},
        "epoch": "legacy@legacy000000",
        "backfill": True,
        "note": note,
    }

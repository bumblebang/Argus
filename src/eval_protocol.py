"""사전등록 평가 프로토콜 — 변경 전에 가설·kill 기준을 등록.

그림자 Δ·얇은 표본만으로 메인/슬리브/게이트를 바꾸지 않는다.
`data/eval_registry.json` 에 등록된 실험만 '검토 후보'로 취급.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

log = get_logger("eval_protocol")

DEFAULT_PATH = Path("data/eval_registry.json")

# 승격 금지 대상 (thin-sample / 그림자만으로는 못 건드림)
PROTECTED = frozenset({
    "main_sleeve", "flat_sleeve", "exit_policy", "risk_gate",
    "conviction_weights", "validation_rules",
})


def _empty() -> dict:
    return {"version": 1, "experiments": []}


def load_registry(path: Path | str = DEFAULT_PATH) -> dict:
    p = Path(path)
    if not p.exists():
        return _empty()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("eval_registry 읽기 실패: %s", e)
        return _empty()


def save_registry(reg: dict, path: Path | str = DEFAULT_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")


def register_experiment(*, name: str, hypothesis: str, metric: str,
                        kill_if: str, min_n: int = 20,
                        touches: list[str] | None = None,
                        path: Path | str = DEFAULT_PATH) -> dict:
    """실험 사전등록. touches 에 PROTECTED 가 있으면 승격에 등록 필수."""
    reg = load_registry(path)
    exp = {
        "id": f"exp_{int(time.time())}",
        "name": name,
        "hypothesis": hypothesis,
        "metric": metric,
        "kill_if": kill_if,
        "min_n": min_n,
        "touches": list(touches or []),
        "status": "registered",   # registered | running | pass | kill | shadow_only
        "created_at": time.time(),
    }
    reg.setdefault("experiments", []).append(exp)
    save_registry(reg, path)
    return exp


def can_promote(*, change: str, evidence_n: int, registry_path: Path | str = DEFAULT_PATH,
                experiment_id: str | None = None) -> tuple[bool, str]:
    """승격 허용 여부. PROTECTED 변경은 등록+표본+kill 미해당이어야 함.

    그림자 장부 Δ만으로는 항상 False.
    """
    if change in PROTECTED or change.startswith("main") or change.startswith("sleeve"):
        reg = load_registry(registry_path)
        if not experiment_id:
            return False, "PROTECTED 변경은 eval_registry 실험 id 필수"
        exps = {e["id"]: e for e in reg.get("experiments") or []}
        exp = exps.get(experiment_id)
        if not exp:
            return False, f"미등록 실험 {experiment_id}"
        if exp.get("status") not in ("pass", "running"):
            return False, f"실험 status={exp.get('status')} — pass/running 만"
        if evidence_n < int(exp.get("min_n") or 20):
            return False, f"표본 {evidence_n} < min_n {exp.get('min_n')}"
        if change not in (exp.get("touches") or []) and "any" not in (exp.get("touches") or []):
            return False, f"{change} 가 실험 touches 에 없음"
        return True, "ok"
    return True, "unprotected"


def shadow_only_label(evidence: dict | None = None) -> str:
    """대시보드/리포트용 — 승격 문구 금지."""
    n = (evidence or {}).get("n") or 0
    return f"관심 섀도(n={n}) — 메인/슬리브 승격 금지"

"""사전등록 평가 프로토콜 — 변경 전에 가설·kill 기준을 등록.

그림자 Δ·얇은 표본만으로 메인/슬리브/게이트를 바꾸지 않는다.
`data/eval_registry.json` 에 등록된 실험만 '검토 후보'로 취급.
"""
from __future__ import annotations

import json
import operator
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

# 리플레이·널 Δ 는 실험 등록과 무관하게 승격 불가 (판단 단위 측정일 뿐).
NO_PROMOTE = frozenset({
    "replay_score", "null_manager", "context_replay", "consistency",
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


_OPS = {
    "<": operator.lt, "<=": operator.le,
    ">": operator.gt, ">=": operator.ge,
    "==": operator.eq, "!=": operator.ne,
}


def register_experiment(*, name: str, hypothesis: str, metric: str,
                        kill_if: str = "", min_n: int = 20,
                        kill: dict | None = None,
                        touches: list[str] | None = None,
                        path: Path | str = DEFAULT_PATH) -> dict:
    """실험 사전등록. touches 에 PROTECTED 가 있으면 승격에 등록 필수.

    kill_if 문자열은 사람용 메모일 뿐 **eval 하지 않는다**.
    집행은 kill={metric, op, threshold, min_n} 구조화 필드 + apply_kill_rules.
    """
    kill_spec = None
    if isinstance(kill, dict) and kill.get("threshold") is not None:
        try:
            kill_spec = {
                "metric": str(kill.get("metric") or metric),
                "op": str(kill.get("op") or "<"),
                "threshold": float(kill["threshold"]),
                "min_n": int(kill.get("min_n") or min_n),
            }
        except (TypeError, ValueError) as e:
            log.warning("kill 스펙 무시: %s", e)
    reg = load_registry(path)
    n_exist = len(reg.get("experiments") or [])
    exp = {
        "id": f"exp_{int(time.time())}_{n_exist}",
        "name": name,
        "hypothesis": hypothesis,
        "metric": metric,
        "kill_if": kill_if,
        "kill": kill_spec,
        "min_n": min_n,
        "touches": list(touches or []),
        "status": "registered",   # registered | running | pass | kill | shadow_only
        "created_at": time.time(),
    }
    reg.setdefault("experiments", []).append(exp)
    save_registry(reg, path)
    return exp


def apply_kill_rules(*, metrics: dict[str, Any], n: int | None = None,
                     path: Path | str = DEFAULT_PATH) -> list[dict]:
    """구조화 kill 만으로 status 를 갱신. 문자열 kill_if 는 읽지 않는다.

    kill 충족 + 표본 충분 → kill (되돌리지 않음).
    표본 부족 → shadow_only.
    표본 충분·조건 미충족 → pass (registered/running/shadow_only 에서만).
    """
    reg = load_registry(path)
    changed: list[dict] = []
    for exp in reg.get("experiments") or []:
        if exp.get("status") == "kill":
            continue
        kill = exp.get("kill")
        if not isinstance(kill, dict) or kill.get("threshold") is None:
            continue
        metric = str(kill.get("metric") or exp.get("metric") or "")
        val = metrics.get(metric)
        min_n = int(kill.get("min_n") or exp.get("min_n") or 20)
        obs_n = n
        n_key = f"{metric}__n"
        if n_key in metrics:
            try:
                obs_n = int(metrics[n_key])
            except (TypeError, ValueError):
                pass
        if obs_n is None:
            continue
        reason = None
        new_status = None
        if obs_n < min_n:
            new_status, reason = "shadow_only", f"n={obs_n} < min_n={min_n}"
        elif val is None:
            continue
        else:
            try:
                val_f, thr = float(val), float(kill["threshold"])
            except (TypeError, ValueError):
                continue
            fn = _OPS.get(str(kill.get("op") or "<"))
            if fn is None:
                log.warning("unknown kill op %s — skip %s", kill.get("op"), exp.get("id"))
                continue
            if fn(val_f, thr):
                new_status = "kill"
                reason = f"{metric} {val_f} {kill.get('op')} {thr} (n={obs_n})"
            elif exp.get("status") in ("registered", "running", "shadow_only"):
                new_status = "pass"
                reason = f"{metric} {val_f} not {kill.get('op')} {thr} (n={obs_n})"
        if new_status and new_status != exp.get("status"):
            exp["status"] = new_status
            exp["status_reason"] = reason
            changed.append(dict(exp))
    if changed:
        save_registry(reg, path)
    return changed


def can_promote(*, change: str, evidence_n: int, registry_path: Path | str = DEFAULT_PATH,
                experiment_id: str | None = None) -> tuple[bool, str]:
    """승격 허용 여부. PROTECTED 변경은 등록+표본+kill 미해당이어야 함.

    그림자 장부 Δ만으로는 항상 False.
    """
    if change in NO_PROMOTE or change.startswith("replay"):
        return False, "리플레이/널 Δ 로는 승격 불가"
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

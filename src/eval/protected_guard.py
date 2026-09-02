"""PROTECTED 경로 변경 감지 — eval_protocol.can_promote 와 CI 배선 (J13 defer)."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from .labels import MIN_N
from ..eval_protocol import can_promote

# 코드 파일 → PROTECTED touch 키
FILE_TOUCHES: dict[str, str] = {
    "src/risk_gate.py": "risk_gate",
    "src/risk.py": "risk_gate",
    "src/engine/exit_policy.py": "exit_policy",
    "src/agents/validation_agent.py": "validation_rules",
    "src/calibration.py": "conviction_weights",
}

# config 최상위 블록 → touch (값 변경만; 키 존재 여부는 deep diff)
CONFIG_BLOCK_TOUCHES: dict[str, str] = {
    "risk": "risk_gate",
    "exit_policy": "exit_policy",
    "agents": "validation_rules",
    "value_trade": "main_sleeve",
}

DEFECT_FIX_MARKERS = (
    "defect-fix:",
    "[defect-fix]",
    "결함 수정",
)

EXPERIMENT_ID_RE = re.compile(
    r"(?:eval_experiment|experiment_id)\s*[:=]\s*(\S+)",
    re.IGNORECASE,
)


def _deep_diff(a: Any, b: Any) -> bool:
    return a != b


def config_touches(old: dict | None, new: dict | None) -> set[str]:
    """config.yaml risk/exit_policy/agents/value_trade 블록 값 변경 → touches."""
    old = old or {}
    new = new or {}
    out: set[str] = set()
    for block, touch in CONFIG_BLOCK_TOUCHES.items():
        if _deep_diff(old.get(block), new.get(block)):
            out.add(touch)
    return out


def touches_for_paths(changed: list[str]) -> set[str]:
    """변경 파일 경로(슬래시 정규화) → PROTECTED touch 집합."""
    out: set[str] = set()
    for raw in changed:
        p = raw.replace("\\", "/").lstrip("./")
        if touch := FILE_TOUCHES.get(p):
            out.add(touch)
    return out


def parse_experiment_id(text: str | None) -> str | None:
    if not text:
        return None
    m = EXPERIMENT_ID_RE.search(text)
    return m.group(1).strip() if m else None


def is_defect_fix(text: str | None) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(m.lower() in low for m in DEFECT_FIX_MARKERS)


def git_changed_files(base: str, head: str = "HEAD") -> list[str]:
    def _run(args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    out = _run(["git", "diff", "--name-only", f"{base}...{head}"])
    if out.returncode != 0:
        out = _run(["git", "diff", "--name-only", base, head])
    stdout = out.stdout or ""
    return [ln.strip() for ln in stdout.splitlines() if ln.strip()]


def git_show_file(ref: str, path: str) -> dict | None:
    proc = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    stdout = proc.stdout or ""
    if proc.returncode != 0 or not stdout.strip():
        return None
    try:
        data = yaml.safe_load(stdout)
        return data if isinstance(data, dict) else None
    except yaml.YAMLError:
        return None


def collect_protected_touches(
    *,
    changed_files: list[str],
    base_ref: str,
    head_ref: str = "HEAD",
) -> set[str]:
    touches = touches_for_paths(changed_files)
    norm = {f.replace("\\", "/").lstrip("./") for f in changed_files}
    for cfg in ("config.yaml", "config.example.yaml"):
        if cfg in norm:
            old = git_show_file(base_ref, cfg) or {}
            new = git_show_file(head_ref, cfg) or {}
            touches |= config_touches(old, new)
    return touches


def check_protected_changes(
    *,
    base_ref: str,
    head_ref: str = "HEAD",
    registry_path: Path | str = "data/eval_registry.json",
    experiment_id: str | None = None,
    pr_body: str | None = None,
    evidence_n: int | None = None,
) -> tuple[bool, list[str]]:
    """PROTECTED touch 가 있으면 defect-fix 또는 can_promote 통과 필요."""
    eff_n = MIN_N if evidence_n is None else evidence_n
    changed = git_changed_files(base_ref, head_ref)
    if not changed:
        return True, ["변경 파일 없음"]

    touches = collect_protected_touches(
        changed_files=changed,
        base_ref=base_ref,
        head_ref=head_ref,
    )
    if not touches:
        return True, ["PROTECTED 경로 변경 없음"]

    exp_id = experiment_id or parse_experiment_id(pr_body)
    if is_defect_fix(pr_body):
        return True, [
            f"defect-fix 예외 — touches={sorted(touches)}",
            f"changed={changed}",
        ]

    if not exp_id:
        return False, [
            f"PROTECTED 변경 touches={sorted(touches)} — eval_experiment: <id> 또는 defect-fix: 필요",
            f"changed={changed}",
        ]

    msgs: list[str] = [f"touches={sorted(touches)}", f"experiment_id={exp_id}"]
    ok_all = True
    for touch in sorted(touches):
        ok, why = can_promote(
            change=touch,
            evidence_n=eff_n,
            registry_path=registry_path,
            experiment_id=exp_id,
        )
        msgs.append(f"{touch}: {why}")
        ok_all = ok_all and ok
    return ok_all, msgs

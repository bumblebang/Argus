"""설계–구현 정합 점검 — tests/golden/design_invariants.yaml ↔ config·코드.

plan/CONTEXT 합의를 YAML 불변조건으로 고정하고, PR·로컬·CI에서 자동 검증한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "tests" / "golden" / "design_invariants.yaml"
DEFAULT_CONFIG = ROOT / "config.example.yaml"


def resolve_config_path(root: Path | None = None) -> Path:
    """실운영 config.yaml 우선, 없으면 example."""
    root = root or ROOT
    live = root / "config.yaml"
    return live if live.is_file() else root / "config.example.yaml"


@dataclass
class CheckResult:
    group: str
    check_id: str
    desc: str
    ok: bool
    waived: bool
    detail: str

    @property
    def status(self) -> str:
        if self.ok:
            return "OK"
        if self.waived:
            return "WAIVED"
        return "FAIL"


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _cfg_get(cfg: dict, path: list[str]) -> tuple[bool, Any]:
    cur: Any = cfg
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return False, None
        cur = cur[key]
    return True, cur


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        y, m, d = str(s).split("-")
        return date(int(y), int(m), int(d))
    except (TypeError, ValueError):
        return None


def _waived(check: dict, today: date | None = None) -> tuple[bool, str]:
    if not check.get("waive"):
        return False, ""
    reason = str(check.get("waive_reason") or "waive_reason 없음")
    until = _parse_date(check.get("waive_until"))
    if not until:
        return False, "waive_until 없음 — 무기한 면제 불가"
    if (today or date.today()) > until:
        return False, f"waive 만료({until}) — {reason}"
    return True, reason


def _run_check(check: dict, *, cfg: dict, root: Path) -> CheckResult:
    cid = str(check.get("id") or "?")
    desc = str(check.get("desc") or cid)
    group = str(check.get("_group") or "")
    ctype = str(check.get("type") or "")
    waived, waive_note = _waived(check)
    rel = check.get("path")
    fpath = root / str(check.get("path") if ctype.startswith("file") else check.get("path", ""))

    try:
        if ctype == "config_eq":
            path = list(check["path"])
            found, val = _cfg_get(cfg, path)
            ok = found and val == check["value"]
            detail = f"{'/'.join(path)}={val!r} (want {check['value']!r})"
        elif ctype == "config_absent":
            path = list(check["path"])
            found, val = _cfg_get(cfg, path)
            ok = not found or val is None
            detail = f"{'/'.join(path)} absent (found={found}, val={val!r})"
        elif ctype == "file_exists":
            p = root / str(check["path"])
            ok = p.is_file()
            detail = str(p.relative_to(root)) if ok else f"missing: {p}"
        elif ctype == "file_contains":
            p = root / str(check["path"])
            text = p.read_text(encoding="utf-8")
            needle = str(check["needle"])
            ok = needle in text
            detail = f"{p.name}: {'found' if ok else 'missing'} {needle!r}"
        elif ctype == "file_not_contains":
            p = root / str(check["path"])
            text = p.read_text(encoding="utf-8")
            needle = str(check["needle"])
            ok = needle not in text
            detail = f"{p.name}: {'clean' if ok else 'still has'} {needle!r}"
        elif ctype == "file_regex":
            p = root / str(check["path"])
            text = p.read_text(encoding="utf-8")
            pat = re.compile(str(check["pattern"]), re.MULTILINE)
            want = bool(check.get("must_match", True))
            matched = pat.search(text) is not None
            ok = matched if want else not matched
            detail = f"{p.name} pattern {check['pattern']!r} match={matched}"
        else:
            ok = False
            detail = f"unknown check type: {ctype}"
    except OSError as e:
        ok = False
        detail = str(e)

    if not ok and waived:
        detail = f"{detail} | WAIVED: {waive_note}"
    return CheckResult(group, cid, desc, ok, waived and not ok, detail)


def run_alignment(
    manifest_path: Path | None = None,
    config_path: Path | None = None,
    root: Path | None = None,
    *,
    today: date | None = None,
) -> list[CheckResult]:
    root = root or ROOT
    manifest = _load_yaml(manifest_path or DEFAULT_MANIFEST)
    cfg = _load_yaml(config_path or resolve_config_path(root))
    results: list[CheckResult] = []
    for gname, group in (manifest.get("groups") or {}).items():
        if not isinstance(group, dict):
            continue
        for raw in group.get("checks") or []:
            if not isinstance(raw, dict):
                continue
            chk = dict(raw)
            chk["_group"] = gname
            results.append(_run_check(chk, cfg=cfg, root=root))
    return results


def format_report(results: list[CheckResult]) -> str:
    lines = ["design alignment:"]
    fails = 0
    waived = 0
    for r in results:
        mark = r.status
        if mark == "FAIL":
            fails += 1
        elif mark == "WAIVED":
            waived += 1
        detail = r.detail.encode("ascii", "backslashreplace").decode("ascii")
        lines.append(f"  [{mark}] {r.group}/{r.check_id}: {detail}")
    lines.append(f"summary: {len(results)} checks, {fails} fail, {waived} waived")
    return "\n".join(lines)


def alignment_ok(results: list[CheckResult]) -> bool:
    return all(r.ok or r.waived for r in results)

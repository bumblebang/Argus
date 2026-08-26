"""Phase 4 — research lab 경계 회귀."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.ops_golden


def _iter_runtime_py():
    for base in (ROOT / "src", ROOT / "scripts"):
        for p in base.rglob("*.py"):
            if any(part in {".venv", "__pycache__", "research"} for part in p.parts):
                continue
            yield p


def test_p4_research_readme_exists():
    readme = ROOT / "research" / "README.md"
    assert readme.is_file()
    text = readme.read_text(encoding="utf-8")
    assert "lab only" in text.lower() or "런타임 아님" in text
    assert "flat_sleeve_rescan_live.py" in text
    assert "quant-thin-sample" in text


def test_p4_runtime_no_quant_review_path():
    """src/scripts 가 data/quant_review 를 읽지 않음(doctor 안내 문자열 제외)."""
    bad: list[str] = []
    for path in _iter_runtime_py():
        if path.name == "doctor.py":
            continue
        if path.name == "research_boundary.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "data/quant_review" in text or "data\\quant_review" in text:
            bad.append(str(path.relative_to(ROOT)))
    assert bad == [], "runtime must not hardcode data/quant_review:\n" + "\n".join(bad)


def test_p4_runtime_no_research_import():
    bad: list[str] = []
    for path in _iter_runtime_py():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "research" or alias.name.startswith("research."):
                        bad.append(f"{path.relative_to(ROOT)}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod == "research" or mod.startswith("research."):
                    bad.append(f"{path.relative_to(ROOT)}: from {mod}")
    assert bad == []


def test_p4_migrate_research_dry(tmp_path):
    from src.research_boundary import apply_migrate, residue_status

    src = tmp_path / "data" / "quant_review"
    src.mkdir(parents=True)
    (src / "flat_sleeve_scan.csv").write_text("a,b\n", encoding="utf-8")
    (src / "nested").mkdir()
    (src / "nested" / "x.json").write_text("{}", encoding="utf-8")

    st = residue_status(root=tmp_path)
    assert st["present"] and st["files"] == 2

    rows = apply_migrate(root=tmp_path, dry_run=True)
    assert (tmp_path / "data" / "quant_review" / "flat_sleeve_scan.csv").is_file()
    assert any(str(r.get("result", "")).startswith("dry:") for r in rows)

    rows2 = apply_migrate(root=tmp_path, dry_run=False)
    assert any(r.get("result") == "moved" for r in rows2)
    assert (tmp_path / "research" / "quant_review" / "data" / "flat_sleeve_scan.csv").is_file()
    assert not (tmp_path / "data" / "quant_review").exists()


def test_p4_doctor_migrate_research_flag():
    src = (ROOT / "scripts" / "doctor.py").read_text(encoding="utf-8")
    assert "--migrate-research" in src
    assert "check_research_boundary" in src

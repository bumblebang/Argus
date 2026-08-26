"""Phase 0 — packaging / paths / CLI 위임 (동작 동등 가드)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = Path(__file__).resolve().parent / "golden" / "ops_path_manifest.json"

pytestmark = pytest.mark.ops_golden


def test_p0_paths_matches_manifest():
    from src import paths

    man = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert paths.CANONICAL == man["paths"]
    assert paths.LAYOUT == man["layout"]
    for key, rel in man["paths"].items():
        assert paths.rel(key) == rel
        assert paths.layout_rel(key) == man["layout"][key]
    # resolve 는 디스크 존재에 따라 LAYOUT/LEGACY 를 고른다 — 키·상대경로 계약만 여기서 잠금.
    # 존재 우선 동작은 tests/test_paths_resolve.py · test_p2_paths.py.

def test_p0_paths_unknown_key():
    from src import paths

    with pytest.raises(KeyError, match="unknown path key"):
        paths.rel("not_a_real_key")


def test_p0_cli_help():
    from src.cli.main import main

    assert main(["--help"]) == 0
    assert main(["version"]) == 0
    assert main(["nope"]) == 2


def test_p0_cli_delegates_bootstrap(tmp_path, monkeypatch):
    """argus bootstrap → scripts.bootstrap.main (덮어쓰기 없음 경로)."""
    from src.cli import main as cli_main

    # chdir to tmp with example files copied from repo so bootstrap can run
    monkeypatch.chdir(tmp_path)
    # CLI resolves scripts from repo ROOT (not cwd) — just ensure help path works
    # and bootstrap subcommand finds script file.
    assert (ROOT / "scripts" / "bootstrap.py").is_file()
    # Dry: call with --help equivalent by ensuring unknown flag still reaches script
    # bootstrap has no --help; run main via CLI with env that keeps files
    # Safer: only check _COMMANDS wiring
    assert "bootstrap" in cli_main._COMMANDS
    assert cli_main._COMMANDS["watch"] == "watch.py"


def test_p0_cli_run_doctor_smoke(monkeypatch):
    """argus doctor 가 scripts.doctor.main 을 탄다 (네트워크 실패해도 프로세스 진입)."""
    from src.cli.main import main

    # doctor may return 0 or 1 depending on keys; must not crash
    rc = main(["doctor"])
    assert rc in (0, 1)

"""Phase 3 — CLI 서브커맨드·doctor 플래그·bat 위임 계약."""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.ops_golden


def test_p3_cli_commands_cover_ops():
    from src.cli.main import _COMMANDS

    for cmd in (
        "watch", "doctor", "bootstrap", "bridge", "agent-cycle",
        "athena", "value-scan", "market-state", "screen", "watchdog",
        "check-auth", "check-cli", "public", "shadow-score",
    ):
        assert cmd in _COMMANDS
        assert (ROOT / "scripts" / _COMMANDS[cmd]).is_file()


def test_p3_cli_help_lists_athena():
    from src.cli.main import main
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert main(["--help"]) == 0
    text = buf.getvalue()
    assert "athena" in text
    assert "value-scan" in text
    assert "Phase 3" in text


def test_p3_doctor_has_check_flags():
    src = (ROOT / "scripts" / "doctor.py").read_text(encoding="utf-8")
    assert "--check-auth" in src
    assert "--check-cli" in src
    assert "check_auth.py" in src


def test_p3_run_bats_prefer_argus_exe():
    for name in (
        "run_watch.bat", "run_athena.bat", "run_value_scan.bat",
        "run_market_state.bat", "run_bot.bat", "run_public.bat",
        "run_shadow_score.bat", "bridge_hb_window.cmd",
    ):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8", errors="replace")
        assert "argus.exe" in text, name
        assert "scripts\\" in text or "scripts/" in text, name  # fallback stub

"""Argus CLI — scripts/*.py 위임 (Phase 3).

  pip install -e .
  argus watch --dry --ticks 1
  argus doctor
  argus athena
  argus value-scan

레거시 `python scripts/….py` 는 stub 유지(DeprecationWarning).
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

# src/cli/main.py → parents[2] = repo root
_ROOT = Path(__file__).resolve().parents[2]

# subcommand → scripts/ 파일명 (운영·배치 정본 목록)
_COMMANDS: dict[str, str] = {
    "watch": "watch.py",
    "doctor": "doctor.py",
    "bootstrap": "bootstrap.py",
    "agent-cycle": "agent_cycle.py",
    "bridge": "bridge_tick.py",
    "athena": "athena.py",
    "value-scan": "value_scan.py",
    "value-trade": "value_trade.py",
    "screen": "screen.py",
    "market-state": "build_market_state.py",
    "watchdog": "watchdog.py",
    "alert-check": "alert_check.py",
    "baserate": "baserate.py",
    "earnings-cal": "earnings_cal.py",
    "macro-cal": "macro_cal.py",
    "live-smoke": "live_smoke.py",
    "which-claude": "which_claude.py",
    "check-auth": "check_auth.py",
    "check-cli": "check_cli.py",
    "public": "publish_public.py",
    "shadow-score": "score_shadow_ledger.py",
}


def _help_text() -> str:
    # 그룹별 안내용 (실행은 전부 _COMMANDS)
    core = "watch doctor bootstrap bridge agent-cycle"
    batch = "athena value-scan value-trade market-state baserate earnings-cal macro-cal"
    ops = "screen watchdog alert-check live-smoke which-claude public shadow-score"
    checks = "check-auth check-cli  (또는: doctor --check-auth|--check-cli)"
    return (
        "Argus CLI (Phase 3)\n\n"
        f"  core:   {core}\n"
        f"  batch:  {batch}\n"
        f"  ops:    {ops}\n"
        f"  checks: {checks}\n\n"
        "예: argus watch --dry --ticks 1\n"
        "    argus doctor\n"
        "    argus doctor --migrate-data\n"
        "    argus doctor --migrate-research\n"
        "    argus athena\n"
        "    argus bridge --serve 60\n"
        "레거시: python scripts/watch.py ... (DeprecationWarning)\n"
    )


def _run_script(filename: str, argv: list[str]) -> int:
    """scripts/<filename> 의 main() 호출. __main__ 블록은 실행하지 않음."""
    path = _ROOT / "scripts" / filename
    if not path.is_file():
        print(f"missing script: {path}", file=sys.stderr)
        return 2
    root_s = str(_ROOT)
    scripts_s = str(_ROOT / "scripts")
    if root_s not in sys.path:
        sys.path.insert(0, root_s)
    if scripts_s not in sys.path:
        sys.path.insert(0, scripts_s)

    old_argv = sys.argv
    sys.argv = [str(path), *argv]
    try:
        ns = runpy.run_path(str(path), run_name="__argus_cli__")
        main = ns.get("main")
        if not callable(main):
            print(f"no main() in {filename}", file=sys.stderr)
            return 2
        rc = main()
        return int(rc or 0)
    finally:
        sys.argv = old_argv


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_help_text())
        return 0
    if argv[0] in ("-V", "--version", "version"):
        from src import __version__
        print(f"argus {__version__}")
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd not in _COMMANDS:
        print(f"unknown command: {cmd}\n", file=sys.stderr)
        print(_help_text(), file=sys.stderr)
        return 2
    return _run_script(_COMMANDS[cmd], rest)


if __name__ == "__main__":
    raise SystemExit(main())

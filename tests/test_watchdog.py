"""워치독 재기동 argv — Windows schtasks / macOS launchctl."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import watchdog as wd  # noqa: E402


def test_restart_argv_windows():
    cmds = wd.restart_argv("win32")
    assert cmds is not None
    assert cmds[0] == ["schtasks", "/End", "/TN", "ArgusWatch"]
    assert cmds[1] == ["schtasks", "/Run", "/TN", "ArgusWatch"]


def test_restart_argv_macos():
    cmds = wd.restart_argv("darwin", uid=501)
    assert cmds == [["launchctl", "kickstart", "-k", "gui/501/local.argus.watch"]]


def test_restart_argv_linux():
    cmds = wd.restart_argv("linux")
    assert cmds == [["systemctl", "--user", "restart", "argus-watch.service"]]


def test_restart_argv_unknown_unwired():
    assert wd.restart_argv("freebsd") is None

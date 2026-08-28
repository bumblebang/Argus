"""워치독 재기동 argv — Windows schtasks / macOS launchctl."""
from __future__ import annotations

import json
import sys
import time
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


def test_watchdog_flags_should_be_open_empty_markets(tmp_path, monkeypatch, capsys):
    hb_path = tmp_path / "watch.heartbeat"
    hb_path.write_text(json.dumps({
        "ts": time.time(), "ticks": 1,
        "should_be_open": ["US"], "markets_open": [], "polled": 0, "ok": False,
    }), encoding="utf-8")
    monkeypatch.setattr(wd, "_heartbeat_path", lambda: hb_path)
    monkeypatch.setattr(wd, "heartbeat_age", lambda: 5.0)
    monkeypatch.setattr(wd, "heartbeat_payload",
                        lambda: json.loads(hb_path.read_text(encoding="utf-8")))
    wd.main()
    log_text = (tmp_path / "logs" / "watchdog.log").read_text(encoding="utf-8") if (tmp_path / "logs" / "watchdog.log").exists() else ""
    out = capsys.readouterr().out
    assert "poll unhealthy" in out or "poll unhealthy" in log_text
    assert "should=" in out or "should=" in log_text or "['US']" in out

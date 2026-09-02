"""code_rev · post-merge 재기동."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))


def test_current_code_rev_from_git():
    from src.code_rev import current_code_rev

    current_code_rev.cache_clear()
    rev = current_code_rev(ROOT)
    assert rev == "unknown" or (len(rev) >= 7 and rev.isalnum())


def test_post_merge_skips_unrelated_paths(monkeypatch):
    from scripts import post_merge_restart as pmr

    monkeypatch.setattr(pmr, "paths_changed", lambda _prev: False)
    monkeypatch.setattr(pmr, "restart_watch", lambda: (_ for _ in ()).throw(AssertionError("no restart")))
    assert pmr.main() == 0


def test_post_merge_restarts_on_code_change(monkeypatch):
    from scripts import post_merge_restart as pmr

    called = []

    monkeypatch.setattr(pmr, "paths_changed", lambda _prev: True)
    monkeypatch.setattr(pmr, "restart_watch", lambda: called.append(1) or True)
    assert pmr.main() == 0
    assert called == [1]


def test_watchdog_code_rev_stale():
    import watchdog as wd

    assert wd.code_rev_stale({})[0] is False
    assert wd.code_rev_stale({"code_rev": "abc123"})[1] == "abc123"


def test_watchdog_restarts_on_code_rev_mismatch(tmp_path, monkeypatch, capsys):
    import watchdog as wd

    hb_path = tmp_path / "watch.heartbeat"
    hb_path.write_text('{"ts": 1, "code_rev": "oldrev111111"}', encoding="utf-8")
    restarted: list[str] = []

    monkeypatch.setattr(wd, "_heartbeat_path", lambda: hb_path)
    monkeypatch.setattr(wd, "heartbeat_age", lambda: 5.0)
    monkeypatch.setattr(wd, "heartbeat_payload",
                        lambda: {"code_rev": "oldrev111111"})
    monkeypatch.setattr(wd, "code_rev_stale",
                        lambda hb: (True, "oldrev111111", "newrev222222"))
    monkeypatch.setattr(wd, "restart", lambda: restarted.append("yes"))

    wd.main()
    assert restarted == ["yes"]
    out = capsys.readouterr().out
    assert "code_rev stale" in out

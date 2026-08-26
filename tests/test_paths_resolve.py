"""paths.resolve LAYOUT dual-search 계약."""
from __future__ import annotations

from pathlib import Path

from src import paths as _paths


def test_resolve_layout_over_legacy(tmp_path: Path):
    root = tmp_path
    (root / "data" / "state").mkdir(parents=True)
    (root / "data" / "ledgers").mkdir(parents=True)
    (root / "data" / "state" / "bot.db").write_bytes(b"new")
    (root / "data" / "bot.db").write_bytes(b"old")
    got = _paths.resolve("db", root=root, configured="data/bot.db")
    assert got == (root / "data" / "state" / "bot.db").resolve()


def test_resolve_abs_legacy_finds_layout(tmp_path: Path, monkeypatch):
    root = tmp_path
    (root / "data" / "state").mkdir(parents=True)
    (root / "data" / "state" / "bot.db").write_bytes(b"new")
    monkeypatch.setattr(_paths, "ROOT", root)
    abs_legacy = root / "data" / "bot.db"
    got = _paths.resolve("db", configured=abs_legacy)
    assert got == (root / "data" / "state" / "bot.db").resolve()


def test_resolve_missing_defaults_to_layout(tmp_path: Path):
    root = tmp_path
    got = _paths.resolve("paper", root=root)
    assert got == (root / "data" / "state" / "paper_account.json").resolve()
    assert not got.exists()


def test_resolve_decisions_ledgers(tmp_path: Path):
    root = tmp_path
    (root / "data" / "ledgers").mkdir(parents=True)
    (root / "data" / "ledgers" / "decisions.jsonl").write_text("{}\n", encoding="utf-8")
    (root / "data" / "decisions.jsonl").write_text("legacy\n", encoding="utf-8")
    got = _paths.resolve("decisions", root=root)
    assert "ledgers" in got.as_posix()


def test_resolve_outside_repo_passthrough(tmp_path: Path):
    other = tmp_path / "elsewhere" / "bot.db"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"x")
    got = _paths.resolve("db", root=tmp_path / "repo", configured=other)
    assert got == other.resolve()

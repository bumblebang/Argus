"""대시보드 _gather 가 snapshots 전표 count 로 멈추지 않는지."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from scripts import dashboard as d


def _mini_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE events (
          id INTEGER PRIMARY KEY, ts REAL, kind TEXT, symbol TEXT, payload TEXT);
        CREATE TABLE positions (
          id INTEGER PRIMARY KEY, symbol TEXT, market TEXT, strategy TEXT, state TEXT,
          qty REAL, avg_price REAL, thesis TEXT, stop_price REAL, target_price REAL,
          opened_at REAL, closed_at REAL, exit_price REAL, pnl REAL, exit_reason TEXT,
          meta TEXT);
        CREATE TABLE dossiers (
          id INTEGER PRIMARY KEY, symbol TEXT, market TEXT, created_at REAL,
          expires_at REAL);
        CREATE TABLE decisions (
          id INTEGER PRIMARY KEY, ts REAL, symbol TEXT, action TEXT, conviction REAL,
          thesis TEXT, verdict TEXT, payload TEXT);
        CREATE TABLE snapshots (
          id INTEGER PRIMARY KEY, ts REAL, symbol TEXT, price REAL, payload TEXT);
        """
    )
    con.execute("INSERT INTO snapshots(ts,symbol,price) VALUES (1,'X',1)")
    con.commit()
    con.close()


def test_gather_skips_snapshots_full_count(tmp_path, monkeypatch):
    """snapshots count(*) 제거 회귀 — 결과 키 없고 gather 가 수 초 안에 끝난다."""
    db = tmp_path / "bot.db"
    _mini_db(db)
    monkeypatch.setattr(d, "DB", db)
    monkeypatch.setattr(d, "PAPER", tmp_path / "missing_paper.json")
    monkeypatch.setattr(d, "MARKET_STATE", tmp_path / "missing_ms.json")
    monkeypatch.setattr(d, "CONFIG", Path("config.example.yaml"))

    t0 = time.perf_counter()
    data = d._gather()
    assert time.perf_counter() - t0 < 5.0
    assert data.get("db") is True
    assert "snap_count" not in data
    assert "snap_last" not in data


def test_dashboard_source_has_no_snapshots_count():
    src = Path("scripts/dashboard.py").read_text(encoding="utf-8")
    assert "count(*) n, max(ts) m from snapshots" not in src

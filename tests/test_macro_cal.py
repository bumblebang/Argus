"""매크로 캘린더 — 큐레이티드 YAML · 병합 · focus 연동."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from src.datasources.macro_cal import (build_calendar, load_curated, merge_events,
                                       with_ddays)
from src.focus import build_focus


def test_load_curated_has_fomc_and_bok():
    ev = load_curated()
    ids = {e["id"] for e in ev}
    assert "fomc" in ids and "bok_mpc" in ids
    assert all("date" in e and "label" in e for e in ev)


def test_merge_curated_wins():
    curated = [{"id": "fomc", "date": "2026-09-16", "label": "FOMC", "source": "curated"}]
    extra = [{"id": "fomc", "date": "2026-09-16", "label": "dup", "source": "finnhub"},
             {"id": "cpi_us", "date": "2026-08-12", "label": "US CPI", "source": "finnhub"}]
    out = merge_events(curated, extra)
    assert len(out) == 2
    fomc = next(e for e in out if e["id"] == "fomc")
    assert fomc["source"] == "curated"


def test_with_ddays():
    today = date(2026, 8, 2)
    out = with_ddays([{"id": "bok_mpc", "date": "2026-08-27", "label": "금통위"}],
                     today=today)
    assert out[0]["dday"] == 25


def test_build_calendar_writes_shape(tmp_path, monkeypatch):
    # Finnhub 호출 차단 — curated only
    import src.datasources.macro_cal as mc
    monkeypatch.setattr(mc, "fetch_finnhub", lambda *a, **kw: [])
    cal = build_calendar(api_key="", today=date(2026, 8, 2))
    assert "asof" in cal and isinstance(cal["events"], list)
    assert cal["n_curated"] >= 8


def test_focus_reads_macro_calendar_file(tmp_path, monkeypatch):
    import src.focus as focus
    cal = {
        "asof": "t",
        "events": [
            {"id": "bok_mpc", "label": "금통위", "date": "2026-08-03", "market": "KR"},  # D+1
            {"id": "fomc", "label": "FOMC", "date": "2026-09-16", "market": "US"},
        ],
    }
    p = tmp_path / "macro_calendar.json"
    p.write_text(json.dumps(cal), encoding="utf-8")
    monkeypatch.setattr(focus, "MACRO_CAL_PATH", p)
    out = build_focus({}, macro_events=None, today=date(2026, 8, 2))
    ids = [ln["id"] for ln in out["lenses"]]
    assert "bok_mpc" in ids
    assert "fomc" not in ids  # D+45 밖


def test_finnhub_403_returns_empty(monkeypatch):
    import src.datasources.macro_cal as mc

    class _Resp:
        status_code = 403
        def raise_for_status(self):
            pass
        def json(self):
            return {}

    monkeypatch.setattr(mc.requests, "get", lambda *a, **kw: _Resp())
    assert mc.fetch_finnhub("x") == []

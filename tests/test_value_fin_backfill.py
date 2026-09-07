"""S0 주간 재무 백필 게이팅 — maybe_weekly_backfill.

DART·네트워크 무접촉(주입 함수 mock). 리포트 신선도만으로 실행 여부가 갈리는지,
비활성/키없음이 조용히 스킵되는지 확인한다.
"""
import json
from types import SimpleNamespace

from src.value_fin_backfill import last_backfill_ts, maybe_weekly_backfill

_NOW = 1_800_000_000.0


def _cfg(**vs):
    raw = {"value_scan": {"markets": ["KR"], "pool": 10, **vs}}
    return SimpleNamespace(raw=raw)


def _fakes(calls):
    def pool_fn(*, pool):
        calls.append(("pool", pool))
        return [{"symbol": "005930", "name": "삼성전자", "market_cap": 1e12}]

    def backfill_fn(api_key, pool_rows, *, weekly=False, n_min=20):
        calls.append(("backfill", weekly, n_min))
        return {"ts": _NOW, "dart_calls": 3, "fetched_year_entries": 1,
                "coverage": {"ok": True}}
    return pool_fn, backfill_fn


def test_리포트_없으면_실행(tmp_path):
    calls = []
    pool_fn, backfill_fn = _fakes(calls)
    out = maybe_weekly_backfill(
        _cfg(), now=_NOW, report_path=tmp_path / "r.json", api_key="k",
        build_pool_rows_fn=pool_fn, backfill_fn=backfill_fn)
    assert out["ran"] is True
    assert ("backfill", True, 20) in calls
    saved = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert saved["dart_calls"] == 3
    assert last_backfill_ts(tmp_path / "r.json") == _NOW


def test_리포트_신선하면_스킵(tmp_path):
    rp = tmp_path / "r.json"
    rp.write_text(json.dumps({"ts": _NOW - 3 * 86400}), encoding="utf-8")
    calls = []
    pool_fn, backfill_fn = _fakes(calls)
    out = maybe_weekly_backfill(
        _cfg(fin_backfill_days=7), now=_NOW, report_path=rp, api_key="k",
        build_pool_rows_fn=pool_fn, backfill_fn=backfill_fn)
    assert out["ran"] is False and out["why"] == "fresh"
    assert calls == []


def test_주기_지나면_실행(tmp_path):
    rp = tmp_path / "r.json"
    rp.write_text(json.dumps({"ts": _NOW - 9 * 86400}), encoding="utf-8")
    calls = []
    pool_fn, backfill_fn = _fakes(calls)
    out = maybe_weekly_backfill(
        _cfg(fin_backfill_days=7), now=_NOW, report_path=rp, api_key="k",
        build_pool_rows_fn=pool_fn, backfill_fn=backfill_fn)
    assert out["ran"] is True


def test_비활성_및_키없음_스킵(tmp_path):
    calls = []
    pool_fn, backfill_fn = _fakes(calls)
    off = maybe_weekly_backfill(
        _cfg(fin_backfill_days=0), now=_NOW, report_path=tmp_path / "r.json",
        api_key="k", build_pool_rows_fn=pool_fn, backfill_fn=backfill_fn)
    assert off == {"ran": False, "why": "disabled"}
    nokey = maybe_weekly_backfill(
        _cfg(), now=_NOW, report_path=tmp_path / "r.json", api_key="",
        build_pool_rows_fn=pool_fn, backfill_fn=backfill_fn)
    assert nokey["why"] == "no_dart_key"
    assert calls == []

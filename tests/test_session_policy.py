"""session_policy — config 파싱·장중 판정 SSOT."""
from datetime import datetime
from zoneinfo import ZoneInfo

import src.market_hours as mh
import src.session_policy as sp

KST = ZoneInfo("Asia/Seoul")


def _kst(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=KST).timestamp()


def test_trading_sessions_from_raw():
    raw = {"trading_sessions": {"KR": ["regular", "premarket"], "US": ["regular"]}}
    out = sp.trading_sessions_from_raw(raw)
    assert out["KR"] == ("regular", "premarket")
    assert out["US"] == ("regular",)


def test_market_tradable_uses_config(tmp_path, monkeypatch):
    path = tmp_path / "market_sessions.json"
    import json
    cache = {"KR": {"market": "KR", "date": "2026-07-14", "fetched": _kst(2026, 7, 14, 7),
                    "sessions": [
                        {"name": "premarket", "start": _kst(2026, 7, 14, 8),
                         "end": _kst(2026, 7, 14, 9)},
                        {"name": "regular", "start": _kst(2026, 7, 14, 9),
                         "end": _kst(2026, 7, 14, 15, 30)},
                    ]}}
    path.write_text(json.dumps(cache), encoding="utf-8")
    monkeypatch.setattr(mh, "_SESSIONS_CACHE", path)
    ts = _kst(2026, 7, 14, 8, 30)
    assert sp.market_tradable("KR", {"KR": ("regular",)}, ts) is False
    assert sp.market_tradable("KR", {"KR": ("regular", "premarket")}, ts) is True


def test_make_tradable_fn(tmp_path, monkeypatch):
    path = tmp_path / "market_sessions.json"
    import json
    cache = {"US": {"market": "US", "date": "2026-07-14", "fetched": _kst(2026, 7, 14, 9),
                    "sessions": [
                        {"name": "regular", "start": _kst(2026, 7, 14, 22, 30),
                         "end": _kst(2026, 7, 15, 5, 0)},
                    ]}}
    path.write_text(json.dumps(cache), encoding="utf-8")
    monkeypatch.setattr(mh, "_SESSIONS_CACHE", path)
    fn = sp.make_tradable_fn({"US": ("regular",)}, now_fn=lambda: _kst(2026, 7, 15, 0, 30))
    assert fn("US") is True
    assert fn("KR") is False

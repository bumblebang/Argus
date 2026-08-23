"""KRX Open API — VKOSPI/풋콜 파싱·캐시·fear_kr inputs 병합. 네트워크 0."""
from __future__ import annotations

import json
import types

from src.datasources.fear_greed import assess, kr_fear_proxy
from src.datasources.krx_open import (cache_fresh, merge_into_fear_kr,
                                       pick_vkospi, put_call_from_rows,
                                       refresh_fear_cache)


def test_pick_vkospi_prefers_official_name():
    rows = [
        {"IDX_NM": "코스피 200 변동성 목표레버리지 24% 지수", "CLSPRC_IDX": "3884"},
        {"IDX_NM": "코스피 200 변동성지수", "CLSPRC_IDX": "17.5"},
        {"IDX_NM": "KRX 저변동성지수", "CLSPRC_IDX": "100"},
    ]
    px, nm = pick_vkospi(rows)
    assert px == 17.5 and nm == "코스피 200 변동성지수"


def test_put_call_from_rows():
    rows = [
        {"RGHT_TP_NM": "CALL", "ACC_TRDVOL": "1,000"},
        {"RGHT_TP_NM": "PUT", "ACC_TRDVOL": "500"},
        {"RGHT_TP_NM": "풋", "ACC_TRDVOL": "250"},
        {"RGHT_TP_NM": "콜", "ACC_TRDVOL": "250"},
    ]
    out = put_call_from_rows(rows)
    assert out["put_vol"] == 750 and out["call_vol"] == 1250
    assert out["put_call_ratio"] == 0.6


def test_merge_into_fear_kr_does_not_touch_score():
    kr = kr_fear_proxy({"breadth_above_ma20": 0.2, "n": 19}, -6.0, -5.0)
    score = kr["score"]
    comps = dict(kr["components"])
    merge_into_fear_kr(kr, {
        "vkospi": 17.5, "put_call_ratio": 0.73,
        "put_vol": 100, "call_vol": 137, "bas_dd": "20260820",
        "vkospi_name": "코스피 200 변동성지수",
    })
    assert kr["score"] == score and kr["components"] == comps
    assert kr["incomplete"] is False
    assert kr["inputs"]["vkospi"] == 17.5
    assert kr["inputs"]["put_call_ratio"] == 0.73
    assert kr["inputs"]["krx_bas_dd"] == "20260820"


def test_refresh_fear_cache_ttl_and_force(tmp_path, monkeypatch):
    path = tmp_path / "krx_fear.json"
    calls = []

    def fake_get(api_path, bas_dd, api_key=None):
        calls.append((api_path, bas_dd))
        if "drvprod" in api_path:
            return {"OutBlock_1": [
                {"IDX_NM": "코스피 200 변동성지수", "CLSPRC_IDX": "18.0"}]}
        if "opt_bydd" in api_path:
            return {"OutBlock_1": [
                {"RGHT_TP_NM": "PUT", "ACC_TRDVOL": "100"},
                {"RGHT_TP_NM": "CALL", "ACC_TRDVOL": "200"},
            ]}
        return {"OutBlock_1": []}

    monkeypatch.setenv("KRX_API_KEY", "test-key")
    t = {"n": 1_700_000_000.0}
    out1 = refresh_fear_cache(path=path, ttl_sec=3600, force=True,
                              get_fn=fake_get, now_fn=lambda: t["n"])
    assert out1["vkospi"] == 18.0
    assert out1["put_call_ratio"] == 0.5
    n1 = len(calls)
    out2 = refresh_fear_cache(path=path, ttl_sec=3600, force=False,
                              get_fn=fake_get, now_fn=lambda: t["n"] + 10)
    assert out2["vkospi"] == 18.0
    assert len(calls) == n1  # TTL hit — 네트워크 0
    assert cache_fresh(out2, 3600, now=t["n"] + 10)


def test_refresh_without_key_keeps_cache(tmp_path, monkeypatch):
    path = tmp_path / "krx_fear.json"
    path.write_text(json.dumps({"ts": 1, "vkospi": 12.0}), encoding="utf-8")
    monkeypatch.delenv("KRX_API_KEY", raising=False)
    out = refresh_fear_cache(path=path, ttl_sec=1, force=True, now_fn=lambda: 999)
    assert out["vkospi"] == 12.0


def test_assess_merges_krx_cache(tmp_path, monkeypatch):
    import src.datasources.fear_greed as fg

    hist = tmp_path / "fh.json"
    hist.write_text(json.dumps({"kr": []}), encoding="utf-8")
    cache = tmp_path / "krx.json"
    cache.write_text(json.dumps({
        "ts": 9_999_999_999, "vkospi": 19.2, "put_call_ratio": 0.8,
        "bas_dd": "20260820", "source": "krx_open",
    }), encoding="utf-8")

    monkeypatch.setattr(fg, "fetch_cnn_fear_greed",
                        lambda **k: {"score": 39.0, "rating": "fear",
                                     "market": "US", "source": "cnn"})
    monkeypatch.setattr(fg, "index_stats",
                        lambda **k: {"drawdown_pct": -6.0, "ret_5d_pct": -5.0})
    monkeypatch.setattr(fg, "record_history", lambda *a, **k: None)

    cfg = types.SimpleNamespace(raw={"market_state": {
        "breadth_min_n": 10,
        "fear_greed": {
            "history_path": str(hist),
            "krx_enrich": True,
            "krx_cache_path": str(cache),
            "krx_ttl_sec": 999999,
        }}})
    state = {"regime": {"KR": {"breadth_above_ma20": 0.20, "n": 19}}}
    out = assess(state, cfg)
    kr = out["fear_kr"]
    assert kr["score"] == 33.0
    assert "vkospi" not in kr["components"]
    assert kr["inputs"]["vkospi"] == 19.2
    assert kr["inputs"]["put_call_ratio"] == 0.8

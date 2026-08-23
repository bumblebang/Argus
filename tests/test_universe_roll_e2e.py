"""E2E: core_refresh 로 universe.yaml 갱신 → UniverseProvider.markets() 가 핫리로드 반영.

데몬 배선의 핵심 계약: 데몬이 파일만 갱신하면 뇌·감시·공시가 재기동 없이 새 유니버스를
본다(UniverseProvider 가 mtime 리로드). 발굴/히스토리는 합성 주입(실네트워크 금지).
"""
import os

import numpy as np
import pandas as pd
import yaml

import src.universe_roll as UR
from src.config import load_config
from src.engine.universe_provider import UniverseProvider


class _Cfg:
    """UniverseProvider 는 resolve_universe(cfg.raw)만 본다 — 최소 스텁."""
    def __init__(self, raw):
        self.raw = raw


def _synth_df(base=70000.0, n=80):
    rng = np.random.default_rng(1234)
    close = base * np.cumprod(1 + rng.normal(0.001, 0.02, n))
    return pd.DataFrame({
        "open": np.concatenate([[close[0]], close[:-1]]),
        "high": close * 1.01, "low": close * 0.99, "close": close,
        "volume": np.full(n, 5_000_000),
    })


def test_core_refresh_reflected_in_provider(tmp_path, monkeypatch):
    out = tmp_path / "universe.yaml"
    monkeypatch.setattr(UR, "OUT", out)
    # 발굴/히스토리 합성 주입.
    kr = [("KR", f"K{i}", f"이름{i}") for i in range(8)]
    dk = lambda cfg, count, dry: kr[:count]
    monkeypatch.setattr(UR, "discover_kr", dk)
    monkeypatch.setattr(UR, "_DISCOVER", {"KR": dk})
    monkeypatch.setattr(UR, "_fetch_fn", lambda cfg, dry: (lambda s, m: _synth_df()))

    cfg = load_config()
    # core_refresh 로 파일 생성.
    assert UR.core_refresh(cfg, "KR") is not None
    assert out.exists()

    # Provider 는 screener.enabled=true 인 raw 로 그 tmp dir 을 읽는다.
    raw = {"universe": {"KR": [{"symbol": "STATIC"}]},
           "screener": {"enabled": True}}
    provider = UniverseProvider(_Cfg(raw), data_dir=tmp_path)
    markets = provider.markets()
    syms = {it["symbol"] for it in markets["KR"]}
    assert "STATIC" not in syms                    # 정적 아님 — 동적 파일 우선
    assert any(s.startswith("K") for s in syms)    # core_refresh 결과 반영
    for it in markets["KR"]:                        # 레이어 태그가 핫리로드로 그대로 전파
        assert it["layer"] == "core"


def test_provider_reloads_after_second_refresh(tmp_path, monkeypatch):
    out = tmp_path / "universe.yaml"
    monkeypatch.setattr(UR, "OUT", out)
    monkeypatch.setattr(UR, "_fetch_fn", lambda cfg, dry: (lambda s, m: _synth_df()))
    cfg = load_config()

    # 1차: K 종목.
    dk1 = lambda cfg, count, dry: [("KR", f"K{i}", f"K{i}") for i in range(8)][:count]
    monkeypatch.setattr(UR, "_DISCOVER", {"KR": dk1})
    UR.core_refresh(cfg, "KR")
    raw = {"universe": {"KR": []}, "screener": {"enabled": True}}
    provider = UniverseProvider(_Cfg(raw), data_dir=tmp_path)
    first = {it["symbol"] for it in provider.markets()["KR"]}
    assert all(s.startswith("K") for s in first)

    # 2차: Z 종목으로 교체 + mtime 을 앞당겨 확실히 리로드 유발.
    dk2 = lambda cfg, count, dry: [("KR", f"Z{i}", f"Z{i}") for i in range(8)][:count]
    monkeypatch.setattr(UR, "_DISCOVER", {"KR": dk2})
    UR.core_refresh(cfg, "KR")
    os.utime(out, (out.stat().st_atime, out.stat().st_mtime + 10))
    second = {it["symbol"] for it in provider.markets()["KR"]}
    assert all(s.startswith("Z") for s in second)   # 재독됨(핫리로드)

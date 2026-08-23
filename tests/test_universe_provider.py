"""engine.universe_provider — data/universe.yaml 런타임 재독(핫리로드) 검증.

실네트워크·실 screen 호출 없이 tmp 경로에 yaml 을 쓰고 mtime 을 조작해, markets() 가
파일 변경 시 resolve_universe 로 재로드하는지·스레드 안전한지 확인한다.
"""
import threading

import yaml

from src.engine.universe_provider import UniverseProvider


class _Cfg:
    """resolve_universe 는 cfg.raw 만 본다 — 최소 스텁."""
    def __init__(self, raw):
        self.raw = raw


def _raw(enabled=True, **sc):
    return {"universe": {"KR": [{"symbol": "005930"}]},
            "screener": {"enabled": enabled, **sc}}


def _write_universe(tmp_path, mapping):
    (tmp_path / "universe.yaml").write_text(yaml.safe_dump(mapping), encoding="utf-8")


def test_static_when_screener_disabled(tmp_path):
    # 스크리너 비활성 → 동적 파일이 있어도 정적 config universe 반환.
    _write_universe(tmp_path, {"KR": [{"symbol": "DYN"}]})
    p = UniverseProvider(_Cfg(_raw(enabled=False)), data_dir=tmp_path)
    assert p.markets()["KR"][0]["symbol"] == "005930"


def test_dynamic_when_fresh(tmp_path):
    _write_universe(tmp_path, {"KR": [{"symbol": "DYN"}]})
    p = UniverseProvider(_Cfg(_raw()), data_dir=tmp_path)
    assert p.markets()["KR"][0]["symbol"] == "DYN"          # 신선 → 동적 우선


def test_static_when_no_file(tmp_path):
    p = UniverseProvider(_Cfg(_raw()), data_dir=tmp_path)   # universe.yaml 없음
    assert p.markets()["KR"][0]["symbol"] == "005930"       # 정적 폴백


def test_stale_falls_back_to_static(tmp_path):
    # 너무 오래된 yaml → resolve_universe 의 정적 폴백 그대로(staleness 판단 위임).
    f = tmp_path / "universe.yaml"
    f.write_text(yaml.safe_dump({"KR": [{"symbol": "DYN"}]}), encoding="utf-8")
    future = f.stat().st_mtime + 2 * 3600                   # 2h 뒤 = max_age(1h) 초과
    p = UniverseProvider(_Cfg(_raw(universe_max_age_hours=1)), data_dir=tmp_path,
                         now_fn=lambda: future)
    assert p.markets()["KR"][0]["symbol"] == "005930"


def test_reloads_on_mtime_change(tmp_path):
    # 핵심: 파일 mtime 이 바뀌면 다음 markets() 호출에서 재로드되어 새 내용 반영.
    f = tmp_path / "universe.yaml"
    f.write_text(yaml.safe_dump({"KR": [{"symbol": "OLD"}]}), encoding="utf-8")
    p = UniverseProvider(_Cfg(_raw()), data_dir=tmp_path)
    assert p.markets()["KR"][0]["symbol"] == "OLD"
    # 내용 교체 후 mtime 을 명시적으로 미래로(테스트 안정성 — 같은 초 재기록 방지).
    f.write_text(yaml.safe_dump({"KR": [{"symbol": "NEW"}]}), encoding="utf-8")
    import os
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 10))
    assert p.markets()["KR"][0]["symbol"] == "NEW"          # 재독됨


def test_no_reload_when_unchanged(tmp_path, monkeypatch):
    # 변경 없으면 resolve_universe 를 다시 부르지 않고 캐시 반환(값싼 stat 만).
    _write_universe(tmp_path, {"KR": [{"symbol": "DYN"}]})
    p = UniverseProvider(_Cfg(_raw()), data_dir=tmp_path)   # 생성자에서 1회 로드
    import src.engine.universe_provider as upmod
    calls = []
    monkeypatch.setattr(upmod, "resolve_universe",
                        lambda *a, **k: calls.append(1) or {"KR": []})
    p.markets(); p.markets()
    assert calls == []                                       # 캐시 히트 — 재로드 없음


def test_reload_exception_keeps_previous_cache(tmp_path, monkeypatch):
    # 파일 반쪽 쓰기 등 리로드 예외는 삼키고 직전 캐시를 유지(죽지 않음).
    _write_universe(tmp_path, {"KR": [{"symbol": "GOOD"}]})
    p = UniverseProvider(_Cfg(_raw()), data_dir=tmp_path)
    assert p.markets()["KR"][0]["symbol"] == "GOOD"
    f = tmp_path / "universe.yaml"
    import os
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 10))  # 리로드 유발
    import src.engine.universe_provider as upmod
    def boom(*a, **k):
        raise ValueError("반쪽 파일")
    monkeypatch.setattr(upmod, "resolve_universe", boom)
    assert p.markets()["KR"][0]["symbol"] == "GOOD"          # 직전 캐시 유지


def test_thread_safe_concurrent_calls(tmp_path):
    # 동시 호출(감시 루프 + 뇌 워커)에도 깨지지 않고 일관된 결과를 반환.
    _write_universe(tmp_path, {"KR": [{"symbol": "DYN"}]})
    p = UniverseProvider(_Cfg(_raw()), data_dir=tmp_path)
    results, errors = [], []

    def worker():
        try:
            for _ in range(200):
                results.append(p.markets()["KR"][0]["symbol"])
        except Exception as e:   # 락이 없으면 자료구조 경합으로 터질 수 있음
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert set(results) == {"DYN"}

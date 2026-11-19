"""wiring.gate_sector_map — 유니버스 밖 종목도 섹터 캐시로 분류(미분류 몰림 방지)."""
import json
import os

from src.agents import wiring
from src.agents.wiring import gate_sector_map, sector_map_from_cache


def _write(path, data, mtime=None):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_cache_fills_symbols_outside_universe_and_universe_wins(tmp_path):
    p = tmp_path / "sector_cache.json"
    _write(p, {"KR": {"030200": {"sector": "커뮤니케이션", "raw": "통신"},
                      "000001": {"sector": None, "raw": "??"}},
               "US": {"BKNG": {"sector": "경기소비재"},
                      "AAPL": {"sector": "경기소비재"}}})
    uni = {"US": [{"symbol": "AAPL", "sector": "Technology"}]}
    m = gate_sector_map(uni, cache_path=p)
    assert m["030200"] == "커뮤니케이션" and m["BKNG"] == "경기소비재"
    assert m["AAPL"] == "정보기술"            # 유니버스(정규화) 우선
    assert "000001" not in m                  # 정규화 실패분은 미지정(=미분류)


def test_cache_reparsed_only_when_file_changes(tmp_path, monkeypatch):
    p = tmp_path / "sector_cache.json"
    _write(p, {"KR": {"A": {"sector": "금융"}}}, mtime=1_000_000)
    wiring._SECTOR_CACHE_MEMO.clear()
    calls = []
    real = wiring.normalize_sector
    monkeypatch.setattr(wiring, "normalize_sector", lambda s: calls.append(s) or real(s))
    assert sector_map_from_cache(p) == {"A": "금융"}
    assert sector_map_from_cache(p) == {"A": "금융"} and len(calls) == 1   # 메모 적중
    _write(p, {"KR": {"A": {"sector": "금융"}, "B": {"sector": "소재"}}}, mtime=2_000_000)
    assert sector_map_from_cache(p) == {"A": "금융", "B": "소재"}


def test_missing_cache_falls_back_to_universe_only(tmp_path):
    uni = {"KR": [{"symbol": "005930", "sector": "정보기술"}]}
    assert gate_sector_map(uni, cache_path=tmp_path / "none.json") == {"005930": "정보기술"}

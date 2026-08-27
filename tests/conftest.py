"""테스트 공통 설정.

운영 config.yaml(실계좌 모드·경로)과 data/universe.yaml 을 테스트가 읽지 않게 한다.
ARGUS_CONFIG 는 config.example.yaml, 동적 유니버스는 끈다.
"""
import os
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ARGUS_DISABLE_DYNAMIC_UNIVERSE", "1")
os.environ.setdefault("ARGUS_CONFIG", str(_ROOT / "config.example.yaml"))


@pytest.fixture(autouse=True)
def _isolate_fear_history(tmp_path, monkeypatch):
    """공포지수 이력 적재의 기본 경로를 tmp 로 돌린다.

    assess() 를 타는 테스트(live_slice 등)가 운영 data/fear_history.json 을 덮어쓰지
    않게 막는다. record_history 를 path= 로 직접 부르는 테스트는 영향받지 않는다.
    """
    import src.datasources.fear_greed as fg
    monkeypatch.setattr(fg, "HISTORY_PATH", str(tmp_path / "fear_history.json"))


@pytest.fixture(autouse=True)
def _sod_snap_when_closed(monkeypatch):
    """SoD 첫 스냅은 장중에 거부된다 — 테스트 기본은 휴장으로 고정(결정적).

    장중 거부 회귀는 개별 테스트가 current_session 을 'regular' 로 덮어쓴다.
    """
    monkeypatch.setattr("src.market_hours.current_session",
                        lambda market, now=None: "closed")
    monkeypatch.setattr("src.paper_account.current_session",
                        lambda market, now=None: "closed")

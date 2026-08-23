"""런타임 유니버스 공급기 — data/universe.yaml 변경을 돌고 있는 데몬이 다시 읽게 한다.

screen 배치(2회/일)가 universe.yaml 을 재생성해도, 기동 시 한 번 잡힌 cfg.universe 로는
반영되지 않는다(재기동 필요). UniverseProvider 는 markets() 호출 시 파일 mtime 을 값싸게
stat 해, 지난 로드 이후 변경됐으면 resolve_universe 를 다시 돌려 캐시를 갱신한다
(market_state 를 build_watchlist 가 매 틱 재독하는 것과 같은 취지의 지연 리로드).

staleness/enabled 판단은 새로 짜지 않고 resolve_universe 에 위임한다(스크리너 비활성·
파일 없음·너무 오래됨은 그쪽의 정적 폴백이 처리). 감시 루프 스레드(build_watchlist)와
뇌 워커 스레드(CycleRunner.run)가 동시 호출하므로 Lock 으로 보호하고, 리로드 예외는
삼켜 직전 캐시를 유지한다(파일 반쪽 쓰기 등에 죽지 않게).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from ..config import ROOT, resolve_universe
from ..logging_setup import get_logger

log = get_logger("engine.universe_provider")


class UniverseProvider:
    """유효 유니버스({market: [item,...]} = cfg.universe shape)를 런타임에 재독하는 공급기.

    data_dir/now_fn 은 테스트 주입용(기본은 production data 경로·실시간). markets() 는
    스레드 안전하며, universe.yaml 이 마지막 로드 이후 변경됐을 때만 resolve_universe 를
    다시 부른다(변경 없으면 캐시 반환).
    """

    def __init__(self, cfg, *, data_dir: str | Path = ROOT / "data",
                 now_fn: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self._data_dir = Path(data_dir)
        self._path = self._data_dir / "universe.yaml"
        self._now = now_fn
        self._lock = threading.Lock()
        self._cache: dict = {}
        self._loaded_mtime: float | None = None
        # 기동 시 1회 즉시 로드(첫 호출 전에 유효 유니버스를 캐시에 채운다).
        with self._lock:
            self._reload_locked(reason="init")

    def markets(self) -> dict:
        """현재 유효 유니버스를 {market: [item,...]} 로 반환(지연 mtime 리로드)."""
        with self._lock:
            mtime = self._current_mtime()
            # 파일이 새로 생겼거나(None→값) mtime 이 지난 로드보다 커졌으면 재독.
            if mtime != self._loaded_mtime:
                self._reload_locked(reason="mtime")
            return self._cache

    # ── 내부: mtime stat(값쌈) / 리로드(예외 삼킴, 직전 캐시 유지) ──
    def _current_mtime(self) -> float | None:
        try:
            return self._path.stat().st_mtime
        except OSError:
            return None      # 파일 없음 — resolve_universe 가 정적 폴백 처리

    def _reload_locked(self, *, reason: str) -> None:
        """resolve_universe 재호출로 캐시 갱신. 실패 시 직전 캐시 유지(로드 mtime 은 안 넘김)."""
        try:
            new = resolve_universe(self.cfg.raw, self._data_dir, now=self._now())
        except Exception as e:   # 파일 반쪽 쓰기 등 — 죽지 말고 직전 캐시 유지.
            log.warning("유니버스 리로드 실패(직전 캐시 유지): %s", e)
            return
        self._cache = new or {}
        self._loaded_mtime = self._current_mtime()
        n = sum(len(v or []) for v in self._cache.values())
        log.info("유니버스 로드(%s) — 시장=%s, 종목수=%d",
                 reason, list(self._cache.keys()), n)

"""그룹별 토큰버킷 rate limiter.

토스 BASIC tier 는 엔드포인트 그룹마다 초당 호출수(TPS)가 다르다. 상시 폴링에서
이 한도를 넘기면 429(+Retry-After) 가 나므로, 모든 호출이 거쳐가는 단일 지점에서
그룹별로 토큰버킷을 둬 사전에 페이싱한다.

설계: acquire(group) 는 토큰이 충분할 때까지 블로킹 대기 후 1개 소비한다.
lock 은 짧게만 잡고(상태 갱신), 대기는 lock 밖에서 sleep → 멀티스레드 안전.
"""
from __future__ import annotations

import threading
import time

# 토스 BASIC tier 그룹별 TPS (메모리/문서 기준). 상위 tier 시 config 로 오버라이드.
BASIC_TIER_TPS: dict[str, float] = {
    "AUTH": 5,
    "ACCOUNT": 1,
    "ASSET": 5,
    "STOCK": 5,
    "MARKET_INFO": 3,
    "MARKET_DATA": 10,
    "MARKET_DATA_CHART": 5,
    "RANKING": 3,          # /api/v1/rankings (스펙 그룹). 보수 상한
    "ORDER": 6,            # 09:00~09:10 은 3 (장초반 보수치는 호출측에서 조정)
    "ORDER_HISTORY": 5,
}


class TokenBucket:
    """단일 그룹용 토큰버킷. rate=초당 충전량, capacity=최대 누적(기본 rate)."""

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self.rate = float(rate)
        self.capacity = float(capacity if capacity is not None else rate)
        self.tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self._updated) * self.rate)
        self._updated = now

    def acquire(self, n: float = 1.0) -> float:
        """토큰 n 개를 확보. 부족하면 충전될 때까지 대기. 총 대기시간(초) 반환."""
        waited = 0.0
        while True:
            with self._lock:
                self._refill()
                if self.tokens >= n:
                    self.tokens -= n
                    return waited
                wait = (n - self.tokens) / self.rate
            time.sleep(wait)
            waited += wait

    def try_acquire(self, n: float = 1.0) -> bool:
        """대기 없이 즉시 가능하면 소비하고 True, 아니면 False."""
        with self._lock:
            self._refill()
            if self.tokens >= n:
                self.tokens -= n
                return True
            return False


class GroupRateLimiter:
    """그룹명 -> TokenBucket. 모르는 그룹은 무제한(통과)."""

    def __init__(self, limits: dict[str, float] | None = None) -> None:
        src = limits or BASIC_TIER_TPS
        self._buckets = {g: TokenBucket(r) for g, r in src.items()}

    def acquire(self, group: str, n: float = 1.0) -> float:
        bucket = self._buckets.get(group)
        if bucket is None:
            return 0.0
        return bucket.acquire(n)

    def try_acquire(self, group: str, n: float = 1.0) -> bool:
        bucket = self._buckets.get(group)
        if bucket is None:
            return True
        return bucket.try_acquire(n)

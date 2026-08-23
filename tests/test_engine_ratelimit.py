"""engine.ratelimit — 토큰버킷 페이싱 검증."""
import time

from src.engine.ratelimit import TokenBucket, GroupRateLimiter


def test_bucket_allows_burst_up_to_capacity():
    b = TokenBucket(rate=10, capacity=10)
    for _ in range(10):
        assert b.try_acquire()
    assert not b.try_acquire()          # 용량 소진 -> 즉시 불가


def test_bucket_refills_over_time():
    b = TokenBucket(rate=100, capacity=100)   # 100/s
    for _ in range(100):
        b.try_acquire()
    assert not b.try_acquire()
    time.sleep(0.05)                    # ~5개 충전
    assert b.try_acquire()


def test_acquire_blocks_when_empty_and_reports_wait():
    b = TokenBucket(rate=50, capacity=1)
    assert b.acquire() == 0.0           # 첫 토큰 즉시
    waited = b.acquire()                # 비었으니 대기
    assert waited > 0


def test_group_limiter_unknown_group_passes():
    rl = GroupRateLimiter({"MARKET_DATA": 10})
    assert rl.try_acquire("UNKNOWN")    # 모르는 그룹은 무제한 통과
    assert rl.acquire("UNKNOWN") == 0.0


def test_group_limiter_enforces_known_group():
    rl = GroupRateLimiter({"ORDER": 2})
    assert rl.try_acquire("ORDER")
    assert rl.try_acquire("ORDER")
    assert not rl.try_acquire("ORDER")  # 2개 소진

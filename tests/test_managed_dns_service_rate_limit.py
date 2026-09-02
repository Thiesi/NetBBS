"""Tests for services.managed_dns.rate_limit (issue #201 Phase 5)."""

from __future__ import annotations

from services.managed_dns.rate_limit import GlobalRateLimiter


class _MutableFloatClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_capacity_then_rejects():
    clock = _MutableFloatClock()
    limiter = GlobalRateLimiter(capacity=3, refill_per_minute=0, clock=clock)

    assert limiter.allow() is True
    assert limiter.allow() is True
    assert limiter.allow() is True
    assert limiter.allow() is False  # exhausted, and refill_per_minute=0 means never


def test_refills_over_time():
    clock = _MutableFloatClock()
    limiter = GlobalRateLimiter(capacity=1, refill_per_minute=60, clock=clock)  # 1 token/second

    assert limiter.allow() is True
    assert limiter.allow() is False  # no time has passed

    clock.now += 1.0  # one full second -- exactly one token's worth
    assert limiter.allow() is True
    assert limiter.allow() is False


def test_refill_never_exceeds_capacity():
    clock = _MutableFloatClock()
    limiter = GlobalRateLimiter(capacity=2, refill_per_minute=60, clock=clock)

    clock.now += 1000.0  # a very long idle period
    assert limiter.allow() is True
    assert limiter.allow() is True
    assert limiter.allow() is False  # capped at capacity=2, not unbounded accumulation


def test_partial_refill_does_not_grant_a_token_early():
    clock = _MutableFloatClock()
    limiter = GlobalRateLimiter(capacity=1, refill_per_minute=60, clock=clock)

    limiter.allow()  # consume the initial token
    clock.now += 0.5  # half a second -- only half a token's worth
    assert limiter.allow() is False

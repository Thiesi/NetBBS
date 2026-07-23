"""Tests for netbbs.net.throttle (design doc round 28, issue #3)."""

from __future__ import annotations

from netbbs.net.throttle import LinkRequestThrottle, LoginThrottle


class FakeClock:
    """Deterministic, manually-advanced clock -- same pattern as
    monkeypatching module-level timing constants elsewhere in this
    project, but as an injectable dependency here since refill math
    needs to *advance* time, not just shrink a fixed timeout."""

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _throttle(clock, **overrides) -> LoginThrottle:
    defaults = dict(
        per_source_capacity=3.0,
        per_source_refill_per_minute=60.0,  # 1 token/sec, easy to reason about
        per_username_capacity=3.0,
        per_username_refill_per_minute=60.0,
        global_capacity=100.0,
        global_refill_per_minute=6000.0,
        max_tracked_keys=10,
        max_concurrent_unauthenticated_sessions=5,
    )
    defaults.update(overrides)
    return LoginThrottle(clock=clock, **defaults)


# -- basic token-bucket behavior ------------------------------------------


def test_attempts_within_capacity_are_allowed():
    clock = FakeClock()
    throttle = _throttle(clock)
    for _ in range(3):
        assert throttle.allow_attempt(source="1.2.3.4", username="alice") is True


def test_attempt_beyond_capacity_is_rejected():
    clock = FakeClock()
    throttle = _throttle(clock)
    for _ in range(3):
        throttle.allow_attempt(source="1.2.3.4", username="alice")
    assert throttle.allow_attempt(source="1.2.3.4", username="alice") is False


def test_bucket_refills_over_time():
    clock = FakeClock()
    throttle = _throttle(clock)
    for _ in range(3):
        throttle.allow_attempt(source="1.2.3.4", username="alice")
    assert throttle.allow_attempt(source="1.2.3.4", username="alice") is False

    clock.advance(1.0)  # 1 token/sec refill rate
    assert throttle.allow_attempt(source="1.2.3.4", username="alice") is True


def test_bucket_never_exceeds_capacity():
    clock = FakeClock()
    throttle = _throttle(clock)
    clock.advance(10_000)  # a very long idle period
    allowed = sum(
        1 for _ in range(10) if throttle.allow_attempt(source="1.2.3.4", username="alice")
    )
    assert allowed == 3  # capacity, not unbounded from the huge elapsed time


# -- reconnecting does not reset throttling (issue #3's core requirement) --


def test_reconnecting_does_not_reset_per_source_budget():
    """The whole point of cross-connection throttling: a fresh
    LoginThrottle isn't created per connection, so exhausting the
    budget on one simulated connection is still exhausted on the next
    one using the same throttle instance."""
    clock = FakeClock()
    throttle = _throttle(clock)
    for _ in range(3):
        assert throttle.allow_attempt(source="1.2.3.4", username="alice") is True
    # Simulate a reconnect: nothing about the throttle changes.
    assert throttle.allow_attempt(source="1.2.3.4", username="bob") is False


# -- independent budget layers ----------------------------------------------


def test_per_source_budget_is_independent_of_username():
    clock = FakeClock()
    throttle = _throttle(clock)
    for _ in range(3):
        throttle.allow_attempt(source="1.2.3.4", username="alice")
    # Same source, different username -- still throttled, because the
    # per-source budget (not just per-username) is exhausted.
    assert throttle.allow_attempt(source="1.2.3.4", username="mallory") is False


def test_per_username_budget_is_independent_of_source():
    clock = FakeClock()
    throttle = _throttle(clock)
    for _ in range(3):
        throttle.allow_attempt(source="1.2.3.4", username="alice")
    # Different source, same username -- still throttled by the
    # per-username budget even though this "attacker" has a fresh IP.
    assert throttle.allow_attempt(source="5.6.7.8", username="alice") is False


def test_different_source_and_username_both_get_fresh_budgets():
    clock = FakeClock()
    throttle = _throttle(clock)
    for _ in range(3):
        throttle.allow_attempt(source="1.2.3.4", username="alice")
    assert throttle.allow_attempt(source="5.6.7.8", username="bob") is True


def test_rejected_attempt_consumes_no_tokens_anywhere():
    """All-or-nothing consumption: two different users sharing one
    source (e.g. behind a NAT) shouldn't have user B's attempt fail
    because it happened to also decrement user A's already-healthy
    per-username budget."""
    clock = FakeClock()
    throttle = _throttle(clock, per_username_capacity=1.0)
    assert throttle.allow_attempt(source="1.2.3.4", username="alice") is True
    # alice's per-username budget is now empty; bob's own budget is untouched.
    assert throttle.allow_attempt(source="1.2.3.4", username="alice") is False
    assert throttle.allow_attempt(source="1.2.3.4", username="bob") is True


def test_global_budget_catches_rotating_source_and_username():
    """The backstop this module's docstring describes: an attacker who
    defeats per-source and per-username throttling by using a fresh
    key every single attempt is still capped by the global budget."""
    clock = FakeClock()
    throttle = _throttle(clock, global_capacity=5.0, global_refill_per_minute=0.0)
    allowed = sum(
        1
        for i in range(20)
        if throttle.allow_attempt(source=f"10.0.0.{i}", username=f"user-{i}")
    )
    assert allowed == 5


def test_username_is_case_and_whitespace_normalized():
    """Prevents trivially bypassing the per-username budget with
    'Alice' vs 'alice' vs ' alice ' against the same real account."""
    clock = FakeClock()
    throttle = _throttle(clock, per_username_capacity=1.0)
    assert throttle.allow_attempt(source="1.2.3.4", username="alice") is True
    assert throttle.allow_attempt(source="5.6.7.8", username=" Alice ") is False


def test_missing_source_address_still_gets_throttled():
    """A transport that genuinely has no peer-address concept
    (Session.peer_address is None) must not bypass throttling entirely
    -- it falls into one shared 'unknown' bucket instead."""
    clock = FakeClock()
    throttle = _throttle(clock)
    for _ in range(3):
        throttle.allow_attempt(source=None, username="alice")
    assert throttle.allow_attempt(source=None, username="mallory") is False


# -- bounded memory / arbitrary-key resistance (acceptance criterion) -------


def test_key_count_is_bounded_by_max_tracked_keys():
    clock = FakeClock()
    throttle = _throttle(clock, max_tracked_keys=10)
    for i in range(1000):
        throttle.allow_attempt(source=f"10.0.0.{i}", username=f"user-{i}")
    assert len(throttle._per_source._buckets) <= 10
    assert len(throttle._per_username._buckets) <= 10


# -- concurrent-unauthenticated-session budget -------------------------------


def test_unauthenticated_sessions_within_capacity_are_admitted():
    clock = FakeClock()
    throttle = _throttle(clock, max_concurrent_unauthenticated_sessions=2)
    assert throttle.try_enter_unauthenticated() is True
    assert throttle.try_enter_unauthenticated() is True


def test_unauthenticated_sessions_beyond_capacity_are_rejected():
    clock = FakeClock()
    throttle = _throttle(clock, max_concurrent_unauthenticated_sessions=2)
    throttle.try_enter_unauthenticated()
    throttle.try_enter_unauthenticated()
    assert throttle.try_enter_unauthenticated() is False


def test_leaving_unauthenticated_frees_a_slot():
    clock = FakeClock()
    throttle = _throttle(clock, max_concurrent_unauthenticated_sessions=1)
    assert throttle.try_enter_unauthenticated() is True
    assert throttle.try_enter_unauthenticated() is False
    throttle.leave_unauthenticated()
    assert throttle.try_enter_unauthenticated() is True


# -- LinkRequestThrottle (design doc §13.9, issue #60's third operational --
# -- slice) -------------------------------------------------------------


def test_link_throttle_allows_requests_within_capacity():
    clock = FakeClock()
    throttle = LinkRequestThrottle(capacity=3, refill_per_minute=60.0, max_tracked_sources=10, clock=clock)
    for _ in range(3):
        assert throttle.allow("198.51.100.7") is True


def test_link_throttle_rejects_once_exhausted():
    clock = FakeClock()
    throttle = LinkRequestThrottle(capacity=3, refill_per_minute=60.0, max_tracked_sources=10, clock=clock)
    for _ in range(3):
        throttle.allow("198.51.100.7")
    assert throttle.allow("198.51.100.7") is False


def test_link_throttle_budgets_are_independent_per_source():
    clock = FakeClock()
    throttle = LinkRequestThrottle(capacity=1, refill_per_minute=60.0, max_tracked_sources=10, clock=clock)
    assert throttle.allow("198.51.100.7") is True
    assert throttle.allow("198.51.100.7") is False
    assert throttle.allow("203.0.113.9") is True  # a different source has its own, untouched budget


def test_link_throttle_refills_over_time():
    clock = FakeClock()
    throttle = LinkRequestThrottle(capacity=1, refill_per_minute=60.0, max_tracked_sources=10, clock=clock)
    throttle.allow("198.51.100.7")
    assert throttle.allow("198.51.100.7") is False

    clock.advance(1.0)  # 1 token/sec refill rate
    assert throttle.allow("198.51.100.7") is True


def test_link_throttle_falls_back_to_unknown_for_a_missing_source():
    clock = FakeClock()
    throttle = LinkRequestThrottle(capacity=1, refill_per_minute=60.0, max_tracked_sources=10, clock=clock)
    assert throttle.allow(None) is True
    assert throttle.allow(None) is False

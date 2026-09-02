"""
Service-wide rate limiting for the managed-DNS service (design doc §16
Decision 3, issue #201 Phase 5) -- bounds how fast *new* registrations
can be created, service-wide, the one thing an attacker cannot multiply
by minting more identities (a node fingerprint and a registration
credential are both free for a remote client to mint, per Decision 2/3's
own reasoning -- a per-node/per-identity limit would just get bypassed
by generating more identities).

A small, standalone token bucket -- mirrors `netbbs.net.throttle.
_TokenBucket`'s own algorithm rather than importing that private class:
`services.managed_dns` is a separate deployable from the installable
`netbbs` node package (see this package's own `__init__.py` docstring),
the same "own implementation, not a cross-package reuse of another
package's private internals" reasoning `store.py` already applies for
its own `Database`/`Migration` pair rather than depending on `netbbs.
storage.database.Database`.
"""

from __future__ import annotations

from collections.abc import Callable

Clock = Callable[[], float]


class GlobalRateLimiter:
    """One bucket, `capacity` tokens, refilling continuously at
    `refill_per_minute / 60` tokens/second, never exceeding `capacity`.
    Deliberately not keyed by node/identity -- design doc §16 Decision 3
    is explicit this must be service-wide, since per-identity is exactly
    what a Sybil attacker can multiply for free."""

    def __init__(
        self, *, capacity: float, refill_per_minute: float, clock: Clock,
        tokens: float | None = None, last_refill: float | None = None,
    ) -> None:
        self._capacity = capacity
        self._refill_rate = refill_per_minute / 60.0
        self._clock = clock
        self._tokens = capacity if tokens is None else min(capacity, max(0.0, tokens))
        self._last_refill = clock() if last_refill is None else last_refill

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last_refill)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
        self._last_refill = now

    def allow(self) -> bool:
        """Non-blocking; consumes one token if available."""
        self._refill()
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True

    def snapshot(self) -> tuple[float, float]:
        return self._tokens, self._last_refill

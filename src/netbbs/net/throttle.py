"""
Cross-connection login throttling (design doc round 28, issue #3).

The three-attempt limit `netbbs.net.login_flow._login` enforces is
scoped to a single connection -- reconnecting resets it, so it does
nothing against an attacker willing to reconnect. `LoginThrottle` is
node-lifetime, shared state (constructed once in `netbbs.__main__`,
alongside `netbbs.chat.ChatHub`, and passed to every session) providing
three independent budgets that reconnecting does *not* reset:

- Per-source (peer address) -- limits one attacker/IP.
- Per-username -- limits guessing against one account regardless of
  which IP the guesses come from.
- Global -- a node-wide ceiling, the backstop against an attacker who
  defeats the other two by rotating both IP and username (e.g. a
  botnet trying many accounts) -- see this module's `LoginThrottle`
  docstring for why this layer matters even though the other two exist.

All three are token buckets, not hard lockouts -- "prefer progressive
delays/token buckets over long hard account lockouts, which can be
abused to deny service to known users" (issue #3's own recommended
direction). A bucket that's run dry simply refills over time; there is
no persistent "locked out" state a legitimate user could get stuck in
or an attacker could weaponize against someone else's account.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Callable

Clock = Callable[[], float]


class _TokenBucket:
    """A single token bucket: `capacity` tokens, refilling continuously
    at `refill_rate` tokens/second, never exceeding `capacity`."""

    __slots__ = ("_capacity", "_refill_rate", "_clock", "_tokens", "_last_refill")

    def __init__(self, capacity: float, refill_rate: float, clock: Clock):
        self._capacity = capacity
        self._refill_rate = refill_rate
        self._clock = clock
        self._tokens = capacity
        self._last_refill = clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last_refill)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
        self._last_refill = now

    def has_token(self) -> bool:
        self._refill()
        return self._tokens >= 1.0

    def consume(self) -> None:
        self._tokens -= 1.0


class _KeyedTokenBuckets:
    """
    One `_TokenBucket` per key (a source address or username), capped
    at `max_keys` distinct keys via LRU eviction.

    The cap is what keeps this "bounded in memory and cannot be abused
    with arbitrary usernames/IP keys" (issue #3's acceptance criteria):
    without it, an attacker could exhaust memory simply by presenting a
    fresh key (a new made-up username, or a spoofed/rotating source
    address) on every attempt. Eviction under attack means an old key's
    throttle state is forgotten and effectively resets -- an accepted,
    documented trade-off (see `LoginThrottle`'s docstring on why the
    global budget layer exists specifically to catch what per-key
    throttling alone cannot).
    """

    def __init__(self, capacity: float, refill_rate: float, max_keys: int, clock: Clock):
        self._capacity = capacity
        self._refill_rate = refill_rate
        self._max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[str, _TokenBucket] = OrderedDict()

    def _bucket_for(self, key: str) -> _TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _TokenBucket(self._capacity, self._refill_rate, self._clock)
        else:
            self._buckets.move_to_end(key)
        self._buckets[key] = bucket
        while len(self._buckets) > self._max_keys:
            self._buckets.popitem(last=False)
        return bucket

    def peek(self, key: str) -> bool:
        return self._bucket_for(key).has_token()

    def consume(self, key: str) -> None:
        self._bucket_for(key).consume()


class LoginThrottle:
    """
    Node-wide login throttling and unauthenticated-connection budget,
    constructed once and shared across every session regardless of
    transport (Telnet, web, and -- for the token-bucket checks only,
    see `netbbs.net.ssh`'s `validate_password` -- SSH).

    Two genuinely separate concerns live here, both node-lifetime state:

    1. `allow_attempt` -- may this *specific password verification* run
       at all? Checked, and its cost charged, *before* the expensive
       Argon2 work (`netbbs.auth.users.authenticate_password_async`)
       runs, not after -- the DoS half of issue #3 is exactly that
       Argon2 verification is expensive, so a rejected attempt must
       never still pay that cost.
    2. `try_enter_unauthenticated`/`leave_unauthenticated` -- how many
       *connections* may sit unauthenticated at once, regardless of how
       many login attempts each one makes. A slow client that never
       finishes logging in still occupies a connection slot even if it
       never calls `allow_attempt` at all, so this is tracked
       independently.
    """

    def __init__(
        self,
        *,
        per_source_capacity: float,
        per_source_refill_per_minute: float,
        per_username_capacity: float,
        per_username_refill_per_minute: float,
        global_capacity: float,
        global_refill_per_minute: float,
        max_tracked_keys: int,
        max_concurrent_unauthenticated_sessions: int,
        clock: Clock = time.monotonic,
    ):
        self._per_source = _KeyedTokenBuckets(
            per_source_capacity, per_source_refill_per_minute / 60.0, max_tracked_keys, clock
        )
        self._per_username = _KeyedTokenBuckets(
            per_username_capacity, per_username_refill_per_minute / 60.0, max_tracked_keys, clock
        )
        self._global = _TokenBucket(global_capacity, global_refill_per_minute / 60.0, clock)
        self._max_concurrent_unauthenticated_sessions = max_concurrent_unauthenticated_sessions
        self._unauthenticated_sessions = 0

    def allow_attempt(self, *, source: str | None, username: str) -> bool:
        """
        Non-blocking; consumes one token from each of the per-source,
        per-username, and global budgets if and only if all three
        currently have one available. All-or-nothing deliberately: a
        rejected attempt consumes nothing anywhere, so being throttled
        on one budget never also drains an unrelated one (e.g. two
        legitimate users sharing a NAT'd source address shouldn't have
        their *per-username* budgets affected by each other's traffic).
        """
        source_key = source or "unknown"
        username_key = username.strip().lower() or "unknown"
        if not (
            self._per_source.peek(source_key)
            and self._per_username.peek(username_key)
            and self._global.has_token()
        ):
            return False
        self._per_source.consume(source_key)
        self._per_username.consume(username_key)
        self._global.consume()
        return True

    def try_enter_unauthenticated(self) -> bool:
        """Reserve one of the concurrent-unauthenticated-session slots.
        Returns False (reserving nothing) if the node is already at its
        configured limit -- the caller should reject the connection
        immediately rather than accept it and let it sit."""
        if self._unauthenticated_sessions >= self._max_concurrent_unauthenticated_sessions:
            return False
        self._unauthenticated_sessions += 1
        return True

    def leave_unauthenticated(self) -> None:
        """Release a slot reserved by `try_enter_unauthenticated`. Must
        be called exactly once per successful reservation, regardless of
        whether the session went on to authenticate successfully, fail,
        or simply disconnect -- callers should do this in a `finally`."""
        self._unauthenticated_sessions -= 1


class LinkRequestThrottle:
    """
    Design doc §13.9 (issue #60's third operational slice): a per-
    source-address request-rate budget for `netbbs.link.transport.
    LinkServer` -- before this, no Link HTTP route had any throttling
    at all, including the two unauthenticated ones (`/hello`, `/peers`).

    Deliberately much simpler than `LoginThrottle` above: one budget,
    keyed by source address only -- there's no per-username-equivalent
    concept for machine-to-machine Link traffic, and no global backstop
    layer either (Link traffic is legitimately bursty in a way
    interactive login attempts aren't, and a single node-wide ceiling
    would let one noisy peer starve every other peer's budget, the
    opposite of what a *per-source* limit is for). Reuses `_KeyedToken
    Buckets` verbatim rather than reinventing it -- same bounded-memory-
    via-LRU-eviction reasoning as `LoginThrottle`'s own per-source/
    per-username budgets.
    """

    def __init__(self, *, capacity: float, refill_per_minute: float, max_tracked_sources: int, clock: Clock = time.monotonic):
        self._buckets = _KeyedTokenBuckets(capacity, refill_per_minute / 60.0, max_tracked_sources, clock)

    def allow(self, source: str | None) -> bool:
        """Non-blocking; consumes one token from `source`'s own budget
        if available. `source or "unknown"` matches `LoginThrottle.
        allow_attempt`'s own fallback for a transport that can't supply
        a real peer address."""
        key = source or "unknown"
        if not self._buckets.peek(key):
            return False
        self._buckets.consume(key)
        return True

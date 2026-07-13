"""
Integration tests for netbbs.net.login_flow's throttling wiring (design
doc round 28, issue #3) -- distinct from tests/test_login_outcomes.py
(pre-existing attempt-count/blocklist behavior) and
tests/test_throttle.py (LoginThrottle in isolation). These exercise
handle_session/_login actually consulting a LoginThrottle: the THROTTLED
and IDLE_TIMEOUT outcomes, the login-deadline wrapper, and the
concurrent-unauthenticated-session budget.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import AuthError, User
from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
from netbbs.net import login_flow
from netbbs.net.nodeconfig import ThrottleConfig
from netbbs.net.throttle import LoginThrottle


def _throttle_config(**overrides) -> ThrottleConfig:
    return ThrottleConfig(**overrides)


def _throttle(config: ThrottleConfig) -> LoginThrottle:
    return LoginThrottle(
        per_source_capacity=config.per_source_capacity,
        per_source_refill_per_minute=config.per_source_refill_per_minute,
        per_username_capacity=config.per_username_capacity,
        per_username_refill_per_minute=config.per_username_refill_per_minute,
        global_capacity=config.global_capacity,
        global_refill_per_minute=config.global_refill_per_minute,
        max_tracked_keys=config.max_tracked_keys,
        max_concurrent_unauthenticated_sessions=config.max_concurrent_unauthenticated_sessions,
    )


class FakeSession:
    def __init__(
        self,
        lines: list[str] | None = None,
        peer_address: str = "203.0.113.5",
        keys: list[str] | None = None,
    ):
        self._lines = iter(lines or [])
        self._keys = iter(keys or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.peer_address = peer_address

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True) -> str:
        return next(self._lines)

    async def read_key(self, echo: bool = True) -> str:
        try:
            return next(self._keys)
        except StopIteration:
            raise AssertionError("main menu must not be entered") from None

    @property
    def output(self) -> str:
        return "".join(self.written)


class HangingSession(FakeSession):
    """A session whose read_line never returns on its own -- used to
    exercise idle-timeout/login-deadline wrapping without a real
    socket, matching this project's convention of testing async
    timeout logic via a controlled fake rather than trusting reasoning
    about it (see design doc round 5's CSI-timeout lesson)."""

    def __init__(self, *, unblock_after: list[str] | None = None, **kwargs):
        super().__init__(**kwargs)
        self._unblock_after = list(unblock_after or [])

    async def read_line(self, echo: bool = True) -> str:
        if self._unblock_after:
            return self._unblock_after.pop(0)
        await asyncio.sleep(3600)
        raise AssertionError("should have been cancelled by a timeout first")


# -- THROTTLED outcome --------------------------------------------------------


def test_throttled_attempt_ends_login_with_throttled_message(monkeypatch):
    async def unexpected_auth(db, username, password):
        raise AssertionError("authenticate_password_async must not run once throttled")

    monkeypatch.setattr(login_flow, "authenticate_password_async", unexpected_auth)

    async def scenario() -> None:
        config = _throttle_config(per_source_capacity=0.0, per_source_refill_per_minute=0.0)
        throttle = _throttle(config)
        session = FakeSession(["alice", "whatever"])
        await login_flow.handle_session(session, object(), ChatHub(), PresenceRegistry(), MessageMailbox(), throttle, config)
        assert "Too many login attempts" in session.output

    asyncio.run(scenario())


# -- IDLE_TIMEOUT outcome ------------------------------------------------------


def test_idle_timeout_while_waiting_for_username(monkeypatch):
    async def scenario() -> None:
        config = _throttle_config(unauthenticated_idle_timeout_seconds=0.05)
        throttle = _throttle(config)
        session = HangingSession()
        await login_flow.handle_session(session, object(), ChatHub(), PresenceRegistry(), MessageMailbox(), throttle, config)
        assert "Timed out waiting for input" in session.output

    asyncio.run(scenario())


def test_idle_timeout_while_waiting_for_password(monkeypatch):
    async def scenario() -> None:
        config = _throttle_config(unauthenticated_idle_timeout_seconds=0.05)
        throttle = _throttle(config)
        # Username arrives fine; password prompt then hangs.
        session = HangingSession(unblock_after=["alice"])
        await login_flow.handle_session(session, object(), ChatHub(), PresenceRegistry(), MessageMailbox(), throttle, config)
        assert "Timed out waiting for input" in session.output

    asyncio.run(scenario())


def test_activity_resets_the_idle_timeout(monkeypatch):
    """A client that's slow but keeps responding within the idle
    window must not be treated as idle -- each read gets its own fresh
    timeout, not one shared budget across the whole exchange."""

    async def fake_auth(db, username, password):
        raise AuthError("login failed")

    monkeypatch.setattr(login_flow, "authenticate_password_async", fake_auth)

    async def scenario() -> None:
        config = _throttle_config(
            unauthenticated_idle_timeout_seconds=0.2, max_attempts_per_connection=1
        )
        throttle = _throttle(config)
        session = FakeSession(["alice", "wrong-password"])
        await login_flow.handle_session(session, object(), ChatHub(), PresenceRegistry(), MessageMailbox(), throttle, config)
        # Reached the normal "too many failed attempts" path, not idle
        # timeout, even though nothing here is instant.
        assert "Too many failed attempts" in session.output
        assert "Timed out waiting for input" not in session.output

    asyncio.run(scenario())


# -- overall login deadline ---------------------------------------------------


def test_login_deadline_exceeded_even_with_continuous_activity(monkeypatch):
    """Distinct from idle timeout: a client that keeps sending *something*
    (so no individual read ever times out) but takes too long overall
    must still be cut off by login_deadline_seconds."""

    async def fake_auth(db, username, password):
        raise AuthError("login failed")

    monkeypatch.setattr(login_flow, "authenticate_password_async", fake_auth)

    class SlowButActiveSession(FakeSession):
        def __init__(self):
            super().__init__()
            self._count = 0

        async def read_line(self, echo: bool = True) -> str:
            await asyncio.sleep(0.05)
            self._count += 1
            return f"value-{self._count}"

    async def scenario() -> None:
        config = _throttle_config(
            unauthenticated_idle_timeout_seconds=10.0,  # generous -- never the trigger here
            login_deadline_seconds=0.12,
            max_attempts_per_connection=1000,  # never the trigger via exhaustion either
        )
        throttle = _throttle(config)
        session = SlowButActiveSession()
        await login_flow.handle_session(session, object(), ChatHub(), PresenceRegistry(), MessageMailbox(), throttle, config)
        assert "Login timed out" in session.output

    asyncio.run(scenario())


# -- concurrent-unauthenticated-session budget --------------------------------


def test_session_rejected_when_unauthenticated_budget_is_exhausted():
    async def scenario() -> None:
        config = _throttle_config(max_concurrent_unauthenticated_sessions=0)
        throttle = _throttle(config)
        session = FakeSession()
        await login_flow.handle_session(session, object(), ChatHub(), PresenceRegistry(), MessageMailbox(), throttle, config)
        assert "too many pending logins" in session.output
        # Rejected before ever writing the welcome banner or prompting.
        assert "Username" not in session.output

    asyncio.run(scenario())


def test_unauthenticated_slot_is_released_after_login_completes(monkeypatch):
    """The budget must free up once a login attempt finishes (success,
    failure, or timeout) -- not stay held for the session's whole
    lifetime, which would make the cap trivially exhaustible by anyone
    who logs in and just stays connected."""

    user = User(
        id=1,
        username="alice",
        user_level=0,
        fingerprint=None,
        created_at="2026-01-01T00:00:00+00:00",
        last_login_at=None,
    )

    async def fake_auth(db, username, password):
        return user

    monkeypatch.setattr(login_flow, "authenticate_password_async", fake_auth)
    monkeypatch.setattr(login_flow, "is_blocked", lambda db, authenticated_user: False)

    async def scenario() -> None:
        config = _throttle_config(max_concurrent_unauthenticated_sessions=1)
        throttle = _throttle(config)

        first_session = FakeSession(["alice", "correct-password"], keys=["l"])
        await login_flow.handle_session(first_session, object(), ChatHub(), PresenceRegistry(), MessageMailbox(), throttle, config)
        assert "Welcome, alice" in first_session.output

        # The slot the first session held should be free again now.
        assert throttle.try_enter_unauthenticated() is True

    asyncio.run(scenario())

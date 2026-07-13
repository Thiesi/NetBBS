"""
Integration test for netbbs.net.login_flow.handle_session's
PresenceRegistry enter()/leave() hook (design doc round 32, sign-off
round 42) -- confirms the one place in the codebase that knows "this
account now has one more/one fewer live connection" actually calls
it, paired correctly around the authenticated portion of a session.
Library-level PresenceRegistry behavior is covered separately in
tests/test_chat_presence.py.
"""

from __future__ import annotations

import asyncio

from netbbs.auth.users import User
from netbbs.chat import ChatHub, PresenceRegistry
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
    def __init__(self, lines: list[str] | None = None, keys: list[str] | None = None):
        self._lines = iter(lines or [])
        self._keys = iter(keys or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True) -> str:
        return next(self._lines)

    async def read_key(self, echo: bool = True) -> str:
        return next(self._keys)

    @property
    def output(self) -> str:
        return "".join(self.written)


class _SpyPresence(PresenceRegistry):
    """Records enter()/leave() calls, otherwise behaves exactly like
    the real registry -- lets the test confirm the hook fires, not
    just that the end state happens to look right."""

    def __init__(self) -> None:
        super().__init__()
        self.entered: list[str] = []
        self.left: list[str] = []

    def enter(self, username: str) -> None:
        self.entered.append(username)
        super().enter(username)

    def leave(self, username: str) -> None:
        self.left.append(username)
        super().leave(username)


def test_handle_session_enters_and_leaves_presence_around_the_main_menu(monkeypatch):
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
        presence = _SpyPresence()
        session = FakeSession(["alice", "correct-password"], keys=["l"])  # "l" = logoff immediately
        config = _throttle_config()
        await login_flow.handle_session(session, object(), ChatHub(), presence, _throttle(config), config)

        assert presence.entered == ["alice"]
        assert presence.left == ["alice"]
        assert presence.is_online("alice") is False

    asyncio.run(scenario())


def test_presence_left_even_if_main_menu_raises(monkeypatch):
    """The leave() side of the hook is in a `finally`, so an
    exception during the authenticated portion must not leak an
    "online forever" session count."""
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

    async def broken_main_menu(session, db, hub, presence, user):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(login_flow, "authenticate_password_async", fake_auth)
    monkeypatch.setattr(login_flow, "is_blocked", lambda db, authenticated_user: False)
    monkeypatch.setattr(login_flow, "_main_menu", broken_main_menu)

    async def scenario() -> None:
        presence = _SpyPresence()
        session = FakeSession(["alice", "correct-password"])
        config = _throttle_config()
        try:
            await login_flow.handle_session(session, object(), ChatHub(), presence, _throttle(config), config)
        except RuntimeError:
            pass

        assert presence.entered == ["alice"]
        assert presence.left == ["alice"]
        assert presence.is_online("alice") is False

    asyncio.run(scenario())

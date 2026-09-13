"""
Integration test for netbbs.net.login_flow.handle_session's
PresenceRegistry enter()/leave() hook -- confirms the one place in
the codebase that knows "this
account now has one more/one fewer live connection" actually calls
it, paired correctly around the authenticated portion of a session.
Library-level PresenceRegistry behavior is covered separately in
tests/test_chat_presence.py.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
from netbbs.net import login_flow
from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.nodeconfig import ThrottleConfig
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.throttle import LoginThrottle
from netbbs.session_history import record_session_end, record_session_start
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


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
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"
        self.supports_truecolor = False

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True, **kwargs) -> str:
        return next(self._lines)

    async def read_key(self, echo: bool = True) -> str:
        return next(self._keys)

    async def read_any_key(self, echo: bool = True) -> str:
        return await self.read_key(echo=echo)

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


def test_handle_session_enters_and_leaves_presence_around_the_main_menu(db, monkeypatch):
    # A real DB-backed account, not a synthetic dataclass -- issue #100's
    # session_history recording now does a real FK-constrained insert
    # keyed on user.id, so a stand-in id with no matching row fails.
    user = create_user(db, "alice", password="hunter2", user_level=0)

    async def fake_auth(db, username, password):
        return user

    monkeypatch.setattr(login_flow, "authenticate_password_async", fake_auth)
    monkeypatch.setattr(login_flow, "is_blocked", lambda db, authenticated_user: False)

    async def scenario() -> None:
        presence = _SpyPresence()
        # "n" answers the one-time post-login Unicode-style prompt.
        session = FakeSession(["alice", "correct-password", "n", "y"], keys=["l"])  # "l" = logoff, confirmed
        config = _throttle_config()
        await login_flow.handle_session(session, db, ChatHub(), presence, MessageMailbox(), _throttle(config), config, ActiveSessionRegistry(), MaintenanceMode())

        assert presence.entered == ["alice"]
        assert presence.left == ["alice"]
        assert presence.is_online("alice") is False

    asyncio.run(scenario())


def test_previous_callers_screen_appears_after_login_and_before_main_menu(db, monkeypatch):
    bob = create_user(db, "bob", password="hunter2", user_level=0)
    prior_id = record_session_start(db, bob)
    record_session_end(db, prior_id)
    alice = create_user(db, "alice", password="hunter2", user_level=0)

    async def fake_auth(db, username, password):
        return alice

    monkeypatch.setattr(login_flow, "authenticate_password_async", fake_auth)
    monkeypatch.setattr(login_flow, "is_blocked", lambda db, authenticated_user: False)

    async def scenario() -> None:
        session = FakeSession(
            ["alice", "correct-password", "n", "y"],
            keys=[" ", "l"],
        )
        config = _throttle_config()
        await login_flow.handle_session(
            session,
            db,
            ChatHub(),
            PresenceRegistry(),
            MessageMailbox(),
            _throttle(config),
            config,
            ActiveSessionRegistry(),
            MaintenanceMode(),
        )
        output = re.sub(r"\x1b\[[0-9;]*m", "", session.output)
        assert output.index("P R E V I O U S") < output.index("Main menu")
        assert "bob" in output

    asyncio.run(scenario())


def test_presence_left_even_if_main_menu_raises(db, monkeypatch):
    """The leave() side of the hook is in a `finally`, so an
    exception during the authenticated portion must not leak an
    "online forever" session count."""
    # Real DB-backed account -- see the sibling test above for why.
    user = create_user(db, "alice", password="hunter2", user_level=0)

    async def fake_auth(db, username, password):
        return user

    async def broken_main_menu(
        session, db, hub, presence, mailbox, history, user, *,
        node_controls=None, lane=None, link_context=None, direct_invites=None,
    ):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(login_flow, "authenticate_password_async", fake_auth)
    monkeypatch.setattr(login_flow, "is_blocked", lambda db, authenticated_user: False)
    monkeypatch.setattr(login_flow, "_main_menu", broken_main_menu)

    async def scenario() -> None:
        presence = _SpyPresence()
        session = FakeSession(["alice", "correct-password"])
        config = _throttle_config()
        try:
            await login_flow.handle_session(session, db, ChatHub(), presence, MessageMailbox(), _throttle(config), config, ActiveSessionRegistry(), MaintenanceMode())
        except RuntimeError:
            pass

        assert presence.entered == ["alice"]
        assert presence.left == ["alice"]
        assert presence.is_online("alice") is False

    asyncio.run(scenario())

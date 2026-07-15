from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import AuthError, User, create_user, set_user_disabled
from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
from netbbs.net import login_flow
from netbbs.net.maintenance import MaintenanceMode
from netbbs.net.nodeconfig import ThrottleConfig
from netbbs.net.session_registry import ActiveSessionRegistry
from netbbs.net.throttle import LoginThrottle
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def _throttle_config(**overrides) -> ThrottleConfig:
    return ThrottleConfig(**overrides)


def _throttle(config: ThrottleConfig | None = None) -> LoginThrottle:
    config = config or _throttle_config()
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
    def __init__(self, lines: list[str]):
        self._lines = iter(lines)
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
        raise AssertionError("main menu must not be entered")

    @property
    def output(self) -> str:
        return "".join(self.written)


def test_blocked_user_does_not_receive_failed_attempt_message(db, monkeypatch):
    user = User(
        id=1,
        username="blocked",
        user_level=0,
        fingerprint=None,
        created_at="2026-01-01T00:00:00+00:00",
        last_login_at=None,
    )

    async def authenticate(db, username, password):
        return user

    monkeypatch.setattr(login_flow, "authenticate_password_async", authenticate)
    monkeypatch.setattr(login_flow, "is_blocked", lambda db, authenticated_user: True)

    async def scenario() -> None:
        session = FakeSession(["blocked", "correct-password"])
        await login_flow.handle_session(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), _throttle(), _throttle_config(), ActiveSessionRegistry(), MaintenanceMode())
        assert "Your access to this system has been revoked." in session.output
        assert "Too many failed attempts" not in session.output
        assert "Welcome, blocked" not in session.output

    asyncio.run(scenario())


def test_exhausted_attempts_receive_failed_attempt_message(db, monkeypatch):
    async def reject(db, username, password):
        raise AuthError("login failed")

    monkeypatch.setattr(login_flow, "authenticate_password_async", reject)

    async def scenario() -> None:
        session = FakeSession(
            [
                "unknown-1",
                "wrong-1",
                "unknown-2",
                "wrong-2",
                "unknown-3",
                "wrong-3",
            ]
        )
        await login_flow.handle_session(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), _throttle(), _throttle_config(), ActiveSessionRegistry(), MaintenanceMode())
        assert "Too many failed attempts. Goodbye." in session.output
        assert "Your access to this system has been revoked." not in session.output

    asyncio.run(scenario())


# -- GitHub issue #25: SSH's two-stage login split --------------------------


class _SSHFakeSession(FakeSession):
    """Same shape as `FakeSession`, but with a working `read_key()`
    fed from its own scripted list -- `handle_ssh_session` skips
    `_login()` entirely and goes straight to the main menu, which
    calls `read_key()` immediately (unlike the plain `FakeSession`
    above, built for tests that must never reach it)."""

    def __init__(self, keys: list[str]):
        super().__init__([])
        self._keys = iter(keys)
        self.authenticated_username: str | None = None

    async def read_key(self, echo: bool = True) -> str:
        return next(self._keys)


def test_ssh_session_skips_login_and_reaches_main_menu_directly(db):
    """The core regression: handle_ssh_session must never call
    _login() -- proven here by a session whose read_line() would raise
    if ever invoked (it's still the inherited FakeSession.read_line,
    with an empty scripted-lines list -- calling it raises
    StopIteration, not silently succeeding)."""
    create_user(db, "alice", password="hunter2", user_level=10)
    session = _SSHFakeSession(["l"])  # logoff immediately
    session.authenticated_username = "alice"

    asyncio.run(
        login_flow.handle_ssh_session(
            session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), ActiveSessionRegistry(), MaintenanceMode()
        )
    )

    assert "Welcome, alice" in session.output
    assert "Username:" not in session.output
    assert "Password:" not in session.output


def test_ssh_session_with_no_authenticated_username_refuses_cleanly(db):
    """Unreachable via a real asyncssh connection (see
    handle_ssh_session's docstring), but must still fail safe rather
    than crash or silently proceed as an anonymous session."""
    session = _SSHFakeSession([])  # read_key must never be reached

    asyncio.run(
        login_flow.handle_ssh_session(
            session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), ActiveSessionRegistry(), MaintenanceMode()
        )
    )

    assert "did not complete" in session.output


def test_ssh_authorize_rejects_a_disabled_account(db):
    """The re-validation piece (GitHub issue #25): even though
    authenticate_password_async/authorize_public_key already checked
    disabled_at at handshake time, _authorize_ssh_authenticated_user
    re-checks fresh immediately before the session actually begins --
    closing the gap between the SSH handshake completing and the
    application session actually starting."""
    sysop = create_user(db, "sysop", password="hunter2", user_level=100)
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    set_user_disabled(db, alice, True, changed_by=sysop)
    session = _SSHFakeSession([])  # read_key must never be reached

    result = asyncio.run(login_flow._authorize_ssh_authenticated_user(session, db, "alice"))

    assert result is login_flow.LoginOutcome.BLOCKED
    assert "no longer available" in session.output


def test_ssh_authorize_rejects_a_deleted_account(db):
    session = _SSHFakeSession([])

    result = asyncio.run(login_flow._authorize_ssh_authenticated_user(session, db, "nosuchuser"))

    assert result is login_flow.LoginOutcome.BLOCKED
    assert "no longer available" in session.output


def test_ssh_authorize_rejects_a_blocklisted_account(db, monkeypatch):
    create_user(db, "alice", password="hunter2", user_level=10)
    monkeypatch.setattr(login_flow, "is_blocked", lambda db, authenticated_user: True)
    session = _SSHFakeSession([])

    result = asyncio.run(login_flow._authorize_ssh_authenticated_user(session, db, "alice"))

    assert result is login_flow.LoginOutcome.BLOCKED
    assert "Your access to this system has been revoked." in session.output


def test_ssh_authorize_returns_the_user_for_a_valid_account(db):
    alice = create_user(db, "alice", password="hunter2", user_level=10)
    session = _SSHFakeSession([])

    result = asyncio.run(login_flow._authorize_ssh_authenticated_user(session, db, "alice"))

    assert result.id == alice.id
    assert result.username == "alice"


def test_ssh_session_respects_maintenance_mode(db):
    session = _SSHFakeSession([])  # read_key must never be reached
    session.authenticated_username = "alice"
    maintenance = MaintenanceMode()
    maintenance.activate()

    asyncio.run(
        login_flow.handle_ssh_session(
            session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), ActiveSessionRegistry(), maintenance
        )
    )

    assert "Welcome, alice" not in session.output

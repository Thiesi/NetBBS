"""
Tests for self-service account registration:

- `netbbs.auth.users`: the reserved `new` sentinel, `pending_approval`
  gating login, and `approve_pending_user`.
- `netbbs.config`: the node-wide `registration_mode` setting --
  open/approval_required/closed.
- `netbbs.net.login_flow`'s Telnet/web registration entry point
  (`_register_new_account`, triggered from `_login` by typing `new` at
  the username prompt), driven end to end via `handle_session` with a
  scripted `FakeSession` -- the same pattern
  tests/test_login_throttling.py already uses for `_login`.

SSH's keyboard-interactive registration path
(`netbbs.net.ssh._NetBBSSSHServer`) is covered separately in
tests/test_ssh_registration.py, which needs a real asyncssh client/
server pair rather than a FakeSession.
"""

from __future__ import annotations

import asyncio

import nacl.signing
import pytest

from netbbs.auth.users import (
    MIN_REGISTRATION_PASSWORD_LENGTH,
    AuthError,
    approve_pending_user,
    authenticate_password_async,
    authorize_public_key,
    create_user,
    get_user_by_username,
)
from netbbs.chat import ChatHub, MessageMailbox, PresenceRegistry
from netbbs.config import RegistrationMode, get_registration_mode, set_registration_mode
from netbbs.moderation.log import list_actions_for_target_user
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


class FakeSession:
    def __init__(self, lines: list[str] | None = None, peer_address: str = "203.0.113.5", keys=None):
        self._lines = iter(lines or [])
        self._keys = iter(keys or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.terminal_height = 24
        self.peer_address = peer_address
        self.supports_truecolor = False

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True, **kwargs) -> str:
        return next(self._lines)

    async def read_key(self, echo: bool = True) -> str:
        try:
            return next(self._keys)
        except StopIteration:
            raise AssertionError("main menu must not be entered") from None

    @property
    def output(self) -> str:
        return "".join(self.written)


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


async def _run_login(session, db, config=None) -> None:
    config = config or _throttle_config()
    throttle = _throttle(config)
    await login_flow.handle_session(
        session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), throttle, config,
        ActiveSessionRegistry(), MaintenanceMode(),
    )


# -- netbbs.auth.users: reserved sentinel, pending_approval gating ----------


def test_create_user_rejects_reserved_sentinel(db):
    for candidate in ("new", "New", "NEW"):
        with pytest.raises(AuthError, match="reserved"):
            create_user(db, candidate, password="hunter2")


def test_pending_approval_defaults_false(db):
    user = create_user(db, "alice", password="hunter2")
    assert user.pending_approval is False


def test_create_user_with_pending_approval_true(db):
    user = create_user(db, "alice", password="hunter2", pending_approval=True)
    assert user.pending_approval is True


def test_password_login_fails_generically_while_pending_approval(db):
    create_user(db, "alice", password="hunter2", pending_approval=True)

    async def scenario():
        with pytest.raises(AuthError, match="login failed"):
            await authenticate_password_async(db, "alice", "hunter2")

    asyncio.run(scenario())


def test_authorize_public_key_fails_while_pending_approval(db):
    key = nacl.signing.SigningKey.generate()
    verify_key = key.verify_key
    create_user(db, "bob", verify_key=verify_key, pending_approval=True)
    with pytest.raises(AuthError, match="login failed"):
        authorize_public_key(db, "bob", verify_key)


def test_approve_pending_user_clears_the_gate_and_allows_login(db):
    sysop = create_user(db, "sysop", password="hunter2", user_level=255)
    alice = create_user(db, "alice", password="hunter2", pending_approval=True)

    updated = approve_pending_user(db, alice, approved_by=sysop)
    assert updated.pending_approval is False

    async def scenario():
        user = await authenticate_password_async(db, "alice", "hunter2")
        assert user.username == "alice"

    asyncio.run(scenario())

    entries = list_actions_for_target_user(db, alice.id)
    assert any(entry.action == "approve_registration" for entry in entries)


def test_approve_pending_user_is_a_no_op_when_not_pending(db):
    sysop = create_user(db, "sysop", password="hunter2", user_level=255)
    alice = create_user(db, "alice", password="hunter2")

    approve_pending_user(db, alice, approved_by=sysop)

    assert list_actions_for_target_user(db, alice.id) == []


# -- netbbs.config: registration_mode ----------------------------------------


def test_registration_mode_defaults_open(db):
    assert get_registration_mode(db) is RegistrationMode.OPEN


def test_registration_mode_round_trips(db):
    for mode in RegistrationMode:
        set_registration_mode(db, mode)
        assert get_registration_mode(db) is mode


def test_registration_mode_falls_back_to_legacy_boolean_key(db):
    # A legacy database that only ever wrote the old boolean key
    # (never migrated) must still resolve correctly -- no explicit
    # migration step is needed; falling back to the legacy key is
    # itself the compatibility path.
    from netbbs.config import set_config

    set_config(db, "require_registration_approval", "1")
    assert get_registration_mode(db) is RegistrationMode.APPROVAL_REQUIRED

    set_config(db, "require_registration_approval", "0")
    assert get_registration_mode(db) is RegistrationMode.OPEN


def test_registration_mode_new_key_takes_precedence_over_legacy(db):
    from netbbs.config import set_config

    set_config(db, "require_registration_approval", "1")
    set_registration_mode(db, RegistrationMode.OPEN)
    assert get_registration_mode(db) is RegistrationMode.OPEN


# -- Telnet/web interactive registration (_login -> _register_new_account) --


def test_typing_new_registers_and_logs_straight_in_when_mode_is_open(db):
    # "n" answers the one-time post-login Unicode-style prompt.
    session = FakeSession(["new", "alice", "hunter2pw", "hunter2pw", "n", "y"], keys=["l"])

    asyncio.run(_run_login(session, db))

    assert "Welcome, alice" in session.output
    assert get_user_by_username(db, "alice").pending_approval is False


def test_registration_with_approval_required_does_not_log_in(db):
    set_registration_mode(db, RegistrationMode.APPROVAL_REQUIRED)
    # First attempt registers (consumed); the remaining scripted lines
    # let the outer _login loop exhaust its attempts cleanly rather than
    # raising StopIteration once registration correctly declines to log
    # the new account straight in.
    session = FakeSession(
        ["new", "alice", "hunter2pw", "hunter2pw", "", "", "", ""],
    )

    asyncio.run(_run_login(session, db, _throttle_config(max_attempts_per_connection=2)))

    assert "Welcome, alice" not in session.output
    assert "must be approved" in session.output.lower() or "must approve" in session.output.lower()
    created = get_user_by_username(db, "alice")
    assert created.pending_approval is True


def test_closed_mode_hides_the_new_account_prompt_option(db):
    set_registration_mode(db, RegistrationMode.CLOSED)
    session = FakeSession(["", "", "", ""])

    asyncio.run(_run_login(session, db, _throttle_config(max_attempts_per_connection=1)))

    assert "'new'" not in session.output
    assert "create an account" not in session.output.lower()


def test_closed_mode_rejects_typing_new_with_a_clear_message_and_no_account(db):
    set_registration_mode(db, RegistrationMode.CLOSED)
    session = FakeSession(["new", "", ""])

    asyncio.run(_run_login(session, db, _throttle_config(max_attempts_per_connection=1)))

    assert "does not accept public registrations" in session.output
    with pytest.raises(AuthError):
        get_user_by_username(db, "alice")


def test_registration_rejects_a_too_short_password(db):
    session = FakeSession(
        ["new", "alice", "short", "short", "", "", "", ""],
    )

    asyncio.run(_run_login(session, db, _throttle_config(max_attempts_per_connection=2)))

    assert f"at least {MIN_REGISTRATION_PASSWORD_LENGTH} characters" in session.output
    with pytest.raises(AuthError):
        get_user_by_username(db, "alice")


def test_registration_rejects_mismatched_passwords(db):
    session = FakeSession(
        ["new", "alice", "hunter2pw", "different-pw", "", "", "", ""],
    )

    asyncio.run(_run_login(session, db, _throttle_config(max_attempts_per_connection=2)))

    assert "did not match" in session.output
    with pytest.raises(AuthError):
        get_user_by_username(db, "alice")


def test_registration_cancels_on_blank_username(db):
    session = FakeSession(["new", "", "", "", "", ""])

    asyncio.run(_run_login(session, db, _throttle_config(max_attempts_per_connection=2)))

    assert "Welcome," not in session.output


def test_registration_refuses_the_reserved_sentinel_as_a_desired_username(db):
    session = FakeSession(
        ["new", "new", "hunter2pw", "hunter2pw", "", "", "", ""],
    )

    asyncio.run(_run_login(session, db, _throttle_config(max_attempts_per_connection=2)))

    assert "reserved" in session.output
    with pytest.raises(AuthError):
        get_user_by_username(db, "new")


def test_registration_refuses_a_username_already_taken(db):
    create_user(db, "alice", password="hunter2")
    session = FakeSession(
        ["new", "alice", "hunter2pw", "hunter2pw", "", "", "", ""],
    )

    asyncio.run(_run_login(session, db, _throttle_config(max_attempts_per_connection=2)))

    assert "already in use" in session.output


# -- dogfood follow-up to issue #156: retry signup in place instead of --
# -- dropping straight back to login on the first fixable mistake -------


def test_registration_shows_an_attempt_counter_from_the_first_screen(db):
    session = FakeSession(["new", "", ""])

    asyncio.run(_run_login(session, db, _throttle_config(max_attempts_per_connection=1)))

    assert f"Attempt 1 of {login_flow._REGISTRATION_MAX_ATTEMPTS}" in session.output


def test_registration_retries_in_place_after_a_too_short_password_and_can_still_succeed(db):
    # Attempt 1: too-short password. Attempt 2: succeeds outright.
    # "n" answers the one-time post-login Unicode-style prompt.
    session = FakeSession(
        ["new", "alice", "short", "short", "alice", "hunter2pw", "hunter2pw", "n", "y"], keys=["l"],
    )

    asyncio.run(_run_login(session, db))

    assert f"at least {MIN_REGISTRATION_PASSWORD_LENGTH} characters" in session.output
    assert "Let's try again" in session.output
    assert f"Attempt 2 of {login_flow._REGISTRATION_MAX_ATTEMPTS}" in session.output
    assert "Welcome, alice" in session.output
    assert get_user_by_username(db, "alice").pending_approval is False


def test_registration_retries_in_place_after_a_username_already_taken(db):
    create_user(db, "bob", password="hunter2")
    # Attempt 1: "bob" is taken. Attempt 2: "alice" succeeds.
    # "n" answers the one-time post-login Unicode-style prompt.
    session = FakeSession(
        ["new", "bob", "hunter2pw", "hunter2pw", "alice", "hunter2pw", "hunter2pw", "n", "y"], keys=["l"],
    )

    asyncio.run(_run_login(session, db))

    assert "already in use" in session.output
    assert "Let's try again" in session.output
    assert "Welcome, alice" in session.output


def test_registration_exhausts_all_attempts_then_returns_to_the_normal_login_prompt(db):
    max_attempts = login_flow._REGISTRATION_MAX_ATTEMPTS
    # Every attempt mismatches -- exhausts all `max_attempts` signup
    # tries, then _login's own outer attempt is spent too, so this
    # connection is left with none and closes.
    lines = ["new"]
    for _ in range(max_attempts):
        lines += ["alice", "hunter2pw", "different-pw"]
    session = FakeSession(lines)

    asyncio.run(_run_login(session, db, _throttle_config(max_attempts_per_connection=1)))

    assert session.output.count("did not match") == max_attempts
    # "Let's try again" only precedes a retry -- shown after attempts 1
    # and 2, not after the final (3rd) failure, which instead gets the
    # issue #156 "returning to login" wording exactly once.
    assert session.output.count("Let's try again") == max_attempts - 1
    assert session.output.count("Returning you to the login prompt") == 1
    with pytest.raises(AuthError):
        get_user_by_username(db, "alice")


def test_registration_blank_username_cancels_immediately_even_mid_retry_sequence(db):
    # Attempt 1 fails (too short); the caller then abandons signup with a
    # blank username on attempt 2 rather than continuing to attempt 3 --
    # a blank username is an explicit cancel regardless of which attempt
    # this is, not a fourth fixable mistake to keep retrying.
    session = FakeSession(["new", "alice", "short", "short", "", "", ""])

    asyncio.run(_run_login(session, db, _throttle_config(max_attempts_per_connection=2)))

    assert f"Attempt 3 of {login_flow._REGISTRATION_MAX_ATTEMPTS}" not in session.output
    with pytest.raises(AuthError):
        get_user_by_username(db, "alice")


def test_registration_is_throttled_by_the_shared_login_throttle(db):
    config = _throttle_config(
        per_source_capacity=0.0, per_source_refill_per_minute=0.0, max_attempts_per_connection=1
    )
    session = FakeSession(["new", "alice", "hunter2pw", "hunter2pw"])

    asyncio.run(_run_login(session, db, config))

    assert "Too many registration attempts" in session.output or "Too many login attempts" in session.output
    with pytest.raises(AuthError):
        get_user_by_username(db, "alice")


def test_registration_username_prompt_is_case_insensitive_for_the_sentinel(db):
    # "n" answers the one-time post-login Unicode-style prompt.
    session = FakeSession(["NEW", "alice", "hunter2pw", "hunter2pw", "n", "y"], keys=["l"])

    asyncio.run(_run_login(session, db))

    assert "Welcome, alice" in session.output


# -- GitHub issue #177: new-account (before/after) banners -----------------


def test_before_banner_shown_once_at_entry_not_on_every_retry(db):
    from netbbs.net.new_account_banner_before import (
        new_account_banner_before_path,
        set_new_account_banner_before_enabled,
    )

    new_account_banner_before_path(db).write_bytes(b"SIGN UP BELOW")
    set_new_account_banner_before_enabled(db, True)
    # Attempt 1: too-short password (fixable, retries in place).
    # Attempt 2: succeeds. "n" answers the Unicode-style prompt.
    session = FakeSession(
        ["new", "alice", "short", "short", "alice", "hunter2pw", "hunter2pw", "n", "y"], keys=["l"],
    )

    asyncio.run(_run_login(session, db))

    assert session.output.count("SIGN UP BELOW") == 1
    assert session.output.index("SIGN UP BELOW") < session.output.index("Create account")


def test_after_banner_shown_on_immediate_success(db):
    from netbbs.net.new_account_banner_after import (
        new_account_banner_after_path,
        set_new_account_banner_after_enabled,
    )

    new_account_banner_after_path(db).write_bytes(b"YOU ARE IN")
    set_new_account_banner_after_enabled(db, True)
    session = FakeSession(["new", "alice", "hunter2pw", "hunter2pw", "n", "y"], keys=["l"])

    asyncio.run(_run_login(session, db))

    assert "YOU ARE IN" in session.output
    assert session.output.index("YOU ARE IN") < session.output.index("Welcome, alice")


def test_after_banner_shown_when_approval_is_required(db):
    from netbbs.net.new_account_banner_after import (
        new_account_banner_after_path,
        set_new_account_banner_after_enabled,
    )

    set_registration_mode(db, RegistrationMode.APPROVAL_REQUIRED)
    new_account_banner_after_path(db).write_bytes(b"YOU ARE IN")
    set_new_account_banner_after_enabled(db, True)
    session = FakeSession(["new", "alice", "hunter2pw", "hunter2pw", "", "", "", ""])

    asyncio.run(_run_login(session, db, _throttle_config(max_attempts_per_connection=2)))

    assert "YOU ARE IN" in session.output


def test_after_banner_not_shown_on_a_fixable_validation_failure(db):
    from netbbs.net.new_account_banner_after import (
        new_account_banner_after_path,
        set_new_account_banner_after_enabled,
    )

    new_account_banner_after_path(db).write_bytes(b"YOU ARE IN")
    set_new_account_banner_after_enabled(db, True)
    session = FakeSession(["new", "alice", "short", "short", "", "", ""])

    asyncio.run(_run_login(session, db, _throttle_config(max_attempts_per_connection=2)))

    assert "YOU ARE IN" not in session.output


def test_disabled_new_account_banners_leave_registration_byte_for_byte_unchanged(db):
    # "n" answers the one-time post-login Unicode-style prompt.
    session = FakeSession(["new", "alice", "hunter2pw", "hunter2pw", "n", "y"], keys=["l"])

    asyncio.run(_run_login(session, db))

    assert "Welcome, alice" in session.output

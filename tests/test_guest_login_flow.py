"""Guest login through the real login prompt (issue #531).

The unit-level behaviour of the designation lives in
tests/test_guest_login.py. These drive `login_flow._login` itself,
because the claim worth proving is about what the login path does *not*
skip: guest login removes the password prompt and nothing else.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user, set_user_disabled
from netbbs.guest import set_guest_user, set_pre_login_notice
from netbbs.moderation.blocklist import block_user
from netbbs.net import login_flow
from netbbs.storage.database import Database
from tests.test_login_outcomes import FakeSession, _throttle


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def _login(db, lines):
    session = FakeSession(lines)
    result = asyncio.run(
        login_flow._login(session, db, _throttle(), idle_timeout=5.0)
    )
    return result, session


def test_the_guest_name_signs_in_without_a_password(db):
    """The whole feature: one line of input, no password prompt."""
    guest = create_user(db, "guest", password="hunter2", user_level=1)
    set_guest_user(db, guest)

    result, session = _login(db, ["guest"])

    assert getattr(result, "id", None) == guest.id
    assert "Password:" not in "".join(session.written)


def test_any_other_account_still_needs_its_password(db):
    guest = create_user(db, "guest", password="hunter2", user_level=1)
    alice = create_user(db, "alice", password="correct", user_level=10)
    set_guest_user(db, guest)

    result, session = _login(db, ["alice", "correct"])

    assert getattr(result, "id", None) == alice.id
    assert "Password:" in "".join(session.written)


def test_the_guest_account_can_still_sign_in_normally(db):
    """Guest login skips the password; it does not remove it."""
    guest = create_user(db, "guest", password="hunter2", user_level=1)
    set_guest_user(db, guest)
    # Designation off: the same account, the ordinary path.
    set_guest_user(db, None)

    result, session = _login(db, ["guest", "hunter2"])

    assert getattr(result, "id", None) == guest.id
    assert "Password:" in "".join(session.written)


# -- What guest login does not skip -----------------------------------


def test_a_blocked_guest_is_still_refused(db):
    """The authorization check after authentication still runs. Guest
    login is a shortcut past the password, not past the blocklist."""
    guest = create_user(db, "guest", password="hunter2", user_level=1)
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    block_user(db, guest, blocked_by=sysop, reason="spam")
    set_guest_user(db, guest)

    result, session = _login(db, ["guest"])

    assert result is login_flow.LoginOutcome.BLOCKED
    assert "revoked" in "".join(session.written)


def test_a_disabled_guest_account_gets_no_passwordless_login(db):
    """`get_user_by_username` filters no account status, so the guest
    path has to apply this gate itself -- the password path applies its
    own. A disabled designation stops being special and falls through
    to the ordinary prompt rather than handing back a session."""
    guest = create_user(db, "guest", password="hunter2", user_level=1)
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    set_user_disabled(db, guest, disabled=True, changed_by=sysop)
    set_guest_user(db, guest)

    _, session = _login(db, ["guest", "hunter2", "guest", "hunter2", "guest", "hunter2"])

    assert "Password:" in "".join(session.written)


def test_a_guest_promoted_to_sysop_gets_no_passwordless_login(db):
    """Checking the level only when the designation is saved left a
    passwordless privilege-escalation path: designate an ordinary
    account, then promote it through the user-detail level action."""
    from netbbs.auth.users import set_user_level

    guest = create_user(db, "guest", password="hunter2", user_level=1)
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    set_guest_user(db, guest)
    set_user_level(db, guest, SYSOP_LEVEL, changed_by=sysop)

    _, session = _login(db, ["guest", "hunter2", "guest", "hunter2", "guest", "hunter2"])

    assert "Password:" in "".join(session.written)


def test_a_guest_session_updates_last_login(db):
    """Every other authentication path records this on its way out; a
    guest account signing in daily should not look like one that never
    has."""
    from netbbs.auth.users import get_user_by_id

    guest = create_user(db, "guest", password="hunter2", user_level=1)
    assert guest.last_login_at is None
    set_guest_user(db, guest)

    result, _ = _login(db, ["guest"])

    assert result.last_login_at is not None
    assert get_user_by_id(db, guest.id).last_login_at is not None


def test_a_deleted_guest_account_falls_back_to_the_password_prompt(db):
    """Fails closed: the name stops being special rather than matching
    something unintended."""
    from netbbs.auth.users import delete_user

    guest = create_user(db, "guest", password="hunter2", user_level=1)
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    set_guest_user(db, guest)
    delete_user(db, guest, deleted_by=sysop)

    result, session = _login(db, ["guest", "hunter2", "guest", "hunter2", "guest", "hunter2"])

    assert result is login_flow.LoginOutcome.ATTEMPTS_EXHAUSTED
    assert "Password:" in "".join(session.written)


# -- The pre-login notice ---------------------------------------------


def test_the_notice_is_shown_before_the_username_prompt(db):
    create_user(db, "alice", password="correct", user_level=10)
    set_pre_login_notice(db, "Here for NetBBS? Sign in as 'guest' to download.")

    _, session = _login(db, ["alice", "correct"])

    text = "".join(session.written)
    assert "Sign in as 'guest' to download." in text
    assert text.index("download.") < text.index("Username:")


def test_no_notice_means_nothing_extra_is_written(db):
    create_user(db, "alice", password="correct", user_level=10)

    _, session = _login(db, ["alice", "correct"])

    assert "Username:" in "".join(session.written)


def test_a_guest_session_cannot_manage_ssh_keys(db):
    """Codex review, twice. The guest is an ordinary account, and that
    is the design -- but "may manage this account's credentials" is a
    question about how the *session* got in, not about the account.

    The first fix guarded adding alone, which is the obvious harm: an
    anonymous caller minting a key that keeps working over SSH after a
    SysOp switches guest access off. `[R]emove a key` was still right
    there, though, and removing the primary key changes the fingerprint
    the account's Link events are authored under. So the guard belongs
    to the screen, and this test drives the screen rather than either
    action -- the version of it that called `_add_key` directly would
    have passed against the code that still allowed removal.
    """
    import netbbs.net.ssh_key_screen as keys

    guest = create_user(db, "guest", password="hunter2", user_level=1)
    set_guest_user(db, guest)

    _, session = _login(db, ["guest"])
    assert getattr(session, "authenticated_without_credential", False) is True

    session.written.clear()
    result = asyncio.run(keys.manage_ssh_keys_screen(session, None, guest, changed_by=guest))

    # `FakeSession.read_key` raises, so reaching the screen's own menu
    # at all would fail this rather than quietly picking `[B]ack`.
    assert "without a password" in "".join(session.written)
    assert result is guest


def test_an_ordinary_session_is_not_marked(db):
    """The flag describes this session, not the account -- signing in
    with the password reaches key management exactly as before."""
    create_user(db, "alice", password="correct", user_level=10)

    _, session = _login(db, ["alice", "correct"])

    assert getattr(session, "authenticated_without_credential", False) is False


# -- The window between the last check and the returned row ------------
#
# `touch_last_login` re-reads the account after the login path's final
# await, so the row it hands back is not the row that was checked.
# Patching it is how these reach that window: the wrapper stands in for
# a SysOp acting while the caller's terminal write was in flight.


def test_a_block_landing_during_the_final_await_is_caught(db):
    """The pre-await `is_blocked` check has already passed by then, and
    the session-revocation watcher would not have caught it afterwards
    either -- `account_still_active` reads account status, not the
    blocklist."""
    guest = create_user(db, "guest", password="hunter2", user_level=1)
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    set_guest_user(db, guest)

    real = login_flow.touch_last_login

    def blocking(database, user):
        block_user(database, user, blocked_by=sysop, reason="during login")
        return real(database, user)

    login_flow.touch_last_login = blocking
    try:
        result, session = _login(db, ["guest"])
    finally:
        login_flow.touch_last_login = real

    assert result is login_flow.LoginOutcome.BLOCKED
    assert "revoked" in "".join(session.written)


def test_a_promotion_landing_during_the_final_await_is_caught(db):
    """The one this window was found through: the refreshed row came
    back at level 255 and ran the session as a SysOp."""
    from netbbs.auth.users import set_user_level

    guest = create_user(db, "guest", password="hunter2", user_level=1)
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    set_guest_user(db, guest)

    real = login_flow.touch_last_login

    def promoting(database, user):
        set_user_level(database, user, SYSOP_LEVEL, changed_by=sysop)
        return real(database, user)

    login_flow.touch_last_login = promoting
    try:
        result, session = _login(db, ["guest", "hunter2", "guest", "hunter2", "guest", "hunter2"])
    finally:
        login_flow.touch_last_login = real

    assert getattr(result, "user_level", 0) != SYSOP_LEVEL
    assert "Password:" in "".join(session.written)


def test_the_account_vanishing_during_the_final_await_is_a_refusal(db):
    """Not a crash: the re-read returned no row and the login task
    subscripted it, dropping the caller's session with a `TypeError`
    instead of the refusal this path already knows how to say."""
    from netbbs.auth.users import delete_user

    guest = create_user(db, "guest", password="hunter2", user_level=1)
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    set_guest_user(db, guest)

    real = login_flow.touch_last_login

    def deleting(database, user):
        delete_user(database, user, deleted_by=sysop)
        return real(database, user)

    login_flow.touch_last_login = deleting
    try:
        result, session = _login(db, ["guest", "hunter2", "guest", "hunter2", "guest", "hunter2"])
    finally:
        login_flow.touch_last_login = real

    assert result is login_flow.LoginOutcome.ATTEMPTS_EXHAUSTED
    assert "Guest access is not available." in "".join(session.written)

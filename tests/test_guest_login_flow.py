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
from netbbs.guest import set_guest_username, set_pre_login_notice
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
    set_guest_username(db, "guest")

    result, session = _login(db, ["guest"])

    assert getattr(result, "id", None) == guest.id
    assert "Password:" not in "".join(session.written)


def test_any_other_account_still_needs_its_password(db):
    create_user(db, "guest", password="hunter2", user_level=1)
    alice = create_user(db, "alice", password="correct", user_level=10)
    set_guest_username(db, "guest")

    result, session = _login(db, ["alice", "correct"])

    assert getattr(result, "id", None) == alice.id
    assert "Password:" in "".join(session.written)


def test_the_guest_account_can_still_sign_in_normally(db):
    """Guest login skips the password; it does not remove it."""
    guest = create_user(db, "guest", password="hunter2", user_level=1)
    set_guest_username(db, "guest")
    # Designation off: the same account, the ordinary path.
    set_guest_username(db, None)

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
    set_guest_username(db, "guest")

    result, session = _login(db, ["guest"])

    assert result is login_flow.LoginOutcome.BLOCKED
    assert "revoked" in "".join(session.written)


def test_a_disabled_guest_account_does_not_let_anyone_in(db):
    """`handle_session` refuses a disabled account after `_login`
    returns it, exactly as for any other account -- the guest path must
    not hand back something that skips that."""
    guest = create_user(db, "guest", password="hunter2", user_level=1)
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    set_user_disabled(db, guest, disabled=True, changed_by=sysop)
    set_guest_username(db, "guest")

    result, _ = _login(db, ["guest"])

    assert getattr(result, "disabled_at", None) is not None


def test_a_deleted_guest_account_falls_back_to_the_password_prompt(db):
    """Fails closed: the name stops being special rather than matching
    something unintended."""
    from netbbs.auth.users import delete_user

    guest = create_user(db, "guest", password="hunter2", user_level=1)
    sysop = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    set_guest_username(db, "guest")
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

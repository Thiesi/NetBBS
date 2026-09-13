"""The SysOp's Guest access screen (issue #531).

Two Codex rounds landed on one field here, and both were about the same
thing from opposite sides: how a SysOp says "no value".

`text_field` used to read a blank entry as "keep the current value",
which is right for a name that must always be *something* and left a
configured guest account impossible to revoke -- while the help text
advertised exactly that workflow. A `'none'` sentinel fixed it and broke
a smaller thing in turn: `RESERVED_USERNAMES` holds only `new`, so an
account genuinely named "none" could never be designated, and a notice
reading "none" could never be set.

Both are gone. Issue #529 gave `text_field` the current value as an
editable prefill, so erasing it and pressing Enter *is* the clear.
"""

from __future__ import annotations

from netbbs.auth.users import create_user
from netbbs.guest import guest_user, pre_login_notice, set_guest_user
from tests.test_admin_flow import (  # noqa: F401
    FakeSession, _normalized_visible, _run, _visible, _written_text, db, lane, sysop,
)


# -- Designating, and undesignating -----------------------------------


def test_an_account_can_be_designated(db, lane, sysop):
    create_user(db, "guest", password="hunter2", user_level=1)
    _run(FakeSession(["s", "g", "g", "guest", "s", "b", "b"]), lane, sysop)
    assert guest_user(db).username == "guest"


def test_erasing_the_field_turns_guest_login_off(db, lane, sysop):
    """The empty string in the queue is a SysOp erasing the prefilled
    value and pressing Enter. Before the prefill existed this read as
    "keep" and there was no way at all to revoke passwordless access
    short of deleting the account."""
    guest = create_user(db, "guest", password="hunter2", user_level=1)
    set_guest_user(db, guest)
    _run(FakeSession(["s", "g", "g", "", "s", "b", "b"]), lane, sysop)
    assert guest_user(db) is None


def test_the_account_survives_being_undesignated(db, lane, sysop):
    from netbbs.auth.users import authenticate_password

    guest = create_user(db, "guest", password="hunter2", user_level=1)
    set_guest_user(db, guest)
    _run(FakeSession(["s", "g", "g", "", "s", "b", "b"]), lane, sysop)
    assert authenticate_password(db, "guest", "hunter2") is not None


# -- The name that used to be unusable --------------------------------


def test_an_account_named_none_can_be_designated(db, lane, sysop):
    """The `'none'` sentinel's own casualty. Nothing reserves the name,
    so a node can have one -- and typing it meant "turn guest login
    off" rather than "designate this account"."""
    create_user(db, "none", password="hunter2", user_level=1)
    _run(FakeSession(["s", "g", "g", "none", "s", "b", "b"]), lane, sysop)
    assert guest_user(db) is not None
    assert guest_user(db).username == "none"


def test_a_notice_reading_none_can_be_set(db, lane, sysop):
    _run(FakeSession(["s", "g", "n", "none", "s", "b", "b"]), lane, sysop)
    assert pre_login_notice(db) == "none"


# -- What the Settings menu says --------------------------------------


def test_the_settings_menu_names_the_guest_account(db, lane, sysop):
    guest = create_user(db, "guest", password="hunter2", user_level=1)
    set_guest_user(db, guest)
    session = FakeSession(["s", "b", "b"])
    _run(session, lane, sysop)
    assert "Guest login as guest" in _visible(_written_text(session))


def test_a_legacy_username_cannot_inject_control_sequences(db, lane, sysop):
    """Account creation rejects a username carrying C1 controls or a
    bidi override, but deliberately left rows that predate that check
    valid -- and `menu_grid` does not sanitize the briefs it is handed
    (Codex review). Designating such an account would otherwise have
    written its raw bytes into the SysOp's own Settings menu.
    """
    guest = create_user(db, "guest", password="hunter2", user_level=1)
    set_guest_user(db, guest)
    # Renamed underneath the designation, which keeps id and created_at
    # -- the pair the designation is keyed on -- exactly as a row
    # predating the check would present them.
    db.connection.execute(
        "UPDATE users SET username = ? WHERE id = ?", ("ev\x9b31mil\u202e", guest.id)
    )
    db.connection.commit()

    session = FakeSession(["s", "b", "b"])
    _run(session, lane, sysop)

    text = _written_text(session)
    assert "\x9b" not in text
    assert "\u202e" not in text


def test_an_account_named_new_cannot_be_designated(db, lane, sysop):
    """`new` is how a caller asks to register, and `_login` acts on it
    before the guest branch is reached -- so designating an account with
    that name saved happily and then signed nobody in (Codex review).
    `RESERVED_USERNAMES` has refused the name since, so only a row
    predating that check can be in this position; it is created here the
    way such a row exists, directly.
    """
    stray = create_user(db, "newcomer", password="hunter2", user_level=1)
    db.connection.execute("UPDATE users SET username = ? WHERE id = ?", ("new", stray.id))
    db.connection.commit()

    session = FakeSession(["s", "g", "g", "new", "s", "b", "y", "b", "b"])
    _run(session, lane, sysop)

    assert guest_user(db) is None
    # Wrapped on screen, so the sentence is reassembled first.
    assert "cannot be the guest account" in _normalized_visible(_written_text(session))

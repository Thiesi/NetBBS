"""A deleted account's username stays retired on a node that has run Link (issue #594).

On the Link an account *is* its username. `local_user_id` on the wire is the
username, and `users.username` is unique only among live rows, so before this
a freed name handed the next registrant the previous holder's Link mail
address, the authorship of their carried posts, the trust state peers had
recorded, and any live attestation. These tests start from the two acts a
SysOp and a caller actually perform -- deleting an account, registering a
name -- and ask what the second one is allowed to do after the first.
"""

from __future__ import annotations

import pytest

from netbbs.auth.users import (
    SYSOP_LEVEL,
    AuthError,
    UserManagementError,
    UsernameRetiredError,
    authenticate_password,
    create_user,
    delete_user,
    get_user_by_username,
    is_username_retired,
    list_retired_usernames,
    list_users,
    release_retired_username,
)
from netbbs.link.onboarding import (
    Participation,
    link_has_ever_run,
    mark_link_has_run,
    set_configured_link_enabled,
    set_participation,
)
from netbbs.moderation.log import list_recent_actions
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="password", user_level=SYSOP_LEVEL)


def _delete(db, sysop, username="alice", *, ever_logged_in=True):
    """Delete an account that has been used, as a deleted account usually has."""
    user = create_user(db, username, password="password")
    if ever_logged_in:
        user = authenticate_password(db, username, "password")
        assert user.last_login_at is not None
    delete_user(db, user, deleted_by=sysop)


# -- a node that has never run Link loses nothing -----------------------------


def test_a_standalone_node_frees_the_name_as_it_always_has(db, sysop):
    assert not link_has_ever_run(db)
    _delete(db, sysop)

    assert list_retired_usernames(db) == []
    assert create_user(db, "alice", password="password").username == "alice"


# -- a node that has holds the name -------------------------------------------


def test_a_link_node_holds_the_name_of_a_deleted_account(db, sysop):
    mark_link_has_run(db)
    _delete(db, sysop)

    assert [entry.username for entry in list_retired_usernames(db)] == ["alice"]
    with pytest.raises(UsernameRetiredError):
        create_user(db, "alice", password="password")
    assert [user.username for user in list_users(db)] == ["sysop"]


def test_the_hold_is_case_insensitive_like_the_uniqueness_it_stands_in_for(db, sysop):
    mark_link_has_run(db)
    _delete(db, sysop, "Alice")

    for spelling in ("alice", "ALICE", "Alice"):
        assert is_username_retired(db, spelling)
        with pytest.raises(UsernameRetiredError):
            create_user(db, spelling, password="password")


def test_a_remote_caller_cannot_tell_a_retired_name_from_a_taken_one(db, sysop):
    """Both registration paths print the exception to whoever is connected, so
    its text must not make registration an oracle for past accounts."""
    mark_link_has_run(db)
    create_user(db, "bob", password="password")
    _delete(db, sysop)

    with pytest.raises(AuthError) as taken:
        create_user(db, "bob", password="password")
    with pytest.raises(UsernameRetiredError) as retired:
        create_user(db, "alice", password="password")

    assert str(retired.value) == str(taken.value).replace("'bob'", "'alice'")
    assert "deleted" not in str(retired.value)
    # The SysOp's version says why, and where the way out is.
    assert "deleted account" in retired.value.sysop_detail
    assert "Retired names" in retired.value.sysop_detail


def test_deleting_the_same_name_twice_is_not_an_error(db, sysop):
    mark_link_has_run(db)
    _delete(db, sysop)
    release_retired_username(db, "alice", released_by=sysop)
    _delete(db, sysop)

    assert [entry.username for entry in list_retired_usernames(db)] == ["alice"]


def test_a_refused_deletion_retires_nothing(db, sysop):
    """One transaction: there is no moment at which the account is gone and
    the name is not held, and none at which the name is held for an account
    that still exists."""
    mark_link_has_run(db)

    with pytest.raises(UserManagementError):
        delete_user(db, sysop, deleted_by=sysop)  # the last active SysOp

    assert list_retired_usernames(db) == []
    assert get_user_by_username(db, "sysop") is not None


def test_an_account_that_never_logged_in_is_not_retired(db, sysop):
    """A declined registration, a test account, a typo: never posted, never
    sent, never vouched for, so there is nothing for a successor to inherit --
    and on an approval-required node, retiring them would let strangers
    consume names permanently just by asking for them."""
    mark_link_has_run(db)
    _delete(db, sysop, ever_logged_in=False)

    assert list_retired_usernames(db) == []
    assert create_user(db, "alice", password="password").username == "alice"


def test_the_hold_is_decided_on_the_fresh_row_not_the_callers_stale_copy(db, sysop):
    """The caller's `User` was read before the account logged in; the delete
    re-reads inside its transaction and must ask the predicate of that."""
    mark_link_has_run(db)
    stale = create_user(db, "alice", password="password")
    authenticate_password(db, "alice", "password")
    assert stale.last_login_at is None

    delete_user(db, stale, deleted_by=sysop)

    assert is_username_retired(db, "alice")


# -- "ever", not "now" ---------------------------------------------------------


def test_the_name_is_held_although_link_is_switched_off_at_the_moment_of_deletion(db, sysop):
    """Codex review of the decision: keyed on the setting at deletion, an
    account deleted in a maintenance window with Link off could be
    re-registered and carried back onto the network under an identity its
    peers already know."""
    mark_link_has_run(db)
    set_configured_link_enabled(db, False)

    _delete(db, sysop)

    with pytest.raises(UsernameRetiredError):
        create_user(db, "alice", password="password")


def test_a_name_stays_held_after_link_is_switched_off(db, sysop):
    mark_link_has_run(db)
    _delete(db, sysop)
    set_configured_link_enabled(db, False)

    with pytest.raises(UsernameRetiredError):
        create_user(db, "alice", password="password")


def test_a_node_whose_last_startup_resolved_link_on_counts_without_the_marker(db, sysop):
    """`netbbs.admin` can delete an account before the daemon has started
    again and set the marker."""
    set_configured_link_enabled(db, True)
    assert link_has_ever_run(db)

    set_configured_link_enabled(db, None)
    assert not link_has_ever_run(db)
    set_participation(db, Participation.ACCEPTED)
    assert link_has_ever_run(db)


def test_a_node_that_ran_link_before_the_marker_existed_is_given_away_by_its_peers(db, sysop):
    set_configured_link_enabled(db, False)
    assert not link_has_ever_run(db)

    db.connection.execute(
        """INSERT INTO link_peers
           (fingerprint, root_public_key, transitions_json, descriptor_json, updated_at)
           VALUES ('peer-fingerprint', 'AA==', '[]', '{}', '2026-01-01T00:00:00.000000Z')"""
    )
    db.connection.commit()

    assert link_has_ever_run(db)
    _delete(db, sysop)
    assert is_username_retired(db, "alice")


def test_marking_twice_is_idempotent(db):
    mark_link_has_run(db)
    mark_link_has_run(db)
    assert link_has_ever_run(db)


# -- the SysOp's way out -------------------------------------------------------


def test_a_sysop_can_release_a_name_and_the_release_is_audited(db, sysop):
    mark_link_has_run(db)
    _delete(db, sysop)

    release_retired_username(db, "ALICE", released_by=sysop)

    assert list_retired_usernames(db) == []
    assert create_user(db, "alice", password="password").username == "alice"
    released = [entry for entry in list_recent_actions(db) if entry.action == "release_retired_username"]
    assert len(released) == 1
    assert "'alice'" in released[0].detail


def test_releasing_a_name_that_is_not_held_is_refused(db, sysop):
    with pytest.raises(UserManagementError, match="not a retired username"):
        release_retired_username(db, "nobody", released_by=sysop)


def test_a_database_upgraded_into_this_starts_with_nothing_held(tmp_path, monkeypatch):
    """Forward only: past deletions are named in the moderation log's free
    text, and guessing reservations out of it would retire the wrong names."""
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import MIGRATIONS

    index = next(i for i, m in enumerate(MIGRATIONS) if m.description.startswith("Issue #594:"))
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:index])
    path = tmp_path / "pre-594.db"
    old = Database(path)
    old.connection.execute(
        "INSERT INTO users (username, password_hash, user_level, created_at) "
        "VALUES ('carol', 'x', 0, '2026-01-01T00:00:00.000000Z')"
    )
    old.connection.commit()
    old.close()
    monkeypatch.undo()

    upgraded = Database(path)
    try:
        assert list_retired_usernames(upgraded) == []
        assert get_user_by_username(upgraded, "carol") is not None
    finally:
        upgraded.close()

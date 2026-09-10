"""Tests for netbbs.session_history -- the persisted "last N sessions"
record backing the caller-facing [H]istory screen (issue #100)."""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user, delete_user
from netbbs.session_history import (
    _MAX_SESSION_HISTORY_ROWS,
    list_recent_sessions,
    previous_callers_enabled,
    reconcile_interrupted_sessions,
    record_session_end,
    record_session_start,
    session_history_name_visible,
    set_previous_callers_enabled,
    set_session_history_name_visible,
)
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def sysop(db):
    return create_user(db, "sysop", password="hunter2", user_level=255)


def test_record_session_start_creates_a_row_with_no_end_yet(db, alice):
    history_id = record_session_start(db, alice)
    entries = list_recent_sessions(db)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.id == history_id
    assert entry.user_id == alice.id
    assert entry.username_label == "alice"
    assert entry.connected_at
    assert entry.disconnected_at is None


def test_record_session_end_fills_in_disconnected_at(db, alice):
    history_id = record_session_start(db, alice)
    record_session_end(db, history_id)
    entry = list_recent_sessions(db)[0]
    assert entry.disconnected_at is not None


def test_record_session_end_on_a_pruned_id_is_a_silent_no_op(db, alice):
    # Doesn't correspond to any real row -- must not raise.
    record_session_end(db, 999999)


def test_list_recent_sessions_most_recent_first(db, alice):
    first = record_session_start(db, alice)
    second = record_session_start(db, alice)
    entries = list_recent_sessions(db)
    assert [e.id for e in entries] == [second, first]


def test_list_recent_sessions_respects_limit(db, alice):
    for _ in range(5):
        record_session_start(db, alice)
    assert len(list_recent_sessions(db, limit=2)) == 2


def test_previous_callers_splash_defaults_on_and_can_be_disabled(db):
    assert previous_callers_enabled(db) is True
    set_previous_callers_enabled(db, False)
    assert previous_callers_enabled(db) is False
    set_previous_callers_enabled(db, True)
    assert previous_callers_enabled(db) is True


def test_row_count_pruning_keeps_only_the_most_recent_rows(db, alice):
    # +5 over the cap -- confirms pruning actually removes the oldest
    # ones rather than merely capping list_recent_sessions's own read.
    for _ in range(_MAX_SESSION_HISTORY_ROWS + 5):
        record_session_start(db, alice)
    total = db.connection.execute("SELECT COUNT(*) AS n FROM session_history").fetchone()["n"]
    assert total == _MAX_SESSION_HISTORY_ROWS


def test_deleting_the_account_sets_user_id_null_but_keeps_the_row_and_label(db, alice, sysop):
    record_session_start(db, alice)
    delete_user(db, alice, deleted_by=sysop)
    entries = list_recent_sessions(db)
    assert len(entries) == 1
    assert entries[0].user_id is None
    assert entries[0].username_label == "alice"  # denormalized -- survives the delete


# -- name_visible_fallback (issue #111) -----------------------------------


def test_new_row_defaults_fallback_to_visible(db, alice):
    record_session_start(db, alice)
    assert list_recent_sessions(db)[0].name_visible_fallback is True


def test_new_row_records_current_opt_out_as_fallback(db, alice):
    set_session_history_name_visible(db, alice, False)
    record_session_start(db, alice)
    assert list_recent_sessions(db)[0].name_visible_fallback is False


def test_toggling_the_preference_updates_the_fallback_on_existing_rows(db, alice):
    """The core mechanism issue #111 relies on: the fallback isn't
    frozen at each row's own connect time -- it tracks the live
    preference for as long as the account exists, so whatever it holds
    at deletion time matches what was actually in effect right up until
    then, not whatever was true back when the row was first created."""
    record_session_start(db, alice)  # connects while still visible (default)
    assert list_recent_sessions(db)[0].name_visible_fallback is True

    set_session_history_name_visible(db, alice, False)  # opts out afterward

    assert list_recent_sessions(db)[0].name_visible_fallback is False


def test_toggling_back_to_visible_also_updates_existing_rows(db, alice):
    record_session_start(db, alice)
    set_session_history_name_visible(db, alice, False)
    set_session_history_name_visible(db, alice, True)
    assert list_recent_sessions(db)[0].name_visible_fallback is True


def test_toggling_never_touches_a_different_users_rows(db, alice):
    bob = create_user(db, "bob", password="hunter2", user_level=10)
    record_session_start(db, bob)
    set_session_history_name_visible(db, alice, False)  # alice, not bob
    assert list_recent_sessions(db)[0].name_visible_fallback is True


def test_deleted_account_that_opted_out_stays_hidden_via_fallback(db, alice, sysop):
    """Issue #111's own acceptance criterion, reproduced exactly: opt
    out, then delete -- the name must not come back once the live
    preference row is gone with the account."""
    record_session_start(db, alice)
    set_session_history_name_visible(db, alice, False)
    delete_user(db, alice, deleted_by=sysop)

    entry = list_recent_sessions(db)[0]
    assert entry.user_id is None
    assert entry.name_visible_fallback is False


def test_deleted_account_that_never_opted_out_still_shows_via_fallback(db, alice, sysop):
    record_session_start(db, alice)
    delete_user(db, alice, deleted_by=sysop)

    entry = list_recent_sessions(db)[0]
    assert entry.user_id is None
    assert entry.name_visible_fallback is True


# -- reconcile_interrupted_sessions (issue #110) -------------------------


def test_reconcile_marks_a_null_row_as_interrupted(db, alice):
    """The core scenario: a row left NULL/NULL by a process that never
    reached record_session_end at all (simulating a hard kill/crash, not
    a call this test itself makes)."""
    record_session_start(db, alice)
    reconciled = reconcile_interrupted_sessions(db)
    assert reconciled == 1

    entry = list_recent_sessions(db)[0]
    assert entry.disconnected_at is None  # never claim this was a real, clean disconnect moment
    assert entry.interrupted_at is not None


def test_reconcile_never_touches_a_cleanly_ended_session(db, alice):
    """A row that already has disconnected_at set is a normal, cleanly-
    ended session -- reconciliation must never touch it, let alone
    overwrite the real recorded end time."""
    history_id = record_session_start(db, alice)
    record_session_end(db, history_id)
    entry_before = list_recent_sessions(db)[0]

    reconciled = reconcile_interrupted_sessions(db)

    assert reconciled == 0
    entry_after = list_recent_sessions(db)[0]
    assert entry_after.disconnected_at == entry_before.disconnected_at
    assert entry_after.interrupted_at is None


def test_reconcile_is_a_no_op_when_nothing_is_open(db):
    assert reconcile_interrupted_sessions(db) == 0


def test_reconcile_only_marks_rows_still_open_at_call_time(db, alice):
    """A session genuinely still open in the *current* process at the
    moment reconciliation runs must not be marked interrupted --
    `netbbs.__main__.run()`'s own placement (before any listener accepts
    a new session) is what actually guarantees this in production; this
    test proves reconcile_interrupted_sessions itself only ever acts on
    rows that are NULL *right now*, regardless of when they were
    created, which is the property that placement relies on."""
    still_open_id = record_session_start(db, alice)
    already_ended_id = record_session_start(db, alice)
    record_session_end(db, already_ended_id)

    reconciled = reconcile_interrupted_sessions(db)

    assert reconciled == 1
    entries = {e.id: e for e in list_recent_sessions(db)}
    assert entries[still_open_id].interrupted_at is not None
    assert entries[already_ended_id].interrupted_at is None
    assert entries[already_ended_id].disconnected_at is not None


# -- session_history_name_visible --------------------------------------


def test_name_visible_defaults_to_true(db, alice):
    assert session_history_name_visible(db, alice) is True


def test_set_name_visible_false(db, alice):
    set_session_history_name_visible(db, alice, False)
    assert session_history_name_visible(db, alice) is False


def test_set_name_visible_true_after_false(db, alice):
    set_session_history_name_visible(db, alice, False)
    set_session_history_name_visible(db, alice, True)
    assert session_history_name_visible(db, alice) is True

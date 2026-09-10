"""
Persisted "last N sessions" history (issue #100) -- one row per
authenticated login, recorded from `netbbs.net.login_flow.
run_authenticated_session`, the single entry point every transport
funnels through once a `User` is known-good.

Distinct from `netbbs.net.session_registry.ActiveSessionRegistry`,
which is in-memory and only ever knows about *currently* connected
sessions (design doc): this is the permanent, DB-backed record a caller
browses to see who has recently visited, independent of who happens to
still be online right now.

Row-count-bounded on every insert, the same reasoning
`netbbs.link.diagnostics`'s own pruning already established for a table
fed by an ongoing stream of events over a node's lifetime -- this
feature only ever wants the most recent slice, so there's no reason to
let it grow without bound.
"""

from __future__ import annotations

from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.config import get_config, set_config
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso
from netbbs.user_preferences import get_user_preference

# Keeps only the most recent this-many rows on every insert -- generous
# enough that "last N sessions" (N well under this) always has a full
# window to draw from, small enough that the table can't grow without
# bound over a node's lifetime.
_MAX_SESSION_HISTORY_ROWS = 500

_NAME_VISIBLE_KEY = "session_history_name_visible"
_PREVIOUS_CALLERS_ENABLED_KEY = "previous_callers_enabled"


@dataclass(frozen=True)
class SessionHistoryEntry:
    id: int
    user_id: int | None
    username_label: str
    connected_at: str
    disconnected_at: str | None
    interrupted_at: str | None
    name_visible_fallback: bool


def record_session_start(db: Database, user: User) -> int:
    """Called once, at the top of `run_authenticated_session` -- returns
    the new row's id, which the caller holds onto for the matching
    `record_session_end` call when that same session ends.

    Issue #111: also records `user`'s current `session_history_name_
    visible` preference into `name_visible_fallback`. While the account
    still exists, `_session_history_display_name` (`netbbs.net.
    login_flow`) keeps re-checking the *live* preference, exactly as
    issue #100 already established (a later opt-out/opt-in takes effect
    retroactively for every existing row); `name_visible_fallback` only
    ever becomes the thing actually consulted once the account is
    deleted and there is no longer a live preference to re-check. It
    isn't frozen at this initial value forever, though --
    `set_session_history_name_visible` below keeps it in sync across
    every one of a user's existing rows for as long as the account
    exists, so whatever it holds at deletion time is exactly the value
    that was in effect immediately before deletion, not whatever
    happened to be true back when a given row first connected."""
    db.connection.execute(
        "INSERT INTO session_history (user_id, username_label, connected_at, name_visible_fallback) "
        "VALUES (?, ?, ?, ?)",
        (user.id, user.username, utc_now_iso(), int(session_history_name_visible(db, user))),
    )
    row_id = db.connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    # Pruning happens alongside the insert that could have grown the
    # table past the cap, the same "bound it right where it grows"
    # placement `LinkDiagnosticLogHandler.emit` uses -- keeps the most
    # recent rows by id (insertion order), not by connected_at, so a
    # session started slightly "out of order" relative to another
    # (clock skew is not a concern here -- both are this node's own
    # utc_now_iso()) is never a factor.
    db.connection.execute(
        """
        DELETE FROM session_history WHERE id NOT IN (
            SELECT id FROM session_history ORDER BY id DESC LIMIT ?
        )
        """,
        (_MAX_SESSION_HISTORY_ROWS,),
    )
    db.connection.commit()
    return row_id


def record_session_end(db: Database, history_id: int) -> SessionHistoryEntry | None:
    """Finalize and return a session row, or ``None`` if it was pruned.

    A missing row is a no-op (an
    extremely long-lived session outlasting `_MAX_SESSION_HISTORY_ROWS`
    worth of *other* logins) -- matches `ActiveSessionRegistry.
    notify_one`/`disconnect_one`'s own tolerance for a target that's no
    longer there by the time this runs. Returning the finalized row lets
    the clean-logoff path present the exact persisted timestamps without
    a second public lookup API or a separately sampled clock."""
    disconnected_at = utc_now_iso()
    db.connection.execute(
        "UPDATE session_history SET disconnected_at = ? WHERE id = ?",
        (disconnected_at, history_id),
    )
    row = db.connection.execute(
        "SELECT id, user_id, username_label, connected_at, disconnected_at, interrupted_at, "
        "name_visible_fallback FROM session_history WHERE id = ?",
        (history_id,),
    ).fetchone()
    db.connection.commit()
    return _entry_from_row(row) if row is not None else None


def list_recent_sessions(db: Database, *, limit: int = 20) -> list[SessionHistoryEntry]:
    """Most recent first."""
    rows = db.connection.execute(
        "SELECT id, user_id, username_label, connected_at, disconnected_at, interrupted_at, "
        "name_visible_fallback FROM session_history ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        _entry_from_row(row)
        for row in rows
    ]


def _entry_from_row(row) -> SessionHistoryEntry:
    return SessionHistoryEntry(
        id=row["id"], user_id=row["user_id"], username_label=row["username_label"],
        connected_at=row["connected_at"], disconnected_at=row["disconnected_at"],
        interrupted_at=row["interrupted_at"],
        name_visible_fallback=bool(row["name_visible_fallback"]),
    )


def previous_callers_enabled(db: Database) -> bool:
    """Whether the post-login previous-callers splash is shown node-wide."""
    return get_config(db, _PREVIOUS_CALLERS_ENABLED_KEY, default="1") == "1"


def set_previous_callers_enabled(db: Database, enabled: bool) -> None:
    """Enable or disable the post-login previous-callers splash node-wide."""
    set_config(db, _PREVIOUS_CALLERS_ENABLED_KEY, "1" if enabled else "0")


def _backfill_name_visibility_fallbacks(db: Database) -> None:
    """Repair legacy fallback values from each live account preference.

    Issue #111's already-shipped migration necessarily defaulted every
    pre-existing row to visible. A user who had opted out before that
    upgrade therefore needs one bounded, idempotent repair before
    listeners start (issue #123). Deleted-account rows are intentionally
    untouched because no live preference remains to consult.
    """
    db.connection.execute(
        """
        UPDATE session_history
        SET name_visible_fallback = CASE
            WHEN EXISTS (
                SELECT 1 FROM user_preferences
                WHERE user_preferences.user_id = session_history.user_id
                  AND user_preferences.key = ?
                  AND user_preferences.value = '0'
            ) THEN 0
            ELSE 1
        END
        WHERE user_id IS NOT NULL
        """,
        (_NAME_VISIBLE_KEY,),
    )


def reconcile_interrupted_sessions(db: Database) -> int:
    """Issue #110: called once, at node startup (`netbbs.__main__.run`),
    *before* any listener can accept a new session -- the same "anything
    already there survived a previous run that was killed, crashed, or
    lost power" placement issue #34's `netbbs.files.storage.
    purge_incoming_staging` already established for stale upload staging
    files.

    Every row with both `disconnected_at IS NULL` and `interrupted_at IS
    NULL` at this exact point was, by construction, left open by a
    *previous* process instance: this
    process's own listeners haven't started yet, so nothing here could
    have called `record_session_start` this run. `record_session_end`
    only ever fills `disconnected_at` from `run_authenticated_session`'s
    own `finally:` block -- a hard kill, power loss, or crash skips that
    entirely, leaving the row silently claiming "still connected" forever
    (until it eventually ages out of the row-count cap). Marking
    `interrupted_at` (never `disconnected_at` itself, which must keep
    meaning "the actual moment the session cleanly ended" -- this is only
    ever "the moment this reconciliation ran," typically well after the
    connection actually dropped) is what lets `netbbs.net.login_flow.
    _last_sessions_screen` show something honest instead of "still
    connected" for a session that cannot possibly still exist.

    Issue #122's second predicate makes the operation idempotent:
    reconciled rows deliberately keep `disconnected_at` NULL, so later
    restarts must not overwrite their first detection timestamp.

    The same before-listeners transaction repairs legacy privacy
    fallbacks (issue #123). Returns only the number of newly interrupted
    sessions, preserving the caller's existing log meaning.
    """
    with db.connection:
        _backfill_name_visibility_fallbacks(db)
        cursor = db.connection.execute(
            "UPDATE session_history SET interrupted_at = ? "
            "WHERE disconnected_at IS NULL AND interrupted_at IS NULL",
            (utc_now_iso(),),
        )
    return cursor.rowcount


def session_history_name_visible(db: Database, user: User) -> bool:
    """Default `True` (shown by name) -- opt-out, not opt-in, per the
    feature's own spec: shown by default, with an explicit toggle to
    anonymize. The opposite default from `netbbs.directory`'s bio
    visibility, same reasoning `netbbs.messaging_preferences` already
    documents: this isn't disclosing new personal content, just whether
    an already-necessarily-visible list entry is labeled or not."""
    return get_user_preference(db, user, _NAME_VISIBLE_KEY, default="1") == "1"


def set_session_history_name_visible(db: Database, user: User, visible: bool) -> None:
    """Issue #111: also updates `name_visible_fallback` across every one
    of `user`'s existing `session_history` rows, not only the preference
    itself. While the account exists this has no visible effect on its
    own (`_session_history_display_name` re-checks the live preference,
    never this column, for an existing account) -- it exists purely so
    that if this account is deleted at any point afterward, the fallback
    every one of its rows falls back to is the value that was actually
    in effect right up until deletion, matching what an ordinary caller
    was already seeing, rather than reverting to whatever was true back
    when each row happened to first connect.

    Issue #123: the live preference and all fallback rows are one
    privacy decision and therefore one SQLite transaction. The generic
    preference helper commits internally, so this typed wrapper performs
    the small UPSERT directly before updating history.
    """
    value = "1" if visible else "0"
    with db.connection:
        db.connection.execute(
            """
            INSERT INTO user_preferences (user_id, key, value) VALUES (?, ?, ?)
            ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
            """,
            (user.id, _NAME_VISIBLE_KEY, value),
        )
        db.connection.execute(
            "UPDATE session_history SET name_visible_fallback = ? WHERE user_id = ?",
            (int(visible), user.id),
        )

"""
Per-user preference storage: a simple key-value store backed by the
database, mirroring `netbbs.config`'s node-wide store exactly (design
doc §13) — that module's own docstring already
anticipated this as "a separate, later layer that sits on top of"
node-wide config, not a replacement for it.

Deliberately generic rather than scoped to any one feature: the
directory/vCard system (`netbbs.directory`) is the first consumer, but
any future per-user setting (e.g. a per-user chat timestamp
preference, design doc) can reuse this same store via its own
typed wrapper functions, the same way `netbbs.timeutil` wraps
`netbbs.config`'s generic store for the node-wide display format/
timezone settings.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from netbbs.auth.users import User
from netbbs.storage.database import Database


# -- session-scoped preferences (issue #1073) ---------------------------------
#
# A guest session (guest login, issue #531) signs in to one account every
# anonymous caller shares. Its display preferences -- character set, colour
# depth, redraw style, sort orders -- are still the caller's to choose: a
# guest on a plain-ASCII or 16-colour terminal needs them as much as anyone.
# But what one guest chooses must not become what the next guest gets, so for
# such a session every preference write lands here, in memory, and lasts as
# long as the call. Reads by that session see its own choices first and the
# account's stored values underneath.
#
# Held in a context variable, not on the `Session`, because every preference
# getter takes `(db, user)` and is reached from a hundred-odd call sites that
# have no session to hand. Each connection runs in its own task, so the
# variable is per caller; `DatabaseLane.run` carries the caller's context onto
# its worker thread, so a getter run on the lane sees it too. Another session
# reading the guest account -- someone else's Directory view, the MRC bridge,
# a sender checking whether the guest takes messages -- runs in its own
# context and reads the stored values, which is why Profile refuses a guest
# the settings other callers see rather than leaving them to this overlay.


@dataclass
class SessionPreferences:
    """What one session chose for `user_id`, kept off the database.

    `values` mirrors the `user_preferences` key/value store; `sort_modes`
    mirrors `netbbs.sort_preferences`, keyed by `(resource_kind,
    community_id, category_id)`, with `None` marking an override this
    session cleared."""

    user_id: int
    values: dict[str, str] = field(default_factory=dict)
    sort_modes: dict[tuple[str, int | None, int | None], str | None] = field(default_factory=dict)


_session_preferences: ContextVar[SessionPreferences | None] = ContextVar(
    "netbbs_session_preferences", default=None
)


@contextmanager
def session_scoped_preferences(user: User) -> Iterator[SessionPreferences]:
    """Within this block, preference writes for `user` made from the current
    context stay in memory instead of reaching the database."""
    overlay = SessionPreferences(user_id=user.id)
    token = _session_preferences.set(overlay)
    try:
        yield overlay
    finally:
        _session_preferences.reset(token)


def session_preferences_for(user: User) -> SessionPreferences | None:
    """The current context's session-scoped preferences, if they are
    `user`'s -- `None` for an ordinary session, and for any other account a
    guest session happens to read."""
    overlay = _session_preferences.get()
    if overlay is None or overlay.user_id != user.id:
        return None
    return overlay


def get_user_preference(db: Database, user: User, key: str, default: str | None = None) -> str | None:
    overlay = session_preferences_for(user)
    if overlay is not None and key in overlay.values:
        return overlay.values[key]
    row = db.connection.execute(
        "SELECT value FROM user_preferences WHERE user_id = ? AND key = ?", (user.id, key)
    ).fetchone()
    return row["value"] if row is not None else default


def set_user_preference(db: Database, user: User, key: str, value: str) -> None:
    overlay = session_preferences_for(user)
    if overlay is not None:
        overlay.values[key] = value
        return
    db.connection.execute(
        """
        INSERT INTO user_preferences (user_id, key, value) VALUES (?, ?, ?)
        ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
        """,
        (user.id, key, value),
    )
    db.connection.commit()

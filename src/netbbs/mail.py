"""
Local asynchronous personal mail (design doc round 93, resolving the
local half of issue #52).

Deliberately a new, persistent domain -- not the same mechanism as
`/msg` (`netbbs.chat.mailbox`), which stays exactly what it is:
ephemeral, online-only, session-addressed, with no fallback to
persistence (round 32's own explicit prohibition). This module is the
opposite shape on purpose: one message per row, independently
toggleable read/deleted state per side, and a quota that never
silently destroys something the recipient hasn't seen yet.

Link messages (the Phase 3 extension of this same mailbox) are not part
of this module -- see design doc round 93's "Link messages" half for
that design; nothing here assumes or depends on it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from netbbs.auth.users import User
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

# Generous but bounded, matching netbbs.directory's own byte-cap
# precedent for the same reason (issue #32: a length cap alone doesn't
# bound total size if not measured in bytes) -- counted in encoded
# UTF-8 bytes, what's actually stored.
MAX_MAIL_SUBJECT_BYTES = 200
MAX_MAIL_BODY_BYTES = 20_000

# Cap on stored (non-deleted) messages per recipient inbox -- a fixed
# module constant, not a SysOp-configurable node_config setting, same
# shape as netbbs.chat.mailbox.MessageMailbox's own per-session cap and
# netbbs.chat.hub.ChatHub's queue bounds. Generous enough that no
# realistically-paced correspondence ever comes close; round 93 didn't
# call for per-node tuning, so this isn't built as a knob speculatively.
MAX_MAIL_PER_RECIPIENT = 500


class MailError(Exception):
    """Raised for a mail-send validation failure (oversized subject/
    body, blank subject) or an unauthorized access attempt (a user
    trying to act on a message they're neither the sender nor recipient
    of)."""


class MailboxFullError(Exception):
    """
    Raised when `send_mail` would otherwise have to silently destroy an
    *unread* message to make room (design doc round 93: "never silently
    drop something a user hasn't seen yet"). The caller is expected to
    report this back to the sender as a bounce -- the message is never
    stored.
    """


@dataclass(frozen=True)
class MailMessage:
    id: int
    sender_user_id: int | None
    sender_label: str
    recipient_user_id: int
    subject: str
    body: str
    created_at: str
    read_at: str | None
    sender_deleted_at: str | None
    recipient_deleted_at: str | None

    @property
    def is_read(self) -> bool:
        return self.read_at is not None


def send_mail(db: Database, sender: User, recipient: User, subject: str, body: str) -> MailMessage:
    """
    Send one message from `sender` to `recipient`.

    Enforces `MAX_MAIL_PER_RECIPIENT`: if the recipient's inbox is
    already at the cap, the oldest **already-read** message is
    hard-deleted to make room (same drop-oldest precedent as
    `netbbs.chat.hub.ChatHub`'s queues and `netbbs.chat.mailbox.
    MessageMailbox`'s own per-session cap). If the inbox is entirely
    unread and full, raises `MailboxFullError` instead of destroying an
    unread message -- deterministic, matching design doc round 93's own
    acceptance criterion.
    """
    subject = subject.strip()
    if not subject:
        raise MailError("subject cannot be blank")
    subject_bytes = len(subject.encode("utf-8"))
    if subject_bytes > MAX_MAIL_SUBJECT_BYTES:
        raise MailError(f"subject cannot exceed {MAX_MAIL_SUBJECT_BYTES} bytes, got {subject_bytes}")
    body_bytes = len(body.encode("utf-8"))
    if body_bytes > MAX_MAIL_BODY_BYTES:
        raise MailError(f"body cannot exceed {MAX_MAIL_BODY_BYTES} bytes, got {body_bytes}")

    _make_room_if_needed(db, recipient)

    created_at = utc_now_iso()
    db.connection.execute(
        """
        INSERT INTO mail_messages
            (sender_user_id, sender_label, recipient_user_id, subject, body, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (sender.id, sender.username, recipient.id, subject, body, created_at),
    )
    db.connection.commit()
    row = db.connection.execute(
        "SELECT * FROM mail_messages WHERE id = last_insert_rowid()"
    ).fetchone()
    return _row_to_message(row)


def _make_room_if_needed(db: Database, recipient: User) -> None:
    count = db.connection.execute(
        "SELECT COUNT(*) AS n FROM mail_messages WHERE recipient_user_id = ? AND recipient_deleted_at IS NULL",
        (recipient.id,),
    ).fetchone()["n"]
    if count < MAX_MAIL_PER_RECIPIENT:
        return

    oldest_read = db.connection.execute(
        """
        SELECT id, sender_deleted_at FROM mail_messages
        WHERE recipient_user_id = ? AND recipient_deleted_at IS NULL AND read_at IS NOT NULL
        ORDER BY created_at ASC LIMIT 1
        """,
        (recipient.id,),
    ).fetchone()
    if oldest_read is None:
        raise MailboxFullError(
            f"{recipient.username!r}'s mailbox is full and every message is still unread"
        )
    _hard_delete_or_mark(db, oldest_read["id"], sender_deleted_at=oldest_read["sender_deleted_at"], recipient_deleted_at=utc_now_iso())


def get_mail(db: Database, user: User, mail_id: int) -> MailMessage:
    row = db.connection.execute("SELECT * FROM mail_messages WHERE id = ?", (mail_id,)).fetchone()
    if row is None:
        raise MailError(f"no such message: {mail_id}")
    message = _row_to_message(row)
    if user.id not in (message.sender_user_id, message.recipient_user_id):
        raise MailError(f"{user.username!r} is not a party to this message")
    return message


def list_inbox(db: Database, user: User) -> list[MailMessage]:
    """Every message in `user`'s inbox they haven't deleted their own
    view of, newest first."""
    rows = db.connection.execute(
        """
        SELECT * FROM mail_messages
        WHERE recipient_user_id = ? AND recipient_deleted_at IS NULL
        ORDER BY created_at DESC
        """,
        (user.id,),
    ).fetchall()
    return [_row_to_message(row) for row in rows]


def list_sent(db: Database, user: User) -> list[MailMessage]:
    """Every message `user` has sent that they haven't deleted their
    own view of, newest first."""
    rows = db.connection.execute(
        """
        SELECT * FROM mail_messages
        WHERE sender_user_id = ? AND sender_deleted_at IS NULL
        ORDER BY created_at DESC
        """,
        (user.id,),
    ).fetchall()
    return [_row_to_message(row) for row in rows]


def unread_count(db: Database, user: User) -> int:
    row = db.connection.execute(
        """
        SELECT COUNT(*) AS n FROM mail_messages
        WHERE recipient_user_id = ? AND recipient_deleted_at IS NULL AND read_at IS NULL
        """,
        (user.id,),
    ).fetchone()
    return row["n"]


def mark_read(db: Database, user: User, message: MailMessage) -> MailMessage:
    """No-op (returning `message` unchanged) if `user` isn't the
    recipient, or it's already read -- mirrors
    `netbbs.auth.users.approve_pending_user`'s own no-op-if-unchanged
    shape rather than raising for an ordinary, expected case (re-opening
    a message already read)."""
    if user.id != message.recipient_user_id or message.is_read:
        return message
    read_at = utc_now_iso()
    db.connection.execute("UPDATE mail_messages SET read_at = ? WHERE id = ?", (read_at, message.id))
    db.connection.commit()
    return get_mail(db, user, message.id)


def delete_for_recipient(db: Database, user: User, message: MailMessage) -> None:
    """Deletes `user`'s (the recipient's) own view of `message`. Hard-
    deletes the row outright once the sender's side is also gone --
    no shared-content reason to keep a personal message around the way
    a board post sometimes needs re-fetching for someone else.

    Re-fetches the row's *current* state before deciding, rather than
    trusting the *other* side's deletion field on the caller-supplied
    `message` -- that parameter can be stale if the other party deleted
    their own view since `message` was last fetched (e.g. a caller that
    fetched `message` once, then calls both `delete_for_recipient` and
    `delete_for_sender` in sequence, as this module's own tests do). A
    stale `sender_deleted_at`/`recipient_deleted_at` reintroduces
    exactly the check-then-act hazard already fixed elsewhere in this
    codebase for other row-mutation functions (GitHub issue #49) -- an
    UPDATE that overwrites a since-set deletion timestamp back to NULL,
    or a message that should have hard-deleted but silently didn't.
    """
    if user.id != message.recipient_user_id:
        raise MailError(f"{user.username!r} is not the recipient of this message")
    current = get_mail(db, user, message.id)
    if current.recipient_deleted_at is not None:
        return
    _hard_delete_or_mark(db, message.id, sender_deleted_at=current.sender_deleted_at, recipient_deleted_at=utc_now_iso())


def delete_for_sender(db: Database, user: User, message: MailMessage) -> None:
    """Symmetric counterpart to `delete_for_recipient` -- see that
    function's docstring for why this re-fetches current state instead
    of trusting `message.recipient_deleted_at`."""
    if user.id != message.sender_user_id:
        raise MailError(f"{user.username!r} is not the sender of this message")
    current = get_mail(db, user, message.id)
    if current.sender_deleted_at is not None:
        return
    _hard_delete_or_mark(db, message.id, sender_deleted_at=utc_now_iso(), recipient_deleted_at=current.recipient_deleted_at)


def _hard_delete_or_mark(
    db: Database, mail_id: int, *, sender_deleted_at: str | None, recipient_deleted_at: str | None
) -> None:
    if sender_deleted_at is not None and recipient_deleted_at is not None:
        db.connection.execute("DELETE FROM mail_messages WHERE id = ?", (mail_id,))
    else:
        db.connection.execute(
            "UPDATE mail_messages SET sender_deleted_at = ?, recipient_deleted_at = ? WHERE id = ?",
            (sender_deleted_at, recipient_deleted_at, mail_id),
        )
    db.connection.commit()


def _row_to_message(row: sqlite3.Row) -> MailMessage:
    return MailMessage(
        id=row["id"],
        sender_user_id=row["sender_user_id"],
        sender_label=row["sender_label"],
        recipient_user_id=row["recipient_user_id"],
        subject=row["subject"],
        body=row["body"],
        created_at=row["created_at"],
        read_at=row["read_at"],
        sender_deleted_at=row["sender_deleted_at"],
        recipient_deleted_at=row["recipient_deleted_at"],
    )

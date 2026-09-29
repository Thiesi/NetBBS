"""
Local asynchronous personal mail (design doc, resolving the
local half of issue #52).

Deliberately a new, persistent domain -- not the same mechanism as
`/msg` (`netbbs.chat.mailbox`), which stays exactly what it is:
ephemeral, online-only, session-addressed, with no fallback to
persistence (an explicit prohibition). This module is the
opposite shape on purpose: one message per row, independently
toggleable read/deleted state per side, and a quota that never
silently destroys something the recipient hasn't seen yet.

Link messages (the Phase 3 extension of this same mailbox) are not part
of this module -- see design doc's "Link messages" half for
that design; nothing here assumes or depends on it.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass

from netbbs.auth.users import User, get_user_by_id, is_usable_sysop, list_users
from netbbs.config import get_mail_min_level, is_node_fingerprint_shape
from netbbs.file_refs import (
    FileRef,
    forget_mail_refs_without_commit,
    recipient_ref_problem,
    sender_ref_problem,
    write_mail_refs_without_commit,
)
from netbbs.guest import guest_is_eligible
from netbbs.permissions.levels import meets_level
from netbbs.search import index_mail_without_commit, unindex_mail_without_commit
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso
from netbbs.user_preferences import get_user_preference, set_user_preference

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
# realistically-paced correspondence ever comes close; the design doc
# doesn't call for per-node tuning, so this isn't built as a knob speculatively.
MAX_MAIL_PER_RECIPIENT = 500

# From this many messages the mailbox warns its owner that the cap is near
# (issue #818): nine in ten, so there is room to act before anything goes.
MAILBOX_NEARLY_FULL = MAX_MAIL_PER_RECIPIENT * 9 // 10

# What the mailbox calls mail the BBS itself sent (issue #819). Shown for
# any row whose `from_system` flag is set, never looked up by name: the
# label stored with the row is only what the NOT NULL column holds.
# "system" is also a name self-service signup refuses
# (`netbbs.auth.users.SELF_SERVICE_RESERVED_USERNAMES`).
SYSTEM_SENDER_LABEL = "System"


class MailError(Exception):
    """Raised for a mail-send validation failure (oversized subject/
    body, blank subject) or an unauthorized access attempt (a user
    trying to act on a message they're neither the sender nor recipient
    of)."""


class MailRecipientRefused(MailError):
    """Raised by `send_mail` for a recipient whose account takes no mail
    (`mail_recipient_refusal`); the text is the reason, for the sender."""


# Who may use mail, and who may be sent it (issue #816, design doc §6.4).
#
# The guest account is the one account refused outright, whatever its level.
# Guest login (issue #531) is otherwise deliberately *not* a special case:
# levels and per-object permissions say what a guest may do, as for anyone.
# Mail is where that stops working, because a mailbox is not an area the
# account may or may not enter -- it is the account's own private
# correspondence. Every guest caller signs in as the same account, so its
# inbox would be read by strangers and anything sent from it would go out
# under one name that many people type into. No level can close that without
# also closing mail for every ordinary account at the guest's level.
#
# "The guest account" is the account guest login currently signs in without
# a password (`guest_is_eligible`). Turn guest login off, and it is an
# ordinary account again with its mail back.

GUEST_MAIL_REFUSAL = (
    "Mail needs an account of your own: the guest account is shared, so it "
    "has no mailbox. Register to send and receive mail."
)


def mail_access_refusal(db: Database, user: User) -> str | None:
    """Why `user` may not open mail -- read, write or reply, local and Link
    alike -- or `None` when they may. The one check every entry point
    makes."""
    if guest_is_eligible(db, user):
        return GUEST_MAIL_REFUSAL
    level = get_mail_min_level(db)
    if not meets_level(user, level):
        return f"Mail is open from access level {level}; yours is {user.user_level}."
    return None


def mail_recipient_refusal(db: Database, recipient: User) -> str | None:
    """Why no mail may be delivered to `recipient`, or `None`. Checked for
    local mail by `send_mail`, `send_system_mail` and at the To prompt, and
    for Link mail on arrival (`netbbs.link.mail.deliver_link_message`,
    which bounces with `mail_recipient_bounce_reason`).

    Three accounts take no mail: the shared guest account (issue #816), a
    disabled account, and a signup still awaiting approval (issue #818).
    Neither of the last two can sign in to read it, so mail sent there
    would sit unread and its sender would never hear. Mail already in an
    account when it is disabled stays where it is, for the day it is
    enabled again.

    An account below the mail level still receives: the mail waits for the
    day the SysOp raises its level, as a board's posts wait for a caller
    who cannot read them yet."""
    if guest_is_eligible(db, recipient):
        return f"{recipient.username} is this board's shared guest account, which has no mailbox."
    if recipient.disabled_at is not None:
        return f"{recipient.username}'s account is disabled, so it can't receive mail."
    if recipient.pending_approval:
        return f"{recipient.username}'s account is still waiting for approval, so it can't receive mail yet."
    return None


def mail_recipient_bounce_reason(db: Database, recipient: User) -> str | None:
    """`mail_recipient_refusal` as the reason code a Link bounce carries
    (`netbbs.link.events`), or `None` when the account takes mail.

    The guest account bounces `no_mailbox`. A disabled account and a
    pending signup share `recipient_unavailable` (issue #818), which says
    only that the account takes no mail at the moment: whether an account
    here is disabled is this node's business, not another node's, and
    both states can end."""
    if mail_recipient_refusal(db, recipient) is None:
        return None
    if guest_is_eligible(db, recipient):
        return "no_mailbox"
    return "recipient_unavailable"


# -- blocked senders (issue #817) -----------------------------------------------
#
# A block is the recipient's: one account refusing mail from one sender. It is
# checked beside `mail_recipient_refusal`, which says whether an account takes
# mail at all, by `mail_sender_refusal`, which says whether it takes mail from
# *this* sender: `send_mail` and the To prompt for local mail, and
# `netbbs.link.mail.deliver_link_message` for Link mail, which bounces
# `blocked_by_recipient`.
#
# The sender is told. A blocked letter is refused at the To prompt and at Send
# with "<name> does not accept mail from you", and a Link letter bounces with
# the same words, rather than being accepted and dropped. Mail promises that
# nothing is lost in silence (§6.4, §10.5), and a silent drop would make the
# one kind of refusal a sender can do something about -- stop writing -- the
# only one they never hear of.
#
# Two senders cannot be blocked. Mail from the system (`send_system_mail`) is
# the BBS telling an account about itself and has no sender to block. And a
# SysOp of this node is not blockable either: the SysOp answers for the
# accounts on the node and must be able to reach them, and a block would buy
# no privacy from the person who runs the database it is stored in.

SENDER_BLOCK_REFUSAL = "{name} does not accept mail from you."


class MailSenderBlocked(MailRecipientRefused):
    """Raised by `send_mail` when the recipient has blocked the sender
    (issue #817)."""


class MailBlockError(MailError):
    """A block that cannot be made: the sender is the caller, the system or
    one of this node's SysOps (issue #817)."""


@dataclass(frozen=True)
class MailBlock:
    """One blocked sender: a local account by id, or a Link sender by its
    `user@<home-node-fingerprint>` address."""
    id: int
    blocked_user_id: int | None
    blocked_address: str | None
    created_at: str


def sender_unblockable_reason(db: Database, blocker: User, sender: User) -> str | None:
    """Why `blocker` may not block local `sender`, or `None` when they may."""
    if sender.id == blocker.id:
        return "You can't block your own mail."
    if is_usable_sysop(sender):
        return f"{sender.username} runs this BBS; mail from its SysOp can't be blocked."
    return None


def block_local_sender(db: Database, blocker: User, sender: User) -> bool:
    """Refuse `sender`'s mail to `blocker` from now on. `False` if it was
    already blocked. Mail already in the inbox stays where it is."""
    reason = sender_unblockable_reason(db, blocker, sender)
    if reason is not None:
        raise MailBlockError(reason)
    cursor = db.connection.execute(
        "INSERT OR IGNORE INTO mail_blocks (user_id, blocked_user_id, created_at) VALUES (?, ?, ?)",
        (blocker.id, sender.id, utc_now_iso()),
    )
    db.connection.commit()
    return cursor.rowcount > 0


def block_link_sender(db: Database, blocker: User, address: str) -> bool:
    """Refuse mail from the Link sender `address` (`user@<fingerprint>`) to
    `blocker` from now on. `False` if it was already blocked."""
    cursor = db.connection.execute(
        "INSERT OR IGNORE INTO mail_blocks (user_id, blocked_address, created_at) VALUES (?, ?, ?)",
        (blocker.id, address, utc_now_iso()),
    )
    db.connection.commit()
    return cursor.rowcount > 0


def unblock_local_sender(db: Database, blocker: User, sender_user_id: int) -> bool:
    cursor = db.connection.execute(
        "DELETE FROM mail_blocks WHERE user_id = ? AND blocked_user_id = ?", (blocker.id, sender_user_id)
    )
    db.connection.commit()
    return cursor.rowcount > 0


def unblock_link_sender(db: Database, blocker: User, address: str) -> bool:
    cursor = db.connection.execute(
        "DELETE FROM mail_blocks WHERE user_id = ? AND blocked_address = ?", (blocker.id, address)
    )
    db.connection.commit()
    return cursor.rowcount > 0


def unblock(db: Database, blocker: User, block: MailBlock) -> bool:
    if block.blocked_user_id is not None:
        return unblock_local_sender(db, blocker, block.blocked_user_id)
    assert block.blocked_address is not None
    return unblock_link_sender(db, blocker, block.blocked_address)


def list_mail_blocks(db: Database, blocker: User) -> list[MailBlock]:
    """`blocker`'s blocked senders, most recently blocked first."""
    rows = db.connection.execute(
        "SELECT id, blocked_user_id, blocked_address, created_at FROM mail_blocks "
        "WHERE user_id = ? ORDER BY id DESC",
        (blocker.id,),
    ).fetchall()
    return [
        MailBlock(
            id=row["id"], blocked_user_id=row["blocked_user_id"],
            blocked_address=row["blocked_address"], created_at=row["created_at"],
        )
        for row in rows
    ]


def blocks_local_sender(db: Database, recipient: User, sender: User) -> bool:
    return db.connection.execute(
        "SELECT 1 FROM mail_blocks WHERE user_id = ? AND blocked_user_id = ?", (recipient.id, sender.id)
    ).fetchone() is not None


def blocks_link_sender(db: Database, recipient: User, address: str) -> bool:
    return db.connection.execute(
        "SELECT 1 FROM mail_blocks WHERE user_id = ? AND blocked_address = ?", (recipient.id, address)
    ).fetchone() is not None


def mail_sender_refusal(
    db: Database, recipient: User, *, sender: User | None = None, sender_address: str | None = None,
) -> str | None:
    """Why `recipient` takes no mail from this sender -- a local account
    (`sender`) or a Link sender (`sender_address`, `user@<fingerprint>`) --
    or `None`. The sender-specific half of the recipient check (issue
    #817), made wherever `mail_recipient_refusal` is made for a letter
    with a sender: `send_mail`, the To prompt, and Link delivery. System
    mail has no sender and is never refused by it; neither is a SysOp of
    this node."""
    if sender is not None:
        if is_usable_sysop(sender) or not blocks_local_sender(db, recipient, sender):
            return None
    elif sender_address is None or not blocks_link_sender(db, recipient, sender_address):
        return None
    return SENDER_BLOCK_REFUSAL.format(name=recipient.username)


class MailboxFullError(Exception):
    """
    Raised when `send_mail` would otherwise have to silently destroy an
    *unread* message to make room (design doc: "never silently
    drop something a user hasn't seen yet"). The caller is expected to
    report this back to the sender as a bounce -- the message is never
    stored.
    """


@dataclass(frozen=True)
class MailMessage:
    id: int
    sender_user_id: int | None
    sender_label: str
    recipient_user_id: int | None
    subject: str
    body: str
    created_at: str
    read_at: str | None
    sender_deleted_at: str | None
    recipient_deleted_at: str | None
    # `user@<home-node-fingerprint>` for mail this node sent over Link,
    # whose `recipient_user_id` is NULL (issue #805).
    recipient_remote_address: str | None = None
    # Link mail only (issue #806): 'pending', 'delivered', 'bounced' or
    # 'expired', and the recipient node's reason code for a bounce. Local
    # mail has neither.
    link_delivery_status: str | None = None
    link_delivery_reason: str | None = None
    # When a still-pending Link letter was left at a relay for the
    # recipient's node to collect (issue #874); NULL otherwise.
    link_relay_handoff_at: str | None = None
    # Sent by the BBS itself (issue #819): no sender account, no reply,
    # never carried over Link. See `send_system_mail`.
    from_system: bool = False
    # The local recipient's name when their account was deleted (issue
    # #818): `recipient_user_id` is NULL then, and the sender's Sent copy
    # still says who the letter went to. NULL while the account exists.
    recipient_label: str | None = None
    # When the recipient moved it to their Kept folder (issue #828): the
    # mailbox cap never evicts it. NULL for every other letter.
    kept_at: str | None = None
    # One copy of a letter to several people (issue #827): the id every
    # copy shares, and the JSON of everyone it went to (`group_members`).
    # Both NULL for a letter to one person.
    mail_group_id: str | None = None
    mail_group_to: str | None = None
    # When the recipient first opened it (issue #829): the time a read
    # receipt shows. Mark unread clears `read_at`, never this.
    first_read_at: str | None = None
    # Whether the sender and the recipient both shared read receipts at
    # that first reading (issue #922). A reading made while either did not
    # is never a receipt, whatever they share later.
    first_read_shared: bool = False
    # When the sender last sent this Link letter again after it bounced or
    # expired (issue #919); NULL otherwise. The new letter is a row of its
    # own, and this one keeps its status and reason.
    resent_at: str | None = None

    @property
    def is_read(self) -> bool:
        return self.read_at is not None


def validate_mail_fields(subject: object, body: object) -> str:
    """The one check every letter's subject and body pass, whether a
    caller wrote it here or it arrived over Link (issue #808): both text,
    a subject that is not blank, both within their byte limits and
    encodable as UTF-8 (JSON can carry a lone surrogate, which SQLite
    cannot store). Returns the subject stripped, as it is stored."""
    if not isinstance(subject, str) or not isinstance(body, str):
        raise MailError("subject and body must be text")
    subject = subject.strip()
    if not subject:
        raise MailError("subject cannot be blank")
    try:
        subject_bytes = len(subject.encode("utf-8"))
        body_bytes = len(body.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise MailError("subject and body must be valid text") from exc
    if subject_bytes > MAX_MAIL_SUBJECT_BYTES:
        raise MailError(f"subject cannot exceed {MAX_MAIL_SUBJECT_BYTES} bytes, got {subject_bytes}")
    if body_bytes > MAX_MAIL_BODY_BYTES:
        raise MailError(f"body cannot exceed {MAX_MAIL_BODY_BYTES} bytes, got {body_bytes}")
    return subject


def send_mail(
    db: Database, sender: User, recipient: User, subject: str, body: str, *, files: list[FileRef] = (),
) -> MailMessage:
    """
    Send one message from `sender` to `recipient`.

    Enforces `MAX_MAIL_PER_RECIPIENT`: if the recipient's inbox is
    already at the cap, the oldest **already-read** message is
    hard-deleted to make room (same drop-oldest precedent as
    `netbbs.chat.hub.ChatHub`'s queues and `netbbs.chat.mailbox.
    MessageMailbox`'s own per-session cap). If the inbox is entirely
    unread and full, raises `MailboxFullError` instead of destroying an
    unread message -- deterministic, matching the design doc's own
    acceptance criterion.

    Raises `MailRecipientRefused` for a recipient that takes no mail (the
    guest account, issue #816), and `MailSenderBlocked` for one that has
    blocked `sender` (issue #817). Whether the sender may write mail at all
    is not checked here: that is the mail screen's gate
    (`mail_access_refusal`).

    `files` are the files in this node's file areas the letter points at
    (issue #830, `netbbs.file_refs`): `MailError` if `sender` cannot open
    one, `MailRecipientRefused` if `recipient` may not read its area.
    """
    message = send_mail_without_commit(db, sender, recipient, subject, body, files=files)
    db.connection.commit()
    return message


def send_mail_without_commit(
    db: Database, sender: User, recipient: User, subject: str, body: str,
    *, group: LetterGroup | None = None, files: list[FileRef] = (),
) -> MailMessage:
    """`send_mail` without the commit, for a letter to several people
    (issue #827), whose copies are written in one transaction. `group`
    makes this one copy of such a letter; `files` are written with it, one
    reference row each (issue #830)."""
    subject = validate_mail_fields(subject, body)
    refusal = mail_recipient_refusal(db, recipient)
    if refusal is not None:
        raise MailRecipientRefused(refusal)
    blocked = mail_sender_refusal(db, recipient, sender=sender)
    if blocked is not None:
        raise MailSenderBlocked(blocked)
    files = list(files)
    if files:
        problem = sender_ref_problem(db, sender, files)
        if problem is not None:
            raise MailError(problem)
        problem = recipient_ref_problem(db, recipient, files)
        if problem is not None:
            raise MailRecipientRefused(problem)

    _make_room_if_needed(db, recipient)

    created_at = utc_now_iso()
    db.connection.execute(
        """
        INSERT INTO mail_messages
            (sender_user_id, sender_label, recipient_user_id, subject, body, created_at,
             mail_group_id, mail_group_to)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sender.id, sender.username, recipient.id, subject, body, created_at,
            group.id if group else None, group.to if group else None,
        ),
    )
    row = db.connection.execute(
        "SELECT * FROM mail_messages WHERE id = last_insert_rowid()"
    ).fetchone()
    index_mail_without_commit(db, row["id"])
    if files:
        write_mail_refs_without_commit(db, row["id"], files)
    return _row_to_message(row)


def send_system_mail(db: Database, recipient: User, subject: str, body: str) -> MailMessage:
    """
    Send one message from the BBS itself to `recipient` (issue #819): a
    notice no person wrote, such as a moderation rejection. Before this a
    rejection came from the moderator's own account, so Reply went to
    them personally.

    The row has no sender account (`sender_user_id` NULL) and
    `from_system` set; the flag is what the mailbox shows as
    `SYSTEM_SENDER_LABEL` and what refuses a reply, so no account can pass
    for it by its name. Nobody's Sent folder holds it, so its sender side
    is deleted from the start and the recipient's delete removes the row.
    It is local by construction -- no remote address, no Link event -- and
    never leaves this node.

    The same limits and cap as `send_mail`: a system message counts
    toward the recipient's `MAX_MAIL_PER_RECIPIENT`, but is the first read
    message evicted to make room (`make_room`), and a mailbox full of
    unread mail raises `MailboxFullError` rather than lose anything. A
    recipient that takes no mail raises `MailRecipientRefused`, as in
    `send_mail` (issue #816). A recipient's blocked senders are not
    consulted (issue #817): system mail has no sender to block.
    """
    message = send_system_mail_without_commit(db, recipient, subject, body)
    db.connection.commit()
    return message


def send_system_mail_without_commit(
    db: Database, recipient: User, subject: str, body: str, *, group: LetterGroup | None = None,
    files: list[FileRef] = (),
) -> MailMessage:
    """`send_system_mail` without the commit: one copy of a notice to all
    callers (issue #827) is written in the same transaction as the rest.
    `files` (issue #830) are the notice's file references, which each
    recipient must be able to open; the SysOp writing it checked they can
    open them at the console."""
    subject = validate_mail_fields(subject, body)
    refusal = mail_recipient_refusal(db, recipient)
    if refusal is not None:
        raise MailRecipientRefused(refusal)
    files = list(files)
    if files:
        problem = recipient_ref_problem(db, recipient, files)
        if problem is not None:
            raise MailRecipientRefused(problem)

    _make_room_if_needed(db, recipient)

    created_at = utc_now_iso()
    db.connection.execute(
        """
        INSERT INTO mail_messages
            (sender_user_id, sender_label, recipient_user_id, subject, body, created_at,
             sender_deleted_at, from_system, mail_group_id, mail_group_to)
        VALUES (NULL, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            SYSTEM_SENDER_LABEL, recipient.id, subject, body, created_at, created_at,
            group.id if group else None, group.to if group else None,
        ),
    )
    row = db.connection.execute(
        "SELECT * FROM mail_messages WHERE id = last_insert_rowid()"
    ).fetchone()
    index_mail_without_commit(db, row["id"])
    if files:
        write_mail_refs_without_commit(db, row["id"], files)
    return _row_to_message(row)


def _make_room_if_needed(db: Database, recipient: User) -> None:
    if not make_room(db, recipient):
        raise MailboxFullError(
            f"{recipient.username!r}'s mailbox is full and every message is unread or kept"
        )


def make_room(db: Database, recipient: User) -> bool:
    """Make room for one more message in `recipient`'s inbox if it is at
    `MAX_MAIL_PER_RECIPIENT`, by evicting the oldest already-read message.
    `False` when it is full and every message is unread: nothing is
    evicted then, and the new message must not be stored. Local and Link
    delivery both come through here, so the rule is one rule.

    A read message from the system goes before any read letter (issue
    #819): a notice the BBS sent is not to push out mail a person wrote.
    A kept letter (issue #828) is never evicted, read or not. It still
    counts toward the cap, so a mailbox full of unread and kept mail
    refuses new mail rather than lose any of it.

    Each eviction is counted for the owner (issue #818), who is told at
    their next main menu how many old messages went
    (`pending_eviction_notice`); nothing about which ones, since the
    notice line is all that is left of them."""
    if inbox_count(db, recipient) < MAX_MAIL_PER_RECIPIENT:
        return True

    oldest_read = db.connection.execute(
        """
        SELECT id, sender_deleted_at FROM mail_messages
        WHERE recipient_user_id = ? AND recipient_deleted_at IS NULL AND read_at IS NOT NULL
          AND kept_at IS NULL
        ORDER BY from_system DESC, id ASC LIMIT 1
        """,
        (recipient.id,),
    ).fetchone()
    if oldest_read is None:
        return False
    db.connection.execute(
        """
        INSERT INTO mail_eviction_notices (user_id, evicted) VALUES (?, 1)
        ON CONFLICT (user_id) DO UPDATE SET evicted = evicted + 1
        """,
        (recipient.id,),
    )
    # Not committed here: the letter it makes room for is written in the
    # same transaction, and a letter to all callers (issue #827) writes
    # every copy in one.
    _hard_delete_or_mark(
        db, oldest_read["id"], sender_deleted_at=oldest_read["sender_deleted_at"],
        recipient_deleted_at=utc_now_iso(), commit=False,
    )
    return True


def mail_has_room(db: Database, recipient: User) -> bool:
    """Whether `make_room` would find room for one more letter in
    `recipient`'s inbox, asked without evicting anything: a letter to
    several people (issue #827) checks every copy before it writes one."""
    if inbox_count(db, recipient) < MAX_MAIL_PER_RECIPIENT:
        return True
    return db.connection.execute(
        """
        SELECT 1 FROM mail_messages
        WHERE recipient_user_id = ? AND recipient_deleted_at IS NULL AND read_at IS NOT NULL
          AND kept_at IS NULL
        LIMIT 1
        """,
        (recipient.id,),
    ).fetchone() is not None


def mailbox_full_text(recipient: User) -> str:
    """What a sender is told about `recipient`'s full mailbox."""
    return f"{recipient.username}'s mailbox is full and cannot accept new mail right now."


def inbox_count(db: Database, user: User) -> int:
    """How many messages count toward `user`'s `MAX_MAIL_PER_RECIPIENT`:
    every one in the inbox they have not deleted, read or not, kept (issue
    #828) or not."""
    return db.connection.execute(
        "SELECT COUNT(*) AS n FROM mail_messages WHERE recipient_user_id = ? AND recipient_deleted_at IS NULL",
        (user.id,),
    ).fetchone()["n"]


def pending_eviction_notice(db: Database, user: User) -> tuple[str | None, int]:
    """The line telling `user` that old read mail was removed to make room
    (issue #818), and the count to acknowledge once it is on screen; `None`
    and 0 when nothing was. Nothing is marked here: a caller who drops
    before the menu is drawn is told next time."""
    row = db.connection.execute(
        "SELECT evicted FROM mail_eviction_notices WHERE user_id = ?", (user.id,)
    ).fetchone()
    if row is None or row["evicted"] <= 0:
        return None, 0
    evicted = row["evicted"]
    what = "your oldest read message was" if evicted == 1 else f"your {evicted} oldest read messages were"
    return (
        f"Your mailbox was full ({MAX_MAIL_PER_RECIPIENT} messages), so {what} removed to make room "
        "for new mail. Unread and kept mail is never removed.",
        evicted,
    )


def acknowledge_eviction_notice(db: Database, user: User, evicted: int) -> None:
    """`user` has been told of `evicted` removals. Subtracted rather than
    cleared, so a removal made while the notice was on its way is told
    next time."""
    if evicted <= 0:
        return
    db.connection.execute(
        "UPDATE mail_eviction_notices SET evicted = evicted - ? WHERE user_id = ?", (evicted, user.id)
    )
    db.connection.execute("DELETE FROM mail_eviction_notices WHERE user_id = ? AND evicted <= 0", (user.id,))
    db.connection.commit()


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
    view of, newest arrival first.

    Arrival, not `created_at`: Link mail keeps the time its sender wrote
    it (issue #808), so a letter that took days to arrive would otherwise
    sort below mail read long ago and look old while still unread."""
    rows = db.connection.execute(
        """
        SELECT * FROM mail_messages
        WHERE recipient_user_id = ? AND recipient_deleted_at IS NULL
        ORDER BY id DESC
        """,
        (user.id,),
    ).fetchall()
    return [_row_to_message(row) for row in rows]


def list_sent(db: Database, user: User) -> list[MailMessage]:
    """Every message `user` has sent that they haven't deleted their
    own view of, newest first (by id, the order they were written)."""
    rows = db.connection.execute(
        """
        SELECT * FROM mail_messages
        WHERE sender_user_id = ? AND sender_deleted_at IS NULL
        ORDER BY id DESC
        """,
        (user.id,),
    ).fetchall()
    return [_row_to_message(row) for row in rows]


#: How many letters, newest first, `recent_correspondents` looks through.
#: Bounds the work on a full mailbox; the people named in the newest few
#: hundred letters are the recent ones.
RECENT_CORRESPONDENT_SCAN = 1000


def recent_correspondents(db: Database, user: User, *, limit: int = 10) -> list[int | str]:
    """The people `user` last had mail from or sent mail to (issue #826),
    newest first and each once, from the letters still in their own Inbox
    and Sent: a local account by its id, a Link correspondent by the stored
    `user@<home-node-fingerprint>`.

    System mail, mail from an account since deleted, and letters to
    oneself name no one to write to and are left out. Whether each one
    still takes mail is the caller's business: this only says who they
    were."""
    rows = db.connection.execute(
        """
        SELECT id, sender_user_id AS account, sender_label AS address, from_system
        FROM mail_messages WHERE recipient_user_id = ? AND recipient_deleted_at IS NULL
        UNION ALL
        SELECT id, recipient_user_id AS account, recipient_remote_address AS address, 0
        FROM mail_messages WHERE sender_user_id = ? AND sender_deleted_at IS NULL
        ORDER BY id DESC LIMIT ?
        """,
        (user.id, user.id, RECENT_CORRESPONDENT_SCAN),
    ).fetchall()
    found: list[int | str] = []
    for row in rows:
        if len(found) >= limit:
            break
        if row["account"] is not None:
            who: int | str = row["account"]
            if who == user.id:
                continue
        elif row["from_system"] or row["address"] is None or split_link_address(row["address"]) is None:
            continue
        else:
            who = row["address"]
        if who not in found:
            found.append(who)
    return found


# -- one letter to several people (issue #827) --------------------------------
#
# A letter to several people is written as one ordinary letter per
# recipient: each copy has its own delivery state, cap handling, block and
# refusal, and every other rule in this module applies to it unchanged. What
# ties the copies together is `mail_group_id`, the same on each, and
# `mail_group_to`, the list of everyone the letter went to, which each copy
# shows as its To. The list is stored with every copy rather than read back
# from the other copies: a copy deleted on both sides is gone, and the
# letter's To must not lose a name because of it.
#
# There is no blind copy: everyone a letter went to sees everyone else.
#
# `mail_group_to` is JSON: a list with one object per recipient, in the order
# they were typed -- `{"id": 5, "name": "bob"}` for an account here (shown by
# its current name, or `name` once the account is gone), `{"address":
# "carol@<fingerprint>"}` for someone on another BBS, `{"name": "dave"}` for
# a name at this BBS that a letter from another BBS listed and no account
# here has -- or `{"all": true}` for SysOp mail to all callers.

#: The most people one letter can go to, local and Link together. A letter
#: is written once per recipient, so this bounds what one Send writes.
MAX_MAIL_RECIPIENTS = 20

#: What a copy of SysOp mail to all callers shows as its To.
ALL_CALLERS_LABEL = "Everyone on this BBS"


@dataclass(frozen=True)
class LetterGroup:
    """What every copy of one letter to several people carries: its
    `mail_group_id` and `mail_group_to` (encoded by `encode_group_to`)."""
    id: str
    to: str


@dataclass(frozen=True)
class GroupMember:
    """One entry of a letter's To (see the section comment)."""
    user_id: int | None = None
    name: str | None = None
    address: str | None = None


def new_mail_group_id() -> str:
    """A fresh id for one letter to several people. Chosen when the letter
    is started and kept with its draft, so a letter sent once cannot be
    sent again by sending its draft (`letter_already_sent`)."""
    return secrets.token_hex(8)


def encode_group_to(members: list[GroupMember] | None) -> str:
    """`mail_group_to` for `members`, or for all callers when `None`."""
    if members is None:
        return json.dumps({"all": True})
    entries = []
    for member in members:
        if member.address is not None:
            entries.append({"address": member.address})
        elif member.user_id is not None:
            entries.append({"id": member.user_id, "name": member.name})
        else:
            entries.append({"name": member.name})
    return json.dumps(entries)


def is_to_all_callers(message: MailMessage) -> bool:
    """Whether `message` is a copy of SysOp mail to all callers."""
    if message.mail_group_to is None:
        return False
    try:
        return json.loads(message.mail_group_to) == {"all": True}
    except ValueError:
        return False


def group_members(message: MailMessage) -> list[GroupMember] | None:
    """Everyone the letter `message` is a copy of went to, in order, or
    `None` for a letter to one person and for mail to all callers."""
    if message.mail_group_to is None:
        return None
    try:
        entries = json.loads(message.mail_group_to)
    except ValueError:
        return None
    if not isinstance(entries, list):
        return None
    members: list[GroupMember] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        address, user_id, name = entry.get("address"), entry.get("id"), entry.get("name")
        if isinstance(address, str):
            members.append(GroupMember(address=address))
        elif isinstance(user_id, int) and isinstance(name, str):
            members.append(GroupMember(user_id=user_id, name=name))
        elif isinstance(name, str):
            members.append(GroupMember(name=name))
    return members


def group_member_label(db: Database, member: GroupMember) -> str:
    """One recipient as a copy's To shows them: an account by its current
    name (the name it had, once it is deleted), a Link address with its
    node's current name."""
    if member.address is not None:
        return link_address_display_label(db, member.address)
    if member.user_id is not None:
        account = get_user_by_id(db, member.user_id)
        if account is not None:
            return account.username
    return member.name or "?"


def group_to_label(db: Database, message: MailMessage) -> str | None:
    """The whole To of a copy of a letter to several people -- "bob,
    carol@Farpoint" -- or `ALL_CALLERS_LABEL`; `None` for a letter to one
    person."""
    if is_to_all_callers(message):
        return ALL_CALLERS_LABEL
    members = group_members(message)
    if members is None:
        return None
    return ", ".join(group_member_label(db, member) for member in members)


def letter_already_sent(db: Database, sender: User | None, group_id: str) -> bool:
    """Whether a letter with this group id was already sent (issue #827):
    a letter to several people cannot go out twice because its draft was
    sent again. `sender` `None` asks about mail to all callers from the
    system."""
    if sender is None:
        row = db.connection.execute(
            "SELECT 1 FROM mail_messages WHERE mail_group_id = ? AND from_system = 1 LIMIT 1", (group_id,),
        ).fetchone()
    else:
        row = db.connection.execute(
            "SELECT 1 FROM mail_messages WHERE mail_group_id = ? AND sender_user_id = ? LIMIT 1",
            (group_id, sender.id),
        ).fetchone()
    return row is not None


def sent_group_copies(db: Database, user: User, message: MailMessage) -> list[MailMessage]:
    """Every copy of the letter `message` is one of that is still in
    `user`'s Sent folder, in the order written; just `message` for a letter
    to one person."""
    if message.mail_group_id is None:
        return [message]
    rows = db.connection.execute(
        """
        SELECT * FROM mail_messages
        WHERE mail_group_id = ? AND sender_user_id = ? AND sender_deleted_at IS NULL
        ORDER BY id
        """,
        (message.mail_group_id, user.id),
    ).fetchall()
    return [_row_to_message(row) for row in rows] or [message]


ALREADY_SENT_TEXT = "This letter was already sent; it is in your Sent folder."


@dataclass(frozen=True)
class AllCallersResult:
    """What `send_to_all_callers` did: the names of the callers it reached
    and of those whose mailbox was full, and how many accounts take no mail
    at all and were left out. `no_file_access` names the callers skipped
    because a file the letter points at is in an area they may not read
    (issue #830)."""
    sent_to: tuple[str, ...]
    mailbox_full: tuple[str, ...]
    left_out: int
    no_file_access: tuple[str, ...] = ()


def all_callers_recipients(db: Database, sender: User | None) -> tuple[list[User], int]:
    """Who SysOp mail to all callers goes to (issue #827): every account
    the To prompt's address book would offer (`mail_recipient_refusal`,
    `mail_sender_refusal`) but the sender's own; and how many accounts were
    left out. Mail from the system goes to the SysOp too."""
    recipients, left_out = [], 0
    for account in list_users(db):
        if sender is not None and account.id == sender.id:
            continue
        if mail_recipient_refusal(db, account) is not None or (
            sender is not None and mail_sender_refusal(db, account, sender=sender) is not None
        ):
            left_out += 1
            continue
        recipients.append(account)
    return recipients, left_out


def send_to_all_callers(
    db: Database, subject: str, body: str, *, group_id: str, sender: User | None,
    files: list[FileRef] = (),
) -> AllCallersResult:
    """SysOp mail to all callers (issue #827): one copy per account that
    takes mail, from `sender`'s own account -- which callers can reply to --
    or, with `sender` `None`, from the system, which nobody can.

    Every copy is an ordinary letter under the ordinary rules: each
    recipient's cap applies, and one whose mailbox is full of unread and
    kept mail is named in the result rather than sent to. All copies are
    written in one transaction, and a `group_id` already sent raises
    `MailError`, so sending the same letter again -- a retry after a
    dropped connection, a kept draft sent twice -- reaches nobody twice.

    `files` (issue #830) are files the letter points at. A caller who may
    not read the area one is in is skipped, and named in the result, as a
    caller with a full mailbox is: a letter to everyone is not held back by
    a few, and nobody is sent a file they cannot open."""
    subject = validate_mail_fields(subject, body)
    if letter_already_sent(db, sender, group_id):
        raise MailError(ALREADY_SENT_TEXT)
    files = list(files)
    if files and sender is not None:
        problem = sender_ref_problem(db, sender, files)
        if problem is not None:
            raise MailError(problem)
    recipients, left_out = all_callers_recipients(db, sender)
    group = LetterGroup(id=group_id, to=encode_group_to(None))
    sent, full, no_access = [], [], []
    try:
        for recipient in recipients:
            if not mail_has_room(db, recipient):
                full.append(recipient.username)
                continue
            if files and recipient_ref_problem(db, recipient, files) is not None:
                no_access.append(recipient.username)
                continue
            if sender is None:
                send_system_mail_without_commit(db, recipient, subject, body, group=group, files=files)
            else:
                send_mail_without_commit(db, sender, recipient, subject, body, group=group, files=files)
            sent.append(recipient.username)
        db.connection.commit()
    except BaseException:
        db.connection.rollback()
        raise
    return AllCallersResult(
        sent_to=tuple(sent), mailbox_full=tuple(full), left_out=left_out, no_file_access=tuple(no_access),
    )


# -- who a letter is from and to, as a reader sees it -----------------------
#
# One answer for the mailbox's list and message view and for the main
# menu's Find (issue #824), which matches a letter by these names.


def split_link_address(technical_address: str) -> tuple[str, str] | None:
    """`(user, fingerprint)` of a stored `user@<home-node-fingerprint>`, or
    `None` for a local name. Split at the last `@`: the user half comes from
    a peer's signed payload and nothing holds it to the username grammar,
    while a fingerprint never contains one."""
    user, separator, fingerprint = technical_address.rpartition("@")
    if not separator or not is_node_fingerprint_shape(fingerprint):
        return None
    return user, fingerprint


def link_address_display_label(db: Database, technical_address: str) -> str:
    """A stored Link address with its home node resolved to the name that
    node goes by now (never stored: a node can rename); a local name is
    returned as it is."""
    # Deferred: `netbbs.link`'s package imports its mail module, which
    # imports this one.
    from netbbs.link.node_profiles import identity_for_fingerprint, link_address_label

    split = split_link_address(technical_address)
    if split is None:
        return technical_address
    user_part, fingerprint = split
    return link_address_label(user_part, identity_for_fingerprint(db, fingerprint).label)


def sender_display_label(db: Database, message: MailMessage) -> str:
    """Who a received letter is from: `SYSTEM_SENDER_LABEL` for mail the
    BBS sent (issue #819) -- by its flag, never by the stored name -- else
    the sender's name or Link address."""
    if message.from_system:
        return SYSTEM_SENDER_LABEL
    return link_address_display_label(db, message.sender_label)


def recipient_display_label(db: Database, message: MailMessage) -> str:
    """Who a sent letter went to: everyone, for a copy of a letter to
    several people (issue #827, `group_to_label`), else the one recipient
    (`copy_recipient_label`)."""
    group = group_to_label(db, message)
    return group if group is not None else copy_recipient_label(db, message)


def copy_recipient_label(db: Database, message: MailMessage) -> str:
    """Who this one copy went to: the remote address of Link mail (issue
    #805), else the local recipient's current name, or the name it had when
    its account was deleted (issue #818)."""
    if message.recipient_remote_address is not None:
        return link_address_display_label(db, message.recipient_remote_address)
    recipient = get_user_by_id(db, message.recipient_user_id) if message.recipient_user_id is not None else None
    if recipient is not None:
        return recipient.username
    if message.recipient_label:
        return f"{message.recipient_label} (deleted account)"
    return "(deleted account)"


def unread_count(db: Database, user: User) -> int:
    row = db.connection.execute(
        """
        SELECT COUNT(*) AS n FROM mail_messages
        WHERE recipient_user_id = ? AND recipient_deleted_at IS NULL AND read_at IS NULL
        """,
        (user.id,),
    ).fetchone()
    return row["n"]


MailKey = tuple[int, str]


def inbox_mail_keys(db: Database, user: User, *, unread_only: bool = False) -> set[MailKey]:
    """`(id, created_at)` of every letter in `user`'s inbox (or only the
    unread ones), for telling which letters are new since the last look
    (issue #823, `netbbs.net.mail_arrivals`).

    The pair, not the id alone: `mail_messages` has no AUTOINCREMENT, so a
    letter removed from both sides can free the highest id for the next
    one, and an id already seen would hide that letter."""
    rows = db.connection.execute(
        f"""
        SELECT id, created_at FROM mail_messages
        WHERE recipient_user_id = ? AND recipient_deleted_at IS NULL
        {"AND read_at IS NULL" if unread_only else ""}
        """,
        (user.id,),
    ).fetchall()
    return {(row["id"], row["created_at"]) for row in rows}


def get_inbox_letters(db: Database, user: User, mail_ids: list[int]) -> list[MailMessage]:
    """The letters among `mail_ids` still in `user`'s inbox, in arrival
    order -- whatever was deleted in the meantime is left out."""
    if not mail_ids:
        return []
    rows = db.connection.execute(
        f"""
        SELECT * FROM mail_messages
        WHERE recipient_user_id = ? AND recipient_deleted_at IS NULL AND id IN ({",".join("?" * len(mail_ids))})
        ORDER BY id
        """,
        (user.id, *mail_ids),
    ).fetchall()
    return [_row_to_message(row) for row in rows]


@dataclass(frozen=True)
class InboxSize:
    """How full one account's inbox is (issue #820), for the SysOp. Counts
    only: the SysOp console never shows a letter's sender, subject or body."""
    user_id: int
    username: str
    total: int
    unread: int
    # Notices from the BBS itself (issue #819); included in `total`.
    system: int
    # Letters the owner keeps (issue #828), read or not; included in
    # `total`, and never evicted.
    kept: int = 0
    # Kept letters among the unread ones, so `evictable` counts each
    # letter once.
    kept_unread: int = 0

    @property
    def read(self) -> int:
        return self.total - self.unread

    @property
    def evictable(self) -> int:
        """Letters the cap may evict to make room: read and not kept. At
        the cap with none, the inbox refuses new mail."""
        return self.read - (self.kept - self.kept_unread)


def inbox_sizes(db: Database) -> list[InboxSize]:
    """Every account with mail in its inbox, the fullest first (by count, then
    by unread mail, which the cap cannot make room by evicting). What counts is
    what counts toward `MAX_MAIL_PER_RECIPIENT`: every message the recipient
    has not deleted, system notices included."""
    rows = db.connection.execute(
        """
        SELECT u.id AS user_id, u.username AS username, COUNT(m.id) AS total,
               SUM(m.read_at IS NULL) AS unread, SUM(m.from_system) AS system,
               SUM(m.kept_at IS NOT NULL) AS kept,
               SUM(m.kept_at IS NOT NULL AND m.read_at IS NULL) AS kept_unread
        FROM mail_messages m JOIN users u ON u.id = m.recipient_user_id
        WHERE m.recipient_deleted_at IS NULL
        GROUP BY u.id
        ORDER BY total DESC, unread DESC, u.username COLLATE NOCASE
        """
    ).fetchall()
    return [
        InboxSize(
            user_id=row["user_id"], username=row["username"], total=row["total"],
            unread=row["unread"] or 0, system=row["system"] or 0,
            kept=row["kept"] or 0, kept_unread=row["kept_unread"] or 0,
        )
        for row in rows
    ]


def mark_read(db: Database, user: User, message: MailMessage) -> MailMessage:
    """No-op (returning `message` unchanged) if `user` isn't the
    recipient, or it's already read -- mirrors
    `netbbs.auth.users.approve_pending_user`'s own no-op-if-unchanged
    shape rather than raising for an ordinary, expected case (re-opening
    a message already read)."""
    if user.id != message.recipient_user_id or message.is_read:
        return message
    read_at = utc_now_iso()
    # The first opening is the read receipt's time (issue #829), and it
    # stays through Mark unread and a later reading. Whether both sides
    # shared receipts is recorded with it (issue #922): a reading made
    # while either did not never becomes a receipt, so neither side can
    # turn receipts on for a moment to see what was read while off.
    # (SQLite evaluates every SET expression against the row as it was.)
    shared = (
        message.sender_user_id is not None
        and _shares_read_receipts_by_id(db, message.recipient_user_id)
        and _shares_read_receipts_by_id(db, message.sender_user_id)
    )
    db.connection.execute(
        """
        UPDATE mail_messages SET read_at = ?,
            first_read_shared = CASE WHEN first_read_at IS NULL THEN ? ELSE first_read_shared END,
            first_read_at = COALESCE(first_read_at, ?)
        WHERE id = ?
        """,
        (read_at, 1 if shared else 0, read_at, message.id),
    )
    db.connection.commit()
    return get_mail(db, user, message.id)


def mark_unread(db: Database, user: User, message: MailMessage) -> MailMessage:
    """`mark_read` undone (issue #810): the message counts as unread again,
    as if it had never been opened. The same no-op shape: `message`
    unchanged if `user` isn't the recipient or it is already unread.

    An unread message is one the mailbox cap will not evict to make room
    (`_make_room_if_needed`), so marking one unread also keeps it -- the
    same protection a message nobody has opened yet gets.

    A read receipt (issue #829) is not taken back: `first_read_at` stays,
    so the sender still sees when it was first read."""
    if user.id != message.recipient_user_id or not message.is_read:
        return message
    db.connection.execute(
        "UPDATE mail_messages SET read_at = NULL WHERE id = ? AND recipient_user_id = ?", (message.id, user.id)
    )
    db.connection.commit()
    return get_mail(db, user, message.id)


# -- read receipts (issue #829) ----------------------------------------------

#: The per-caller preference: "1" (the default) lets senders see when the
#: caller has read their mail, and lets the caller see receipts; "0" does
#: neither.
READ_RECEIPTS_PREFERENCE = "mail_read_receipts"

#: A receipt's states. `RECEIPT_WITHHELD`: the recipient does not share
#: receipts, which the sender is always told. `RECEIPT_HIDDEN`: the sender
#: does not share them, so sees none. The others are what a receipt says;
#: a letter deleted unopened is `RECEIPT_NOT_READ` (issue #922), so the
#: recipient's deletion stays their own.
RECEIPT_READ = "read"
RECEIPT_NOT_READ = "not_read"
RECEIPT_WITHHELD = "withheld"
RECEIPT_HIDDEN = "hidden"


def shares_read_receipts(db: Database, user: User) -> bool:
    """Whether `user` lets senders see when they have read their mail --
    and so sees other people's receipts (reciprocal). On by default."""
    return get_user_preference(db, user, READ_RECEIPTS_PREFERENCE, default="1") != "0"


def _shares_read_receipts_by_id(db: Database, user_id: int) -> bool:
    row = db.connection.execute(
        "SELECT value FROM user_preferences WHERE user_id = ? AND key = ?", (user_id, READ_RECEIPTS_PREFERENCE)
    ).fetchone()
    return row is None or row["value"] != "0"


def set_shares_read_receipts(db: Database, user: User, shares: bool) -> None:
    set_user_preference(db, user, READ_RECEIPTS_PREFERENCE, "1" if shares else "0")


@dataclass(frozen=True)
class ReadReceipt:
    state: str
    # The recipient's first reading, for `RECEIPT_READ` only.
    read_at: str | None = None


def read_receipts(db: Database, sender: User, messages: list[MailMessage]) -> dict[int, ReadReceipt]:
    """What `sender` may see of whether each of their letters was read,
    by letter id. Only local letters `sender` wrote to an account that
    still exists have a receipt: Link mail, system mail and a deleted
    account's letters are left out.

    A reading is a receipt only if both sides shared receipts when it
    happened (`first_read_shared`, issue #922) *and* both share them now: a
    caller who turns receipts off hides the ones already given, turning them
    on again shows those again, but a reading made while either side had
    them off never shows -- so neither can switch receipts on for a moment
    to peek. Such a reading, and a letter deleted unopened, read as
    `RECEIPT_NOT_READ`. A recipient who does not share receipts is
    `RECEIPT_WITHHELD` whatever the sender's own setting -- the one thing
    the sender is always told, so a letter nobody reports as read is never
    taken for one not read yet."""
    local = [
        message for message in messages
        if message.sender_user_id == sender.id and message.recipient_user_id is not None
        and message.recipient_remote_address is None and not message.from_system
    ]
    if not local:
        return {}
    recipient_ids = sorted({message.recipient_user_id for message in local})
    withheld: set[int] = set()
    for chunk in _chunks(recipient_ids):
        placeholders = ",".join("?" * len(chunk))
        withheld.update(
            row["user_id"] for row in db.connection.execute(
                f"SELECT user_id FROM user_preferences WHERE key = ? AND value = '0' AND user_id IN ({placeholders})",
                (READ_RECEIPTS_PREFERENCE, *chunk),
            )
        )
    sender_shares = shares_read_receipts(db, sender)
    receipts: dict[int, ReadReceipt] = {}
    for message in local:
        if message.recipient_user_id in withheld:
            receipt = ReadReceipt(RECEIPT_WITHHELD)
        elif not sender_shares:
            receipt = ReadReceipt(RECEIPT_HIDDEN)
        elif message.first_read_at is not None and message.first_read_shared:
            receipt = ReadReceipt(RECEIPT_READ, message.first_read_at)
        else:
            receipt = ReadReceipt(RECEIPT_NOT_READ)
        receipts[message.id] = receipt
    return receipts


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


# -- managing many letters at once (issue #828) --------------------------------
#
# The mailbox list marks letters and deletes or keeps them together, and
# empties the Inbox of what has been read. Each function acts only on the
# letters among `mail_ids` still on the caller's own side -- anything else,
# someone else's letter included, is skipped, not refused: a list can be a
# moment stale -- and returns how many it acted on.

# Ids per statement: well under SQLite's bound variable limit, and a Sent
# folder, unlike the Inbox, has no cap.
_ID_CHUNK = 400


def _chunks(ids: list[int]) -> list[list[int]]:
    return [ids[start:start + _ID_CHUNK] for start in range(0, len(ids), _ID_CHUNK)]


def delete_letters(db: Database, user: User, mail_ids: list[int], *, sent: bool) -> int:
    """Delete `user`'s own view of each letter in `mail_ids`: their Sent
    copy (`sent`) or their Inbox copy, kept letters included. The one-letter
    rule (`delete_for_recipient`, `delete_for_sender`), applied to many in
    one transaction: a letter whose other side is already gone is removed,
    search entry and all (issue #824); one the other side still has is
    marked, so their delete removes it later."""
    own, own_deleted, other_deleted = (
        ("sender_user_id", "sender_deleted_at", "recipient_deleted_at") if sent
        else ("recipient_user_id", "recipient_deleted_at", "sender_deleted_at")
    )
    now = utc_now_iso()
    count = 0
    for chunk in _chunks(sorted(set(mail_ids))):
        marks = ",".join("?" * len(chunk))
        ids = [
            row["id"] for row in db.connection.execute(
                f"SELECT id FROM mail_messages WHERE {own} = ? AND {own_deleted} IS NULL AND id IN ({marks})",
                (user.id, *chunk),
            )
        ]
        if not ids:
            continue
        count += len(ids)
        held = ",".join("?" * len(ids))
        _delete_letters_without_commit(db, f"id IN ({held}) AND {other_deleted} IS NOT NULL", tuple(ids))
        db.connection.execute(
            f"UPDATE mail_messages SET {own_deleted} = ? WHERE id IN ({held})", (now, *ids),
        )
    db.connection.commit()
    return count


def set_kept(db: Database, user: User, mail_ids: list[int], *, kept: bool) -> int:
    """Move the letters among `mail_ids` in `user`'s Inbox to their Kept
    folder (`kept`), or back to the Inbox (issue #828). A kept letter is
    one the mailbox cap never evicts (`make_room`)."""
    count = 0
    now = utc_now_iso()
    for chunk in _chunks(sorted(set(mail_ids))):
        marks = ",".join("?" * len(chunk))
        if kept:
            cursor = db.connection.execute(
                f"""
                UPDATE mail_messages SET kept_at = ?
                WHERE recipient_user_id = ? AND recipient_deleted_at IS NULL AND kept_at IS NULL
                  AND id IN ({marks})
                """,
                (now, user.id, *chunk),
            )
        else:
            cursor = db.connection.execute(
                f"""
                UPDATE mail_messages SET kept_at = NULL
                WHERE recipient_user_id = ? AND recipient_deleted_at IS NULL AND kept_at IS NOT NULL
                  AND id IN ({marks})
                """,
                (user.id, *chunk),
            )
        count += cursor.rowcount
    db.connection.commit()
    return count


# -- conversations (issue #828) ----------------------------------------------
#
# The mailbox can list a folder by conversation. Replies record no parent
# letter, so a conversation is what a reader would call one: the same
# correspondent and the same subject once its reply and forward prefixes
# are gone -- the prefixes `netbbs.quoting.reply_subject` and
# `forward_subject` add or recognize ("Re:", "Fwd:", "Fw:").

_THREAD_PREFIXES = ("re:", "fwd:", "fw:")


def thread_subject(subject: str) -> str:
    """`subject` as a conversation is told by: every leading "Re:", "Fwd:"
    or "Fw:" removed, however many and in whatever case, and case folded."""
    text = subject.strip()
    while True:
        lowered = text.lower()
        prefix = next((prefix for prefix in _THREAD_PREFIXES if lowered.startswith(prefix)), None)
        if prefix is None:
            return text.casefold()
        text = text[len(prefix):].lstrip()


def thread_key(message: MailMessage, *, sent: bool) -> tuple:
    """Which conversation `message` belongs to in the Sent folder (`sent`)
    or the Inbox: its correspondent -- the recipient of a sent letter, the
    sender of a received one -- and its `thread_subject`. Mail from the
    system is its own correspondent, whatever name an account has."""
    if sent and message.mail_group_to is not None:
        # A letter to several people (issue #827): its correspondents are
        # all of them together.
        who: tuple = ("to-group", message.mail_group_to)
    elif sent:
        who = (
            "to", message.recipient_remote_address.casefold() if message.recipient_remote_address else None,
            message.recipient_user_id, message.recipient_label,
        )
    elif message.from_system:
        who = ("system",)
    elif message.sender_user_id is not None:
        who = ("from", message.sender_user_id)
    else:
        who = ("from", message.sender_label.casefold())
    return (*who, thread_subject(message.subject))


def _hard_delete_or_mark(
    db: Database, mail_id: int, *, sender_deleted_at: str | None, recipient_deleted_at: str | None,
    commit: bool = True,
) -> None:
    if sender_deleted_at is not None and recipient_deleted_at is not None:
        _remove_letters_without_commit(db, [mail_id])
    else:
        db.connection.execute(
            "UPDATE mail_messages SET sender_deleted_at = ?, recipient_deleted_at = ? WHERE id = ?",
            (sender_deleted_at, recipient_deleted_at, mail_id),
        )
    if commit:
        db.connection.commit()


def release_mail_of_deleted_account_without_commit(db: Database, user: User) -> None:
    """The mail side of deleting `user`'s account (issue #818), run by
    `netbbs.auth.users.delete_user` inside its transaction, just before
    the account row goes.

    A letter is one row with two views, the sender's Sent copy and the
    recipient's inbox copy. The deleted account's view of each of its
    letters is deleted as if they had deleted it themselves: a letter
    nobody else can see any more is removed now, and one the other side
    still has is marked, so their own delete removes the row later
    (`_hard_delete_or_mark`) instead of leaving it behind. Before this, a
    deleted recipient took the sender's Sent copy with it (the foreign key
    cascaded), and a deleted sender left rows nobody could ever delete.

    A local letter the deleted account received keeps its name in
    `recipient_label`, so the sender's Sent copy can still say who it went
    to. Outbound Link mail keeps its row as ever: its delivery status is
    still tracked, and a pending one is still sent."""
    now = utc_now_iso()
    conn = db.connection
    # Received mail no one else sees: from Link or the system (no local
    # sender), from an account already deleted, from itself, or deleted
    # by its sender already.
    _delete_letters_without_commit(
        db,
        """
        recipient_user_id = ?
          AND (sender_user_id IS NULL OR sender_user_id = recipient_user_id OR sender_deleted_at IS NOT NULL)
        """,
        (user.id,),
    )
    conn.execute(
        """
        UPDATE mail_messages
        SET recipient_deleted_at = COALESCE(recipient_deleted_at, ?), recipient_label = ?
        WHERE recipient_user_id = ?
        """,
        (now, user.username, user.id),
    )
    # Sent local mail whose recipient no longer has it.
    _delete_letters_without_commit(
        db,
        """
        sender_user_id = ? AND recipient_remote_address IS NULL
          AND (recipient_user_id IS NULL OR recipient_deleted_at IS NOT NULL)
        """,
        (user.id,),
    )
    conn.execute(
        "UPDATE mail_messages SET sender_deleted_at = ? WHERE sender_user_id = ? AND sender_deleted_at IS NULL",
        (now, user.id),
    )


def _delete_letters_without_commit(db: Database, where: str, parameters: tuple) -> None:
    """Delete the letters `where` selects, and their search entries (issue
    #824) with them."""
    ids = [row["id"] for row in db.connection.execute(f"SELECT id FROM mail_messages WHERE {where}", parameters)]
    if ids:
        _remove_letters_without_commit(db, ids)


def _remove_letters_without_commit(db: Database, mail_ids: list[int]) -> None:
    """The one way a letter is deleted for good: its row, its search entry
    (issue #824) and its file references (issue #830) together. Nothing else
    may `DELETE FROM mail_messages`: `mail_messages` has no AUTOINCREMENT, so
    an id freed here can be the next letter's, which must not inherit the
    old one's words or files."""
    db.connection.executemany("DELETE FROM mail_messages WHERE id = ?", [(mail_id,) for mail_id in mail_ids])
    unindex_mail_without_commit(db, mail_ids)
    forget_mail_refs_without_commit(db, mail_ids)


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
        recipient_remote_address=row["recipient_remote_address"],
        link_delivery_status=row["link_delivery_status"],
        link_delivery_reason=row["link_delivery_reason"],
        link_relay_handoff_at=row["link_relay_handoff_at"],
        from_system=bool(row["from_system"]),
        recipient_label=row["recipient_label"],
        kept_at=row["kept_at"],
        mail_group_id=row["mail_group_id"],
        mail_group_to=row["mail_group_to"],
        first_read_at=row["first_read_at"],
        first_read_shared=bool(row["first_read_shared"]),
        resent_at=row["resent_at"],
    )

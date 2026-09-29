"""
Local asynchronous personal mail UI (design doc), wiring
`netbbs.mail`'s core module into the interactive session.

Kept in its own module rather than growing login_flow.py indefinitely --
matches the project's modular-package approach (design doc §3), same
reasoning as chat_flow.py/file_flow.py.

Deliberately does not reuse `netbbs.attestation.format_name_for_resource`
-- that machinery exists for *public* resources (boards/channels/file
areas) where a real name needs to survive a colored-vs-text-only
rendering distinction for onlookers. Mail is a private 1:1 exchange with
no shared audience to forge an identity in front of, so `sender_label`
(a plain denormalized username, see `netbbs.mail`) is shown as-is.

**First module migrated onto the two-lane database
execution model (design doc, issue #57)** -- every function here takes `lane:
DatabaseLane` instead of `db: Database`, and every business-logic call
goes through `await lane.run(func, *args, **kwargs)` rather than a
direct synchronous call. Two consequences worth being explicit about,
both driven by the same underlying cause (a lane owns its own
connection; nothing here holds a `Database` of its own to reach into
directly anymore):

- The mailbox list's rows are drawn synchronously, off the lane, so
  every per-row value that needs a DB read (a sender or recipient
  label, an identity warning, a formatted date) is fetched once per
  reload, via the lane, into plain `_MailRow` cells (`_load_mail_rows`).
  `netbbs.timeutil.resolve_display_preferences` exists specifically for
  this: fetch the node's format/timezone once per reload, not once per
  row.
- `_letter_draft_path` takes no `Database` at all -- it only needs
  the connection's file *path*, not a query, so it reads `lane.path`
  directly (a plain in-memory attribute, see `DatabaseLane.path`'s own
  docstring) rather than going through the lane's worker thread for
  something that was never actually blocking.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from netbbs.auth.users import (
    AuthError, User, get_user_by_id, get_user_by_username, is_usable_sysop, list_users,
)
from netbbs.identity.addressing import is_valid_user_part, user_part_problem
from netbbs.link.boards import LinkContext
from netbbs.link.mail import (
    DELIVERY_STATUS_LABELS, RELAYED_DISPLAY_STATUS, LinkMailError, acknowledge_delivery_notices, compose_link_message,
    delivery_display_status, delivery_explanation, record_resend,
)
from netbbs.link.node_profiles import (
    ambiguous_node_guidance, link_address_label, unknown_node_guidance, unquote_reference,
    identity_for_fingerprint, latest_identity_observation, resolve_stored_peer_reference,
)
from netbbs.mail import (
    GUEST_MAIL_REFUSAL,
    MAILBOX_NEARLY_FULL,
    KeptFullError,
    MAX_KEPT_PER_RECIPIENT,
    MAX_MAIL_BODY_BYTES,
    MAX_MAIL_PER_RECIPIENT,
    MAX_MAIL_RECIPIENTS,
    MAX_MAIL_SUBJECT_BYTES,
    SYSTEM_SENDER_LABEL,
    MailBlock,
    MailBlockError,
    MailboxFullError,
    MailError,
    MailMessage,
    RECEIPT_HIDDEN,
    RECEIPT_NOT_READ,
    RECEIPT_READ,
    RECEIPT_WITHHELD,
    ReadReceipt,
    all_callers_recipients,
    block_link_sender,
    block_local_sender,
    blocks_link_sender,
    blocks_local_sender,
    copy_recipient_label,
    delete_for_recipient,
    delete_for_sender,
    delete_letters,
    get_mail,
    group_members,
    group_to_label,
    is_to_all_callers,
    link_address_display_label,
    list_inbox,
    list_mail_blocks,
    list_sent,
    mail_access_refusal,
    mail_recipient_refusal,
    mail_sender_refusal,
    mark_read,
    mark_unread,
    new_mail_group_id,
    read_receipts,
    recipient_display_label,
    send_mail,
    send_to_all_callers,
    sent_group_copies,
    sender_display_label,
    sender_unblockable_reason,
    set_kept,
    shares_read_receipts,
    split_link_address,
    thread_key,
    unblock,
    unblock_link_sender,
    unblock_local_sender,
)
from netbbs.messaging_preferences import MESSAGES_AND_MAIL_BLOCK_REFUSAL
from netbbs.file_refs import (
    AVAILABLE,
    MAX_FILE_REFS,
    FileRef,
    body_with_link_text,
    mail_refs,
    open_ref,
    sender_ref_problem,
)
from netbbs.mail_groups import LetterRecipient, LetterRefused, send_letter, too_many_recipients_text
from netbbs.net.file_ref_view import (
    attached_rows,
    change_attached_files,
    file_actions,
    get_referenced_file,
    open_refs,
    ref_rows,
)
from netbbs.net.file_transfer import TransferGrants
from netbbs.net.char_input import (
    HELP_KEY, REDRAW_KEY, EditorKey, EditorKeyKind, InputCancelled, reject_unhandled_key,
)
from netbbs.net.color_depth_preference import effective_truecolor
from netbbs.net.composition import (
    ReviewAction,
    characters_over,
    edit_line_body,
    read_prefilled_field,
    read_subject,
    review_composition,
    show_compose_screen,
    too_long_message,
)
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.draft_storage import (
    delete_draft, delete_draft_fields, load_draft, load_draft_fields, save_draft_fields,
)
from netbbs.net.editor_preference import fullscreen_editor_enabled
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.net.node_theme import effective_accent_color_256, effective_header_color_256
from netbbs.net.help_overlay import show_help
from netbbs.net.post_color_preference import post_colors_enabled
from netbbs.net.prose_editor import EditorHeader, edit_prose
from netbbs.net.detail_view import show_detail
from netbbs.net.picker import pick_item
from netbbs.net.mail_arrivals import arrival_event, nudge
from netbbs.net.mail_recipients import (
    RecipientCompleter,
    choose_recipient,
    gather_address_book,
    join_recipients,
    link_mail_refusal,
    picker_request,
    read_to_line_options,
    split_recipients,
)
from netbbs.net.notices import announce, announce_styled, pending_notice_rows, take_notices, write_notices
from netbbs.net.session import Session, write_prompt
from netbbs.rendering.detail import Section, Styled
from netbbs.quoting import forward_body, forward_subject, quote_body, reply_subject, sign_forward
from netbbs.signature import append_signature, get_signature
from netbbs.rendering import (
    ERROR_COLOR,
    LABEL_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    RULE_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    WARNING_COLOR,
    MenuEntry,
    action_bar,
    colored,
    display_width,
    menu_grid,
    menu_key,
    sanitize_text,
    screen_title,
    truncate_to_width,
    wrap_to_width,
)
from netbbs.rendering.post_body import plain_post_body, post_body_mode, post_body_rows, post_body_text
from netbbs.rendering.charset import ellipsis_for
from netbbs.rendering.reflow import wrap_terminal_text
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, resolve_display_preferences
from netbbs.user_preferences import get_user_preference, set_user_preference

# Cap on the plain (non-fullscreen-editor) line-at-a-time body prompt --
# same shape as `netbbs.directory.MAX_BIO_LINES`, just sized for a
# letter rather than a short bio. `netbbs.mail.MAX_MAIL_BODY_BYTES` is
# still the one place actually enforcing a limit (checked by
# `send_mail` after the fact, same as post/bio validation elsewhere in
# this codebase) -- this is only a practical bound on the input loop
# itself.
_MAX_PLAIN_MAIL_LINES = 200


def _menu_row(entries: list[MenuEntry], *, width: int, height: int, description_level: str) -> str:
    """Compact `action_bar` packing when descriptions are off, `menu_grid`'s
    taller one-entry-per-line layout once the caller has opted into "brief"/
    "detailed" (issue #160's rollout) -- see `netbbs.net.resource_editor.
    edit_resource_draft`'s identical branch for why `menu_grid` alone isn't a
    byte-for-byte substitute for `action_bar`'s packed row at the off level."""
    if description_level == "off":
        return action_bar([e.label for e in entries], width=width)
    return menu_grid([("", entries)], width=width, height=height, description_level=description_level)


async def browse_mail(
    session: Session,
    lane: DatabaseLane,
    user: User,
    *,
    link_context: LinkContext | None = None,
    choice_prompt: Callable[[], str] | None = None,
    transfers: TransferGrants | None = None,
) -> None:
    """Entry point from the main menu's `[E]-mail` option: the mailbox
    itself (issue #810), opening on the Inbox with Sent, Compose and a kept
    letter's `[D]raft` on its action bar. Before #810 it opened a
    four-option menu, so every visit cost a keystroke before any mail
    showed.

    `link_context` (design doc), if given, lets `_compose_mail`
    recognize a `user@node` address and send a Link message
    instead of ordinary local mail -- `None` whenever this node has Link
    disabled, the same convention `netbbs.link.boards.LinkContext`
    itself already establishes for boards.

    `choice_prompt` draws the list's `Choice: ` prompt; the main menu
    passes its own, so the mailbox shows the same clock and node-status
    tags. `None` is the bare `Choice: `.

    `transfers` is the node's browser-transfer grants, for downloading a
    file a letter points at (issue #830) where Zmodem cannot carry it.

    Refused, with the reason carried to the next screen, for a caller
    `netbbs.mail.mail_access_refusal` turns away (issue #816): the guest
    account or a session that signed in as it, or an account below the mail
    level. The main menu does not
    offer mail to them either; this is the gate itself, so that no other
    way in can skip it."""
    refusal = await lane.run(lambda db: caller_mail_refusal(session, db, user))
    if refusal is not None:
        announce(session, refusal, tone="error")
        return
    _adopt_legacy_mail_draft(lane, user)
    screen = _MailboxScreen(
        session, lane, user, link_context=link_context, choice_prompt=choice_prompt, transfers=transfers,
    )
    await screen.run()


def caller_mail_refusal(session: Session, db: Database, user: User) -> str | None:
    """`netbbs.mail.mail_access_refusal` for the caller on `session`.

    A session that came in through guest login keeps the guest's refusal for
    as long as it lasts (issue #816, review): `mail_access_refusal` reads the
    node's *current* guest setting, so turning guest login off -- or moving
    it to another account -- would otherwise hand every guest still
    connected the old guest account's mail. How the caller got in is the
    session's to say (`authenticated_without_credential`, set by the guest
    branch of the login flow), not the account's."""
    if getattr(session, "authenticated_without_credential", False):
        return GUEST_MAIL_REFUSAL
    return mail_access_refusal(db, user)


# -- the mailbox list (issue #810) ---------------------------------------------
#
# One screen, two folders: the Inbox and Sent are each a table with a cursor
# -- the board post list's shape (issue #679) -- and a message is read on
# its own screen on `show_detail`. A mailbox is bounded
# (`netbbs.mail.MAX_MAIL_PER_RECIPIENT`), so the whole folder is loaded and
# paged here rather than fetched a page at a time.

_MIN_LIST_ROWS = 3
_MAX_LIST_ROWS = 30
# Below this width a row is prose ("name: subject") instead of columns
# (design doc §3.6), the width the board list switches at.
_TABLE_MIN_WIDTH = 60
_NEW_MARKER = "new"
_MARKER_WIDTH = len(_NEW_MARKER)
# Put in front of the sender's name when their node's identity changed
# (`_link_mail_identity_warning`), and explained above the list.
_IDENTITY_FLAG = "!"
_SUBJECT_MIN_WIDTH = 8
_DELIVERY_HEADING = "Delivery"
# Sent's read receipts (issue #829) share the Delivery column, which is
# headed "Status" once a row shows one. A letter to several people shows one
# summary: every copy that reports read, some, or none; "no receipt" when no
# recipient shares receipts.
_STATUS_HEADING = "Status"
_RECEIPT_READ_ALL = "receipt_read"
_RECEIPT_SOME_READ = "receipt_some_read"
_RECEIPT_NONE_READ = "receipt_not_read"
_RECEIPT_NOT_SHARED = "receipt_not_shared"
_RECEIPT_STATUS_LABELS = {
    _RECEIPT_READ_ALL: "read",
    _RECEIPT_SOME_READ: "some read",
    _RECEIPT_NONE_READ: "not read",
    _RECEIPT_NOT_SHARED: "no receipt",
}
# A Link letter that bounced or expired and was sent again with Resend
# (issue #919). Still a failed letter, so it ranks with bounced and expired
# -- after them, since those still wait on the caller -- and above a copy on
# its way or a receipt.
_RESENT_STATUS = "resent"
_FAILED_STATUSES = ("bounced", "expired")
_STATUS_LABELS = {**DELIVERY_STATUS_LABELS, _RESENT_STATUS: "resent", **_RECEIPT_STATUS_LABELS}
_STATUS_WIDTH = max([len(_DELIVERY_HEADING), len(_STATUS_HEADING), *(len(label) for label in _STATUS_LABELS.values())])
_HINT_MIN_HEIGHT = 20
# From this height the list is set off by blank rows and rules; below it
# every row goes to the mail (the 40x12 floor has none to spare).
_ROOMY_HEIGHT = 16
# A list with fewer rows than this is worth trading the action bar's
# descriptions for.
_COMFORTABLE_LIST_ROWS = 6
# The Inbox's order (issue #810), a per-caller preference: newest first, or
# unread mail first and newest first within each group.
_ORDER_PREFERENCE = "mail_order"
_ORDER_NEWEST = "newest"
_ORDER_UNREAD = "unread"
# Grouped by conversation (issue #828): `netbbs.mail.thread_key`.
_ORDER_THREADS = "threads"
_ORDERS = (_ORDER_NEWEST, _ORDER_UNREAD, _ORDER_THREADS)
_ORDER_LABELS = {_ORDER_UNREAD: "unread first", _ORDER_THREADS: "by conversation"}
_ORDER_ANNOUNCEMENTS = {
    _ORDER_NEWEST: "Newest mail first.",
    _ORDER_UNREAD: "Unread mail first.",
    _ORDER_THREADS: "Mail by conversation, newest conversation first.",
}

# The three folders (issue #828 added Kept): the Inbox and Kept are the two
# halves of what was sent to the caller, Sent is what they sent.
_INBOX = "inbox"
_SENT = "sent"
_KEPT = "kept"
_FOLDER_TITLES = {_INBOX: "Inbox", _SENT: "Sent", _KEPT: "Kept"}
# Put in the list's second column on a marked row (issue #828).
_MARK = "*"
# A later letter of the same conversation, listed by conversation, is
# indented under the conversation's newest one.
_THREAD_INDENT = "  "

_EMPTY_INBOX = "Your inbox is empty. New mail will appear here."
_EMPTY_SENT = "You haven't sent any mail. [C]ompose writes a new message."
_EMPTY_KEPT = (
    f"Nothing kept. K[e]ep in the Inbox moves a letter here, where the mailbox cap never removes it. "
    f"Kept holds {MAX_KEPT_PER_RECIPIENT} letters."
)
def kept_capacity_note(kept: int) -> str | None:
    """What Kept says once it is full (issue #921): keeping another letter
    is refused until some go back to the Inbox or are deleted."""
    if kept < MAX_KEPT_PER_RECIPIENT:
        return None
    return (
        f"Kept is full ({MAX_KEPT_PER_RECIPIENT} letters): K[e]ep in the Inbox is refused until you move "
        "some back to the Inbox or delete them."
    )


def mailbox_capacity_note(total: int, unread: int) -> tuple[str, str] | None:
    """What the Inbox says about its cap (issue #818), and its tone, once it
    holds `MAILBOX_NEARLY_FULL` messages; `None` below that. Kept letters
    count toward Kept's own limit, not this one (issue #921), so `total`
    and `unread` are the Inbox's alone."""
    if total >= MAX_MAIL_PER_RECIPIENT and unread >= total:
        return (
            f"Your mailbox is full of unread mail ({MAX_MAIL_PER_RECIPIENT} messages): new mail is "
            "turned away until you read or delete some, or move some to Kept.",
            "error",
        )
    if total >= MAX_MAIL_PER_RECIPIENT:
        return (
            f"Your mailbox is full ({MAX_MAIL_PER_RECIPIENT} messages): each new message removes your "
            "oldest read one. Delete what you don't need, and move what you want to keep to Kept.",
            "warning",
        )
    if total >= MAILBOX_NEARLY_FULL:
        return (
            f"Your mailbox is nearly full: at {MAX_MAIL_PER_RECIPIENT} messages, each new one removes "
            "your oldest read message. Unread mail is never removed.",
            "warning",
        )
    return None


_IDENTITY_NOTE = (
    f"{_IDENTITY_FLAG} Identity changed: that sender's BBS now has a different cryptographic identity. "
    "Open the message for details."
)

_LIST_HELP = [
    "Up/Down      move the highlight",
    "Enter, 1-9   read the highlighted message, or row number N",
    "N / P        next page, previous page (also PgDn/PgUp)",
    "C            write a new message",
    "D            resume or delete your unfinished letter",
    "S            your sent mail; B there comes back to the Inbox",
    "K            your kept mail; B there comes back to the Inbox",
    "U            mark the highlighted message unread, or read",
    "M, Space     mark the highlighted message, or unmark it",
    "L            delete the marked messages, or the highlighted one",
    "E            keep the marked (or highlighted) messages, or in",
    "             Kept, move them back to the Inbox",
    "R            delete every read message in the Inbox",
    "O            order: newest first, unread first (not in Sent),",
    "             or by conversation",
    "F            show only mail with a word in its name or subject",
    "Ctrl-L       redraw the list",
    "B            back to the main menu",
    "",
    "\"new\" marks mail you have not opened. Opening a message marks",
    "it read; [U]nread in the message or on the list takes that back.",
    "* marks a message for Delete or Keep. Kept mail is never removed",
    "to make room, but still counts toward the mailbox's size.",
    "By conversation groups mail with one person under one subject,",
    "Re: and Fwd: aside, newest conversation first.",
    "Sent shows where mail to another BBS stands under Delivery.",
    "Mail from System is a notice from this BBS; it has no Reply.",
]


def _mail_order(db: Database, user: User) -> str:
    stored = get_user_preference(db, user, _ORDER_PREFERENCE, default=_ORDER_NEWEST)
    return stored if stored in _ORDERS else _ORDER_NEWEST


def _set_mail_order(db: Database, user: User, order: str) -> None:
    set_user_preference(db, user, _ORDER_PREFERENCE, order)


@dataclass(frozen=True)
class _MailRow:
    """One message as its list row shows it: plain, sanitized cells."""

    message: MailMessage
    name: str
    subject: str
    when: str
    identity_changed: bool = False
    # Listed by conversation, a later letter of the conversation above it
    # (issue #828): drawn indented.
    continues: bool = False
    # In Sent, every copy of a letter to several people (issue #827), which
    # the one row stands for; empty for a letter to one person.
    copies: tuple[MailMessage, ...] = ()
    # In Sent, the read-receipt summary (issue #829): a `_RECEIPT_*` key, or
    # None when the row has none to show.
    receipt: str | None = None

    @property
    def ids(self) -> list[int]:
        """The letters this row acts on: every copy it stands for."""
        return [copy.id for copy in self.copies] if self.copies else [self.message.id]

    @property
    def shown_subject(self) -> str:
        return _THREAD_INDENT + self.subject if self.continues else self.subject

    @property
    def status(self) -> str | None:
        statuses = [_sent_copy_status(copy) for copy in (self.copies or (self.message,))]
        # A letter to several people shows the copy that needs the caller
        # most: one that did not arrive, then one that did not and was
        # resent (issue #919) -- so `resent` shows only once every failed
        # copy was -- then one still on its way, then whether its local
        # copies were read (issue #829).
        for wanted in (*_FAILED_STATUSES, _RESENT_STATUS, "pending", RELAYED_DISPLAY_STATUS):
            if wanted in statuses:
                return wanted
        if self.receipt is not None:
            return self.receipt
        return "delivered" if "delivered" in statuses else None


def _sent_copy_status(copy: MailMessage) -> str | None:
    """Where one copy in Sent stands, for the list's column: its delivery
    state (`delivery_display_status`), except that a failed one the caller
    sent again is `_RESENT_STATUS` (issue #919). `None` for local mail."""
    status = delivery_display_status(copy.link_delivery_status, copy.link_relay_handoff_at)
    if status in _FAILED_STATUSES and copy.resent_at is not None:
        return _RESENT_STATUS
    return status if status in DELIVERY_STATUS_LABELS else None


def _cell(text: str) -> str:
    """Untrusted text as one table cell: sanitized, and tabs made spaces
    before anything is measured -- `sanitize_text` keeps a tab, the width
    helpers count it as no column, and the transport writes it as one."""
    return sanitize_text(text).replace("\t", " ")


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def _fit(text: str, width: int, ellipsis: str = "...") -> str:
    """`text` in exactly `width` columns, cut with `ellipsis` when it is
    wider -- a name or subject cut short says so."""
    return _pad(truncate_to_width(text, width, ellipsis=ellipsis), width)


def _name_text(row: _MailRow) -> str:
    return f"{_IDENTITY_FLAG} {row.name}" if row.identity_changed else row.name


def _mail_column_widths(
    rows: list[_MailRow], *, width: int, number_width: int, sent: bool, show_status: bool,
) -> tuple[int, int, int] | None:
    """(name, subject, date) widths that fit `width`, or `None` when no
    readable table fits and the rows should be prose instead. The name
    gets up to two fifths of what the fixed columns leave -- a Link
    address carries its node's name, so it has no fixed cap as a board's
    author does -- and gives way before the subject does: the message
    view shows it in full."""
    if width < _TABLE_MIN_WIDTH:
        return None
    date_width = max([display_width(row.when) for row in rows] + [len("Date")])
    # The two-column lead ("> " or "  "), two spaces between columns, and
    # one column kept free: a row that reaches the last column makes many
    # terminals wrap the cursor onto the next row.
    fixed = [number_width, date_width]
    if not sent:
        fixed.append(_MARKER_WIDTH)
    if show_status:
        fixed.append(_STATUS_WIDTH)
    gaps = 2 * (len(fixed) + 1)
    available = width - 2 - sum(fixed) - gaps - 1
    heading = "To" if sent else "From"
    name_width = min(
        max([display_width(_name_text(row)) for row in rows] + [len(heading)]),
        max(len(heading), available * 2 // 5),
    )
    subject_width = available - name_width
    if subject_width < _SUBJECT_MIN_WIDTH:
        return None
    return name_width, subject_width, date_width


def _receipt_summary(receipts: list[ReadReceipt]) -> str | None:
    """The Sent list's read state for a letter's local copies (issue #829):
    whether every copy that reports its reading was read, some or none; that
    no recipient shares receipts; or None with no receipt to show."""
    reported = [receipt for receipt in receipts if receipt.state in (RECEIPT_READ, RECEIPT_NOT_READ)]
    if reported:
        read = sum(receipt.state == RECEIPT_READ for receipt in reported)
        if read == len(reported):
            return _RECEIPT_READ_ALL
        return _RECEIPT_SOME_READ if read else _RECEIPT_NONE_READ
    if any(receipt.state == RECEIPT_WITHHELD for receipt in receipts):
        return _RECEIPT_NOT_SHARED
    return None


def _status_heading(rows: list[_MailRow]) -> str:
    return _STATUS_HEADING if any(row.status in _RECEIPT_STATUS_LABELS for row in rows) else _DELIVERY_HEADING


def _mail_list_heading(
    widths: tuple[int, int, int], *, number_width: int, sent: bool, show_status: bool,
    status_heading: str = _DELIVERY_HEADING,
) -> str:
    name_width, subject_width, date_width = widths
    parts = [f"{'#':>{number_width}}"]
    if not sent:
        parts.append(" " * _MARKER_WIDTH)
    parts.append(_pad("To" if sent else "From", name_width))
    parts.append(_pad("Subject", subject_width))
    if show_status:
        parts.append(_pad(status_heading, _STATUS_WIDTH))
    parts.append("Date")
    return colored(("  " + "  ".join(parts)).rstrip(), fg_color=LABEL_COLOR, bold=True)


def _styled_name(name_plain: str, row: _MailRow) -> str:
    """The name cell, its identity flag in the warning color."""
    if row.identity_changed and name_plain.startswith(_IDENTITY_FLAG):
        return colored(_IDENTITY_FLAG, fg_color=WARNING_COLOR, bold=True) + colored(
            name_plain[len(_IDENTITY_FLAG):], fg_color=METADATA_COLOR
        )
    return colored(name_plain, fg_color=METADATA_COLOR)


def _mail_list_rows(
    rows: list[_MailRow],
    *,
    width: int,
    first_number: int,
    number_width: int,
    widths: tuple[int, int, int] | None,
    highlighted: int | None,
    sent: bool,
    show_status: bool,
    accent: int | tuple[int, int, int],
    ellipsis: str = "...",
    marked: frozenset[int] | set[int] = frozenset(),
) -> list[str]:
    """One styled row per message, fitted to `width` in display columns.
    `first_number` is the number the first row shows; the highlighted row
    (an index into `rows`) is drawn in reverse video, the way the board
    list draws its cursor. A row whose message id is in `marked` (issue
    #828) shows the mark in the column after the cursor's."""
    lines: list[str] = []
    styled_mark = colored(_MARK, fg_color=WARNING_COLOR, bold=True)
    for index, row in enumerate(rows):
        is_marked = row.message.id in marked
        number = f"{first_number + index:>{number_width}}"
        unread = not sent and not row.message.is_read
        marker = _NEW_MARKER if unread else ""
        status = _STATUS_LABELS[row.status] if row.status else ""
        if widths is None:
            # Prose, for a terminal too narrow for columns: which message
            # it is comes first; the message view shows the date.
            tag = marker or status
            # The name takes at most half the row, so the subject shows.
            name = truncate_to_width(_name_text(row), max(8, (width - 1) // 2), ellipsis=ellipsis)
            lead = _MARK if is_marked else ""
            plain = truncate_to_width(
                f"{lead}{number} {tag + ' ' if tag else ''}{name}: {row.shown_subject}", max(1, width - 1),
                ellipsis=ellipsis,
            )
            if index == highlighted:
                lines.append(colored(plain, reverse=True))
                continue
            rest = plain[len(lead) + len(number) + 1:]
            styled_tag = ""
            if tag and rest.startswith(tag):
                styled_tag = colored(
                    tag, fg_color=SUCCESS_COLOR if unread else _DELIVERY_COLORS.get(row.status, VALUE_COLOR),
                    bold=unread,
                )
                rest = rest[len(tag):]
            lines.append(
                (styled_mark if is_marked else "") + colored(number, fg_color=accent) + " " + styled_tag + rest
            )
            continue
        name_width, subject_width, date_width = widths
        name_cell = _fit(_name_text(row), name_width, ellipsis)
        subject_cell = _fit(row.shown_subject, subject_width, ellipsis)
        date_cell = _fit(row.when, date_width, ellipsis)
        if index == highlighted:
            cells = [number]
            if not sent:
                cells.append(_pad(marker, _MARKER_WIDTH))
            cells += [name_cell, subject_cell]
            if show_status:
                cells.append(_pad(status, _STATUS_WIDTH))
            cells.append(date_cell)
            lines.append(colored(">" + (_MARK if is_marked else " ") + "  ".join(cells), reverse=True))
            continue
        styled = [colored(number, fg_color=accent)]
        if not sent:
            styled.append(
                colored(marker, fg_color=SUCCESS_COLOR, bold=True) if marker else " " * _MARKER_WIDTH
            )
        styled.append(_styled_name(name_cell, row))
        # An unread subject stands out, as an unopened letter does.
        styled.append(colored(subject_cell, bold=True) if unread else subject_cell)
        if show_status:
            styled.append(
                colored(_pad(status, _STATUS_WIDTH), fg_color=_DELIVERY_COLORS.get(row.status, VALUE_COLOR))
                if status else " " * _STATUS_WIDTH
            )
        styled.append(colored(date_cell, fg_color=METADATA_COLOR))
        lines.append(" " + (styled_mark if is_marked else " ") + "  ".join(styled))
    return lines


async def _load_mail_rows(lane: DatabaseLane, user: User, *, sent: bool) -> list[_MailRow]:
    """The folder's messages, newest first, as list rows. A name is
    resolved once per address, not once per message: a Link label and an
    identity warning each cost a lane call."""
    messages = await lane.run(list_sent if sent else list_inbox, user)
    display_format, display_timezone = await lane.run(resolve_display_preferences)
    names: dict[tuple[str | None, int | None], str] = {}
    warnings: dict[str, bool] = {}
    rows: list[_MailRow] = []
    # Sent lists a letter to several people once (issue #827): its newest
    # copy stands for all of them.
    group_copies: dict[str, list[MailMessage]] = {}
    receipts: dict[int, ReadReceipt] = {}
    if sent:
        for message in messages:
            if message.mail_group_id is not None:
                group_copies.setdefault(message.mail_group_id, []).append(message)
        # Receipts are reciprocal (issue #829): a caller who does not share
        # them sees no read state in the list.
        if await lane.run(shares_read_receipts, user):
            receipts = await lane.run(read_receipts, user, messages)
    for message in messages:
        identity_changed = False
        if sent and message.mail_group_id is not None:
            copies = group_copies.get(message.mail_group_id)
            if copies is None or copies[0] is not message:
                continue
            key = ("group:" + message.mail_group_id, None)
            names[key] = await _display_recipient_label(lane, message)
        elif sent:
            # A deleted recipient has no id; its kept name tells it apart
            # from another deleted one (issue #818).
            key = (message.recipient_remote_address or message.recipient_label, message.recipient_user_id)
            if key not in names:
                names[key] = await _display_recipient_label(lane, message)
        elif message.from_system:
            # Keyed apart from any account's name (issue #819): a SysOp
            # can still create an account called "System".
            key = (SYSTEM_SENDER_LABEL, 0)
            names[key] = SYSTEM_SENDER_LABEL
        else:
            key = (message.sender_label, None)
            if key not in names:
                names[key] = await _display_sender_label(lane, message)
            if message.sender_label not in warnings:
                warnings[message.sender_label] = (
                    await _link_mail_identity_warning(lane, message.sender_label) is not None
                )
            identity_changed = warnings[message.sender_label]
        rows.append(_MailRow(
            message=message,
            name=_cell(names[key]),
            subject=_cell(message.subject),
            when=format_for_display(
                message.created_at, override_format=display_format, override_timezone=display_timezone
            ),
            identity_changed=identity_changed,
            copies=tuple(reversed(group_copies[message.mail_group_id]))
            if sent and message.mail_group_id is not None else (),
            receipt=_receipt_summary([
                receipts[copy.id]
                for copy in (group_copies[message.mail_group_id] if message.mail_group_id is not None else [message])
                if copy.id in receipts
            ]) if sent else None,
        ))
    return rows


def _matches(row: _MailRow, query: str) -> bool:
    """[F]ind (issue #810): a word in the name or the subject, as the
    list shows them -- never the "new" marker, which the old picker's
    search matched as part of the subject."""
    needle = query.casefold()
    return needle in row.name.casefold() or needle in row.subject.casefold()


def _by_conversation(rows: list[_MailRow], *, sent: bool) -> list[_MailRow]:
    """`rows` (newest first) grouped by conversation (issue #828,
    `netbbs.mail.thread_key`): the conversation with the newest letter
    first, and within each its letters newest first, every one after the
    first marked as continuing it."""
    groups: dict[tuple, list[_MailRow]] = {}
    for row in rows:
        groups.setdefault(thread_key(row.message, sent=sent), []).append(row)
    return [
        replace(row, continues=index > 0)
        for group in groups.values()
        for index, row in enumerate(group)
    ]


async def _read_list_key(session: Session) -> tuple[EditorKey, bool]:
    """A structured key, so Up/Down/Enter arrive as keys, with the plain
    `read_key` fallback lightweight sessions need. The flag says whether
    the key was echoed (only a `read_key` echoes) -- the board list's
    contract."""
    read_editor_key = getattr(session, "read_editor_key", None)
    if read_editor_key is not None:
        try:
            return await read_editor_key(distinguish_ctrl_h=True), False
        except NotImplementedError:
            pass
    return EditorKey(EditorKeyKind.CHAR, char=await session.read_key()), True


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _count_rows(text: str, width: int) -> int:
    return wrap_terminal_text(text, max(1, width)).count("\r\n") + 1 if text else 0


class _MailboxScreen:
    """The mailbox (issue #810): the Inbox or Sent as a list with a
    cursor, and every mail action on its action bar."""

    def __init__(
        self, session: Session, lane: DatabaseLane, user: User, *,
        link_context: LinkContext | None, choice_prompt: Callable[[], str] | None,
        transfers: TransferGrants | None = None,
    ) -> None:
        self.session = session
        self.lane = lane
        self.user = user
        self.link_context = link_context
        self.transfers = transfers
        self.choice_prompt = choice_prompt
        self.folder = _INBOX
        self.order = _ORDER_NEWEST
        self.query: str | None = None
        # The folder's letters, and for the Inbox and Kept, every letter
        # the caller received: the two share the cap (issue #828).
        self.all_rows: list[_MailRow] = []
        self.received_rows: list[_MailRow] = []
        # Ids of the letters marked in this folder (issue #828).
        self.marked: set[int] = set()
        self.rows: list[_MailRow] = []
        self.highlighted: int | None = None
        # The page on screen at the last render: its first row, which the
        # digit keys count from, how many rows it shows, and how many it
        # holds, which the page keys step by.
        self.shown_top = 0
        self.shown_count = 0
        self.shown_limit = _MIN_LIST_ROWS
        # The first row of the page, kept rather than derived from the
        # highlight: an outcome notice takes a row from one render's
        # budget, and a page worked out as `highlighted // limit` would
        # then shift under the caller (review on #877).
        self.top = 0
        self.has_draft = False

    @property
    def sent(self) -> bool:
        return self.folder == _SENT

    @property
    def kept(self) -> bool:
        return self.folder == _KEPT

    def _effective_order(self) -> str:
        """The order the folder is listed in: Sent has no unread mail, so
        unread first is newest first there."""
        if self.sent and self.order == _ORDER_UNREAD:
            return _ORDER_NEWEST
        return self.order

    def _next_order(self) -> str:
        """What `[O]rder` switches to: newest first, unread first, by
        conversation and round again; in Sent, newest first and by
        conversation."""
        current = self._effective_order()
        if self.sent:
            return _ORDER_NEWEST if current == _ORDER_THREADS else _ORDER_THREADS
        return _ORDERS[(_ORDERS.index(current) + 1) % len(_ORDERS)]

    async def _settings(self) -> None:
        lane, user = self.lane, self.user
        self.description_level = await lane.run(menu_description_level, user)
        self.redraw_in_place = await lane.run(redraw_in_place_enabled, user)
        self.unicode_style = await lane.run(unicode_style_enabled, user)
        self.collapsed = await lane.run(breadcrumb_collapsed_enabled, user)
        self.header_color = await lane.run(effective_header_color_256)
        self.accent = await lane.run(effective_accent_color_256)
        self.order = await lane.run(_mail_order, user)

    async def run(self) -> None:
        await self._settings()
        await self._reload()
        await self._render()
        session = self.session
        while True:
            key, echoed = await self._next_key()
            char = key.char.lower() if key.kind == EditorKeyKind.CHAR and key.char else ""
            if await self._handle(key, char, echoed):
                return

    async def _next_key(self) -> tuple[EditorKey, bool]:
        """The next key, redrawing the folder whenever mail arrives while
        the caller is looking at it (issue #823): the new letter's row, the
        counts, and its notice above the prompt. The pending key read is
        kept across the redraw, never cancelled, so no keystroke is lost
        (the live screen's way, `netbbs.net.live_screen`)."""
        arrived = arrival_event(self.session)
        if arrived is None:
            return await _read_list_key(self.session)
        key_task = asyncio.create_task(_read_list_key(self.session))
        try:
            while True:
                arrival = asyncio.create_task(arrived.wait())
                try:
                    done, _pending = await asyncio.wait({key_task, arrival}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    arrival.cancel()
                    await asyncio.gather(arrival, return_exceptions=True)
                if key_task in done:
                    return key_task.result()
                arrived.clear()
                await self._reload()
                await self._render()
        except BaseException:
            key_task.cancel()
            await asyncio.gather(key_task, return_exceptions=True)
            raise

    async def _handle(self, key: EditorKey, char: str, echoed: bool) -> bool:
        """Act on one key; `True` when the caller left the mailbox."""
        session = self.session

        async def moved_on() -> None:
            # A key `read_key` echoed leaves the cursor after it: start the
            # next output on a fresh line.
            if echoed:
                await session.write_line("")

        if key.kind in (EditorKeyKind.UP, EditorKeyKind.DOWN) and self.rows:
            step = -1 if key.kind == EditorKeyKind.UP else 1
            current = self.highlighted if self.highlighted is not None else (-1 if step == 1 else 0)
            self.highlighted = (current + step) % len(self.rows)
            await self._render()
        elif key.kind in (EditorKeyKind.PAGE_UP, EditorKeyKind.PAGE_DOWN) or char in ("n", "p"):
            forward = key.kind == EditorKeyKind.PAGE_DOWN or char == "n"
            if not self._turn_page(forward):
                await self._reject(key, echoed)
                return False
            await moved_on()
            await self._render()
        elif (key.kind == EditorKeyKind.ENTER or char in ("\r", "\n")) and self.highlighted is not None:
            await moved_on()
            await self._open(self.highlighted)
        elif len(char) == 1 and char in "123456789" and int(char) <= min(9, self.shown_count):
            await moved_on()
            await self._open(self.shown_top + int(char) - 1)
        elif (key.kind == EditorKeyKind.CTRL and key.char == "l") or char == REDRAW_KEY:
            await self._reload()
            await self._render()
        elif (key.kind == EditorKeyKind.CTRL and key.char == "h") or char == HELP_KEY:
            await show_help(
                session, "Mail keys", _LIST_HELP, header_color=self.header_color, unicode_style=self.unicode_style,
            )
            await self._render()
        elif char == "c":
            await moved_on()
            await _compose_mail(session, self.lane, self.user, link_context=self.link_context)
            await self._reload()
            await self._render()
        elif char == "d" and self.has_draft:
            await moved_on()
            await self._draft()
            await self._reload()
            await self._render()
        elif char == "s" and self.folder == _INBOX:
            await moved_on()
            await self._switch(_SENT)
        elif char == "k" and self.folder == _INBOX:
            await moved_on()
            await self._switch(_KEPT)
        elif char == "b":
            await moved_on()
            if self.folder != _INBOX:
                await self._switch(_INBOX)
                return False
            return True
        elif char in ("m", " ") and self.highlighted is not None:
            await moved_on()
            self._toggle_mark()
            await self._render()
        elif char == "l" and self.highlighted is not None:
            await moved_on()
            await self._delete()
            await self._render()
        elif char == "e" and not self.sent and self.highlighted is not None:
            await moved_on()
            await self._keep()
            await self._render()
        elif char == "r" and self._read_count():
            await moved_on()
            await self._delete_read()
            await self._render()
        elif char == "u" and not self.sent and self.highlighted is not None:
            await moved_on()
            row = self.rows[self.highlighted]
            if row.message.is_read:
                await self.lane.run(mark_unread, self.user, row.message)
                announce(session, "Marked unread.", tone="muted")
            else:
                await self.lane.run(mark_read, self.user, row.message)
                announce(session, "Marked read.", tone="muted")
            await self._reload()
            await self._render()
        elif char == "o" and self.all_rows:
            await moved_on()
            self.order = self._next_order()
            await self.lane.run(_set_mail_order, self.user, self.order)
            announce(session, _ORDER_ANNOUNCEMENTS[self.order], tone="muted")
            await self._reload()
            await self._render()
        elif char == "f" and self.all_rows:
            await moved_on()
            await self._find()
            await self._render()
        else:
            await self._reject(key, echoed)
        return False

    async def _reject(self, key: EditorKey, echoed: bool) -> None:
        await self.session.write(reject_unhandled_key(key.char) if echoed and key.char else "\a")

    # -- state ------------------------------------------------------------

    async def _reload(self, *, keep: int | None = None) -> None:
        """Fetch the folder again, keeping the highlight on the message it
        was on (or `keep`, a message id) rather than on its row number:
        reading a message, marking it, or a new one arriving moves rows."""
        was_on = keep
        if was_on is None and self.highlighted is not None and self.highlighted < len(self.rows):
            was_on = self.rows[self.highlighted].message.id
        was_index = self.highlighted
        loaded = await _load_mail_rows(self.lane, self.user, sent=self.sent)
        if self.sent:
            self.received_rows = []
            self.all_rows = loaded
        else:
            self.received_rows = loaded
            self.all_rows = [row for row in loaded if (row.message.kept_at is not None) == self.kept]
        # A mark on a letter no longer in this folder (deleted, kept,
        # moved back) goes with it.
        self.marked &= {row.message.id for row in self.all_rows}
        rows = self.all_rows
        if self.query:
            rows = [row for row in rows if _matches(row, self.query)]
        order = self._effective_order()
        if order == _ORDER_UNREAD:
            # Stable: newest first within the unread and the read.
            rows = sorted(rows, key=lambda row: row.message.is_read)
        elif order == _ORDER_THREADS:
            rows = _by_conversation(rows, sent=self.sent)
        self.rows = rows
        # Offered only for a letter that loads: [D]raft on one that does
        # not would do nothing (review on #877).
        self.has_draft = _load_letter_draft(_letter_draft_path(self.lane, self.user)) is not None
        if not rows:
            self.highlighted = None
            return
        index = next((i for i, row in enumerate(rows) if row.message.id == was_on), None)
        if index is None:
            index = min(was_index, len(rows) - 1) if was_index is not None else 0
        self.highlighted = index

    async def _switch(self, folder: str) -> None:
        self.folder = folder
        self.query = None
        self.highlighted = None
        self.top = 0
        self.marked = set()
        await self._reload()
        await self._render()

    # -- managing letters (issue #828) ----------------------------------------

    def _toggle_mark(self) -> None:
        """[M]ark (or Space): mark the highlighted letter, or unmark it, and
        move on to the next, so a run of letters is marked key by key."""
        assert self.highlighted is not None
        mail_id = self.rows[self.highlighted].message.id
        if mail_id in self.marked:
            self.marked.discard(mail_id)
        else:
            self.marked.add(mail_id)
        if self.highlighted + 1 < len(self.rows):
            self.highlighted += 1

    def _targets(self) -> list[_MailRow]:
        """What Delete and Keep act on: the marked letters, or with none
        marked, the highlighted one."""
        if self.marked:
            return [row for row in self.all_rows if row.message.id in self.marked]
        assert self.highlighted is not None
        return [self.rows[self.highlighted]]

    def _read_count(self) -> int:
        """How many letters `Delete [r]ead` would delete: the read ones in
        the Inbox. Kept mail is kept, and unread mail has not been seen."""
        if self.folder != _INBOX:
            return 0
        return sum(1 for row in self.all_rows if row.message.is_read)

    async def _delete(self) -> None:
        """De[l]ete: the marked letters, or the highlighted one, after one
        confirmation. Each goes from the caller's own side only
        (`netbbs.mail.delete_letters`)."""
        targets = self._targets()
        if len(targets) == 1 and not self.marked:
            subject = truncate_to_width(targets[0].subject, 40, ellipsis=ellipsis_for(self.session, unicode_style=self.unicode_style))
            question = f"Delete \"{subject}\"?"
        else:
            question = f"Delete the {_count(len(targets), 'marked message')}?"
        if not await prompt_yes_no(self.session, question, default=False):
            return
        deleted = await self.lane.run(
            delete_letters, self.user, [mail_id for row in targets for mail_id in row.ids], sent=self.sent,
        )
        self.marked = set()
        if deleted and any(row.copies for row in targets):
            # Counted as the list shows them: a letter to several people is
            # one letter, however many copies went (issue #827).
            deleted = len(targets)
        announce(self.session, f"Deleted {_count(deleted, 'message')}.")
        await self._reload()

    async def _delete_read(self) -> None:
        """Delete [r]ead: every read letter in the Inbox, after one
        confirmation that says how many. Unread mail and Kept stay."""
        count = self._read_count()
        if not await prompt_yes_no(
            self.session,
            f"Delete all {_count(count, 'read message')} in your Inbox? Unread and kept mail stays.",
            default=False,
        ):
            return
        # The letters the count was of: one read since, elsewhere, stays.
        ids = [row.message.id for row in self.all_rows if row.message.is_read]
        deleted = await self.lane.run(delete_letters, self.user, ids, sent=False)
        self.marked = set()
        announce(self.session, f"Deleted {_count(deleted, 'read message')}.")
        await self._reload()

    async def _keep(self) -> None:
        """K[e]ep in the Inbox moves the marked letters, or the highlighted
        one, to Kept; Mov[e] to Inbox in Kept moves them back. Nothing is
        lost either way, so nothing is asked."""
        targets = self._targets()
        try:
            moved = await self.lane.run(
                set_kept, self.user, [row.message.id for row in targets], kept=not self.kept,
            )
        except KeptFullError as exc:
            # Refused in place (issue #921), marks and all, so the caller
            # can unmark some or make room and try again.
            announce(self.session, str(exc), tone="error")
            return
        self.marked = set()
        where = "back to the Inbox" if self.kept else "to Kept"
        announce(self.session, f"Moved {_count(moved, 'message')} {where}.", tone="muted")
        await self._reload()

    def _turn_page(self, forward: bool) -> bool:
        """Move the highlight to the first row of the page after or before
        the one on screen; `False` when there is none."""
        if not self.rows:
            return False
        if forward:
            target = self.shown_top + self.shown_limit
            if target >= len(self.rows):
                return False
        else:
            if self.shown_top == 0:
                return False
            target = max(0, self.shown_top - self.shown_limit)
        self.top = self.highlighted = target
        return True

    def _top(self, limit: int) -> int:
        """The page's first row, moved only as far as keeps the highlight
        on screen: up to it, or down until it is the last row."""
        top = min(self.top, max(0, len(self.rows) - 1))
        if self.highlighted is not None:
            if self.highlighted < top:
                top = self.highlighted
            elif self.highlighted >= top + limit:
                top = self.highlighted - limit + 1
        self.top = top
        return top

    async def _open(self, index: int) -> None:
        if index >= len(self.rows):
            return
        self.highlighted = index
        message = self.rows[index].message
        if self.sent:
            await _show_sent_message(self.session, self.lane, self.user, message, link_context=self.link_context)
        else:
            await _show_inbox_message(
                self.session, self.lane, self.user, message, link_context=self.link_context, transfers=self.transfers,
            )
        await self._reload(keep=message.id)
        await self._render()

    async def _draft(self) -> None:
        """[D]raft: the kept new letter (issue #814) -- resume it, delete
        it, or leave it, the same choice a board's saved post draft gets."""
        draft = _load_letter_draft(_letter_draft_path(self.lane, self.user))
        if draft is None:
            return
        outcome = await _letter_draft_choice(self.session, self.lane, self.user, draft, starting_new=False)
        if outcome == "resume":
            await _compose_mail(self.session, self.lane, self.user, link_context=self.link_context, resume=True)
        elif outcome == "discard":
            _forget_letter(_letter_draft_path(self.lane, self.user))
            announce(self.session, "Draft deleted.", tone="muted")

    async def _find(self) -> None:
        """[F]ind: narrow the folder to mail with a word in the name or
        the subject; an empty line shows everything again, Esc leaves the
        list as it was."""
        session = self.session
        discard_buffered_enter = getattr(session, "discard_buffered_enter", None)
        if discard_buffered_enter is not None:
            await discard_buffered_enter()
        who = "To" if self.sent else "From"
        await write_prompt(session, f"Find in {who} or Subject (empty shows all): ")
        try:
            text = (await session.read_line(cancellable=True)).strip()
        except InputCancelled:
            return
        self.query = text or None
        self.highlighted = 0
        self.top = 0
        await self._reload(keep=-1)

    # -- drawing ------------------------------------------------------------

    def _roomy(self) -> bool:
        return self.session.terminal_height >= _ROOMY_HEIGHT

    def _show_status(self) -> bool:
        return self.sent and any(row.status for row in self.all_rows)

    def _subtitle(self) -> str:
        separator = colored(" · " if self.unicode_style else " - ", fg_color=MUTED_COLOR)
        total = len(self.all_rows)
        # Below the table width the header keeps to one row where it can
        # (issue #828): short counts, and what the list itself shows -- its
        # order, how much is in Kept -- left out.
        narrow = self.session.terminal_width < _TABLE_MIN_WIDTH
        parts: list[str] = []
        if self.sent:
            parts.append(colored(_count(total, "sent message") if not narrow else f"{total} sent", fg_color=VALUE_COLOR))
        else:
            unread = sum(1 for row in self.all_rows if not row.message.is_read)
            if self.kept:
                parts.append(colored(_count(total, "kept message") if not narrow else f"{total} kept", fg_color=VALUE_COLOR))
                if unread:
                    parts.append(colored(f"{unread} unread", fg_color=self.accent))
            else:
                # Unread counts in the highlight colour, as on the main menu
                # and in the new-mail notices (issue #917): news, not a
                # warning. The cap nearing full stays a warning.
                parts.append(
                    colored(_count(unread, "unread message") if not narrow else f"{unread} unread", fg_color=self.accent)
                    if unread else colored("Inbox caught up", fg_color=SUCCESS_COLOR)
                )
            # Counted against the folder's own limit, read or not, whatever
            # [F]ind is showing: the Inbox's cap (issue #818), or Kept's
            # (issue #921).
            held = sum(1 for row in self.received_rows if (row.message.kept_at is not None) == self.kept)
            limit, warn_at = (
                (MAX_KEPT_PER_RECIPIENT, MAX_KEPT_PER_RECIPIENT) if self.kept
                else (MAX_MAIL_PER_RECIPIENT, MAILBOX_NEARLY_FULL)
            )
            parts.append(colored(
                f"{held} of {limit}",
                fg_color=WARNING_COLOR if held >= warn_at else VALUE_COLOR,
            ))
            if not self.kept:
                kept = [row.message for row in self.received_rows if row.message.kept_at is not None]
                kept_unread = sum(1 for message in kept if not message.is_read)
                if kept_unread:
                    # The main menu counts these as unread too (review on
                    # #908): said at any width, or the Inbox would read
                    # "caught up" while the main menu says otherwise.
                    parts.append(colored(f"{kept_unread} unread in Kept", fg_color=self.accent))
                elif kept and not narrow:
                    parts.append(colored(f"{len(kept)} in Kept", fg_color=MUTED_COLOR))
        order = self._effective_order()
        if order in _ORDER_LABELS and not narrow:
            parts.append(colored(_ORDER_LABELS[order], fg_color=MUTED_COLOR))
        if self.marked:
            parts.append(colored(f"{len(self.marked)} marked", fg_color=WARNING_COLOR, bold=True))
        if self.query:
            parts.append(colored(f"matching \"{sanitize_text(self.query)}\"", fg_color=MUTED_COLOR))
        return separator.join(parts)

    def _options(self, *, row_count: int, pages: tuple[bool, bool], measuring: bool = False) -> list[MenuEntry]:
        """The folder's action bar. `measuring` asks for the busiest bar the
        folder can draw -- every entry that can appear, each with its longer
        label -- which the page budget is measured against."""
        has_next, has_previous = pages
        highlighted = (
            self.rows[self.highlighted]
            if self.highlighted is not None and self.highlighted < len(self.rows) else None
        )
        options: list[MenuEntry] = []
        if row_count:
            keys = "1" if row_count == 1 else f"1-{min(row_count, 9)}"
            options.append(MenuEntry(label=menu_key(keys, "/Enter read"), brief="Read a message"))
        if has_next:
            options.append(MenuEntry(label=menu_key("N", "ext page"), brief="Show the next page"))
        if has_previous:
            options.append(MenuEntry(label=menu_key("P", "rev page"), brief="Show the previous page"))
        options.append(MenuEntry(label=menu_key("C", "ompose"), brief="Write a new message"))
        if self.has_draft:
            options.append(MenuEntry(label=menu_key("D", "raft"), brief="Resume or delete your unfinished letter"))
        if self.folder == _INBOX:
            options.append(MenuEntry(label=menu_key("S", "ent"), brief="Review mail you've sent"))
            options.append(MenuEntry(label=menu_key("K", "ept"), brief="Mail you keep from the mailbox cap"))
        if row_count:
            if not measuring and (highlighted is None or highlighted.message.id not in self.marked):
                options.append(MenuEntry(label=menu_key("M", "ark"), brief="Mark the highlighted message"))
            else:
                # Un[m]ark is the longer label, which the page budget measures.
                options.append(MenuEntry(label=menu_key("m", "ark", prefix="Un"), brief="Unmark it"))
            options.append(MenuEntry(
                label=menu_key("l", "ete", prefix="De"),
                brief="Delete the marked messages" if self.marked else "Delete the highlighted message",
            ))
            if self.folder == _INBOX:
                options.append(MenuEntry(
                    label=menu_key("e", "ep", prefix="K"), brief="Move to Kept, safe from the mailbox cap",
                ))
            elif self.kept:
                options.append(MenuEntry(label=menu_key("e", " to Inbox", prefix="Mov"), brief="Move back to the Inbox"))
            if not self.sent:
                if measuring or highlighted is None or highlighted.message.is_read:
                    # The longer label, which the page budget measures.
                    options.append(MenuEntry(label=menu_key("U", "nread"), brief="Mark the highlighted message unread"))
                else:
                    options.append(MenuEntry(label=menu_key("U", " Read"), brief="Mark the highlighted message read"))
        if self.folder == _INBOX and (measuring or self._read_count()):
            options.append(MenuEntry(
                label=menu_key("r", "ead", prefix="Delete "), brief="Delete every read message in the Inbox",
            ))
        if self.all_rows:
            options.append(MenuEntry(
                label=menu_key("O", "rder"),
                brief={
                    _ORDER_NEWEST: "Newest first", _ORDER_UNREAD: "Unread mail first",
                    _ORDER_THREADS: "By conversation",
                }[self._next_order()],
            ))
            options.append(MenuEntry(
                label=menu_key("F", "ind"), brief=f"Find by {'recipient' if self.sent else 'sender'} or subject",
            ))
        options.append(MenuEntry(
            label=menu_key("B", "ack"),
            brief="Return to the main menu" if self.folder == _INBOX else "Back to the Inbox",
        ))
        return options

    def _frame(
        self, *, row_count: int, pages: tuple[bool, bool], measuring: bool = False,
    ) -> tuple[str, str]:
        """Everything above the list's rows and everything below them."""
        session = self.session
        width = session.terminal_width
        header = screen_title(
            _FOLDER_TITLES[self.folder],
            breadcrumb=(session.node_display_name, "Mail"),
            subtitle=self._subtitle(),
            width=width,
            clear=self.redraw_in_place,
            unicode_style=self.unicode_style, collapsed=self.collapsed,
            header_color=self.header_color,
            node_name_gradient=session.node_name_gradient,
        )
        # (urgency, text, color): the lower, the more urgent.
        pending: list[tuple[int, str, int]] = []
        draft = _load_letter_draft(_letter_draft_path(self.lane, self.user)) if self.has_draft else None
        if draft is not None:
            # Said on the mail screen, not asked on the way in (issue #814).
            pending.append((1, _letter_draft_notice(draft), MUTED_COLOR))
        if self.kept:
            # Kept has a limit of its own (issue #921).
            kept_note = kept_capacity_note(sum(1 for row in self.received_rows if row.message.kept_at is not None))
            if kept_note is not None:
                pending.append((0, kept_note, WARNING_COLOR))
        elif not self.sent:
            # The cap counts the Inbox alone (issue #921).
            received = [row.message for row in self.received_rows if row.message.kept_at is None]
            capacity = mailbox_capacity_note(len(received), sum(1 for message in received if not message.is_read))
            if capacity is not None:
                text, tone = capacity
                pending.append((0, text, ERROR_COLOR if tone == "error" else WARNING_COLOR))
        if not self.sent and any(row.identity_changed for row in self.all_rows):
            pending.append((2, _IDENTITY_NOTE, WARNING_COLOR))
        notes: list[str] = []
        if not self._roomy() and pending:
            # A short terminal gives the notes one row, the most urgent
            # one's: the list is what the caller came for, and the 40x12
            # floor has three rows for it under a bar that takes four. What
            # the others say is still on screen in brief -- the [D]raft key,
            # the "N of 500" count, a row's "!".
            _urgency, text, color = min(pending, key=lambda item: item[0])
            text = truncate_to_width(
                text, max(1, width - 1), ellipsis=ellipsis_for(self.session, unicode_style=self.unicode_style)
            )
            notes.append(colored(text, fg_color=color))
        else:
            for _urgency, text, color in pending:
                notes.extend(colored(row, fg_color=color) for row in wrap_to_width(text, max(1, width - 1)))
        above = "\r\n".join(["", header, *notes])
        options = self._options(row_count=row_count, pages=pages, measuring=measuring)
        # Descriptions double the action bar. Where they would leave the
        # list fewer rows than a page worth having, the bar goes compact,
        # decided against the busiest bar this folder can draw -- the one
        # the page budget measures -- so the budget and the frame agree.
        busiest = _menu_row(
            self._options(row_count=9, pages=(True, True), measuring=True),
            width=width, height=session.terminal_height, description_level=self.description_level,
        )
        room = (
            session.terminal_height - _count_rows(above, width) - _count_rows(busiest, width)
            - self._furniture_rows() - 1
        )
        compact = self.description_level != "off" and room < _COMFORTABLE_LIST_ROWS
        menu = _menu_row(
            options, width=width, height=session.terminal_height,
            description_level="off" if compact else self.description_level,
        )
        below_rows = [menu]
        # The hint is the first thing a short terminal can spare.
        if session.terminal_height >= _HINT_MIN_HEIGHT:
            below_rows.append(colored("(Up/Down to move, Ctrl-H for help)", fg_color=MUTED_COLOR))
        return above, "\r\n".join(below_rows)

    def _furniture_rows(self) -> int:
        """Rows around the list that are neither header nor action bar: the
        column heading, and when there is room, a blank row and a rule on
        each side."""
        heading = 1 if self.session.terminal_width >= _TABLE_MIN_WIDTH else 0
        return heading + (4 if self._roomy() else 0)

    def _page_limit(self) -> int:
        """As many rows as fit under the frame, measured against the
        busiest frame the folder can draw -- a page does not change size
        because [P]rev page appeared on it."""
        width = self.session.terminal_width
        above, below = self._frame(row_count=9, pages=(True, True), measuring=True)
        fixed = (
            _count_rows(above, width) + self._furniture_rows() + _count_rows(below, width)
            + pending_notice_rows(self.session) + 1
        )
        return max(_MIN_LIST_ROWS, min(_MAX_LIST_ROWS, self.session.terminal_height - fixed))

    async def _render(self) -> None:
        session = self.session
        width = session.terminal_width
        limit = self._page_limit()
        top = self._top(limit)
        page_rows = self.rows[top:top + limit]
        self.shown_top, self.shown_count, self.shown_limit = top, len(page_rows), limit
        pages = (top + limit < len(self.rows), top > 0)
        above, below = self._frame(row_count=len(page_rows), pages=pages)
        roomy = self._roomy()
        rule = colored(("─" if self.unicode_style else "-") * min(width, 78), fg_color=RULE_COLOR)
        lines = [above]
        if roomy:
            lines.append("")
        if page_rows:
            show_status = self._show_status()
            number_width = len(str(len(page_rows)))
            widths = _mail_column_widths(
                page_rows, width=width, number_width=number_width, sent=self.sent, show_status=show_status,
            )
            if widths is not None:
                lines.append(_mail_list_heading(
                    widths, number_width=number_width, sent=self.sent, show_status=show_status,
                    status_heading=_status_heading(self.all_rows),
                ))
            if roomy:
                lines.append(rule)
            lines.extend(_mail_list_rows(
                page_rows, width=width, first_number=1, number_width=number_width, widths=widths,
                highlighted=self.highlighted - top if self.highlighted is not None else None,
                sent=self.sent, show_status=show_status, accent=self.accent,
                ellipsis=ellipsis_for(self.session, unicode_style=self.unicode_style), marked=self.marked,
            ))
            if roomy:
                lines.append(rule)
        else:
            if self.query:
                empty = f"Nothing here matches \"{sanitize_text(self.query)}\". [F]ind with an empty line shows all."
            else:
                empty = {_INBOX: _EMPTY_INBOX, _SENT: _EMPTY_SENT, _KEPT: _EMPTY_KEPT}[self.folder]
            lines.append(colored(empty, fg_color=MUTED_COLOR))
        if roomy:
            lines.append("")
        lines.append(below)
        for line in lines:
            await session.write_line(line)
        await write_notices(session)
        await write_prompt(session, self.choice_prompt() if self.choice_prompt is not None else "Choice: ")


async def _message_view(
    session: Session,
    lane: DatabaseLane,
    user: User,
    *,
    message: MailMessage,
    to_label: str | None,
    unicode_style: bool = False,
    collapsed: bool = False,
    copies: list[MailMessage] | None = None,
) -> tuple[str, list[str], list[str]]:
    """The title, the header rows (From or To, Date, any identity warning)
    and the body rows of one message, for `show_detail` to draw a page at a
    time (issue #679: a long message used to scroll its own header away)."""
    mailbox = "Sent" if to_label is not None else ("Kept" if message.kept_at is not None else "Inbox")
    title = screen_title(
        sanitize_text(message.subject),
        breadcrumb=(session.node_display_name, "Mail", mailbox),
        width=session.terminal_width,
        clear=False,
        unicode_style=unicode_style, collapsed=collapsed,
        header_color=await lane.run(effective_header_color_256),
        node_name_gradient=session.node_name_gradient,
    )
    accent = await lane.run(effective_accent_color_256)
    display_format, display_timezone = await lane.run(resolve_display_preferences)
    preamble: list[str] = []
    if to_label is not None:
        preamble.append(colored("To: ", fg_color=LABEL_COLOR) + colored(sanitize_text(to_label), fg_color=accent))
        if message.mail_group_id is not None:
            # A letter to several people (issue #827): each Link copy has a
            # delivery of its own, named by whom it went to.
            for copy in copies or [message]:
                shown_status = delivery_display_status(copy.link_delivery_status, copy.link_relay_handoff_at)
                delivery = delivery_explanation(shown_status, copy.link_delivery_reason)
                if delivery is None:
                    continue
                name = sanitize_text(await lane.run(copy_recipient_label, copy))
                preamble.append(
                    colored(f"Delivery to {name}: ", fg_color=LABEL_COLOR)
                    + colored(delivery, fg_color=_DELIVERY_COLORS.get(shown_status, VALUE_COLOR))
                )
                if copy.resent_at is not None:
                    preamble.append(
                        _resent_line(f"Resent to {name}: ", copy.resent_at, display_format, display_timezone)
                    )
        else:
            shown_status = delivery_display_status(message.link_delivery_status, message.link_relay_handoff_at)
            delivery = delivery_explanation(shown_status, message.link_delivery_reason)
            if delivery is not None:
                preamble.append(
                    colored("Delivery: ", fg_color=LABEL_COLOR)
                    + colored(delivery, fg_color=_DELIVERY_COLORS.get(shown_status, VALUE_COLOR))
                )
            if message.resent_at is not None:
                preamble.append(_resent_line("Resent: ", message.resent_at, display_format, display_timezone))
        preamble.extend(await _read_receipt_lines(
            lane, user, message, copies or [message],
            when=lambda iso: format_for_display(
                iso, override_format=display_format, override_timezone=display_timezone
            ),
        ))
    else:
        sender_label = await _display_sender_label(lane, message)
        preamble.append(
            colored("From: ", fg_color=LABEL_COLOR) + colored(sanitize_text(sender_label), fg_color=accent)
        )
        warning = (
            None if message.from_system else await _link_mail_identity_warning(lane, message.sender_label)
        )
        if warning is not None:
            preamble.append(colored(warning, fg_color=MUTED_COLOR, bold=True))
        if message.from_system:
            preamble.append(colored(_system_mail_note(session), fg_color=MUTED_COLOR))
        # Received mail names its recipient too (issue #810): the reader,
        # as a letter's envelope would -- or everyone a letter to several
        # people went to (issue #827).
        received_to = await lane.run(group_to_label, message) or user.username
        preamble.append(colored("To: ", fg_color=LABEL_COLOR) + colored(sanitize_text(received_to), fg_color=accent))
    displayed_date = format_for_display(
        message.created_at, override_format=display_format, override_timezone=display_timezone
    )
    preamble.append(colored("Date: ", fg_color=LABEL_COLOR) + colored(displayed_date, fg_color=METADATA_COLOR))
    # Files the letter points at (issue #830), as this reader finds them.
    refs = await lane.run(_letter_file_refs, message, copies)
    preamble.extend(ref_rows(await open_refs(lane, user, refs), accent=accent))
    body_mode = await lane.run(_mail_body_mode, user)
    truecolor = await lane.run(lambda db: effective_truecolor(session, db, user))
    body_rows = post_body_rows(message.body, session.terminal_width, body_mode, truecolor=truecolor, layout="lines")
    return title, preamble, body_rows


def _resent_line(label: str, resent_at: str, display_format, display_timezone) -> str:
    """The line under a failed letter's Delivery saying it was sent again
    (issue #919), and where the new one is."""
    when = format_for_display(resent_at, override_format=display_format, override_timezone=display_timezone)
    return colored(label, fg_color=LABEL_COLOR) + colored(f"{when} (the new copy is in Sent)", fg_color=METADATA_COLOR)


def _letter_file_refs(db: Database, message: MailMessage, copies: list[MailMessage] | None = None) -> list[FileRef]:
    """The files `message` points at (issue #830). A letter to several
    people writes them with each local copy and none with a Link copy, so
    the Sent view of one -- shown by one of its copies -- takes them from
    whichever copy has them."""
    refs = mail_refs(db, message.id)
    if refs or not copies:
        return refs
    for copy in copies:
        refs = mail_refs(db, copy.id)
        if refs:
            return refs
    return []


_RECEIPTS_OFF_TEXT = "not shown, as you don't share read receipts yourself (Profile)"


async def _read_receipt_lines(
    lane: DatabaseLane, user: User, message: MailMessage, copies: list[MailMessage],
    *, when: Callable[[str], str],
) -> list[str]:
    """The read-receipt lines of a letter `user` sent (issue #829), for its
    local copies; none for Link mail or a deleted account's copy.

    A letter to one person has one `Read:` line. A letter to several people
    names its recipients by what their receipts say -- read (with when),
    not read yet, not shared -- a line each, which keeps twenty recipients
    to three lines. A letter deleted unopened is "not read yet" like any
    other (issue #922): the recipient's deletion is not the sender's to see. Mail to all callers counts instead of
    naming. A recipient who does not share receipts is always said to, even
    to a sender who does not share them either: that is only their setting,
    and it keeps "not read" from being guessed. Of everything else, a sender
    who does not share receipts is told only that it is not shown."""
    receipts = await lane.run(read_receipts, user, copies)
    if not receipts:
        return []

    def line(label: str, text: str, color: int) -> str:
        return colored(label, fg_color=LABEL_COLOR) + colored(text, fg_color=color)

    if message.mail_group_id is None:
        receipt = receipts.get(message.id)
        if receipt is None:
            return []
        name = sanitize_text(await lane.run(copy_recipient_label, message))
        if receipt.state == RECEIPT_READ and receipt.read_at:
            return [line("Read: ", when(receipt.read_at), SUCCESS_COLOR)]
        text, color = {
            RECEIPT_NOT_READ: ("not yet", MUTED_COLOR),
            RECEIPT_WITHHELD: (f"not shown, as {name} doesn't share read receipts", MUTED_COLOR),
            RECEIPT_HIDDEN: (_RECEIPTS_OFF_TEXT, MUTED_COLOR),
        }[receipt.state]
        return [line("Read: ", text, color)]
    states = [receipts[copy.id].state for copy in copies if copy.id in receipts]
    if is_to_all_callers(message):
        # Hundreds of names would be no list at all: a count, of those who
        # share receipts only.
        withheld = states.count(RECEIPT_WITHHELD)
        others = f" ({withheld} more don't share them)" if withheld else ""
        if RECEIPT_HIDDEN in states:
            return [line("Read: ", _RECEIPTS_OFF_TEXT + others, MUTED_COLOR)]
        sharing = len(states) - withheld
        if not sharing:
            return [line("Read: ", "not shown, as no recipient shares read receipts", MUTED_COLOR)]
        read = states.count(RECEIPT_READ)
        return [line("Read: ", f"by {read} of the {sharing} who share read receipts{others}", VALUE_COLOR)]
    grouped: dict[str, list[str]] = {}
    for copy in copies:
        receipt = receipts.get(copy.id)
        if receipt is None:
            continue
        name = sanitize_text(await lane.run(copy_recipient_label, copy))
        if receipt.state == RECEIPT_READ and receipt.read_at:
            name = f"{name} ({when(receipt.read_at)})"
        grouped.setdefault(receipt.state, []).append(name)
    lines: list[str] = []
    for state, label, color in (
        (RECEIPT_READ, "Read by: ", SUCCESS_COLOR),
        (RECEIPT_NOT_READ, "Not read yet: ", MUTED_COLOR),
        (RECEIPT_WITHHELD, "Don't share read receipts: ", MUTED_COLOR),
    ):
        if state in grouped:
            lines.append(line(label, ", ".join(grouped[state]), color))
    if RECEIPT_HIDDEN in grouped:
        lines.append(line("Read: ", _RECEIPTS_OFF_TEXT, MUTED_COLOR))
    return lines


def _mail_body_mode(db: Database, user: User) -> str:
    """How `user` sees a mail body (issue #809): as a board post on a
    board that allows color -- pipe codes and SGR filtered by
    `netbbs.rendering.post_body`, the same for local mail and mail
    carried from another node -- in color, or plain with the codes
    removed when the reader turned "Pos[t] colors" off. Mail has no
    SysOp setting of its own: a letter is between its writer and its
    reader, and the filter already keeps a body from moving the cursor
    or clearing the screen."""
    return post_body_mode(board_allows_color=True, reader_wants_color=post_colors_enabled(db, user))


async def _show_message(
    session: Session,
    lane: DatabaseLane,
    user: User,
    message: MailMessage,
    *,
    to_label: str | None,
    actions: list[tuple[str, str]],
    page: int,
    copies: list[MailMessage] | None = None,
) -> tuple[str, int]:
    """One message on `show_detail`: returns the action key and the page
    it was pressed on. `copies` are the Sent copies of a letter to several
    people (issue #827)."""
    unicode_style = await lane.run(unicode_style_enabled, user)
    title, preamble, body_rows = await _message_view(
        session, lane, user, message=message, to_label=to_label,
        unicode_style=unicode_style,
        collapsed=await lane.run(breadcrumb_collapsed_enabled, user),
        copies=copies,
    )
    return await show_detail(
        session,
        title=title,
        sections=[Section(None, [Styled(body_rows)])],
        actions=actions,
        redraw_in_place=await lane.run(redraw_in_place_enabled, user),
        unicode_style=unicode_style,
        page=page,
        preamble=preamble,
        message="\r\n".join(take_notices(session)) or None,
    )


# `netbbs.mail.split_link_address`, public here for the board reader's
# [M]ail author (issue #821); this module's own call sites keep the name they
# had.
_split_link_address = split_link_address


async def _display_link_address(lane: DatabaseLane, technical_address: str) -> str:
    """Resolve a stored Link address's technical home node only at render
    time; a local name is returned as it is."""
    return await lane.run(link_address_display_label, technical_address)


async def _display_sender_label(lane: DatabaseLane, message: MailMessage) -> str:
    """`netbbs.mail.sender_display_label`: the name a received letter shows
    as its sender, which Find matches too (issue #824)."""
    return await lane.run(sender_display_label, message)


def _system_mail_note(session: Session) -> str:
    """What a system message's view says about its sender (issue #819),
    in place of the Reply key it does not offer."""
    name = sanitize_text(session.node_display_name)
    return f"A notice from {name} itself. There is no one to reply to."


async def _display_recipient_label(lane: DatabaseLane, message: MailMessage) -> str:
    """`netbbs.mail.recipient_display_label`: the name a sent letter shows
    as its recipient, which Find matches too (issue #824)."""
    return await lane.run(recipient_display_label, message)


_DELIVERY_COLORS = {
    "pending": MUTED_COLOR,
    RELAYED_DISPLAY_STATUS: MUTED_COLOR,
    "delivered": SUCCESS_COLOR,
    "bounced": ERROR_COLOR,
    "expired": ERROR_COLOR,
    # Dealt with (issue #919): the new copy has a row of its own.
    _RESENT_STATUS: MUTED_COLOR,
    _RECEIPT_READ_ALL: SUCCESS_COLOR,
    _RECEIPT_SOME_READ: VALUE_COLOR,
    _RECEIPT_NONE_READ: MUTED_COLOR,
    _RECEIPT_NOT_SHARED: MUTED_COLOR,
}


async def _link_mail_identity_warning(
    lane: DatabaseLane, technical_address: str,
) -> str | None:
    split = _split_link_address(technical_address)
    if split is None:
        return None
    _user_id, fingerprint = split
    observation = await lane.run(latest_identity_observation, fingerprint)
    if observation is None or observation.severity != "security":
        return None
    return (
        "Caution: this familiar node name now has a different cryptographic identity. "
        "Mail remains available, but verify the change if it was unexpected."
    )


async def _show_inbox_message(
    session: Session, lane: DatabaseLane, user: User, message: MailMessage,
    *, link_context: LinkContext | None = None, transfers: TransferGrants | None = None,
) -> None:
    message = await lane.run(mark_read, user, message)
    # Issue #830: `[G]et file` downloads a file the letter points at.
    refs = await lane.run(_letter_file_refs, message)
    block_target = await lane.run(_block_target, user, message)
    # Issue #827: a copy of a letter to several people answers them all.
    reply_all = await lane.run(_reply_all_entries, user, message)
    page = 0
    while True:
        # Mail the BBS sent has nobody to answer (issue #819): no Reply key,
        # and the view says why.
        actions = [] if message.from_system else [("r", menu_key("R", "eply"))]
        if len(reply_all) > 1:
            actions.append(("a", menu_key("a", "ll", prefix="Reply ")))
        actions += [
            # Offered on system mail too (issue #822): passing a notice on
            # to someone -- the SysOp, say -- harms no one.
            ("f", menu_key("F", "orward")),
            *([("g", menu_key("G", "et file"))] if refs else []),
            ("u", menu_key("U", "nread")),
            # Issue #828: to the Kept folder, which the cap never evicts
            # from, and back; Kept has its own limit (issue #921).
            ("e", menu_key("e", " to Inbox", prefix="Mov") if message.kept_at else menu_key("e", "ep", prefix="K")),
            ("d", menu_key("D", "elete")),
        ]
        if block_target is not None:
            # Issue #817: a toggle, labelled by what it will do.
            blocked = await lane.run(is_blocked, user, block_target)
            actions.append(("k", menu_key("k", " sender", prefix="Unbloc" if blocked else "Bloc")))
        actions.append(("b", menu_key("B", "ack")))
        choice, page = await _show_message(session, lane, user, message, to_label=None, actions=actions, page=page)
        if choice == "b":
            return
        if choice == "k":
            text, tone = await lane.run(toggle_block, user, block_target)
            announce(session, text, tone=tone)
            continue
        if choice == "f":
            await _forward_message(session, lane, user, message, sent=False, link_context=link_context)
            continue
        if choice == "g":
            await get_referenced_file(
                session, lane, user, refs, noun="letter", breadcrumb=("Mail",),
                style=await _picker_style(lane, user), transfers=transfers,
            )
            continue
        if choice == "a":
            shown = await _display_sender_label(lane, message)
            await _write_to_several(
                session, lane, user, reply_all,
                subject=reply_subject(message.subject, max_bytes=MAX_MAIL_SUBJECT_BYTES),
                body=quote_body(plain_post_body(message.body), author=shown),
                link_context=link_context, reply_key=_reply_all_key(message),
            )
            continue
        if choice == "e":
            keep = message.kept_at is None
            try:
                await lane.run(set_kept, user, [message.id], kept=keep)
            except KeptFullError as exc:
                # Refused in place (issue #921): the letter stays open.
                announce(session, str(exc), tone="error")
                continue
            announce(session, "Moved to Kept." if keep else "Moved back to the Inbox.", tone="muted")
            return
        if choice == "u":
            # Opening a message is what marks it read; this takes that back
            # (issue #810), and the list shows it "new" again.
            await lane.run(mark_unread, user, message)
            announce(session, "Marked unread.", tone="muted")
            return
        if choice == "d":
            if not await prompt_yes_no(session, "Delete this message?", default=False):
                continue
            await lane.run(delete_for_recipient, user, message)
            announce(session, "Message deleted.")
            return
        if message.from_system:
            announce(session, _system_mail_note(session), tone="error")
            continue
        # The same subject rule and quote a board reply uses (issue #675).
        subject = reply_subject(message.subject, max_bytes=MAX_MAIL_SUBJECT_BYTES)
        link_sender = (
            _split_link_address(message.sender_label) if message.sender_user_id is None else None
        )
        if link_sender is not None:
            # Mail from another BBS has no local sender account; the reply
            # goes back over Link to the address it came from (issue #805).
            shown = await _display_sender_label(lane, message)
            if link_context is None:
                announce(
                    session,
                    f"This BBS is not linked with other BBSes right now, so a reply can't reach {shown}.",
                    tone="error",
                )
                continue
            checked = await lane.run(_check_link_reply_address, message.sender_label)
            if isinstance(checked, str):
                announce_styled(session, colored(checked, fg_color=ERROR_COLOR))
                continue
            await _compose_mail(
                session, lane, user, prefill_link_address=message.sender_label,
                prefill_subject=subject,
                prefill_body=quote_body(plain_post_body(message.body), author=shown) or None,
                link_context=link_context, reply_key=_reply_key(message),
            )
            continue
        sender = (
            await lane.run(get_user_by_id, message.sender_user_id)
            if message.sender_user_id is not None
            else None
        )
        if sender is None:
            announce(session, "That sender's account no longer exists -- can't reply.", tone="error")
            continue
        await _compose_mail(
            session, lane, user, prefill_recipient=sender,
            prefill_subject=subject,
            prefill_body=quote_body(plain_post_body(message.body), author=message.sender_label) or None,
            reply_key=_reply_key(message),
        )


async def _picker_style(lane: DatabaseLane, user: User) -> dict:
    return {
        "description_level": await lane.run(menu_description_level, user),
        "redraw_in_place": await lane.run(redraw_in_place_enabled, user),
        "unicode_style": await lane.run(unicode_style_enabled, user),
        "collapsed": await lane.run(breadcrumb_collapsed_enabled, user),
        "accent_color": await lane.run(effective_accent_color_256),
        "header_color": await lane.run(effective_header_color_256),
    }


# -- attaching files (issue #830) --------------------------------------------
#
# The review screen of a letter has `[A]ttach file`, and `[R]emove file` once
# one is attached: a file in a file area here, chosen from the areas and files
# the writer can open (`netbbs.net.file_ref_view.change_attached_files`, shared
# with board posts since issue #924). The letter points at it; nothing is
# copied. Who it goes to is checked at Send.


def _file_rows(files: list[FileRef], *, accent: int, to_another_bbs: bool) -> list[str]:
    rows = attached_rows(files, accent=accent)
    if files and to_another_bbs:
        rows.append(colored(
            "Someone on another BBS gets each file's name, size and file area as text at the end of the "
            "letter, not a download.",
            fg_color=MUTED_COLOR,
        ))
    return rows


def _files_field(files: list[FileRef]) -> dict[str, str]:
    """A letter's files as its draft keeps them: a `"files"` field holding a
    JSON string (the draft's fields are strings), none without files."""
    if not files:
        return {}
    return {"files": json.dumps([
        {"file_id": ref.file_id, "filename": ref.filename, "area": ref.area_name, "size": ref.size_bytes}
        for ref in files
    ])}


def _decode_files(text: str | None) -> list[FileRef]:
    if not text:
        return []
    try:
        entries = json.loads(text)
    except ValueError:
        return []
    files = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        file_id, filename, area, size = entry.get("file_id"), entry.get("filename"), entry.get("area"), entry.get("size")
        if isinstance(file_id, str) and isinstance(filename, str) and isinstance(area, str) and isinstance(size, int):
            files.append(FileRef(file_id=file_id, filename=filename, area_name=area, size_bytes=size))
    return files[:MAX_FILE_REFS]


# -- blocked people (issues #817, #925) ----------------------------------------
#
# A caller blocks someone from a letter they received (`Bloc[k] sender` on
# its view, a toggle), from Who's online (`Bloc[k]`, the same toggle), or by
# name from Profile > Blocked people, which lists them and unblocks. Local
# accounts are blocked by account id, Link users by their `user@<fingerprint>`
# address. One list stops both mail and live messages (issue #925). The rules
# -- who cannot be blocked, what the sender is told -- are `netbbs.mail`'s
# for mail and `netbbs.messaging_preferences.live_message_refusal`'s for live
# messages.

_BLOCKED_NOTICE = "Blocked {name}: their mail and live messages are refused from now on, and they are told so."
_UNBLOCKED_NOTICE = "Unblocked {name}: their mail and live messages are accepted again."


@dataclass(frozen=True)
class BlockTarget:
    """Who a `Bloc[k]` toggle acts on -- on a received letter or on Who's
    online: a local account by id, or a Link user by address."""
    user_id: int | None
    address: str | None


def _block_target(db: Database, reader: User, message: MailMessage) -> BlockTarget | None:
    """The sender a received letter's view can block, or `None`: system
    mail, a deleted account, the reader's own mail, and a SysOp of this
    node offer no block (`netbbs.mail.sender_unblockable_reason`)."""
    if message.from_system:
        return None
    if message.sender_user_id is not None:
        sender = get_user_by_id(db, message.sender_user_id)
        if sender is None or sender_unblockable_reason(db, reader, sender) is not None:
            return None
        return BlockTarget(user_id=sender.id, address=None)
    if _split_link_address(message.sender_label) is not None:
        return BlockTarget(user_id=None, address=message.sender_label)
    return None


_link_sender_name = link_address_display_label


def is_blocked(db: Database, reader: User, target: BlockTarget) -> bool:
    """Whether `reader` blocks `target` (mail and live messages alike)."""
    if target.user_id is not None:
        sender = get_user_by_id(db, target.user_id)
        return sender is not None and blocks_local_sender(db, reader, sender)
    assert target.address is not None
    return blocks_link_sender(db, reader, target.address)


def toggle_block(db: Database, reader: User, target: BlockTarget) -> tuple[str, str]:
    """Block `target` if they are not blocked, else unblock them. Returns
    the outcome line and its tone. Shared by the letter view and Who's
    online (issue #925)."""
    if target.user_id is not None:
        sender = get_user_by_id(db, target.user_id)
        if sender is None:
            return "That sender's account no longer exists.", "error"
        if blocks_local_sender(db, reader, sender):
            unblock_local_sender(db, reader, sender.id)
            return _UNBLOCKED_NOTICE.format(name=sender.username), "success"
        try:
            block_local_sender(db, reader, sender)
        except MailBlockError as exc:
            return str(exc), "error"
        return _BLOCKED_NOTICE.format(name=sender.username), "success"
    assert target.address is not None
    name = _link_sender_name(db, target.address)
    if blocks_link_sender(db, reader, target.address):
        unblock_link_sender(db, reader, target.address)
        return _UNBLOCKED_NOTICE.format(name=name), "success"
    block_link_sender(db, reader, target.address)
    return _BLOCKED_NOTICE.format(name=name), "success"


@dataclass(frozen=True)
class _BlockedRow:
    block: MailBlock
    name: str
    where: str


def _load_blocked_rows(db: Database, user: User) -> list[_BlockedRow]:
    display_format, display_timezone = resolve_display_preferences(db)
    rows = []
    for block in list_mail_blocks(db, user):
        since = format_for_display(block.created_at, override_format=display_format, override_timezone=display_timezone)
        if block.blocked_user_id is not None:
            account = get_user_by_id(db, block.blocked_user_id)
            name = account.username if account is not None else "(deleted account)"
            if account is not None and is_usable_sysop(account):
                # Kept, but not applied while they run the board (#817).
                where = f"a SysOp now, so not applied; blocked {since}"
            else:
                where = f"on this BBS; blocked {since}"
        else:
            assert block.blocked_address is not None
            name = _link_sender_name(db, block.blocked_address)
            where = f"on a linked BBS; blocked {since}"
        rows.append(_BlockedRow(block=block, name=name, where=where))
    return rows


def _block_by_name(db: Database, user: User, text: str) -> tuple[str, str]:
    """Block the sender `text` names: a local user name, or `name@TheirBBS`
    for someone on a linked BBS. Returns the outcome line and its tone."""
    if "@" in text:
        resolved = _resolve_link_address(db, text)
        if isinstance(resolved, str):
            return resolved, "error"
        name = link_address_label(resolved.user, identity_for_fingerprint(db, resolved.fingerprint).label)
        if not block_link_sender(db, user, f"{resolved.user}@{resolved.fingerprint}"):
            return f"{name} is already blocked.", "muted"
        return _BLOCKED_NOTICE.format(name=name), "success"
    try:
        sender = get_user_by_username(db, text)
    except AuthError:
        return f"No such user: {text!r}", "error"
    try:
        added = block_local_sender(db, user, sender)
    except MailBlockError as exc:
        return str(exc), "error"
    if not added:
        return f"{sender.username} is already blocked.", "muted"
    return _BLOCKED_NOTICE.format(name=sender.username), "success"


def _unblock_row(db: Database, user: User, row: _BlockedRow) -> tuple[str, str]:
    unblock(db, user, row.block)
    return _UNBLOCKED_NOTICE.format(name=row.name), "success"


async def blocked_senders_screen(session: Session, lane: DatabaseLane, user: User) -> None:
    """Profile > Blocked people (issues #817, #925): everyone `user` refuses
    mail and live messages from, newest first. `[A]dd` blocks someone by name -- a local user, or
    `name@TheirBBS` for someone on a linked BBS -- and `[U]nblock`, or
    picking a row, unblocks it. Each outcome is carried into the redraw."""

    async def _reload() -> list[_BlockedRow]:
        return await lane.run(_load_blocked_rows, user)

    async def _add() -> list[_BlockedRow] | None:
        await session.write_line("")
        await write_prompt(session, "Block (a user name, or name@TheirBBS; empty cancels): ")
        try:
            text = (await session.read_line(cancellable=True)).strip()
        except InputCancelled:
            text = ""
        if not text:
            return None
        outcome, tone = await lane.run(_block_by_name, user, text)
        announce(session, outcome, tone=tone)
        return await _reload()

    async def _unblock(row: _BlockedRow) -> list[_BlockedRow]:
        outcome, tone = await lane.run(_unblock_row, user, row)
        announce(session, outcome, tone=tone)
        return await _reload()

    rows = await _reload()
    while True:
        selected = await pick_item(
            session, rows,
            name_of=lambda row: row.name,
            stable_id_of=lambda row: row.block.id,
            description_of=lambda row: row.where,
            title="Blocked people",
            breadcrumb=("Profile",),
            empty_message="You block no one. Mail and live messages from anyone reach you.",
            refresh=_reload,
            live_keys={"a": _add},
            item_keys={"u": _unblock},
            live_nav=[
                MenuEntry(label=menu_key("A", "dd"), brief="Block someone by name"),
                MenuEntry(label=menu_key("U", "nblock"), brief="Accept their mail and messages again"),
            ],
            description_level=await lane.run(menu_description_level, user),
            redraw_in_place=await lane.run(redraw_in_place_enabled, user),
            unicode_style=await lane.run(unicode_style_enabled, user),
            collapsed=await lane.run(breadcrumb_collapsed_enabled, user),
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
        )
        if selected is None:
            return
        rows = await _unblock(selected)


async def _show_sent_message(
    session: Session, lane: DatabaseLane, user: User, message: MailMessage,
    *, link_context: LinkContext | None = None,
) -> None:
    """A letter the caller sent. `[R]eply` writes to its recipient again (a
    follow-up, issue #825); `Re[s]end`, on Link mail that bounced or
    expired, sends the same letter again as a new one, and the old one then
    shows as resent (issue #919). A letter sent from either, or from
    `[F]orward` (issue #919), returns to the Sent list, where it now is,
    with "Message sent." above the prompt; anything else comes back to
    this view."""
    if message.mail_group_id is not None:
        await _show_sent_group(session, lane, user, message, link_context=link_context)
        return
    to_label = await _display_recipient_label(lane, message)
    failed = message.link_delivery_status in _FAILED_STATUSES
    if failed:
        # Seen here, so the main menu need not tell it again (issue #806).
        await lane.run(acknowledge_delivery_notices, [message.id])
    actions = [("r", menu_key("R", "eply"))]
    if failed and message.recipient_remote_address is not None:
        # Only a letter that did not arrive (issue #825): one that did, or
        # may yet, would reach its reader twice. Once resent, the key says
        # it would be another copy (issue #919).
        again = " again" if message.resent_at is not None else ""
        actions.append(("s", menu_key("s", "end" + again, prefix="Re")))
    actions += [("f", menu_key("F", "orward")), ("d", menu_key("D", "elete")), ("b", menu_key("B", "ack"))]
    page = 0
    while True:
        choice, page = await _show_message(session, lane, user, message, to_label=to_label, actions=actions, page=page)
        if choice == "b":
            return
        if choice == "f":
            # Like Reply and Resend (issue #919): a letter sent from here
            # returns to the list it was opened from.
            if await _forward_message(session, lane, user, message, sent=True, link_context=link_context):
                return
            continue
        if choice in ("r", "s"):
            if await _write_to_recipient(
                session, lane, user, message, resend=choice == "s", link_context=link_context,
            ):
                return
            continue
        if not await prompt_yes_no(session, "Delete this message?", default=False):
            continue
        await lane.run(delete_for_sender, user, message)
        announce(session, "Message deleted.")
        return


async def _show_sent_group(
    session: Session, lane: DatabaseLane, user: User, message: MailMessage,
    *, link_context: LinkContext | None,
) -> None:
    """A letter the caller sent to several people (issue #827), shown once
    for all its copies: To names everyone, and each Link copy's delivery is
    on a line of its own. `[R]eply` writes to them all again; `Re[s]end`
    sends the letter again to those whose copy bounced or expired, and to
    no one else; `[D]elete` removes every copy from Sent. Mail to all
    callers has no Reply or Resend: it is sent from the SysOp console."""
    copies = await lane.run(sent_group_copies, user, message)
    to_all = is_to_all_callers(message)
    failed = [copy for copy in copies if copy.link_delivery_status in _FAILED_STATUSES]
    if failed:
        # Seen here, so the main menu need not tell them again (issue #806).
        await lane.run(acknowledge_delivery_notices, [copy.id for copy in failed])
    # Resend goes to the failed copies not yet resent; once every one was
    # (issue #919), "Resend again" goes to them all again.
    to_resend = [copy for copy in failed if copy.resent_at is None] or failed
    actions: list[tuple[str, str]] = []
    if not to_all:
        actions.append(("r", menu_key("R", "eply")))
        if failed:
            again = " again" if all(copy.resent_at is not None for copy in failed) else ""
            actions.append(("s", menu_key("s", "end" + again, prefix="Re")))
    actions += [("f", menu_key("F", "orward")), ("d", menu_key("D", "elete")), ("b", menu_key("B", "ack"))]
    to_label = await _display_recipient_label(lane, message)
    page = 0
    while True:
        choice, page = await _show_message(
            session, lane, user, message, to_label=to_label, actions=actions, page=page, copies=copies,
        )
        if choice == "b":
            return
        if choice == "f":
            # Like Reply and Resend (issue #919): a letter sent from here
            # returns to the list it was opened from.
            if await _forward_message(session, lane, user, message, sent=True, link_context=link_context):
                return
            continue
        if choice == "r":
            entries = await lane.run(_sent_group_entries, copies)
            if await _write_to_several(
                session, lane, user, entries,
                subject=reply_subject(message.subject, max_bytes=MAX_MAIL_SUBJECT_BYTES),
                body=quote_body(plain_post_body(message.body), author=user.username),
                link_context=link_context, reply_key=_reply_key(message),
            ):
                return
            continue
        if choice == "s":
            if link_context is None:
                announce(session, "This BBS is not linked with other BBSes right now, so it can't be resent.", tone="error")
                continue
            # The letter as it was sent, to the ones it did not reach.
            if await _write_to_several(
                session, lane, user,
                [copy.recipient_remote_address for copy in to_resend if copy.recipient_remote_address],
                subject=message.subject, body=post_body_text(message.body),
                link_context=link_context, resend_key=_resend_key(message),
                resend_of=tuple(copy.id for copy in to_resend),
            ):
                return
            continue
        question = f"Delete this message? It goes from Sent for all {len(copies)} recipients."
        if not await prompt_yes_no(session, question, default=False):
            continue
        await lane.run(delete_letters, user, [copy.id for copy in copies], sent=True)
        announce(session, "Message deleted.")
        return


async def _write_to_recipient(
    session: Session, lane: DatabaseLane, user: User, message: MailMessage,
    *, resend: bool, link_context: LinkContext | None,
) -> bool:
    """[R]eply or Re[s]end on a sent letter (issue #825): a new letter to
    the one it went to, by `mail_someone`, so every check a letter to them
    makes is made before anything is written, and again at Send.

    A reply is a follow-up: "Re:" and the caller's own letter quoted, as a
    reply to a received one is -- the recipient may no longer have it, and
    the quote is easy to delete. A resend is the letter itself, unquoted,
    under its own subject, in a draft slot of its own (`_resend_key`); the
    old row stays as it is. Returns whether a letter was sent."""
    if message.recipient_remote_address is None and message.recipient_user_id is None:
        # Deleted since it was sent (issue #818): no one to write to.
        name = f"{sanitize_text(message.recipient_label)}'s account" if message.recipient_label else "That account"
        what = "this letter can't be sent again" if resend else "a reply can't reach them"
        announce(session, f"{name} no longer exists, so {what}.", tone="error")
        return False
    if resend:
        subject = message.subject
        # The letter as it was sent, signature and all: escape sequences
        # out, color pipe codes kept (issue #809).
        body = post_body_text(message.body)
        keys = {"resend_key": _resend_key(message), "resend_of": (message.id,)}
    else:
        subject = reply_subject(message.subject, max_bytes=MAX_MAIL_SUBJECT_BYTES)
        body = quote_body(plain_post_body(message.body), author=user.username)
        keys = {"reply_key": _reply_key(message)}
    if message.recipient_remote_address is not None:
        return await mail_someone(
            session, lane, user, link_address=message.recipient_remote_address, subject=subject,
            quote=body, link_context=link_context, **keys,
        )
    recipient = await lane.run(get_user_by_id, message.recipient_user_id)
    if recipient is None:
        announce(session, "That account no longer exists, so a letter can't reach it.", tone="error")
        return False
    return await mail_someone(
        session, lane, user, recipient=recipient, subject=subject, quote=body, link_context=link_context, **keys,
    )


# -- a letter opened from outside the mailbox (issue #824) --------------------


LETTER_GONE_NOTICE = "That message is no longer in your mailbox."


def current_letter(db: Database, user: User, mail_id: int, *, sent: bool) -> MailMessage | None:
    """Letter `mail_id` as it stands now, while it is still in `user`'s
    Sent folder (`sent`) or Inbox; `None` once they deleted it from that
    side, or it is gone altogether."""
    try:
        message = get_mail(db, user, mail_id)
    except MailError:
        return None
    if sent:
        still_there = message.sender_user_id == user.id and message.sender_deleted_at is None
    else:
        still_there = message.recipient_user_id == user.id and message.recipient_deleted_at is None
    return message if still_there else None


async def open_letter(
    session: Session, lane: DatabaseLane, user: User, mail_id: int, *, sent: bool,
    link_context: LinkContext | None = None, transfers: TransferGrants | None = None,
) -> bool:
    """Open one of the caller's own letters found by the main menu's Find
    (issue #824) in the mailbox's own message view, with all its actions:
    an Inbox letter is marked read on opening, as in the mailbox. `B`ack
    returns to the caller's screen.

    Mail's gate is checked again here (issue #816): the SysOp can close
    mail while the results are on screen. Returns whether the letter is
    still in that folder afterwards -- `False` once the caller deleted it
    in the view, or when it was gone before it could open (said so)."""
    refusal = await lane.run(lambda db: caller_mail_refusal(session, db, user))
    if refusal is not None:
        announce(session, refusal, tone="error")
        return True
    message = await lane.run(lambda db: current_letter(db, user, mail_id, sent=sent))
    if message is None:
        announce(session, LETTER_GONE_NOTICE, tone="muted")
        return False
    if sent:
        await _show_sent_message(session, lane, user, message, link_context=link_context)
    else:
        await _show_inbox_message(session, lane, user, message, link_context=link_context, transfers=transfers)
    return await lane.run(lambda db: current_letter(db, user, mail_id, sent=sent)) is not None


# -- mail from where callers meet (issue #821) --------------------------------
#
# Directory, Who's online, Previous callers and the board reader each offer a
# Mail action. They all come through here, so every one of them makes the same
# checks the mailbox's own To prompt makes, and lands on the same compose
# screen with the recipient already filled in.
#
# None of them offers Mail to a local caller who has blocked the viewer
# (issues #948 and #953): `mail_blocked_notice` says why, where the screen has
# room. A linked node's block list is not visible here, so a Link address is
# still offered and the To prompt or a bounce answers.


async def mail_open_to(session: Session, lane: DatabaseLane, user: User) -> bool:
    """Whether a screen outside the mailbox offers its Mail action at all:
    only while mail is open to the caller (issue #816). `mail_someone`
    checks again when the key is pressed, since the SysOp can close mail
    while the screen is up."""
    return await lane.run(lambda db: caller_mail_refusal(session, db, user)) is None


def mail_blocked_notice(db: Database, recipient: User, *, sender: User) -> str | None:
    """"<name> does not accept messages or mail from you." when local
    `recipient` has blocked local `sender`, else `None` (issues #948, #953).

    A screen that meets callers hides its Mail action on this, and shows
    the sentence where it has room: the block stops live messages as well
    as letters, so it says both. `mail_sender_refusal` decides it, so a
    SysOp of this node, whom nobody can block, is never told it."""
    if mail_sender_refusal(db, recipient, sender=sender) is None:
        return None
    return MESSAGES_AND_MAIL_BLOCK_REFUSAL.format(name=recipient.username)


async def mail_someone(
    session: Session,
    lane: DatabaseLane,
    user: User,
    *,
    recipient: User | None = None,
    link_address: str | None = None,
    subject: str = "",
    quote: str | None = None,
    reply_key: str | None = None,
    resend_key: str | None = None,
    resend_of: tuple[int, ...] = (),
    link_context: LinkContext | None = None,
) -> bool:
    """Write to `recipient`, a local account, or to `link_address`, the
    stable `user@<fingerprint>` of someone on a linked BBS (issue #821).

    The compose screen opens with To filled in; `subject` and `quote` start
    the Subject prompt and the body, as the board reader's private reply
    uses them. `reply_key` gives such a reply a draft slot of its own (see
    `_letter_draft_path`); without one the letter is the caller's new
    letter, and a new letter already kept there is offered first, as
    `[C]ompose` offers it. `resend_key` is a sent letter sent again (issue
    #825): `quote` is then that letter's text, not a quote of it, and
    `resend_of` the failed letter, marked resent once this one is sent
    (issue #919, see `_compose_mail`).

    Nothing is written here. Every outcome -- a refusal, "Message sent.",
    "Cancelled." -- is announced (`netbbs.net.notices`), so the screen the
    caller came from shows it above its prompt when it redraws.

    The checks are the mailbox's own: the caller's mail access
    (`caller_mail_refusal`), the recipient's (`mail_recipient_refusal` --
    the guest account has no mailbox) and `mail_sender_refusal` (a
    recipient who blocked the caller, issue #817), and for a Link address that this
    node is linked with that BBS and will send it mail (issue #804), in
    words for an address the caller did not type.

    Returns whether a letter was sent."""
    refusal = await lane.run(lambda db: caller_mail_refusal(session, db, user))
    if refusal is not None:
        announce(session, refusal, tone="error")
        return False
    if link_address is not None:
        shown = await _display_link_address(lane, link_address)
        if link_context is None:
            announce(
                session, f"This BBS is not linked with other BBSes right now, so mail can't reach {shown}.",
                tone="error",
            )
            return False
        checked = await lane.run(lambda db: _check_link_reply_address(db, link_address, reply=False))
        if isinstance(checked, str):
            announce_styled(session, colored(checked, fg_color=ERROR_COLOR))
            return False
        return await _compose_mail(
            session, lane, user, prefill_link_address=link_address, prefill_subject=subject,
            prefill_body=quote or None, link_context=link_context, reply_key=reply_key, resend_key=resend_key,
            resend_of=resend_of,
        )
    if recipient is None:
        raise ValueError("mail_someone needs a recipient or a link_address")
    # The account as it is now: renamed, or deleted, since the screen drew.
    current = await lane.run(get_user_by_id, recipient.id)
    if current is None:
        announce(session, f"{recipient.username}'s account no longer exists.", tone="error")
        return False
    if current.id == user.id:
        announce(session, "That is your own account.", tone="muted")
        return False
    refused = await lane.run(mail_recipient_refusal, current)
    if refused is None:
        # Nor one who blocked the caller (issue #817), as the To prompt says.
        refused = await lane.run(lambda db: mail_sender_refusal(db, current, sender=user))
    if refused is not None:
        announce(session, refused, tone="error")
        return False
    return await _compose_mail(
        session, lane, user, prefill_recipient=current, prefill_subject=subject,
        prefill_body=quote or None, link_context=link_context, reply_key=reply_key, resend_key=resend_key,
        resend_of=resend_of,
    )


# -- SysOp mail to all callers (issue #827) -------------------------------------
#
# Written from the SysOp console (Operations > Mail), not the mailbox: it is
# the SysOp acting for the BBS, as the console's other mail tools are, and a
# To-prompt keyword for "everyone" would be one no caller could find and every
# SysOp could type by accident. The letter goes out as one ordinary copy per
# account that takes mail (`netbbs.mail.send_to_all_callers`), from the
# SysOp's own account, which callers can reply to, or from the system, which
# nobody can.

#: How many names the outcome of mail to all callers lists before counting.
_ALL_CALLERS_NAMES_SHOWN = 5


def _all_callers_draft_path(lane: DatabaseLane, user: User, *, as_system: bool) -> Path:
    """Mail to all callers has a draft slot of its own, one for each
    sender, apart from the SysOp's own new letter."""
    directory = lane.path.parent / f"{lane.path.name}_drafts"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"mail_{'notice' if as_system else 'all'}_{user.id}.draft"


def all_callers_outcome(
    sent: int, mailbox_full: tuple[str, ...], left_out: int, no_file_access: tuple[str, ...] = (),
) -> tuple[str, int]:
    """What sending to all callers did, in one line for the SysOp, and its
    color: how many it reached, whose mailbox was full and who may not open
    a file it points at (named, then counted), and how many accounts take
    no mail."""
    parts = [f"Sent to {_count(sent, 'caller')}."]

    def named(names: tuple[str, ...]) -> str:
        text = ", ".join(sanitize_text(name) for name in names[:_ALL_CALLERS_NAMES_SHOWN])
        more = len(names) - _ALL_CALLERS_NAMES_SHOWN
        return text + (f" and {more} more" if more > 0 else "")

    if mailbox_full:
        parts.append(f"Not delivered, mailbox full of unread mail: {named(mailbox_full)}.")
    if no_file_access:
        parts.append(f"Not delivered, can't open a file area it points at: {named(no_file_access)}.")
    if left_out:
        parts.append(
            f"{_count(left_out, 'account')} left out: the guest account, disabled accounts and signups "
            "awaiting approval take no mail."
        )
    return " ".join(parts), WARNING_COLOR if mailbox_full or no_file_access else SUCCESS_COLOR


async def write_to_all_callers(
    session: Session, lane: DatabaseLane, user: User, *, as_system: bool,
) -> bool:
    """Write one letter to every account on this BBS that takes mail (issue
    #827): from `user`'s own account -- signed, and callers can reply -- or,
    `as_system`, from the BBS itself (issue #819): no signature, no reply.

    The same compose screens as any letter: Subject, the caller's editor,
    and the review screen, where Send sends it. There is no To: who it goes
    to is decided at Send (`netbbs.mail.all_callers_recipients`), and the
    review screen says how many. The letter keeps a draft slot of its own,
    and with it the group id that makes a second Send of the same letter --
    a retry, a kept draft -- refused rather than sent twice. What happened
    is carried to the screen the SysOp came from: how many it reached, and
    each recipient whose full mailbox turned it away. Returns whether it
    was sent."""
    discard_buffered_enter = getattr(session, "discard_buffered_enter", None)
    if discard_buffered_enter is not None:
        await discard_buffered_enter()
    description_level = await lane.run(menu_description_level, user)
    redraw_in_place = await lane.run(redraw_in_place_enabled, user)
    unicode_style = await lane.run(unicode_style_enabled, user)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, user)
    accent_color = await lane.run(effective_accent_color_256)
    header_color = await lane.run(effective_header_color_256)
    truecolor = await lane.run(lambda db: effective_truecolor(session, db, user))
    body_mode = await lane.run(_mail_body_mode, user)
    title = "Notice to all callers" if as_system else "Letter to all callers"
    sender = None if as_system else user
    kept_notice = f"Draft saved -- you'll be offered it the next time you open {title}."

    draft_path = _all_callers_draft_path(lane, user, as_system=as_system)
    resumed = _load_letter_draft(draft_path)
    if resumed is not None:
        outcome = await _letter_draft_choice(session, lane, user, resumed, starting_new=True)
        if outcome == "back":
            return False
        if outcome == "discard":
            _forget_letter(draft_path)
            announce(session, "Draft deleted.", tone="muted")
            resumed = None
    group_id = resumed.group_id if resumed is not None and resumed.group_id else new_mail_group_id()
    files = resumed.files if resumed is not None else []

    async def audience() -> str:
        recipients, _left_out = await lane.run(all_callers_recipients, sender)
        who = "from the BBS itself, with no reply" if as_system else "from you; they can reply"
        return f"To all callers: {_count(len(recipients), 'account')} that take mail, {who}."

    await show_compose_screen(
        session, title=title, breadcrumb=("SysOp", "Mail"), fields=[], hint=await audience(),
        redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
        header_color=header_color, accent_color=accent_color,
    )
    if resumed is not None and resumed.subject is not None:
        subject = resumed.subject
    else:
        subject = await read_subject(session, max_bytes=MAX_MAIL_SUBJECT_BYTES)
        if subject is None:
            announce(session, "Message cancelled.", tone="muted")
            return False

    def editor_header() -> EditorHeader:
        return EditorHeader(title, (("To", "All callers"), ("Subject", subject)), color=header_color)

    def keep_fields() -> None:
        save_draft_fields(
            draft_path,
            {"to": None, "reply_address": None, "subject": subject, "group": group_id, **_files_field(files)},
        )

    keep_fields()
    body = await _compose_mail_body(
        session, lane, user, initial_text=resumed.body if resumed is not None else None,
        cursor_at_end=resumed is not None, header=editor_header(), draft_path=draft_path,
    )
    if body is None or not body.strip():
        if body is None and draft_path.exists():
            announce(session, kept_notice, tone="muted")
            return False
        _forget_letter(draft_path)
        announce(session, "Message cancelled.", tone="muted")
        return False
    if not as_system:
        # The SysOp's own letter is signed as any of theirs; a notice from
        # the BBS is not the SysOp's to sign.
        signature = await lane.run(get_signature, user)
        if signature:
            body = append_signature(body, signature)

    while True:
        too_long = _too_long_to_send(subject, body)
        if too_long is not None:
            announce(session, too_long, tone="error")
        announce(session, await audience(), tone="muted")
        action = await review_composition(
            session, recipient=None, subject=subject, body=body,
            commit_key="s", commit_label="end", commit_brief="Send it to all callers",
            description_level=description_level, redraw_in_place=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed, accent_color=accent_color,
            header_color=header_color, truecolor=truecolor, body_mode=body_mode, body_layout="lines",
            breadcrumb=("SysOp", "Mail", title),
            extra_rows=_file_rows(files, accent=accent_color, to_another_bbs=False),
            extra_actions=file_actions(files, noun="letter"),
        )
        if isinstance(action, str):
            files = await change_attached_files(
                session, lane, user, files, action, noun="letter", breadcrumb=("SysOp", "Mail", title),
                style=await _picker_style(lane, user),
            )
            keep_fields()
            continue
        if action is ReviewAction.CANCEL:
            _forget_letter(draft_path)
            announce(session, "Message cancelled.", tone="muted")
            return False
        if action is ReviewAction.EDIT_SUBJECT:
            subject = await read_subject(session, max_bytes=MAX_MAIL_SUBJECT_BYTES, current=subject)
            keep_fields()
            continue
        if action is ReviewAction.EDIT_BODY:
            keep_fields()
            revised = await _compose_mail_body(
                session, lane, user, initial_text=body, header=editor_header(), draft_path=draft_path,
            )
            if revised is not None:
                body = revised
            elif draft_path.exists():
                announce(session, kept_notice, tone="muted")
                return False
            else:
                announce(session, "Body unchanged.", tone="muted")
            continue
        if action is not ReviewAction.COMMIT or too_long is not None:
            continue
        try:
            if files:
                # The SysOp writing a notice from the system checks the files
                # as a sender would.
                unavailable = await lane.run(
                    lambda db: [ref for ref in files if open_ref(db, user, ref).state != AVAILABLE]
                )
                if unavailable:
                    announce(
                        session,
                        f"{unavailable[0].filename} is no longer available to you. [R]emove it, then send.",
                        tone="error",
                    )
                    continue
            result = await lane.run(
                lambda db: send_to_all_callers(db, subject, body, group_id=group_id, sender=sender, files=files)
            )
        except MailError as exc:
            announce(session, f"Could not send: {exc}", tone="error")
            continue
        _forget_letter(draft_path)
        # Issue #823: those online now hear of it now.
        for name in result.sent_to:
            nudge(name)
        text, color = all_callers_outcome(
            len(result.sent_to), result.mailbox_full, result.left_out, result.no_file_access,
        )
        announce(session, text, color=color)
        return True


# -- forwarding (issue #822) ---------------------------------------------------


async def _forward_message(
    session: Session, lane: DatabaseLane, user: User, message: MailMessage,
    *, sent: bool, link_context: LinkContext | None,
) -> bool:
    """[F]orward on a letter's view, Inbox or Sent: a new letter under
    "Fwd:" whose body is the letter itself, whole, under a header saying
    whom it was from and to, when, and under what subject
    (`netbbs.quoting.forward_body`). The caller writes a note above it if
    they like, and types the recipient at the To prompt, which makes every
    check it makes for a new letter -- a local name or, on a linked node, a
    Link address, whatever the original was.

    The header names people as the view does: a Link address by
    `link_address_label`, system mail by `SYSTEM_SENDER_LABEL`, the date
    in the caller's own format. A forward of a letter at the size limit is
    over it once the header is added; the review screen says by how much
    and refuses Send until it is shortened (issue #812).

    Refused, as every way into mail is, while the caller's mail is closed
    (`caller_mail_refusal`): the SysOp can close it while a letter is open.
    Each letter's forward keeps its own draft slot (`_forward_key`).

    The files the letter points at (issue #830) go with the forward, those
    the forwarder can open themselves; Send checks them for its new
    recipients as for any letter. Returns whether the forward was sent."""
    refusal = await lane.run(lambda db: caller_mail_refusal(session, db, user))
    if refusal is not None:
        announce(session, refusal, tone="error")
        return False
    if sent:
        sender_label, recipient_label = user.username, await _display_recipient_label(lane, message)
    else:
        sender_label, recipient_label = await _display_sender_label(lane, message), user.username
    display_format, display_timezone = await lane.run(resolve_display_preferences)
    date = format_for_display(message.created_at, override_format=display_format, override_timezone=display_timezone)
    body = forward_body(
        # Escape sequences out, color pipe codes kept (issue #809).
        post_body_text(message.body),
        sender=sender_label, recipient=recipient_label, date=date, subject=message.subject,
    )
    copies = await lane.run(sent_group_copies, user, message) if sent else None
    refs = await lane.run(_letter_file_refs, message, copies)
    opened = await open_refs(lane, user, refs)
    return await _compose_mail(
        session, lane, user,
        prefill_subject=forward_subject(message.subject, max_bytes=MAX_MAIL_SUBJECT_BYTES),
        prefill_body=body, link_context=link_context, forward_key=_forward_key(message),
        prefill_files=[item.ref for item in opened if item.state == AVAILABLE],
    )


async def _compose_mail(
    session: Session,
    lane: DatabaseLane,
    user: User,
    *,
    prefill_recipient: User | None = None,
    prefill_link_address: str | None = None,
    prefill_to: list[str] | None = None,
    prefill_subject: str = "",
    prefill_body: str | None = None,
    link_context: LinkContext | None = None,
    reply_key: str | None = None,
    forward_key: str | None = None,
    resend_key: str | None = None,
    resend_of: tuple[int, ...] = (),
    resume: bool = False,
    prefill_files: list[FileRef] | None = None,
) -> bool:
    """
    `link_context`, if given, lets the "To:" prompt accept a `user@node`
    address (design doc) in addition to a
    plain local username -- routed to `netbbs.link.mail.compose_link_
    message` instead of `netbbs.mail.send_mail`.

    A reply is addressed for the caller: `prefill_recipient` to a local
    account, or `prefill_link_address` -- the stored `user@<fingerprint>`
    of a Link message's sender (issue #805) -- to another BBS. The latter
    needs `link_context`; To shows the node by its current name, and the
    message goes to that technical address unless the caller retypes To
    on the review screen.

    Composing is a screen of its own (issue #813): "New message" or
    "Reply", with the To and Subject prompts under its title, and the
    fullscreen editor and the review screen both showing whom the letter
    is for and under what subject.

    `forward_key` (issue #822, see `_forward_key`) makes the letter a
    forward: titled "Forward", with To asked for as a new letter's is, and
    the editor opening at the top of `prefill_body`, where a note goes.

    `resend_key` (issue #825, see `_resend_key`) makes it a sent letter
    sent again: titled "Resend", `prefill_body` the letter as it was sent,
    signature included, so none is added. `resend_of` names the failed
    letters it repeats (issue #919): once it is sent, each of them that
    went to an address it went to is marked resent (`record_resend`), so
    a copy `[T]o` dropped stays a failure in Sent.

    Each letter has its own draft slot (issue #814): the new letter, a
    reply to one message (`reply_key`, see `_reply_key`), and a forward
    of one (`forward_key`), and a resend of one (`resend_key`). Either editor keeps the text there as
    it is typed, with its To and Subject beside it, and "Keep draft &
    exit" or `/exit` leaves it for later. A letter found in its slot is
    offered before anything is asked -- resume it, delete it and start
    again, or go back -- and is never loaded into another letter's
    editor. `resume` skips that choice: the caller already made it.

    Several people (issue #827): the To field takes addresses separated by
    commas, each checked as it is typed and named when it is refused, up
    to `MAX_MAIL_RECIPIENTS`. Such a letter is sent as one copy per
    recipient (`netbbs.mail_groups.send_letter`), all or none: a recipient
    who cannot take it is named at Send, and [T]o drops or fixes them.
    `prefill_to` fills To with several addresses -- Reply all, a follow-up
    to a letter to several people -- which Send checks like typed ones.
    Each letter keeps a group id with its draft, so a letter to several
    people sent once is never sent again from its draft.

    Files (issue #830): the review screen's `[A]ttach file` points the
    letter at a file in a file area here, up to `MAX_FILE_REFS`, and
    `[R]emove file` takes one off; `prefill_files` starts a forward with
    the files of the letter it passes on. They are kept with the draft.
    Send refuses a local recipient who may not read a file's area, naming
    them; a recipient on another BBS gets the files named in text.

    Returns whether the letter was sent.
    """
    # Both entry points here are a hotkey (`[C]ompose`/`[R]eply`)
    # immediately followed by a `read_line()` prompt -- an Enter that
    # arrives right behind that hotkey (e.g. typed as one "C<Enter>"
    # habit) would otherwise be consumed as a blank answer to whichever
    # prompt comes first, cancelling compose outright on the fresh-
    # compose path. Same fix `netbbs.net.confirm.read_confirmation_
    # choice` already applies for Y/N prompts, just not previously wired
    # up for a hotkey-to-text-prompt transition. `getattr` guard matches
    # that same call site -- not every lightweight `Session`-like test
    # double implements this optional method.
    discard_buffered_enter = getattr(session, "discard_buffered_enter", None)
    if discard_buffered_enter is not None:
        await discard_buffered_enter()

    description_level = await lane.run(menu_description_level, user)
    redraw_in_place = await lane.run(redraw_in_place_enabled, user)
    unicode_style = await lane.run(unicode_style_enabled, user)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, user)
    accent_color = await lane.run(effective_accent_color_256)
    header_color = await lane.run(effective_header_color_256)
    truecolor = await lane.run(lambda db: effective_truecolor(session, db, user))
    body_mode = await lane.run(_mail_body_mode, user)
    if forward_key is not None:
        title = "Forward"
    elif resend_key is not None:
        title = "Resend"
    elif reply_key is not None:
        title = "Reply"
    else:
        title = "New message"
    link_enabled = link_context is not None

    draft_path = _letter_draft_path(lane, user, reply_key, forward_key=forward_key, resend_key=resend_key)
    resumed = _load_letter_draft(draft_path)
    if resumed is not None and not resume:
        outcome = await _letter_draft_choice(session, lane, user, resumed, starting_new=True)
        if outcome == "back":
            return False
        if outcome == "discard":
            _forget_letter(draft_path)
            announce(session, "Draft deleted.", tone="muted")
            resumed = None
    if resumed is not None:
        # The letter as it was left: its To, Subject and text replace what
        # a fresh start would have filled in.
        prefill_body = resumed.body
        if resumed.subject is not None:
            prefill_subject = resumed.subject
    if forward_key is not None:
        kept_notice = "Draft saved -- you'll be offered it when you forward this message again."
    elif resend_key is not None:
        kept_notice = "Draft saved -- you'll be offered it when you resend this message again."
    elif reply_key is not None:
        kept_notice = "Draft saved -- you'll be offered it when you reply to this message again."
    else:
        kept_notice = "Draft saved -- it is under [D]raft on the mail screen."

    async def compose_screen(fields: list[tuple[str, str]], hint: str | None = None) -> None:
        await show_compose_screen(
            session, title=title, breadcrumb=("Mail",), fields=fields, hint=hint,
            redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            header_color=header_color, accent_color=accent_color,
        )

    def picker_style() -> dict:
        return {
            "description_level": description_level, "redraw_in_place": redraw_in_place,
            "unicode_style": unicode_style, "collapsed": collapsed,
            "accent_color": accent_color, "header_color": header_color,
        }

    async def settle_recipient(text: str) -> tuple[str, str]:
        """What to keep as the address, and how to show it (issue #813). A
        local name is kept as the account spells it, so `[T]o` opens on
        that too; a Link address is kept as typed, for Send to check again.
        Several addresses (issue #827) are each settled so."""
        entries = split_recipients(text)
        if len(entries) > 1:
            settled: list[tuple[str, str]] = []
            for entry in entries:
                kept, label = await settle_one(entry)
                # One person named twice -- `bob, Bob`, or `sysop` and the
                # SysOp's own name -- is one recipient (review on #910), and
                # a list that comes down to one is a letter to one person.
                if kept.casefold() not in {other.casefold() for other, _label in settled}:
                    settled.append((kept, label))
            if len(settled) == 1:
                return settled[0]
            return join_recipients([kept for kept, _label in settled]), join_recipients(
                [label for _kept, label in settled]
            )
        return await settle_one(text)

    async def settle_one(text: str) -> tuple[str, str]:
        # "sysop" wherever an address is typed, [T]o included (#840).
        text = await lane.run(resolve_sysop_alias, text)
        if link_enabled and "@" in text:
            # Kept by the node's technical identity once it names one
            # (issue #826): Send and a resumed draft reach the node the
            # caller chose, even if another node takes its name meanwhile.
            resolved = await lane.run(_resolve_link_address, text)
            if not isinstance(resolved, str):
                text = f"{resolved.user}@{resolved.fingerprint}"
        label = await lane.run(_recipient_label, text, link_enabled)
        return (text if link_enabled and "@" in text else label), label

    # The technical address a Link reply goes to, while To still shows it.
    reply_address: str | None = None
    recipient_text: str | None = None
    recipient_label = ""
    # Kept with the draft (issue #827): a letter to several people sent
    # from it once is refused if its draft is sent again.
    group_id = resumed.group_id if resumed is not None and resumed.group_id else new_mail_group_id()
    files: list[FileRef] = resumed.files if resumed is not None else list(prefill_files or [])
    if resumed is not None and resumed.reply_address is not None:
        prefill_link_address, prefill_recipient = resumed.reply_address, None
    elif resumed is not None and resumed.recipient_text is not None:
        prefill_link_address, prefill_recipient = None, None
        recipient_text, recipient_label = await settle_recipient(resumed.recipient_text)
    elif prefill_to:
        prefill_link_address, prefill_recipient = None, None
        recipient_text, recipient_label = await settle_recipient(join_recipients(prefill_to))
    if prefill_link_address is not None:
        reply_address = prefill_link_address
        recipient_text = await _display_link_address(lane, prefill_link_address)
        recipient_label = recipient_text
    elif prefill_recipient is not None:
        recipient_text = prefill_recipient.username
        recipient_label = recipient_text
    if recipient_text is not None:
        # A resumed letter shows its Subject too: nothing is asked again.
        await compose_screen(
            [("To", recipient_label), *([("Subject", prefill_subject)] if resumed and resumed.subject else [])]
        )
    else:
        to_hint = (
            "Who is it for? Type their user name, or name@TheirBBS for someone on a linked BBS. "
            if link_enabled else "Who is it for? Type their user name. "
        ) + (
            f"Several people: separate them with commas (up to {MAX_MAIL_RECIPIENTS}). "
            "Tab completes a name; ? and Enter lists who you can write to. An empty line or Esc cancels."
        )
        await compose_screen([], hint=to_hint)
        # Tab and ? (issue #826): gathered once, as the prompt opens.
        book = await lane.run(lambda db: gather_address_book(db, user, link_enabled=link_enabled))
        to_options = read_to_line_options(RecipientCompleter(book, session, "To: "))
        picked = False
        # What the prompt opens with: a list with one address refused is
        # given back to fix, not typed again (issue #827).
        seed = ""
        while True:
            await write_prompt(session, "To: ")
            try:
                if seed:
                    typed_text = await session.read_line(cancellable=True, initial=seed, **to_options)
                else:
                    typed_text = await session.read_line(cancellable=True, **to_options)
            except InputCancelled:
                typed_text = ""
            seed = ""
            entries = split_recipients(typed_text)
            if not entries:
                # A resumed letter from before #814 is still kept (review on
                # #873): say so, not that it is gone.
                announce(session, kept_notice if resumed is not None else "Cancelled.", tone="muted")
                return False
            # `?` as the last address (issue #826) lists who to add.
            request = picker_request(entries[-1], link_enabled=link_enabled)
            picked = False
            if request is not None:
                chosen = await choose_recipient(session, book, request, **picker_style())
                await compose_screen([], hint=to_hint)
                if chosen is None:
                    if len(entries) > 1:
                        seed = join_recipients(entries[:-1]) + ", "
                    continue
                # Checked below exactly as a typed address is.
                entries[-1], picked = chosen, True
            if len(entries) > MAX_MAIL_RECIPIENTS:
                await session.write_line(colored(too_many_recipients_text(len(entries)), fg_color=ERROR_COLOR))
                seed = join_recipients(entries)
                continue
            # Each address checked as it is typed (issues #807, #816, #817):
            # one that is refused is asked for again here, not after the
            # message is written, and a list says which one.
            checked = await lane.run(
                lambda db: [_check_to_entry(db, user, entry, link_enabled) for entry in entries]
            )
            problems = [(entry, result) for entry, result in zip(entries, checked) if isinstance(result, str)]
            if problems:
                for entry, problem in problems:
                    shown = _name_the_problem(entry, problem) if len(entries) > 1 else problem
                    await session.write_line(colored(shown, fg_color=ERROR_COLOR))
                if len(entries) > 1:
                    seed = join_recipients(entries)
                continue
            break
        recipient_text, recipient_label = await settle_recipient(join_recipients(entries))
        if picked:
            # Chosen from the list, so never typed on this screen: shown.
            await compose_screen([("To", recipient_label)])

    if resumed is not None and resumed.subject is not None:
        subject = resumed.subject
    else:
        # Checked here rather than at Send (issue #812): an empty subject is
        # asked for again, one that is too long says by how much, and only
        # Esc on a fresh prompt gives up on the message.
        subject = await read_subject(session, max_bytes=MAX_MAIL_SUBJECT_BYTES, current=prefill_subject or None)
        if subject is None:
            announce(session, kept_notice if resumed is not None else "Message cancelled.", tone="muted")
            return False

    def editor_header() -> EditorHeader:
        return EditorHeader(title, (("To", recipient_label), ("Subject", subject)), color=header_color)

    def keep_fields() -> None:
        # What the text is for, beside it -- the editors only keep the
        # text (issue #814).
        save_draft_fields(
            draft_path,
            {
                "to": recipient_text, "reply_address": reply_address, "subject": subject, "group": group_id,
                **_files_field(files),
            },
        )

    keep_fields()
    # A reply starts on the quote, with the cursor under it (issue #675); a
    # resumed letter where it was left off. A fresh forward starts above
    # the letter it carries, where a note to its new reader goes (#822).
    fresh_forward = forward_key is not None and resumed is None
    body = await _compose_mail_body(
        session, lane, user, initial_text=prefill_body,
        cursor_at_end=prefill_body is not None and not fresh_forward,
        start_at_top=fresh_forward,
        header=editor_header(), draft_path=draft_path,
    )
    if body is None or not body.strip():
        if body is None and draft_path.exists():
            announce(session, kept_notice, tone="muted")
            return False
        _forget_letter(draft_path)
        announce(session, "Message cancelled.", tone="muted")
        return False
    # Appended once, right after the message is first composed -- not on
    # every subsequent "edit body" pass over the same draft (`netbbs.
    # signature.append_signature`'s own docstring): from here on the
    # signature is just part of the editable body, the same way a real
    # mail client's compose buffer already works. Idempotent, so a
    # resumed letter that already carries it does not get it twice.
    signature = await lane.run(get_signature, user)
    if forward_key is not None:
        # Under the note, above the letter passed on (issue #822).
        body = sign_forward(body, signature)
    elif resend_key is not None:
        # Signed when it was first sent (issue #825); a signature changed
        # since would sign it twice.
        pass
    elif signature:
        body = append_signature(body, signature)

    while True:
        # Before Review, not at Send (issue #812): an editor stops the
        # body at the limit, but the signature is added afterwards and can
        # carry it over. Said on the review screen, where [B]ody and
        # [U]pdate subject fix it; Send is refused until then.
        to_another_bbs = link_enabled and (
            reply_address is not None or any("@" in entry for entry in split_recipients(recipient_text))
        )
        too_long = _too_long_to_send(subject, body)
        if too_long is None and files and to_another_bbs:
            # A letter to another BBS ends with a line naming each file
            # (issue #830), which counts toward its length there.
            too_long = _file_lines_too_long(await lane.run(body_with_link_text, body, files))
        if too_long is not None:
            announce(session, too_long, tone="error")
        action = await review_composition(
            session,
            # The account's own name, not the text as typed (issue #813).
            recipient=recipient_label,
            subject=subject,
            body=body,
            commit_key="s",
            commit_label="end",
            commit_brief="Send this message",
            description_level=description_level,
            redraw_in_place=redraw_in_place,
            unicode_style=unicode_style,
            collapsed=collapsed,
            accent_color=accent_color,
            header_color=header_color,
            truecolor=truecolor,
            # Previewed as its reader will see it, lines kept (issue #809).
            body_mode=body_mode,
            body_layout="lines",
            breadcrumb=("Mail", title),
            extra_rows=_file_rows(files, accent=accent_color, to_another_bbs=to_another_bbs),
            extra_actions=file_actions(files, noun="letter"),
        )
        if isinstance(action, str):
            files = await change_attached_files(
                session, lane, user, files, action, noun="letter", breadcrumb=("Mail", title),
                style=picker_style(),
            )
            keep_fields()
            continue
        if action is ReviewAction.CANCEL:
            _forget_letter(draft_path)
            announce(session, "Message cancelled.", tone="muted")
            return False
        if action is ReviewAction.EDIT_RECIPIENT:
            # Opened on the name the caller reads, not a technical identity
            # the To prompt resolved it to (issue #826); unchanged keeps it.
            edited = await read_prefilled_field(session, "To", recipient_label)
            edited_entries = split_recipients(edited)
            request = picker_request(edited_entries[-1], link_enabled=link_enabled) if edited_entries else None
            if request is not None:
                # ? here too, as the last address, and what is chosen is
                # checked at Send.
                book = await lane.run(lambda db: gather_address_book(db, user, link_enabled=link_enabled))
                chosen = await choose_recipient(session, book, request, **picker_style())
                edited = join_recipients([*edited_entries[:-1], chosen]) if chosen else recipient_label
            if edited != recipient_label:
                reply_address = None
                recipient_text, recipient_label = await settle_recipient(edited)
            continue
        if action is ReviewAction.EDIT_SUBJECT:
            subject = await read_subject(session, max_bytes=MAX_MAIL_SUBJECT_BYTES, current=subject)
            continue
        if action is ReviewAction.EDIT_BODY:
            # A letter kept from here keeps the To and Subject review
            # changed, not the ones it was started with.
            keep_fields()
            revised = await _compose_mail_body(
                session, lane, user, initial_text=body, header=editor_header(), draft_path=draft_path,
            )
            if revised is not None:
                body = revised
            elif draft_path.exists():
                # "Keep draft & exit" or /exit while revising: the whole
                # letter is kept for later, as on a board (issue #149).
                announce(session, kept_notice, tone="muted")
                return False
            else:
                announce(session, "Body unchanged.", tone="muted")
            continue
        if too_long is not None:
            continue

        if reply_address is None and len(split_recipients(recipient_text)) > 1:
            if await _send_to_several(
                session, lane, user, split_recipients(recipient_text), subject, body,
                group_id=group_id, link_context=link_context, files=files, resend_of=resend_of,
            ):
                _forget_letter(draft_path)
                return True
            continue

        if link_enabled and (reply_address is not None or "@" in recipient_text):
            # The To prompt checks this too; the address may have been
            # edited from the review screen since, and a peer's standing
            # can change while the message is written.
            if reply_address is not None:
                # Worded for what the letter is: a reply, or a letter to
                # someone met elsewhere or sent again (issue #825).
                is_reply = reply_key is not None
                checked = await lane.run(
                    lambda db: _check_link_reply_address(db, reply_address, reply=is_reply)
                )
            else:
                checked = await lane.run(_check_link_recipient, recipient_text)
            if isinstance(checked, str):
                announce_styled(session, colored(checked, fg_color=ERROR_COLOR))
                continue
            technical_recipient = f"{checked.user}@{checked.fingerprint}"
            warning = await _link_mail_identity_warning(lane, technical_recipient)
            if warning is not None:
                await session.write_line(colored(warning, fg_color=MUTED_COLOR, bold=True))
            if files:
                problem = await lane.run(lambda db: _unavailable_file_problem(db, user, files))
                if problem is not None:
                    announce(session, problem, tone="error")
                    continue
            try:
                # Files go to another BBS as text only (issue #830).
                link_body = await lane.run(body_with_link_text, body, files)
                await lane.run(
                    compose_link_message, user, technical_recipient, subject, link_body,
                    node_identity=link_context.node_identity,
                )
            except (LinkMailError, MailError) as exc:
                announce(session, f"Could not send: {exc}", tone="error")
                continue
            if resend_of:
                await lane.run(record_resend, user, list(resend_of), [technical_recipient])
            _forget_letter(draft_path)
            announce(session, "Message sent.")
            return True

        try:
            recipient = await lane.run(get_user_by_username, recipient_text)
        except AuthError:
            announce(session, f"Could not send: no such user {recipient_text!r}.", tone="error")
            continue
        try:
            await lane.run(lambda db: send_mail(db, user, recipient, subject, body, files=files))
        except MailboxFullError:
            announce(session, f"{recipient.username}'s mailbox is full and cannot accept new mail right now.", tone="error")
            continue
        except MailError as exc:
            announce(session, f"Could not send: {exc}", tone="error")
            continue
        # Issue #823: a recipient online now hears of it now.
        nudge(recipient.username)
        _forget_letter(draft_path)
        announce(session, "Message sent.")
        return True


@dataclass(frozen=True)
class _LetterDraft:
    """A letter kept for later (issue #814): its text, and what it was
    for. `recipient_text` and `subject` are `None` for a draft kept before
    #814, which had only its text; `reply_address` is a Link reply's
    stored `user@<fingerprint>`."""

    body: str
    recipient_text: str | None
    reply_address: str | None
    subject: str | None
    # The letter's group id (issue #827), for a draft kept since.
    group_id: str | None = None
    # The files it points at (issue #830).
    files: list[FileRef] = field(default_factory=list)


def _reply_key(message: MailMessage) -> str:
    """The message a reply answers, for its draft slot's name: its id, and
    a digest of when and from whom it came -- a mail id can be handed out
    again once the newest message is gone, and a kept reply must not be
    offered for a different message that got the same id."""
    digest = hashlib.sha256(f"{message.created_at}|{message.sender_label}".encode("utf-8")).hexdigest()[:12]
    return f"{message.id}_{digest}"


def _reply_all_key(message: MailMessage) -> str:
    """The draft slot of Reply all to a message (issue #827): apart from a
    kept Reply to its sender alone."""
    return f"all_{_reply_key(message)}"


def _forward_key(message: MailMessage) -> str:
    """The message a forward carries, for its draft slot's name (issue
    #822): the same id and digest a reply's slot uses, in a slot of its
    own, so a kept reply and a kept forward of one letter do not meet."""
    return _reply_key(message)


def _resend_key(message: MailMessage) -> str:
    """The sent letter a resend repeats, for its draft slot's name (issue
    #825): the same id and digest a reply's slot uses, in a slot of its
    own."""
    return _reply_key(message)


def post_reply_key(root_post_id: str) -> str:
    """The draft slot of a private reply to a board post's author (issue
    #821), apart from any reply to a mail message: a digest of the post's
    stable id, which is safe in a file name whatever a peer put in it."""
    return "post_" + hashlib.sha256(root_post_id.encode("utf-8")).hexdigest()[:16]


def _letter_draft_path(
    lane: DatabaseLane, user: User, reply_key: str | None = None, *, forward_key: str | None = None,
    resend_key: str | None = None,
) -> Path:
    """One slot per letter (issue #814): the caller's new letter, and one
    per message they are replying to, forwarding (issue #822) or resending
    (issue #825). Before
    #814 every letter shared one body-only file, so a kept letter was
    offered in place of the next one's text -- a reply to someone else
    lost its quote to it."""
    directory = lane.path.parent / f"{lane.path.name}_drafts"
    directory.mkdir(parents=True, exist_ok=True)
    if forward_key is not None:
        return directory / f"mail_forward_{user.id}_{forward_key}.draft"
    if resend_key is not None:
        return directory / f"mail_resend_{user.id}_{resend_key}.draft"
    if reply_key is not None:
        return directory / f"mail_reply_{user.id}_{reply_key}.draft"
    return directory / f"mail_new_{user.id}.draft"


def _adopt_legacy_mail_draft(lane: DatabaseLane, user: User) -> None:
    """A letter kept before #814 is in the one shared `mail_<id>.draft`,
    with no To or Subject. It becomes the caller's new letter, unless one
    is already kept there; resuming it asks for To and Subject again."""
    new_slot = _letter_draft_path(lane, user)
    legacy = new_slot.with_name(f"mail_{user.id}.draft")
    if legacy.exists() and not new_slot.exists():
        try:
            legacy.replace(new_slot)
        except OSError:
            pass


def _load_letter_draft(path: Path) -> _LetterDraft | None:
    if not path.exists():
        return None
    try:
        body = load_draft(path)
    except (OSError, UnicodeDecodeError):
        return None
    fields = load_draft_fields(path)
    return _LetterDraft(
        body=body, recipient_text=fields.get("to"), reply_address=fields.get("reply_address"),
        subject=fields.get("subject"),
        group_id=fields.get("group") if isinstance(fields.get("group"), str) else None,
        files=_decode_files(fields.get("files")),
    )


def _forget_letter(path: Path) -> None:
    delete_draft(path)
    delete_draft_fields(path)


def _letter_draft_notice(draft: _LetterDraft) -> str:
    """"You have an unfinished letter to Bob: Lunch?" -- plain text; the
    caller styles it."""
    to = f" to {sanitize_text(draft.recipient_text)}" if draft.recipient_text else ""
    about = f": {sanitize_text(draft.subject)}" if draft.subject else ""
    return f"You have an unfinished letter{to}{about}"


async def _letter_draft_choice(
    session: Session, lane: DatabaseLane, user: User, draft: _LetterDraft, *, starting_new: bool,
) -> str:
    """"resume", "discard" or "back" for a kept letter (issue #814) --
    the same three a board's saved post draft offers. `starting_new` is
    the caller's [C]ompose or [R]eply, where [D]iscard deletes the kept
    letter and then starts afresh; from [D]raft it only deletes it."""
    description_level = await lane.run(menu_description_level, user)
    await session.write_line(colored(f"\r\n{_letter_draft_notice(draft)}", fg_color=MUTED_COLOR))
    await session.write_line(
        _menu_row(
            [
                MenuEntry(label=menu_key("R", "esume"), brief="Open it where you left off"),
                MenuEntry(
                    label=menu_key("D", "iscard"),
                    brief="Delete it, then start again" if starting_new else "Delete the draft",
                ),
                MenuEntry(label=menu_key("B", "ack"), brief="Leave it for later"),
            ],
            width=session.terminal_width, height=session.terminal_height, description_level=description_level,
        )
    )
    await write_prompt(session, "Choice: ")
    while True:
        choice = (await session.read_key()).lower()
        if choice in ("r", "d", "b"):
            await session.write_line("")
            return {"r": "resume", "d": "discard", "b": "back"}[choice]
        await session.write(reject_unhandled_key(choice))


def _link_mail_refusal(db, fingerprint: str) -> str | None:
    """Why this node will not send mail to `fingerprint`, in words for the
    caller, or `None` when it will (issue #804). Nothing is queued that the
    push loop would refuse, and "Message sent." is never shown for it. The
    words are `link_mail_refusal`'s, whose tag the To prompt's list shows
    (issue #920)."""
    refusal = link_mail_refusal(db, fingerprint)
    return None if refusal is None else refusal.sentence


@dataclass(frozen=True)
class _LinkRecipient:
    user: str
    fingerprint: str


def resolve_sysop_alias(db, recipient_text: str) -> str:
    """`sysop` as a To address (issue #840, F087): the classic BBS way to
    write to whoever runs the node. It names the node's first usable SysOp
    account, unless an account is actually called that. The field test's
    newcomer got "No such user: 'sysop'" and had to find the SysOp's name
    in a post."""
    if recipient_text.strip().lower() != "sysop":
        return recipient_text
    try:
        return get_user_by_username(db, recipient_text).username
    except AuthError:
        pass
    sysops = [account for account in list_users(db) if is_usable_sysop(account)]
    if not sysops:
        return recipient_text
    return min(sysops, key=lambda account: account.id).username


def _check_link_recipient(db, recipient_text: str) -> _LinkRecipient | str:
    """Check a typed `user@node` address (issue #807): its form, that it
    names exactly one node this BBS is linked with, and that this node will
    send that node mail (issue #804). Returns the recipient, or why not in
    words that say what to type instead."""
    resolved = _resolve_link_address(db, recipient_text)
    if isinstance(resolved, str):
        return resolved
    refusal = _link_mail_refusal(db, resolved.fingerprint)
    if refusal is not None:
        return refusal
    return resolved


@dataclass(frozen=True)
class _CheckedRecipient:
    """One To address that passed the To prompt's checks: an account here
    (`user`), or a Link recipient (`link`). `text` is what To keeps for it:
    the account's name, or `user@<fingerprint>`."""
    text: str
    user: User | None = None
    link: _LinkRecipient | None = None


def _check_to_entry(db, sender: User, text: str, link_enabled: bool) -> _CheckedRecipient | str:
    """The To prompt's checks for one address, typed or chosen (issues
    #807, #816, #817, #840): `sysop` names the node's SysOp; a Link address
    must name a node this BBS sends mail to; a local name must be an
    account that takes mail from `sender`. Returns the recipient, or why
    not, sanitized, in the words the prompt shows."""
    text = resolve_sysop_alias(db, text)
    if link_enabled and "@" in text:
        checked = _check_link_recipient(db, text)
        if isinstance(checked, str):
            return checked
        return _CheckedRecipient(text=f"{checked.user}@{checked.fingerprint}", link=checked)
    try:
        account = get_user_by_username(db, text)
    except AuthError:
        return f"No such user: {sanitize_text(text)!r}"
    refused = mail_recipient_refusal(db, account)
    if refused is None:
        refused = mail_sender_refusal(db, account, sender=sender)
    if refused is not None:
        return sanitize_text(refused)
    return _CheckedRecipient(text=account.username, user=account)


def _name_the_problem(entry: str, problem: str) -> str:
    """A refusal for one address of several (issue #827), saying which:
    the address in front, unless the words already name it."""
    shown = sanitize_text(entry)
    return problem if shown.casefold() in problem.casefold() else f"{shown}: {problem}"


async def _send_to_several(
    session: Session, lane: DatabaseLane, user: User, entries: list[str], subject: str, body: str,
    *, group_id: str, link_context: LinkContext | None, files: list[FileRef] = (),
    resend_of: tuple[int, ...] = (),
) -> bool:
    """Send from the review screen to several people (issue #827). Every
    address is checked again as the To prompt checks it -- To may have been
    edited, a peer's standing can change while the letter is written --
    and then the letter goes to all of them or to none
    (`netbbs.mail_groups.send_letter`). Every refusal is carried to the
    review screen, one line per recipient. A resend (`resend_of`, issue
    #919) marks the failed copies it reached as resent. Returns whether
    it was sent."""
    link_enabled = link_context is not None
    if len(entries) > MAX_MAIL_RECIPIENTS:
        announce(session, too_many_recipients_text(len(entries)), tone="error")
        return False
    checked = await lane.run(lambda db: [_check_to_entry(db, user, entry, link_enabled) for entry in entries])
    problems = [(entry, result) for entry, result in zip(entries, checked) if isinstance(result, str)]
    if problems:
        for entry, problem in problems:
            announce_styled(session, colored(_name_the_problem(entry, problem), fg_color=ERROR_COLOR))
        announce(session, "Nothing was sent. [T]o changes who it is for.", tone="muted")
        return False
    recipients = [
        LetterRecipient(user=result.user) if result.user is not None else LetterRecipient(address=result.text)
        for result in checked
        if isinstance(result, _CheckedRecipient)
    ]
    for recipient in recipients:
        if recipient.address is not None:
            # Each Link recipient's node on its own (review on #910): the
            # caution names whose node it is.
            warning = await _link_mail_identity_warning(lane, recipient.address)
            if warning is not None:
                name = sanitize_text(await _display_link_address(lane, recipient.address))
                await session.write_line(colored(f"{name}: {warning}", fg_color=MUTED_COLOR, bold=True))
    node_identity = link_context.node_identity if link_context is not None else None
    try:
        count = await lane.run(
            lambda db: send_letter(
                db, user, recipients, subject, body, group_id=group_id, node_identity=node_identity,
                files=list(files),
            )
        )
    except LetterRefused as exc:
        for recipient, problem in exc.problems:
            if recipient.user is not None:
                name = recipient.user.username
            else:
                assert recipient.address is not None
                name = await _display_link_address(lane, recipient.address)
            announce_styled(session, colored(_name_the_problem(name, problem), fg_color=ERROR_COLOR))
        announce(session, "Nothing was sent. [T]o changes who it is for.", tone="muted")
        return False
    except (LinkMailError, MailError) as exc:
        announce(session, f"Could not send: {exc}", tone="error")
        return False
    if resend_of:
        addresses = [recipient.address for recipient in recipients if recipient.address is not None]
        await lane.run(record_resend, user, list(resend_of), addresses)
    # Issue #823: a recipient online now hears of it now.
    for recipient in recipients:
        if recipient.user is not None:
            nudge(recipient.user.username)
    announce(session, f"Message sent to {count} people.")
    return True


def _reply_all_entries(db, reader: User, message: MailMessage) -> list[str]:
    """Who Reply all on a received copy of a letter to several people
    writes to (issue #827), as To addresses: its sender first, then
    everyone else it went to but the reader. An account is named by its
    current name; one deleted since, or a name another BBS listed that no
    account here has, is left out. Empty for a letter to one person, and
    for mail to all callers, which is answered by Reply alone."""
    members = group_members(message)
    if members is None:
        return []
    entries: list[str] = []
    if message.sender_user_id is not None:
        sender = get_user_by_id(db, message.sender_user_id)
        if sender is not None and sender.id != reader.id:
            entries.append(sender.username)
    elif not message.from_system and _split_link_address(message.sender_label) is not None:
        entries.append(message.sender_label)
    for member in members:
        if member.address is not None:
            entries.append(member.address)
        elif member.user_id is not None and member.user_id != reader.id:
            account = get_user_by_id(db, member.user_id)
            if account is not None:
                entries.append(account.username)
    unique: list[str] = []
    for entry in entries:
        if entry.casefold() not in {kept.casefold() for kept in unique}:
            unique.append(entry)
    return unique


def _sent_group_entries(db, copies: list[MailMessage]) -> list[str]:
    """Who a letter to several people went to (issue #827), as To
    addresses for a follow-up: a Link recipient by the address it went to,
    an account by its current name; one deleted since is left out."""
    entries = []
    for copy in copies:
        if copy.recipient_remote_address is not None:
            entries.append(copy.recipient_remote_address)
        elif copy.recipient_user_id is not None:
            account = get_user_by_id(db, copy.recipient_user_id)
            if account is not None:
                entries.append(account.username)
    return entries


async def _write_to_several(
    session: Session, lane: DatabaseLane, user: User, entries: list[str], *,
    subject: str, body: str, link_context: LinkContext | None, **keys,
) -> bool:
    """A letter to several people the caller did not type (issue #827):
    Reply all, a follow-up to a letter to several people, a resend of its
    copies that bounced. Each address gets the To prompt's checks first;
    one that fails is left out, and said so, as `mail_someone` refuses a
    single recipient before anything is written. Returns whether a letter
    was sent."""
    refusal = await lane.run(lambda db: caller_mail_refusal(session, db, user))
    if refusal is not None:
        announce(session, refusal, tone="error")
        return False
    link_enabled = link_context is not None
    checked = await lane.run(lambda db: [_check_to_entry(db, user, entry, link_enabled) for entry in entries])
    kept = [result.text for result in checked if isinstance(result, _CheckedRecipient)]
    for entry, result in zip(entries, checked):
        if isinstance(result, str):
            name = await _display_link_address(lane, entry)
            announce_styled(session, colored(f"Left out: {_name_the_problem(name, result)}", fg_color=WARNING_COLOR))
    if not kept:
        announce(session, "No one it went to can be written to now.", tone="error")
        return False
    return await _compose_mail(
        session, lane, user, prefill_to=kept, prefill_subject=subject, prefill_body=body or None,
        link_context=link_context, **keys,
    )


def _resolve_link_address(db, recipient_text: str) -> _LinkRecipient | str:
    """The form and node half of `_check_link_recipient`, without asking
    whether this node sends that node mail: what blocking a Link sender by
    name needs too (issue #817).

    The user half ends at the first `@`: a user name cannot contain one,
    and a node's friendly name can. The node half may be quoted, the way
    `link_address_label` shows a name that contains `@`."""
    user, _, node_reference = recipient_text.partition("@")
    user, node_reference = user.strip(), node_reference.strip()
    shown_node = sanitize_text(unquote_reference(node_reference))
    if not user:
        return f"Type the user's name before the @, like alice@{shown_node}." if shown_node else (
            "Type the user's name, then @ and the name of their BBS."
        )
    if not shown_node:
        return f"Type the name of their BBS after the @, like {sanitize_text(user)}@TheirBBS."
    if not is_valid_user_part(user):
        return sanitize_text(user_part_problem(user))
    resolved = resolve_stored_peer_reference(db, node_reference, met_only=True)
    if isinstance(resolved, list):
        if not resolved:
            return unknown_node_guidance(shown_node)
        return ambiguous_node_guidance(
            shown_node, sanitize_text(user),
            [
                (fingerprint, sanitize_text(identity_for_fingerprint(db, fingerprint).label))
                for fingerprint in resolved[:5]
            ],
        )
    return _LinkRecipient(user=user, fingerprint=resolved)


def _recipient_label(db, recipient_text: str, link_enabled: bool) -> str:
    """The recipient as the review screen and the editor show it (issue
    #813): a local account by its own name -- `Alice`, however it was
    typed -- and a Link address by the name its node goes by. Text that
    names no one yet (a `[T]o` edit Send will refuse) is shown as typed."""
    if link_enabled and "@" in recipient_text:
        # Named by its node even where Send will refuse that node: the
        # address may be kept by technical identity (issue #826).
        checked = _resolve_link_address(db, recipient_text)
        if isinstance(checked, str):
            return recipient_text
        return link_address_label(checked.user, identity_for_fingerprint(db, checked.fingerprint).label)
    try:
        return get_user_by_username(db, recipient_text).username
    except AuthError:
        return recipient_text


def _check_link_reply_address(db, technical_address: str, *, reply: bool = True) -> _LinkRecipient | str:
    """Check the stored `user@<fingerprint>` a Link reply goes to (issue
    #805) the way `_check_link_recipient` checks a typed address -- the
    same refusal when this node will not send that peer mail -- in words
    that fit an address the caller did not type.

    `reply=False` is a letter started from where the caller met that person
    (issue #821) -- Who's online, a carried post's author -- rather than an
    answer to their mail, and says so."""
    what = "a reply" if reply else "mail"
    split = _split_link_address(technical_address)
    if split is None:
        return "This message has no address a reply could go to." if reply else "There is no address mail could go to."
    user, fingerprint = split
    shown = sanitize_text(link_address_label(user, identity_for_fingerprint(db, fingerprint).label))
    if not is_valid_user_part(user):
        return f"{shown} is not an address mail can be sent to, so {what} can't reach it."
    if resolve_stored_peer_reference(db, fingerprint, met_only=True) != fingerprint:
        linked = "no longer linked" if reply else "not linked"
        return f"This BBS is {linked} with the BBS {shown} writes from, so {what} can't reach it."
    return _check_link_recipient(db, f"{user}@{fingerprint}")


def _unavailable_file_problem(db: Database, sender: User, files: list[FileRef]) -> str | None:
    """Why a letter pointing at `files` cannot go, for the one path that
    does not reach `netbbs.mail`'s check: a letter to one person on another
    BBS, which names its files in text."""
    return sender_ref_problem(db, sender, files)


def _file_lines_too_long(link_body: str) -> str | None:
    """Why a letter whose Link copy is `link_body` -- the body with its
    file lines (issue #830) -- is too long to send, or `None`."""
    over = characters_over(link_body, MAX_MAIL_BODY_BYTES)
    if over:
        return (
            f"{too_long_message('With the file lines added for someone on another BBS, the message is', over)}"
            " -- shorten it with [B]ody or [R]emove a file."
        )
    return None


def _too_long_to_send(subject: str, body: str) -> str | None:
    """Why this message cannot be sent as it stands, in characters -- or
    `None`. The same limits `netbbs.mail.send_mail` and
    `netbbs.link.mail.compose_link_message` enforce in bytes."""
    over = characters_over(subject.strip(), MAX_MAIL_SUBJECT_BYTES)
    if over:
        return f"{too_long_message('The subject is', over)} -- shorten it with [U]pdate subject."
    over = characters_over(body, MAX_MAIL_BODY_BYTES)
    if over:
        return f"{too_long_message('The message is', over)} -- shorten it with [B]ody."
    return None


async def _compose_mail_body(
    session: Session, lane: DatabaseLane, user: User, *, initial_text: str | None, draft_path: Path,
    cursor_at_end: bool = False, header: EditorHeader | None = None, start_at_top: bool = False,
) -> str | None:
    """Enter or revise one mail body through the user's chosen editor.

    `start_at_top` (a fresh forward, issue #822) has the line editor write
    typed lines above `initial_text`, as the fullscreen editor's cursor
    starts there; `/end` moves to the end.

    Both paths accept the current draft and only return text/explicit cancel;
    the caller owns review and persistence. `header` is what the fullscreen
    editor shows above the text; the line editor writes under the compose
    or review screen, which already shows it.

    `draft_path` is this letter's own slot (issue #814), so both editors
    keep the text as it is typed and offer "Keep draft & exit" / `/exit`.
    Neither asks about a draft found there: `_compose_mail` has already
    offered it and passes it in as `initial_text`. `None` with the draft
    still on disk means the letter was kept; without it, cancelled.

    Pasted color is kept as pipe codes (issue #809), as on a board that
    allows color: mail shows it (`_mail_body_mode`).
    """
    if await lane.run(fullscreen_editor_enabled, user):
        return await edit_prose(
            session, initial_text=initial_text, draft_path=draft_path, max_bytes=MAX_MAIL_BODY_BYTES,
            unicode_style=await lane.run(unicode_style_enabled, user), cursor_at_end=cursor_at_end,
            header=header, offer_recovery=False, keep_pasted_color=True,
        )
    return await edit_line_body(
        session,
        initial_text=initial_text,
        max_bytes=MAX_MAIL_BODY_BYTES,
        max_lines=_MAX_PLAIN_MAIL_LINES,
        draft_path=draft_path,
        offer_recovery=False,
        keep_pasted_color=True,
        start_at=0 if start_at_top else None,
    )

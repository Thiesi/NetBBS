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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from netbbs.auth.users import (
    AuthError, User, get_user_by_id, get_user_by_username, is_usable_sysop, list_users,
)
from netbbs.identity.addressing import is_valid_user_part, user_part_problem
from netbbs.link.boards import LinkContext
from netbbs.link.enforcement import LinkPolicyAction, decide_node_action
from netbbs.link.trust import TrustState
from netbbs.link.mail import (
    DELIVERY_STATUS_LABELS, RELAYED_DISPLAY_STATUS, LinkMailError, acknowledge_delivery_notices, compose_link_message,
    delivery_display_status, delivery_explanation,
)
from netbbs.link.node_profiles import (
    ambiguous_node_guidance, link_address_label, unknown_node_guidance, unquote_reference,
    identity_for_fingerprint, latest_identity_observation, resolve_stored_peer_reference,
)
from netbbs.mail import (
    GUEST_MAIL_REFUSAL,
    MAILBOX_NEARLY_FULL,
    MAX_MAIL_BODY_BYTES,
    MAX_MAIL_PER_RECIPIENT,
    MAX_MAIL_SUBJECT_BYTES,
    SYSTEM_SENDER_LABEL,
    MailBlock,
    MailBlockError,
    MailboxFullError,
    MailError,
    MailMessage,
    block_link_sender,
    block_local_sender,
    blocks_link_sender,
    blocks_local_sender,
    delete_for_recipient,
    delete_for_sender,
    get_mail,
    link_address_display_label,
    list_inbox,
    list_mail_blocks,
    list_sent,
    mail_access_refusal,
    mail_recipient_refusal,
    mail_sender_refusal,
    mark_read,
    mark_unread,
    recipient_display_label,
    send_mail,
    sender_display_label,
    sender_unblockable_reason,
    split_link_address,
    unblock,
    unblock_link_sender,
    unblock_local_sender,
)
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
    picker_request,
    read_to_line_options,
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
    screen = _MailboxScreen(session, lane, user, link_context=link_context, choice_prompt=choice_prompt)
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
_STATUS_WIDTH = max([len(_DELIVERY_HEADING), *(len(label) for label in DELIVERY_STATUS_LABELS.values())])
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

_EMPTY_INBOX = "Your inbox is empty. New mail will appear here."
_EMPTY_SENT = "You haven't sent any mail. [C]ompose writes a new message."
def mailbox_capacity_note(total: int, unread: int) -> tuple[str, str] | None:
    """What the Inbox says about its cap (issue #818), and its tone, once it
    holds `MAILBOX_NEARLY_FULL` messages; `None` below that."""
    if total >= MAX_MAIL_PER_RECIPIENT and unread >= total:
        return (
            f"Your mailbox is full of unread mail ({MAX_MAIL_PER_RECIPIENT} messages): new mail is "
            "turned away until you read or delete some.",
            "error",
        )
    if total >= MAX_MAIL_PER_RECIPIENT:
        return (
            f"Your mailbox is full ({MAX_MAIL_PER_RECIPIENT} messages): each new message removes your "
            "oldest read one. Delete what you don't need to keep it.",
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
    "U            mark the highlighted message unread, or read",
    "O            Inbox order: newest first, or unread first",
    "F            show only mail with a word in its name or subject",
    "Ctrl-L       redraw the list",
    "B            back to the main menu",
    "",
    "\"new\" marks mail you have not opened. Opening a message marks",
    "it read; [U]nread in the message or on the list takes that back.",
    "Sent shows where mail to another BBS stands under Delivery.",
    "Mail from System is a notice from this BBS; it has no Reply.",
]


def _mail_order(db: Database, user: User) -> str:
    stored = get_user_preference(db, user, _ORDER_PREFERENCE, default=_ORDER_NEWEST)
    return _ORDER_UNREAD if stored == _ORDER_UNREAD else _ORDER_NEWEST


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

    @property
    def status(self) -> str | None:
        status = delivery_display_status(
            self.message.link_delivery_status, self.message.link_relay_handoff_at
        )
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


def _mail_list_heading(
    widths: tuple[int, int, int], *, number_width: int, sent: bool, show_status: bool,
) -> str:
    name_width, subject_width, date_width = widths
    parts = [f"{'#':>{number_width}}"]
    if not sent:
        parts.append(" " * _MARKER_WIDTH)
    parts.append(_pad("To" if sent else "From", name_width))
    parts.append(_pad("Subject", subject_width))
    if show_status:
        parts.append(_pad(_DELIVERY_HEADING, _STATUS_WIDTH))
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
) -> list[str]:
    """One styled row per message, fitted to `width` in display columns.
    `first_number` is the number the first row shows; the highlighted row
    (an index into `rows`) is drawn in reverse video, the way the board
    list draws its cursor."""
    lines: list[str] = []
    for index, row in enumerate(rows):
        number = f"{first_number + index:>{number_width}}"
        unread = not sent and not row.message.is_read
        marker = _NEW_MARKER if unread else ""
        status = DELIVERY_STATUS_LABELS[row.status] if row.status else ""
        if widths is None:
            # Prose, for a terminal too narrow for columns: which message
            # it is comes first; the message view shows the date.
            tag = marker or status
            # The name takes at most half the row, so the subject shows.
            name = truncate_to_width(_name_text(row), max(8, (width - 1) // 2), ellipsis=ellipsis)
            plain = truncate_to_width(
                f"{number} {tag + ' ' if tag else ''}{name}: {row.subject}", max(1, width - 1), ellipsis=ellipsis,
            )
            if index == highlighted:
                lines.append(colored(plain, reverse=True))
                continue
            rest = plain[len(number) + 1:]
            styled_tag = ""
            if tag and rest.startswith(tag):
                styled_tag = colored(
                    tag, fg_color=SUCCESS_COLOR if unread else _DELIVERY_COLORS.get(row.status, VALUE_COLOR),
                    bold=unread,
                )
                rest = rest[len(tag):]
            lines.append(colored(number, fg_color=accent) + " " + styled_tag + rest)
            continue
        name_width, subject_width, date_width = widths
        name_cell = _fit(_name_text(row), name_width, ellipsis)
        subject_cell = _fit(row.subject, subject_width, ellipsis)
        date_cell = _fit(row.when, date_width, ellipsis)
        if index == highlighted:
            cells = [number]
            if not sent:
                cells.append(_pad(marker, _MARKER_WIDTH))
            cells += [name_cell, subject_cell]
            if show_status:
                cells.append(_pad(status, _STATUS_WIDTH))
            cells.append(date_cell)
            lines.append(colored("> " + "  ".join(cells), reverse=True))
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
        lines.append("  " + "  ".join(styled))
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
    for message in messages:
        identity_changed = False
        if sent:
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
        ))
    return rows


def _matches(row: _MailRow, query: str) -> bool:
    """[F]ind (issue #810): a word in the name or the subject, as the
    list shows them -- never the "new" marker, which the old picker's
    search matched as part of the subject."""
    needle = query.casefold()
    return needle in row.name.casefold() or needle in row.subject.casefold()


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


def _count_rows(text: str, width: int) -> int:
    return wrap_terminal_text(text, max(1, width)).count("\r\n") + 1 if text else 0


class _MailboxScreen:
    """The mailbox (issue #810): the Inbox or Sent as a list with a
    cursor, and every mail action on its action bar."""

    def __init__(
        self, session: Session, lane: DatabaseLane, user: User, *,
        link_context: LinkContext | None, choice_prompt: Callable[[], str] | None,
    ) -> None:
        self.session = session
        self.lane = lane
        self.user = user
        self.link_context = link_context
        self.choice_prompt = choice_prompt
        self.sent = False
        self.order = _ORDER_NEWEST
        self.query: str | None = None
        self.all_rows: list[_MailRow] = []
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
        elif char == "s" and not self.sent:
            await moved_on()
            await self._switch(sent=True)
        elif char == "b":
            await moved_on()
            if self.sent:
                await self._switch(sent=False)
                return False
            return True
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
        elif char == "o" and not self.sent and self.all_rows:
            await moved_on()
            self.order = _ORDER_NEWEST if self.order == _ORDER_UNREAD else _ORDER_UNREAD
            await self.lane.run(_set_mail_order, self.user, self.order)
            announce(
                session,
                "Unread mail first." if self.order == _ORDER_UNREAD else "Newest mail first.",
                tone="muted",
            )
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
        self.all_rows = await _load_mail_rows(self.lane, self.user, sent=self.sent)
        rows = self.all_rows
        if not self.sent and self.order == _ORDER_UNREAD:
            # Stable: newest first within the unread and the read.
            rows = sorted(rows, key=lambda row: row.message.is_read)
        if self.query:
            rows = [row for row in rows if _matches(row, self.query)]
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

    async def _switch(self, *, sent: bool) -> None:
        self.sent = sent
        self.query = None
        self.highlighted = None
        self.top = 0
        await self._reload()
        await self._render()

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
            await _show_inbox_message(self.session, self.lane, self.user, message, link_context=self.link_context)
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
        parts: list[str] = []
        if self.sent:
            parts.append(colored(f"{total} sent message{'s' if total != 1 else ''}", fg_color=VALUE_COLOR))
        else:
            unread = sum(1 for row in self.all_rows if not row.message.is_read)
            parts.append(
                colored(f"{unread} unread message{'s' if unread != 1 else ''}", fg_color=WARNING_COLOR)
                if unread else colored("Inbox caught up", fg_color=SUCCESS_COLOR)
            )
            # Counted against the cap (issue #818): the whole Inbox, read or
            # not, whatever [F]ind is showing.
            parts.append(colored(
                f"{total} of {MAX_MAIL_PER_RECIPIENT}",
                fg_color=WARNING_COLOR if total >= MAILBOX_NEARLY_FULL else VALUE_COLOR,
            ))
            if self.order == _ORDER_UNREAD:
                parts.append(colored("unread first", fg_color=MUTED_COLOR))
        if self.query:
            parts.append(colored(f"matching \"{sanitize_text(self.query)}\"", fg_color=MUTED_COLOR))
        return separator.join(parts)

    def _options(self, *, row_count: int, pages: tuple[bool, bool], measuring: bool = False) -> list[MenuEntry]:
        has_next, has_previous = pages
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
        if not self.sent:
            options.append(MenuEntry(label=menu_key("S", "ent"), brief="Review mail you've sent"))
            if row_count:
                highlighted = self.rows[self.highlighted] if self.highlighted is not None else None
                if measuring or highlighted is None or highlighted.message.is_read:
                    # The longer label, which the page budget measures.
                    options.append(MenuEntry(label=menu_key("U", "nread"), brief="Mark the highlighted message unread"))
                else:
                    options.append(MenuEntry(label=menu_key("U", " Read"), brief="Mark the highlighted message read"))
            if self.all_rows:
                options.append(MenuEntry(
                    label=menu_key("O", "rder"),
                    brief="Newest first" if self.order == _ORDER_UNREAD else "Unread mail first",
                ))
        if self.all_rows:
            options.append(MenuEntry(
                label=menu_key("F", "ind"), brief=f"Find by {'recipient' if self.sent else 'sender'} or subject",
            ))
        options.append(MenuEntry(
            label=menu_key("B", "ack"), brief="Back to the Inbox" if self.sent else "Return to the main menu",
        ))
        return options

    def _frame(
        self, *, row_count: int, pages: tuple[bool, bool], measuring: bool = False,
    ) -> tuple[str, str]:
        """Everything above the list's rows and everything below them."""
        session = self.session
        width = session.terminal_width
        header = screen_title(
            "Sent" if self.sent else "Inbox",
            breadcrumb=(session.node_display_name, "Mail"),
            subtitle=self._subtitle(),
            width=width,
            clear=self.redraw_in_place,
            unicode_style=self.unicode_style, collapsed=self.collapsed,
            header_color=self.header_color,
            node_name_gradient=session.node_name_gradient,
        )
        notes: list[str] = []

        def note(text: str, color: int) -> None:
            # A short terminal gives each note one row: the list is what
            # the caller came for, and the 40x12 floor has three rows for it.
            if not self._roomy():
                text = truncate_to_width(text, max(1, width - 1), ellipsis="…" if self.unicode_style else "...")
            notes.extend(colored(row, fg_color=color) for row in wrap_to_width(text, max(1, width - 1)))

        draft = _load_letter_draft(_letter_draft_path(self.lane, self.user)) if self.has_draft else None
        if draft is not None:
            # Said on the mail screen, not asked on the way in (issue #814).
            note(_letter_draft_notice(draft), MUTED_COLOR)
        if not self.sent:
            capacity = mailbox_capacity_note(
                len(self.all_rows), sum(1 for row in self.all_rows if not row.message.is_read),
            )
            if capacity is not None:
                text, tone = capacity
                note(text, ERROR_COLOR if tone == "error" else WARNING_COLOR)
        if not self.sent and any(row.identity_changed for row in self.all_rows):
            note(_IDENTITY_NOTE, WARNING_COLOR)
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
                ))
            if roomy:
                lines.append(rule)
            lines.extend(_mail_list_rows(
                page_rows, width=width, first_number=1, number_width=number_width, widths=widths,
                highlighted=self.highlighted - top if self.highlighted is not None else None,
                sent=self.sent, show_status=show_status, accent=self.accent,
                ellipsis="…" if self.unicode_style else "...",
            ))
            if roomy:
                lines.append(rule)
        else:
            if self.query:
                empty = f"Nothing here matches \"{sanitize_text(self.query)}\". [F]ind with an empty line shows all."
            else:
                empty = _EMPTY_SENT if self.sent else _EMPTY_INBOX
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
) -> tuple[str, list[str], list[str]]:
    """The title, the header rows (From or To, Date, any identity warning)
    and the body rows of one message, for `show_detail` to draw a page at a
    time (issue #679: a long message used to scroll its own header away)."""
    mailbox = "Sent" if to_label is not None else "Inbox"
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
    preamble: list[str] = []
    if to_label is not None:
        preamble.append(colored("To: ", fg_color=LABEL_COLOR) + colored(sanitize_text(to_label), fg_color=accent))
        shown_status = delivery_display_status(message.link_delivery_status, message.link_relay_handoff_at)
        delivery = delivery_explanation(shown_status, message.link_delivery_reason)
        if delivery is not None:
            preamble.append(
                colored("Delivery: ", fg_color=LABEL_COLOR)
                + colored(delivery, fg_color=_DELIVERY_COLORS.get(shown_status, VALUE_COLOR))
            )
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
        # as a letter's envelope would.
        preamble.append(colored("To: ", fg_color=LABEL_COLOR) + colored(sanitize_text(user.username), fg_color=accent))
    display_format, display_timezone = await lane.run(resolve_display_preferences)
    displayed_date = format_for_display(
        message.created_at, override_format=display_format, override_timezone=display_timezone
    )
    preamble.append(colored("Date: ", fg_color=LABEL_COLOR) + colored(displayed_date, fg_color=METADATA_COLOR))
    body_mode = await lane.run(_mail_body_mode, user)
    truecolor = await lane.run(lambda db: effective_truecolor(session, db, user))
    body_rows = post_body_rows(message.body, session.terminal_width, body_mode, truecolor=truecolor, layout="lines")
    return title, preamble, body_rows


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
) -> tuple[str, int]:
    """One message on `show_detail`: returns the action key and the page
    it was pressed on."""
    unicode_style = await lane.run(unicode_style_enabled, user)
    title, preamble, body_rows = await _message_view(
        session, lane, user, message=message, to_label=to_label,
        unicode_style=unicode_style,
        collapsed=await lane.run(breadcrumb_collapsed_enabled, user),
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
    *, link_context: LinkContext | None = None,
) -> None:
    message = await lane.run(mark_read, user, message)
    block_target = await lane.run(_block_target, user, message)
    page = 0
    while True:
        # Mail the BBS sent has nobody to answer (issue #819): no Reply key,
        # and the view says why.
        actions = [] if message.from_system else [("r", menu_key("R", "eply"))]
        actions += [
            # Offered on system mail too (issue #822): passing a notice on
            # to someone -- the SysOp, say -- harms no one.
            ("f", menu_key("F", "orward")),
            ("u", menu_key("U", "nread")),
            ("d", menu_key("D", "elete")),
        ]
        if block_target is not None:
            # Issue #817: a toggle, labelled by what it will do.
            blocked = await lane.run(_is_blocked, user, block_target)
            actions.append(("k", menu_key("k", " sender", prefix="Unbloc" if blocked else "Bloc")))
        actions.append(("b", menu_key("B", "ack")))
        choice, page = await _show_message(session, lane, user, message, to_label=None, actions=actions, page=page)
        if choice == "b":
            return
        if choice == "k":
            text, tone = await lane.run(_toggle_block, user, block_target)
            announce(session, text, tone=tone)
            continue
        if choice == "f":
            await _forward_message(session, lane, user, message, sent=False, link_context=link_context)
            continue
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


# -- blocked senders (issue #817) ---------------------------------------------
#
# A caller blocks a sender from a letter they received (`Bloc[k] sender` on
# its view, a toggle) or by name from Profile > Blocked senders, which lists
# them and unblocks. Local senders are blocked by account id, Link senders by
# the `user@<fingerprint>` address their mail came from. The rules --
# who cannot be blocked, what the sender is told -- are `netbbs.mail`'s.

_BLOCKED_NOTICE = "Blocked {name}: mail from them is refused from now on, and they are told so."
_UNBLOCKED_NOTICE = "Unblocked {name}: their mail is accepted again."


@dataclass(frozen=True)
class _BlockTarget:
    """Who `Bloc[k] sender` on a received letter acts on: a local account
    by id, or a Link sender by address."""
    user_id: int | None
    address: str | None


def _block_target(db: Database, reader: User, message: MailMessage) -> _BlockTarget | None:
    """The sender a received letter's view can block, or `None`: system
    mail, a deleted account, the reader's own mail, and a SysOp of this
    node offer no block (`netbbs.mail.sender_unblockable_reason`)."""
    if message.from_system:
        return None
    if message.sender_user_id is not None:
        sender = get_user_by_id(db, message.sender_user_id)
        if sender is None or sender_unblockable_reason(db, reader, sender) is not None:
            return None
        return _BlockTarget(user_id=sender.id, address=None)
    if _split_link_address(message.sender_label) is not None:
        return _BlockTarget(user_id=None, address=message.sender_label)
    return None


_link_sender_name = link_address_display_label


def _is_blocked(db: Database, reader: User, target: _BlockTarget) -> bool:
    if target.user_id is not None:
        sender = get_user_by_id(db, target.user_id)
        return sender is not None and blocks_local_sender(db, reader, sender)
    assert target.address is not None
    return blocks_link_sender(db, reader, target.address)


def _toggle_block(db: Database, reader: User, target: _BlockTarget) -> tuple[str, str]:
    """Block the sender if they are not blocked, else unblock them. Returns
    the outcome line and its tone."""
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
    """Profile > Blocked senders (issue #817): everyone `user` refuses mail
    from, newest first. `[A]dd` blocks someone by name -- a local user, or
    `name@TheirBBS` for someone on a linked BBS -- and `[U]nblock`, or
    picking a row, unblocks it. Each outcome is carried into the redraw."""

    async def _reload() -> list[_BlockedRow]:
        return await lane.run(_load_blocked_rows, user)

    async def _add() -> list[_BlockedRow] | None:
        await session.write_line("")
        await write_prompt(session, "Block mail from (a user name, or name@TheirBBS; empty cancels): ")
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
            title="Blocked senders",
            breadcrumb=("Profile",),
            empty_message="You block no one. Mail from anyone reaches you.",
            refresh=_reload,
            live_keys={"a": _add},
            item_keys={"u": _unblock},
            live_nav=[
                MenuEntry(label=menu_key("A", "dd"), brief="Block mail from someone by name"),
                MenuEntry(label=menu_key("U", "nblock"), brief="Accept their mail again"),
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
    expired, sends the same letter again as a new one. A letter sent from
    either returns to the Sent list, where it now is, with "Message sent."
    above the prompt; anything else comes back to this view."""
    to_label = await _display_recipient_label(lane, message)
    failed = message.link_delivery_status in ("bounced", "expired")
    if failed:
        # Seen here, so the main menu need not tell it again (issue #806).
        await lane.run(acknowledge_delivery_notices, [message.id])
    actions = [("r", menu_key("R", "eply"))]
    if failed and message.recipient_remote_address is not None:
        # Only a letter that did not arrive (issue #825): one that did, or
        # may yet, would reach its reader twice.
        actions.append(("s", menu_key("s", "end", prefix="Re")))
    actions += [("f", menu_key("F", "orward")), ("d", menu_key("D", "elete")), ("b", menu_key("B", "ack"))]
    page = 0
    while True:
        choice, page = await _show_message(session, lane, user, message, to_label=to_label, actions=actions, page=page)
        if choice == "b":
            return
        if choice == "f":
            await _forward_message(session, lane, user, message, sent=True, link_context=link_context)
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
        keys = {"resend_key": _resend_key(message)}
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
    link_context: LinkContext | None = None,
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
        await _show_inbox_message(session, lane, user, message, link_context=link_context)
    return await lane.run(lambda db: current_letter(db, user, mail_id, sent=sent)) is not None


# -- mail from where callers meet (issue #821) --------------------------------
#
# Directory, Who's online, Previous callers and the board reader each offer a
# Mail action. They all come through here, so every one of them makes the same
# checks the mailbox's own To prompt makes, and lands on the same compose
# screen with the recipient already filled in.


async def mail_open_to(session: Session, lane: DatabaseLane, user: User) -> bool:
    """Whether a screen outside the mailbox offers its Mail action at all:
    only while mail is open to the caller (issue #816). `mail_someone`
    checks again when the key is pressed, since the SysOp can close mail
    while the screen is up."""
    return await lane.run(lambda db: caller_mail_refusal(session, db, user)) is None


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
    #825): `quote` is then that letter's text, not a quote of it.

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
    )


# -- forwarding (issue #822) ---------------------------------------------------


async def _forward_message(
    session: Session, lane: DatabaseLane, user: User, message: MailMessage,
    *, sent: bool, link_context: LinkContext | None,
) -> None:
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
    Each letter's forward keeps its own draft slot (`_forward_key`)."""
    refusal = await lane.run(lambda db: caller_mail_refusal(session, db, user))
    if refusal is not None:
        announce(session, refusal, tone="error")
        return
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
    await _compose_mail(
        session, lane, user,
        prefill_subject=forward_subject(message.subject, max_bytes=MAX_MAIL_SUBJECT_BYTES),
        prefill_body=body, link_context=link_context, forward_key=_forward_key(message),
    )


async def _compose_mail(
    session: Session,
    lane: DatabaseLane,
    user: User,
    *,
    prefill_recipient: User | None = None,
    prefill_link_address: str | None = None,
    prefill_subject: str = "",
    prefill_body: str | None = None,
    link_context: LinkContext | None = None,
    reply_key: str | None = None,
    forward_key: str | None = None,
    resend_key: str | None = None,
    resume: bool = False,
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
    signature included, so none is added.

    Each letter has its own draft slot (issue #814): the new letter, a
    reply to one message (`reply_key`, see `_reply_key`), and a forward
    of one (`forward_key`), and a resend of one (`resend_key`). Either editor keeps the text there as
    it is typed, with its To and Subject beside it, and "Keep draft &
    exit" or `/exit` leaves it for later. A letter found in its slot is
    offered before anything is asked -- resume it, delete it and start
    again, or go back -- and is never loaded into another letter's
    editor. `resume` skips that choice: the caller already made it.

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
        that too; a Link address is kept as typed, for Send to check again."""
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
    if resumed is not None and resumed.reply_address is not None:
        prefill_link_address, prefill_recipient = resumed.reply_address, None
    elif resumed is not None and resumed.recipient_text is not None:
        prefill_link_address, prefill_recipient = None, None
        recipient_text, recipient_label = await settle_recipient(resumed.recipient_text)
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
        ) + "Tab completes a name; ? and Enter lists who you can write to. An empty line or Esc cancels."
        await compose_screen([], hint=to_hint)
        # Tab and ? (issue #826): gathered once, as the prompt opens.
        book = await lane.run(lambda db: gather_address_book(db, user, link_enabled=link_enabled))
        to_options = read_to_line_options(RecipientCompleter(book, session, "To: "))
        picked = False
        while True:
            await write_prompt(session, "To: ")
            try:
                recipient_text = (await session.read_line(cancellable=True, **to_options)).strip()
            except InputCancelled:
                recipient_text = ""
            if not recipient_text:
                # A resumed letter from before #814 is still kept (review on
                # #873): say so, not that it is gone.
                announce(session, kept_notice if resumed is not None else "Cancelled.", tone="muted")
                return False
            request = picker_request(recipient_text, link_enabled=link_enabled)
            picked = False
            if request is not None:
                chosen = await choose_recipient(session, book, request, **picker_style())
                await compose_screen([], hint=to_hint)
                if chosen is None:
                    continue
                # Checked below exactly as a typed address is.
                recipient_text, picked = chosen, True
            # "sysop" reaches the node's SysOp (issue #840, F087).
            recipient_text = await lane.run(resolve_sysop_alias, recipient_text)
            if link_enabled and "@" in recipient_text:
                # Checked as it is typed, like a local name (issue #807):
                # a bad address is asked for again here, not after the
                # message is written.
                checked = await lane.run(_check_link_recipient, recipient_text)
                if isinstance(checked, str):
                    await session.write_line(colored(checked, fg_color=ERROR_COLOR))
                    continue
                break
            try:
                typed = await lane.run(get_user_by_username, recipient_text)
            except AuthError:
                # Retry in place rather than discarding the whole compose
                # attempt on one typo -- the identical error at the final
                # commit step below already only re-prompts for the
                # recipient, keeping subject/body intact; this matches
                # that, instead of the harsher "start over" outcome
                # hitting it here first would otherwise cause.
                await session.write_line(
                    colored(f"No such user: {sanitize_text(recipient_text)!r}", fg_color=ERROR_COLOR)
                )
                continue
            # The guest account takes no mail (issue #816): said here, before
            # anything is written, and asked again.
            refused = await lane.run(mail_recipient_refusal, typed)
            if refused is None:
                # Nor does one that blocked this caller (issue #817): said
                # before the letter is written, not after.
                refused = await lane.run(lambda db: mail_sender_refusal(db, typed, sender=user))
            if refused is not None:
                await session.write_line(colored(sanitize_text(refused), fg_color=ERROR_COLOR))
                continue
            break
        recipient_text, recipient_label = await settle_recipient(recipient_text)
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
            draft_path, {"to": recipient_text, "reply_address": reply_address, "subject": subject},
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
        too_long = _too_long_to_send(subject, body)
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
        )
        if action is ReviewAction.CANCEL:
            _forget_letter(draft_path)
            announce(session, "Message cancelled.", tone="muted")
            return False
        if action is ReviewAction.EDIT_RECIPIENT:
            # Opened on the name the caller reads, not a technical identity
            # the To prompt resolved it to (issue #826); unchanged keeps it.
            edited = await read_prefilled_field(session, "To", recipient_label)
            request = picker_request(edited, link_enabled=link_enabled)
            if request is not None:
                # ? here too, and what is chosen is checked at Send.
                book = await lane.run(lambda db: gather_address_book(db, user, link_enabled=link_enabled))
                edited = await choose_recipient(session, book, request, **picker_style()) or recipient_label
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
            try:
                await lane.run(
                    compose_link_message, user, technical_recipient, subject, body,
                    node_identity=link_context.node_identity,
                )
            except (LinkMailError, MailError) as exc:
                announce(session, f"Could not send: {exc}", tone="error")
                continue
            _forget_letter(draft_path)
            announce(session, "Message sent.")
            return True

        try:
            recipient = await lane.run(get_user_by_username, recipient_text)
        except AuthError:
            announce(session, f"Could not send: no such user {recipient_text!r}.", tone="error")
            continue
        try:
            await lane.run(send_mail, user, recipient, subject, body)
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


def _reply_key(message: MailMessage) -> str:
    """The message a reply answers, for its draft slot's name: its id, and
    a digest of when and from whom it came -- a mail id can be handed out
    again once the newest message is gone, and a kept reply must not be
    offered for a different message that got the same id."""
    digest = hashlib.sha256(f"{message.created_at}|{message.sender_label}".encode("utf-8")).hexdigest()[:12]
    return f"{message.id}_{digest}"


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
    push loop would refuse, and "Message sent." is never shown for it."""
    decision = decide_node_action(db, fingerprint, LinkPolicyAction.LINK_MAIL)
    if decision.allowed:
        return None
    label = sanitize_text(identity_for_fingerprint(db, fingerprint).label)
    if decision.state == TrustState.PROBATIONARY:
        return f"{label} is newly linked; mail opens once the SysOp establishes it."
    return f"Mail to {label} is closed on this BBS."


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

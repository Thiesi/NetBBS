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

- `pick_item`'s `name_of`/`description_of` callbacks are synchronous
  (`netbbs.net.picker.pick_item`'s own contract) and run inside its
  render loop, off the lane entirely -- any per-item display data that
  needs a DB read (recipient labels, formatted timestamps) is fetched
  *before* calling `pick_item`, once, via the lane, into a plain dict
  the callback closures then just index into. `netbbs.timeutil.
  resolve_display_preferences` exists specifically for this: fetch the
  node's format/timezone once per picker call, not once per item.
- `_letter_draft_path` takes no `Database` at all -- it only needs
  the connection's file *path*, not a query, so it reads `lane.path`
  directly (a plain in-memory attribute, see `DatabaseLane.path`'s own
  docstring) rather than going through the lane's worker thread for
  something that was never actually blocking.
"""

from __future__ import annotations

import hashlib
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
    DELIVERY_STATUS_LABELS, LinkMailError, acknowledge_delivery_notices, compose_link_message, delivery_explanation,
)
from netbbs.link.node_profiles import (
    ambiguous_node_guidance, link_address_label, unknown_node_guidance, unquote_reference,
    identity_for_fingerprint, is_node_fingerprint, latest_identity_observation, resolve_stored_peer_reference,
)
from netbbs.mail import (
    MAX_MAIL_BODY_BYTES,
    MAX_MAIL_SUBJECT_BYTES,
    MailboxFullError,
    MailError,
    MailMessage,
    delete_for_recipient,
    delete_for_sender,
    list_inbox,
    list_sent,
    mark_read,
    send_mail,
    unread_count,
)
from netbbs.net.char_input import InputCancelled, reject_unhandled_key
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
from netbbs.net.picker import pick_item
from netbbs.net.prose_editor import EditorHeader, edit_prose
from netbbs.net.detail_view import show_detail
from netbbs.net.notices import announce, announce_styled, take_notices, write_notices
from netbbs.net.session import Session, write_prompt
from netbbs.rendering.detail import Section, Styled
from netbbs.quoting import quote_body, reply_subject
from netbbs.signature import append_signature, get_signature
from netbbs.rendering import (
    ERROR_COLOR,
    LABEL_COLOR,
    METADATA_COLOR,
    MUTED_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    WARNING_COLOR,
    MenuEntry,
    action_bar,
    colored,
    menu_grid,
    menu_key,
    reflow,
    sanitize_text,
    screen_title,
)
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display, resolve_display_preferences

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
    session: Session, lane: DatabaseLane, user: User, *, link_context: LinkContext | None = None
) -> None:
    """Entry point from the main menu's `[E]-mail` option.

    `link_context` (design doc), if given, lets `_compose_mail`
    recognize a `user@node` address and send a Link message
    instead of ordinary local mail -- `None` whenever this node has Link
    disabled, the same convention `netbbs.link.boards.LinkContext`
    itself already establishes for boards."""
    description_level = await lane.run(menu_description_level, user)
    redraw_in_place = await lane.run(redraw_in_place_enabled, user)
    unicode_style = await lane.run(unicode_style_enabled, user)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, user)
    _adopt_legacy_mail_draft(lane, user)
    await _render_mail_menu(session, lane, user, description_level, redraw_in_place, unicode_style, collapsed)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "d" and _letter_draft_path(lane, user).exists():
            # The kept new letter (issue #814): resume it, delete it, or
            # leave it -- the same choice a board's saved post draft gets.
            await session.write_line("")
            draft = _load_letter_draft(_letter_draft_path(lane, user))
            if draft is not None:
                outcome = await _letter_draft_choice(session, lane, user, draft, starting_new=False)
                if outcome == "resume":
                    await _compose_mail(session, lane, user, link_context=link_context, resume=True)
                elif outcome == "discard":
                    _forget_letter(_letter_draft_path(lane, user))
                    announce(session, "Draft deleted.", tone="muted")
            await _render_mail_menu(session, lane, user, description_level, redraw_in_place, unicode_style, collapsed)
        elif choice == "b":
            await session.write_line("")
            return
        elif choice == "i":
            await session.write_line("")
            await _show_inbox(session, lane, user, link_context=link_context)
            await _render_mail_menu(session, lane, user, description_level, redraw_in_place, unicode_style, collapsed)
        elif choice == "s":
            await session.write_line("")
            await _show_sent(session, lane, user)
            await _render_mail_menu(session, lane, user, description_level, redraw_in_place, unicode_style, collapsed)
        elif choice == "c":
            await session.write_line("")
            await _compose_mail(session, lane, user, link_context=link_context)
            await _render_mail_menu(session, lane, user, description_level, redraw_in_place, unicode_style, collapsed)
        else:
            await session.write(reject_unhandled_key(choice))


async def _render_mail_menu(
    session: Session, lane: DatabaseLane, user: User, description_level: str, redraw_in_place: bool = False,
    unicode_style: bool = False,
    collapsed: bool = False,
) -> None:
    unread = await lane.run(unread_count, user)
    subtitle = (
        colored(f"{unread} unread message{'s' if unread != 1 else ''}", fg_color=WARNING_COLOR)
        if unread
        else colored("Inbox caught up", fg_color=SUCCESS_COLOR)
    )
    header = screen_title("Mail",
            breadcrumb=(session.node_display_name,), subtitle=subtitle, width=session.terminal_width, clear=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            header_color=await lane.run(effective_header_color_256), node_name_gradient=session.node_name_gradient)
    await session.write_line(f"\r\n{header}")

    draft = _load_letter_draft(_letter_draft_path(lane, user))
    if draft is not None:
        # Said on the mail screen, not asked on the way in (issue #814).
        await session.write_line(colored(f"\r\n{_letter_draft_notice(draft)}", fg_color=MUTED_COLOR))
    options = [
        MenuEntry(label=menu_key("I", "nbox"), brief="Read your received mail"),
        MenuEntry(label=menu_key("S", "ent"), brief="Review mail you've sent"),
        MenuEntry(label=menu_key("C", "ompose"), brief="Write a new message"),
    ]
    if draft is not None:
        options.append(MenuEntry(label=menu_key("D", "raft"), brief="Resume or delete your unfinished letter"))
    options.append(MenuEntry(label=menu_key("B", "ack"), brief="Return to the main menu"))
    await session.write_line(
        f"\r\n{_menu_row(options, width=session.terminal_width, height=session.terminal_height, description_level=description_level)}"
    )
    await write_notices(session)
    await session.write("Choice: ")


async def _show_inbox(
    session: Session, lane: DatabaseLane, user: User, *, link_context: LinkContext | None = None
) -> None:
    description_level = await lane.run(menu_description_level, user)
    redraw_in_place = await lane.run(redraw_in_place_enabled, user)
    unicode_style = await lane.run(unicode_style_enabled, user)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, user)
    while True:
        messages = await lane.run(list_inbox, user)
        display_format, display_timezone = await lane.run(resolve_display_preferences)
        # Pre-fetched once, outside pick_item's synchronous callbacks --
        # see this module's own docstring for why.
        sender_labels = {
            m.id: await _display_sender_label(lane, m) for m in messages
        }
        identity_warnings = {
            m.id: await _link_mail_identity_warning(lane, m.sender_label) for m in messages
        }
        descriptions = {
            m.id: f"from {sender_labels[m.id]} "
            f"{'[IDENTITY CHANGED] ' if identity_warnings[m.id] else ''}"
            f"({format_for_display(m.created_at, override_format=display_format, override_timezone=display_timezone)})"
            for m in messages
        }
        names = {m.id: f"{'' if m.is_read else '[NEW] '}{m.subject}" for m in messages}

        message = await pick_item(
            session,
            messages,
            name_of=lambda m: names[m.id],
            description_of=lambda m: descriptions[m.id],
            stable_id_of=lambda m: m.id,
            title="Inbox",
            empty_message="Your inbox is empty. New mail will appear here.",
            description_level=description_level,
            redraw_in_place=redraw_in_place,
            unicode_style=unicode_style,
            collapsed=collapsed,
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
        )
        if message is None:
            return
        await _show_inbox_message(session, lane, user, message, link_context=link_context)


async def _show_sent(session: Session, lane: DatabaseLane, user: User) -> None:
    description_level = await lane.run(menu_description_level, user)
    redraw_in_place = await lane.run(redraw_in_place_enabled, user)
    unicode_style = await lane.run(unicode_style_enabled, user)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, user)
    while True:
        messages = await lane.run(list_sent, user)
        display_format, display_timezone = await lane.run(resolve_display_preferences)

        # One lane call per message to resolve its recipient's current
        # username -- sequential, not batched, since no bulk
        # get-users-by-ids lookup exists yet; acceptable at this
        # project's declared scale (mailboxes are quota-bounded, design
        # doc §14) and no slower than today's per-item synchronous
        # lookups were.
        recipient_labels = {m.id: await _display_recipient_label(lane, m) for m in messages}

        descriptions = {
            m.id: f"to {recipient_labels[m.id]} "
            f"{_delivery_tag(m)}"
            f"({format_for_display(m.created_at, override_format=display_format, override_timezone=display_timezone)})"
            for m in messages
        }

        message = await pick_item(
            session,
            messages,
            name_of=lambda m: m.subject,
            description_of=lambda m: descriptions[m.id],
            stable_id_of=lambda m: m.id,
            title="Sent Mail",
            empty_message="You haven't sent any mail. Compose one from the Mail menu.",
            description_level=description_level,
            redraw_in_place=redraw_in_place,
            unicode_style=unicode_style,
            collapsed=collapsed,
            accent_color=await lane.run(effective_accent_color_256),
            header_color=await lane.run(effective_header_color_256),
        )
        if message is None:
            return
        await _show_sent_message(session, lane, user, message)


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
        delivery = delivery_explanation(message.link_delivery_status, message.link_delivery_reason)
        if delivery is not None:
            preamble.append(
                colored("Delivery: ", fg_color=LABEL_COLOR)
                + colored(delivery, fg_color=_DELIVERY_COLORS.get(message.link_delivery_status, VALUE_COLOR))
            )
    else:
        sender_label = await _display_sender_label(lane, message)
        preamble.append(
            colored("From: ", fg_color=LABEL_COLOR) + colored(sanitize_text(sender_label), fg_color=accent)
        )
        warning = await _link_mail_identity_warning(lane, message.sender_label)
        if warning is not None:
            preamble.append(colored(warning, fg_color=MUTED_COLOR, bold=True))
    display_format, display_timezone = await lane.run(resolve_display_preferences)
    displayed_date = format_for_display(
        message.created_at, override_format=display_format, override_timezone=display_timezone
    )
    preamble.append(colored("Date: ", fg_color=LABEL_COLOR) + colored(displayed_date, fg_color=METADATA_COLOR))
    body = reflow(sanitize_text(message.body, allow_newlines=True), width=session.terminal_width)
    body_rows = [colored(line, fg_color=VALUE_COLOR) if line else "" for line in body.splitlines()]
    return title, preamble, body_rows


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


def _split_link_address(technical_address: str) -> tuple[str, str] | None:
    """`(user, fingerprint)` of a stored `user@<home-node-fingerprint>`, or
    `None` for a local name. Split at the last `@`: the user half comes from
    a peer's signed payload and nothing holds it to the username grammar,
    while a fingerprint never contains one."""
    user, separator, fingerprint = technical_address.rpartition("@")
    if not separator or not is_node_fingerprint(fingerprint):
        return None
    return user, fingerprint


async def _display_link_address(lane: DatabaseLane, technical_address: str) -> str:
    """Resolve a stored Link address's technical home node only at render
    time; a local name is returned as it is."""
    split = _split_link_address(technical_address)
    if split is None:
        return technical_address
    user, fingerprint = split
    node_label = (await lane.run(identity_for_fingerprint, fingerprint)).label
    return link_address_label(user, node_label)


async def _display_sender_label(lane: DatabaseLane, message: MailMessage) -> str:
    return await _display_link_address(lane, message.sender_label)


async def _display_recipient_label(lane: DatabaseLane, message: MailMessage) -> str:
    """Who a sent message went to: the remote address of Link mail (issue
    #805), else the local recipient's current name."""
    if message.recipient_remote_address is not None:
        return await _display_link_address(lane, message.recipient_remote_address)
    recipient = (
        await lane.run(get_user_by_id, message.recipient_user_id)
        if message.recipient_user_id is not None
        else None
    )
    return recipient.username if recipient is not None else "(deleted account)"


_DELIVERY_COLORS = {
    "pending": MUTED_COLOR,
    "delivered": SUCCESS_COLOR,
    "bounced": ERROR_COLOR,
    "expired": ERROR_COLOR,
}


def _delivery_tag(message: MailMessage) -> str:
    """Where a sent Link message stands, as a tag for its Sent list row
    (issue #806); empty for local mail. The reason is on the message."""
    status = message.link_delivery_status
    if status not in DELIVERY_STATUS_LABELS:
        return ""
    return f"[{DELIVERY_STATUS_LABELS[status].upper()}] "


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
    actions = [
        ("r", menu_key("R", "eply")),
        ("d", menu_key("D", "elete")),
        ("b", menu_key("B", "ack")),
    ]
    page = 0
    while True:
        choice, page = await _show_message(session, lane, user, message, to_label=None, actions=actions, page=page)
        if choice == "b":
            return
        if choice == "d":
            if not await prompt_yes_no(session, "Delete this message?", default=False):
                continue
            await lane.run(delete_for_recipient, user, message)
            announce(session, "Message deleted.")
            return
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
                prefill_body=quote_body(message.body, author=shown) or None,
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
            prefill_body=quote_body(message.body, author=message.sender_label) or None,
            reply_key=_reply_key(message),
        )


async def _show_sent_message(session: Session, lane: DatabaseLane, user: User, message: MailMessage) -> None:
    to_label = await _display_recipient_label(lane, message)
    if message.link_delivery_status in ("bounced", "expired"):
        # Seen here, so the main menu need not tell it again (issue #806).
        await lane.run(acknowledge_delivery_notices, [message.id])
    actions = [("d", menu_key("D", "elete")), ("b", menu_key("B", "ack"))]
    page = 0
    while True:
        choice, page = await _show_message(session, lane, user, message, to_label=to_label, actions=actions, page=page)
        if choice == "b":
            return
        if not await prompt_yes_no(session, "Delete this message?", default=False):
            continue
        await lane.run(delete_for_sender, user, message)
        announce(session, "Message deleted.")
        return


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
    resume: bool = False,
) -> None:
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

    Each letter has its own draft slot (issue #814): the new letter, and a
    reply to one message (`reply_key`, see `_reply_key`). Either editor keeps the text there as
    it is typed, with its To and Subject beside it, and "Keep draft &
    exit" or `/exit` leaves it for later. A letter found in its slot is
    offered before anything is asked -- resume it, delete it and start
    again, or go back -- and is never loaded into another letter's
    editor. `resume` skips that choice: the caller already made it.
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
    title = "Reply" if reply_key is not None else "New message"
    link_enabled = link_context is not None

    draft_path = _letter_draft_path(lane, user, reply_key)
    resumed = _load_letter_draft(draft_path)
    if resumed is not None and not resume:
        outcome = await _letter_draft_choice(session, lane, user, resumed, starting_new=True)
        if outcome == "back":
            return
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
    kept_notice = (
        "Draft saved -- you'll be offered it when you reply to this message again."
        if reply_key is not None
        else "Draft saved -- it is under [D]raft on the mail screen."
    )

    async def compose_screen(fields: list[tuple[str, str]], hint: str | None = None) -> None:
        await show_compose_screen(
            session, title=title, breadcrumb=("Mail",), fields=fields, hint=hint,
            redraw_in_place=redraw_in_place, unicode_style=unicode_style, collapsed=collapsed,
            header_color=header_color, accent_color=accent_color,
        )

    async def settle_recipient(text: str) -> tuple[str, str]:
        """What to keep as the address, and how to show it (issue #813). A
        local name is kept as the account spells it, so `[T]o` opens on
        that too; a Link address is kept as typed, for Send to check again."""
        # "sysop" wherever an address is typed, [T]o included (#840).
        text = await lane.run(resolve_sysop_alias, text)
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
        await compose_screen(
            [],
            hint=(
                "Who is it for? Type their user name, or name@TheirBBS for someone on a linked BBS. "
                if link_enabled else "Who is it for? Type their user name. "
            ) + "An empty line or Esc cancels.",
        )
        while True:
            await write_prompt(session, "To: ")
            try:
                recipient_text = (await session.read_line(cancellable=True)).strip()
            except InputCancelled:
                recipient_text = ""
            if not recipient_text:
                # A resumed letter from before #814 is still kept (review on
                # #873): say so, not that it is gone.
                announce(session, kept_notice if resumed is not None else "Cancelled.", tone="muted")
                return
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
                await lane.run(get_user_by_username, recipient_text)
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
            break
        recipient_text, recipient_label = await settle_recipient(recipient_text)

    if resumed is not None and resumed.subject is not None:
        subject = resumed.subject
    else:
        # Checked here rather than at Send (issue #812): an empty subject is
        # asked for again, one that is too long says by how much, and only
        # Esc on a fresh prompt gives up on the message.
        subject = await read_subject(session, max_bytes=MAX_MAIL_SUBJECT_BYTES, current=prefill_subject or None)
        if subject is None:
            announce(session, kept_notice if resumed is not None else "Message cancelled.", tone="muted")
            return

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
    # resumed letter where it was left off.
    body = await _compose_mail_body(
        session, lane, user, initial_text=prefill_body, cursor_at_end=prefill_body is not None,
        header=editor_header(), draft_path=draft_path,
    )
    if body is None or not body.strip():
        if body is None and draft_path.exists():
            announce(session, kept_notice, tone="muted")
            return
        _forget_letter(draft_path)
        announce(session, "Message cancelled.", tone="muted")
        return
    # Appended once, right after the message is first composed -- not on
    # every subsequent "edit body" pass over the same draft (`netbbs.
    # signature.append_signature`'s own docstring): from here on the
    # signature is just part of the editable body, the same way a real
    # mail client's compose buffer already works. Idempotent, so a
    # resumed letter that already carries it does not get it twice.
    signature = await lane.run(get_signature, user)
    if signature:
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
            breadcrumb=("Mail", title),
        )
        if action is ReviewAction.CANCEL:
            _forget_letter(draft_path)
            announce(session, "Message cancelled.", tone="muted")
            return
        if action is ReviewAction.EDIT_RECIPIENT:
            edited = await read_prefilled_field(session, "To", recipient_text)
            if edited != recipient_text:
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
                return
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
                checked = await lane.run(_check_link_reply_address, reply_address)
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
            return

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
        _forget_letter(draft_path)
        announce(session, "Message sent.")
        return


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


def _letter_draft_path(lane: DatabaseLane, user: User, reply_key: str | None = None) -> Path:
    """One slot per letter (issue #814): the caller's new letter, and one
    per message they are replying to. Before #814 every letter shared one
    body-only file, so a kept letter was offered in place of the next
    one's text -- a reply to someone else lost its quote to it."""
    directory = lane.path.parent / f"{lane.path.name}_drafts"
    directory.mkdir(parents=True, exist_ok=True)
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
    words that say what to type instead.

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
    refusal = _link_mail_refusal(db, resolved)
    if refusal is not None:
        return refusal
    return _LinkRecipient(user=user, fingerprint=resolved)


def _recipient_label(db, recipient_text: str, link_enabled: bool) -> str:
    """The recipient as the review screen and the editor show it (issue
    #813): a local account by its own name -- `Alice`, however it was
    typed -- and a Link address by the name its node goes by. Text that
    names no one yet (a `[T]o` edit Send will refuse) is shown as typed."""
    if link_enabled and "@" in recipient_text:
        checked = _check_link_recipient(db, recipient_text)
        if isinstance(checked, str):
            return recipient_text
        return link_address_label(checked.user, identity_for_fingerprint(db, checked.fingerprint).label)
    try:
        return get_user_by_username(db, recipient_text).username
    except AuthError:
        return recipient_text


def _check_link_reply_address(db, technical_address: str) -> _LinkRecipient | str:
    """Check the stored `user@<fingerprint>` a Link reply goes to (issue
    #805) the way `_check_link_recipient` checks a typed address -- the
    same refusal when this node will not send that peer mail -- in words
    that fit an address the caller did not type."""
    split = _split_link_address(technical_address)
    if split is None:
        return "This message has no address a reply could go to."
    user, fingerprint = split
    shown = sanitize_text(link_address_label(user, identity_for_fingerprint(db, fingerprint).label))
    if not is_valid_user_part(user):
        return f"{shown} is not an address mail can be sent to, so a reply can't reach it."
    if resolve_stored_peer_reference(db, fingerprint, met_only=True) != fingerprint:
        return f"This BBS is no longer linked with the BBS {shown} writes from, so a reply can't reach it."
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
    cursor_at_end: bool = False, header: EditorHeader | None = None,
) -> str | None:
    """Enter or revise one mail body through the user's chosen editor.

    Both paths accept the current draft and only return text/explicit cancel;
    the caller owns review and persistence. `header` is what the fullscreen
    editor shows above the text; the line editor writes under the compose
    or review screen, which already shows it.

    `draft_path` is this letter's own slot (issue #814), so both editors
    keep the text as it is typed and offer "Keep draft & exit" / `/exit`.
    Neither asks about a draft found there: `_compose_mail` has already
    offered it and passes it in as `initial_text`. `None` with the draft
    still on disk means the letter was kept; without it, cancelled.
    """
    if await lane.run(fullscreen_editor_enabled, user):
        return await edit_prose(
            session, initial_text=initial_text, draft_path=draft_path, max_bytes=MAX_MAIL_BODY_BYTES,
            unicode_style=await lane.run(unicode_style_enabled, user), cursor_at_end=cursor_at_end,
            header=header, offer_recovery=False,
        )
    return await edit_line_body(
        session,
        initial_text=initial_text,
        max_bytes=MAX_MAIL_BODY_BYTES,
        max_lines=_MAX_PLAIN_MAIL_LINES,
        draft_path=draft_path,
        offer_recovery=False,
    )

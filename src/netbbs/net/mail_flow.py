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
- `_mail_draft_path` no longer takes a `Database` at all -- it only
  ever needed the connection's file *path*, not a query, so it now
  reads `lane.path` directly (a plain in-memory attribute, see
  `DatabaseLane.path`'s own docstring) rather than going through the
  lane's worker thread for something that was never actually blocking.
"""

from __future__ import annotations

from pathlib import Path

from netbbs.auth.users import AuthError, User, get_user_by_id, get_user_by_username
from netbbs.link.boards import LinkContext
from netbbs.link.mail import LinkMailError, compose_link_message
from netbbs.mail import (
    MAX_MAIL_BODY_BYTES,
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
from netbbs.net.char_input import reject_unhandled_key
from netbbs.net.color_depth_preference import effective_truecolor
from netbbs.net.composition import ReviewAction, edit_line_body, review_composition
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.editor_preference import fullscreen_editor_enabled
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.net.node_theme import effective_accent_color_256, effective_header_color_256
from netbbs.net.picker import pick_item
from netbbs.net.prose_editor import edit_prose
from netbbs.net.session import Session
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
    recognize a `user@node-fingerprint` address and send a Link message
    instead of ordinary local mail -- `None` whenever this node has Link
    disabled, the same convention `netbbs.link.boards.LinkContext`
    itself already establishes for boards."""
    description_level = await lane.run(menu_description_level, user)
    redraw_in_place = await lane.run(redraw_in_place_enabled, user)
    unicode_style = await lane.run(unicode_style_enabled, user)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, user)
    await _render_mail_menu(session, lane, user, description_level, redraw_in_place, unicode_style, collapsed)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "i":
            await session.write_line("")
            await _show_inbox(session, lane, user)
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

    options = [
        MenuEntry(label=menu_key("I", "nbox"), brief="Read your received mail"),
        MenuEntry(label=menu_key("S", "ent"), brief="Review mail you've sent"),
        MenuEntry(label=menu_key("C", "ompose"), brief="Write a new message"),
        MenuEntry(label=menu_key("B", "ack"), brief="Return to the main menu"),
    ]
    await session.write_line(
        f"\r\n{_menu_row(options, width=session.terminal_width, height=session.terminal_height, description_level=description_level)}"
    )
    await session.write("Choice: ")


async def _show_inbox(session: Session, lane: DatabaseLane, user: User) -> None:
    description_level = await lane.run(menu_description_level, user)
    redraw_in_place = await lane.run(redraw_in_place_enabled, user)
    unicode_style = await lane.run(unicode_style_enabled, user)
    collapsed = await lane.run(breadcrumb_collapsed_enabled, user)
    while True:
        messages = await lane.run(list_inbox, user)
        display_format, display_timezone = await lane.run(resolve_display_preferences)
        # Pre-fetched once, outside pick_item's synchronous callbacks --
        # see this module's own docstring for why.
        descriptions = {
            m.id: f"from {m.sender_label} "
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
        await _show_inbox_message(session, lane, user, message)


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
        recipient_labels: dict[int, str] = {}
        for m in messages:
            recipient = await lane.run(get_user_by_id, m.recipient_user_id)
            recipient_labels[m.id] = recipient.username if recipient is not None else "(deleted account)"

        descriptions = {
            m.id: f"to {recipient_labels[m.id]} "
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


async def _render_message(
    session: Session,
    lane: DatabaseLane,
    user: User,
    *,
    message: MailMessage,
    to_label: str | None,
    redraw_in_place: bool = False,
    unicode_style: bool = False,
    collapsed: bool = False,
) -> None:
    mailbox = "Sent" if to_label is not None else "Inbox"
    header = screen_title(
        sanitize_text(message.subject),
        breadcrumb=(session.node_display_name, "Mail", mailbox),
        width=session.terminal_width,
        clear=redraw_in_place,
        unicode_style=unicode_style, collapsed=collapsed,
        header_color=await lane.run(effective_header_color_256),
    node_name_gradient=session.node_name_gradient)
    await session.write_line(f"\r\n{header}")
    accent = await lane.run(effective_accent_color_256)
    if to_label is not None:
        await session.write_line(
            colored("To: ", fg_color=LABEL_COLOR)
            + colored(sanitize_text(to_label), fg_color=accent)
        )
    else:
        await session.write_line(
            colored("From: ", fg_color=LABEL_COLOR)
            + colored(sanitize_text(message.sender_label), fg_color=accent)
        )
    display_format, display_timezone = await lane.run(resolve_display_preferences)
    displayed_date = format_for_display(
        message.created_at, override_format=display_format, override_timezone=display_timezone
    )
    await session.write_line(
        colored("Date: ", fg_color=LABEL_COLOR)
        + colored(displayed_date, fg_color=METADATA_COLOR)
    )
    await session.write_line("")
    rule_char = "─" if unicode_style else "-"
    truecolor = await lane.run(lambda db: effective_truecolor(session, db, user))
    divider_color = 238 if truecolor else METADATA_COLOR
    divider = colored(rule_char * min(session.terminal_width, 78), fg_color=divider_color)
    await session.write_line(divider)
    body = reflow(sanitize_text(message.body, allow_newlines=True), width=session.terminal_width)
    await session.write_line(colored(body, fg_color=VALUE_COLOR))
    await session.write_line(divider)


async def _show_inbox_message(session: Session, lane: DatabaseLane, user: User, message: MailMessage) -> None:
    message = await lane.run(mark_read, user, message)
    await _render_message(
        session, lane, user, message=message, to_label=None,
        redraw_in_place=await lane.run(redraw_in_place_enabled, user),
        unicode_style=await lane.run(unicode_style_enabled, user),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, user),
    )
    description_level = await lane.run(menu_description_level, user)

    while True:
        options = [
            MenuEntry(label=menu_key("R", "eply"), brief="Reply to the sender"),
            MenuEntry(label=menu_key("D", "elete"), brief="Delete this message"),
            MenuEntry(label=menu_key("B", "ack"), brief="Return to the inbox"),
        ]
        await session.write_line(
            f"\r\n{_menu_row(options, width=session.terminal_width, height=session.terminal_height, description_level=description_level)}"
        )
        await session.write("Choice: ")
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "d":
            await session.write_line("")
            if not await prompt_yes_no(session, "Delete this message?", default=False):
                continue
            await lane.run(delete_for_recipient, user, message)
            await session.write_line(colored("Message deleted.", fg_color=SUCCESS_COLOR))
            return
        elif choice == "r":
            await session.write_line("")
            sender = (
                await lane.run(get_user_by_id, message.sender_user_id)
                if message.sender_user_id is not None
                else None
            )
            if sender is None:
                await session.write_line(
                    colored("That sender's account no longer exists -- can't reply.", fg_color=ERROR_COLOR)
                )
                continue
            reply_subject = message.subject if message.subject.lower().startswith("re:") else f"Re: {message.subject}"
            await _compose_mail(session, lane, user, prefill_recipient=sender, prefill_subject=reply_subject)
        else:
            await session.write(reject_unhandled_key(choice))


async def _show_sent_message(session: Session, lane: DatabaseLane, user: User, message: MailMessage) -> None:
    recipient = await lane.run(get_user_by_id, message.recipient_user_id)
    to_label = recipient.username if recipient is not None else "(deleted account)"
    await _render_message(
        session, lane, user, message=message, to_label=to_label,
        redraw_in_place=await lane.run(redraw_in_place_enabled, user),
        unicode_style=await lane.run(unicode_style_enabled, user),
        collapsed=await lane.run(breadcrumb_collapsed_enabled, user),
    )
    description_level = await lane.run(menu_description_level, user)

    while True:
        options = [
            MenuEntry(label=menu_key("D", "elete"), brief="Delete this message"),
            MenuEntry(label=menu_key("B", "ack"), brief="Return to sent mail"),
        ]
        await session.write_line(
            f"\r\n{_menu_row(options, width=session.terminal_width, height=session.terminal_height, description_level=description_level)}"
        )
        await session.write("Choice: ")
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "d":
            await session.write_line("")
            if not await prompt_yes_no(session, "Delete this message?", default=False):
                continue
            await lane.run(delete_for_sender, user, message)
            await session.write_line(colored("Message deleted.", fg_color=SUCCESS_COLOR))
            return
        else:
            await session.write(reject_unhandled_key(choice))


async def _compose_mail(
    session: Session,
    lane: DatabaseLane,
    user: User,
    *,
    prefill_recipient: User | None = None,
    prefill_subject: str = "",
    link_context: LinkContext | None = None,
) -> None:
    """
    `link_context`, if given, lets the "To:" prompt accept a `user@
    node-fingerprint` address (design doc) in addition to a
    plain local username -- routed to `netbbs.link.mail.compose_link_
    message` instead of `netbbs.mail.send_mail`. Only checked on the
    fresh-compose path: a reply always targets an already-resolved
    local `User` (`prefill_recipient`), never a typed address.
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

    if prefill_recipient is not None:
        recipient_text = prefill_recipient.username
        await session.write_line(f"To: {sanitize_text(recipient_text)}")
    else:
        prompt = "username or user@node-fingerprint" if link_context is not None else "username"
        while True:
            await session.write(f"\r\nTo ({prompt}): ")
            recipient_text = (await session.read_line()).strip()
            if not recipient_text:
                await session.write_line(colored("Cancelled.", fg_color=MUTED_COLOR))
                return
            if link_context is not None and "@" in recipient_text:
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

    if prefill_subject:
        await session.write(f"Subject [{sanitize_text(prefill_subject)}] (Enter to keep): ")
        subject = (await session.read_line()).strip() or prefill_subject
    else:
        await session.write("Subject: ")
        subject = (await session.read_line()).strip()
    if not subject:
        await session.write_line(colored("Cancelled -- a subject is required.", fg_color=ERROR_COLOR))
        return

    body = await _compose_mail_body(session, lane, user, initial_text=None)
    if body is None or not body.strip():
        await session.write_line(colored("Message cancelled.", fg_color=MUTED_COLOR))
        return
    # Appended once, right after the message is first composed -- not on
    # every subsequent "edit body" pass over the same draft (`netbbs.
    # signature.append_signature`'s own docstring): from here on the
    # signature is just part of the editable body, the same way a real
    # mail client's compose buffer already works.
    signature = await lane.run(get_signature, user)
    if signature:
        body = append_signature(body, signature)

    review_description_level = await lane.run(menu_description_level, user)
    review_redraw_in_place = await lane.run(redraw_in_place_enabled, user)
    review_unicode_style = await lane.run(unicode_style_enabled, user)
    review_collapsed = await lane.run(breadcrumb_collapsed_enabled, user)
    review_accent_color = await lane.run(effective_accent_color_256)
    review_header_color = await lane.run(effective_header_color_256)
    review_truecolor = await lane.run(lambda db: effective_truecolor(session, db, user))
    while True:
        action = await review_composition(
            session,
            recipient=recipient_text,
            subject=subject,
            body=body,
            commit_key="s",
            commit_label="end",
            commit_brief="Send this message",
            description_level=review_description_level,
            redraw_in_place=review_redraw_in_place,
            unicode_style=review_unicode_style,
            collapsed=review_collapsed,
            accent_color=review_accent_color,
            header_color=review_header_color,
            truecolor=review_truecolor,
        )
        if action is ReviewAction.CANCEL:
            await session.write_line(colored("Message cancelled.", fg_color=MUTED_COLOR))
            return
        if action is ReviewAction.EDIT_RECIPIENT:
            await session.write(f"To [{sanitize_text(recipient_text)}] (Enter to keep): ")
            recipient_text = (await session.read_line()).strip() or recipient_text
            continue
        if action is ReviewAction.EDIT_SUBJECT:
            await session.write(f"Subject [{sanitize_text(subject)}] (Enter to keep): ")
            subject = (await session.read_line()).strip() or subject
            continue
        if action is ReviewAction.EDIT_BODY:
            revised = await _compose_mail_body(session, lane, user, initial_text=body)
            if revised is not None:
                body = revised
            else:
                await session.write_line(colored("Body unchanged.", fg_color=MUTED_COLOR))
            continue

        if link_context is not None and "@" in recipient_text:
            try:
                await lane.run(
                    compose_link_message, user, recipient_text, subject, body,
                    node_identity=link_context.node_identity,
                )
            except (LinkMailError, MailError) as exc:
                await session.write_line(colored(f"Could not send: {exc}", fg_color=ERROR_COLOR))
                continue
            await session.write_line(colored("Message sent.", fg_color=SUCCESS_COLOR))
            return

        try:
            recipient = await lane.run(get_user_by_username, recipient_text)
        except AuthError:
            await session.write_line(
                colored(f"Could not send: no such user {sanitize_text(recipient_text)!r}.", fg_color=ERROR_COLOR)
            )
            continue
        try:
            await lane.run(send_mail, user, recipient, subject, body)
        except MailboxFullError:
            await session.write_line(
                colored(
                    f"{recipient.username}'s mailbox is full and cannot accept new mail right now.",
                    fg_color=ERROR_COLOR,
                )
            )
            continue
        except MailError as exc:
            await session.write_line(colored(f"Could not send: {exc}", fg_color=ERROR_COLOR))
            continue
        await session.write_line(colored("Message sent.", fg_color=SUCCESS_COLOR))
        return


async def _compose_mail_body(
    session: Session, lane: DatabaseLane, user: User, *, initial_text: str | None
) -> str | None:
    """Enter or revise one mail body through the user's chosen editor.

    Both paths accept the current draft and only return text/explicit cancel;
    the caller owns review and persistence.
    """
    if await lane.run(fullscreen_editor_enabled, user):
        return await edit_prose(
            session, initial_text=initial_text, draft_path=_mail_draft_path(lane, user), max_bytes=MAX_MAIL_BODY_BYTES,
            unicode_style=await lane.run(unicode_style_enabled, user),
        )
    return await edit_line_body(
        session,
        initial_text=initial_text,
        max_bytes=MAX_MAIL_BODY_BYTES,
        max_lines=_MAX_PLAIN_MAIL_LINES,
    )


def _mail_draft_path(lane: DatabaseLane, user: User) -> Path:
    directory = lane.path.parent / f"{lane.path.name}_drafts"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"mail_{user.id}.draft"

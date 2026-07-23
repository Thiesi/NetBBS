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
from netbbs.net.editor_preference import fullscreen_editor_enabled
from netbbs.net.picker import pick_item
from netbbs.net.prose_editor import edit_prose
from netbbs.net.session import Session
from netbbs.rendering import HEADER_COLOR, MUTED_COLOR, colored, menu_key, reflow, reject_keystroke, sanitize_text
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


async def browse_mail(
    session: Session, lane: DatabaseLane, user: User, *, link_context: LinkContext | None = None
) -> None:
    """Entry point from the main menu's `[E]-mail` option.

    `link_context` (design doc), if given, lets `_compose_mail`
    recognize a `user@node-fingerprint` address and send a Link message
    instead of ordinary local mail -- `None` whenever this node has Link
    disabled, the same convention `netbbs.link.boards.LinkContext`
    itself already establishes for boards."""
    await _render_mail_menu(session, lane, user)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "i":
            await session.write_line("")
            await _show_inbox(session, lane, user)
            await _render_mail_menu(session, lane, user)
        elif choice == "s":
            await session.write_line("")
            await _show_sent(session, lane, user)
            await _render_mail_menu(session, lane, user)
        elif choice == "c":
            await session.write_line("")
            await _compose_mail(session, lane, user, link_context=link_context)
            await _render_mail_menu(session, lane, user)
        else:
            await session.write(reject_keystroke())


async def _render_mail_menu(session: Session, lane: DatabaseLane, user: User) -> None:
    unread = await lane.run(unread_count, user)
    header = colored("\r\nMail:", fg_color=HEADER_COLOR, bold=True)
    suffix = f" ({unread} unread)" if unread else ""
    await session.write_line(f"{header}{suffix}")

    options = "  ".join(
        [
            menu_key("I", "nbox"),
            menu_key("S", "ent"),
            menu_key("C", "ompose"),
            menu_key("B", "ack"),
        ]
    )
    await session.write_line(options)
    await session.write("Choice: ")


async def _show_inbox(session: Session, lane: DatabaseLane, user: User) -> None:
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
        names = {m.id: f"{'' if m.is_read else '* '}{m.subject}" for m in messages}

        message = await pick_item(
            session,
            messages,
            name_of=lambda m: names[m.id],
            description_of=lambda m: descriptions[m.id],
            stable_id_of=lambda m: m.id,
            title="Inbox",
            empty_message="Your inbox is empty.",
        )
        if message is None:
            return
        await _show_inbox_message(session, lane, user, message)


async def _show_sent(session: Session, lane: DatabaseLane, user: User) -> None:
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
            empty_message="You haven't sent any mail.",
        )
        if message is None:
            return
        await _show_sent_message(session, lane, user, message)


async def _render_message(
    session: Session, lane: DatabaseLane, *, message: MailMessage, to_label: str | None
) -> None:
    header = colored(f"\r\nSubject: {sanitize_text(message.subject)}", fg_color=HEADER_COLOR, bold=True)
    await session.write_line(header)
    if to_label is not None:
        await session.write_line(f"To: {sanitize_text(to_label)}")
    else:
        await session.write_line(f"From: {sanitize_text(message.sender_label)}")
    display_format, display_timezone = await lane.run(resolve_display_preferences)
    await session.write_line(
        f"Date: {format_for_display(message.created_at, override_format=display_format, override_timezone=display_timezone)}"
    )
    await session.write_line("")
    await session.write_line(reflow(sanitize_text(message.body, allow_newlines=True), width=session.terminal_width))


async def _show_inbox_message(session: Session, lane: DatabaseLane, user: User, message: MailMessage) -> None:
    message = await lane.run(mark_read, user, message)
    await _render_message(session, lane, message=message, to_label=None)

    while True:
        options = "  ".join([menu_key("R", "eply"), menu_key("D", "elete"), menu_key("B", "ack")])
        await session.write_line(f"\r\n{options}")
        await session.write("Choice: ")
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "d":
            await session.write_line("")
            await lane.run(delete_for_recipient, user, message)
            await session.write_line("Message deleted.")
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
                    colored("That sender's account no longer exists -- can't reply.", fg_color=MUTED_COLOR)
                )
                continue
            reply_subject = message.subject if message.subject.lower().startswith("re:") else f"Re: {message.subject}"
            await _compose_mail(session, lane, user, prefill_recipient=sender, prefill_subject=reply_subject)
        else:
            await session.write(reject_keystroke())


async def _show_sent_message(session: Session, lane: DatabaseLane, user: User, message: MailMessage) -> None:
    recipient = await lane.run(get_user_by_id, message.recipient_user_id)
    to_label = recipient.username if recipient is not None else "(deleted account)"
    await _render_message(session, lane, message=message, to_label=to_label)

    while True:
        options = "  ".join([menu_key("D", "elete"), menu_key("B", "ack")])
        await session.write_line(f"\r\n{options}")
        await session.write("Choice: ")
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "d":
            await session.write_line("")
            await lane.run(delete_for_sender, user, message)
            await session.write_line("Message deleted.")
            return
        else:
            await session.write(reject_keystroke())


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
    recipient: User | None = None
    link_recipient_address: str | None = None

    if prefill_recipient is not None:
        recipient = prefill_recipient
        await session.write_line(f"To: {sanitize_text(recipient.username)}")
    else:
        prompt = "username or user@node-fingerprint" if link_context is not None else "username"
        await session.write(f"\r\nTo ({prompt}): ")
        typed = (await session.read_line()).strip()
        if not typed:
            await session.write_line(colored("Cancelled.", fg_color=MUTED_COLOR))
            return
        if link_context is not None and "@" in typed:
            link_recipient_address = typed
        else:
            try:
                recipient = await lane.run(get_user_by_username, typed)
            except AuthError:
                await session.write_line(colored(f"No such user: {typed!r}", fg_color=MUTED_COLOR))
                return

    if prefill_subject:
        await session.write(f"Subject [{prefill_subject}] (Enter to keep): ")
        subject = (await session.read_line()).strip() or prefill_subject
    else:
        await session.write("Subject: ")
        subject = (await session.read_line()).strip()
    if not subject:
        await session.write_line(colored("Cancelled -- a subject is required.", fg_color=MUTED_COLOR))
        return

    body = await _compose_mail_body(session, lane, user)
    if body is None or not body.strip():
        await session.write_line(colored("Cancelled -- message body cannot be blank.", fg_color=MUTED_COLOR))
        return

    if link_recipient_address is not None:
        try:
            await lane.run(
                compose_link_message, user, link_recipient_address, subject, body,
                node_identity=link_context.node_identity,
            )
        except (LinkMailError, MailError) as exc:
            await session.write_line(colored(f"Could not send: {exc}", fg_color=MUTED_COLOR))
            return
        await session.write_line("Message sent.")
        return

    try:
        await lane.run(send_mail, user, recipient, subject, body)
    except MailboxFullError:
        await session.write_line(
            colored(
                f"{recipient.username}'s mailbox is full and cannot accept new mail right now.",
                fg_color=MUTED_COLOR,
            )
        )
        return
    except MailError as exc:
        await session.write_line(colored(f"Could not send: {exc}", fg_color=MUTED_COLOR))
        return
    await session.write_line("Message sent.")


async def _compose_mail_body(session: Session, lane: DatabaseLane, user: User) -> str | None:
    """The single place a mail body is actually entered -- the
    fullscreen prose editor if `user` has opted in
    (`netbbs.net.editor_preference`), otherwise a repeated-`read_line`-
    until-blank-line prompt, matching `netbbs.net.login_flow._edit_bio`'s
    own plain-path shape (a letter benefits from multiple lines the way
    a bio does, unlike a board post's single-line plain fallback)."""
    if await lane.run(fullscreen_editor_enabled, user):
        return await edit_prose(
            session, initial_text=None, draft_path=_mail_draft_path(lane, user), max_bytes=MAX_MAIL_BODY_BYTES
        )
    await session.write_line("Enter your message. Blank line to finish.")
    lines: list[str] = []
    for _ in range(_MAX_PLAIN_MAIL_LINES):
        line = (await session.read_line()).strip()
        if not line:
            break
        lines.append(line)
    return "\n".join(lines)


def _mail_draft_path(lane: DatabaseLane, user: User) -> Path:
    directory = lane.path.parent / f"{lane.path.name}_drafts"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"mail_{user.id}.draft"

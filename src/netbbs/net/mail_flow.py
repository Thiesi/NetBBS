"""
Local asynchronous personal mail UI (design doc round 93/104), wiring
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
"""

from __future__ import annotations

from pathlib import Path

from netbbs.auth.users import AuthError, User, get_user_by_id, get_user_by_username
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
from netbbs.storage.database import Database
from netbbs.timeutil import format_for_display

# Cap on the plain (non-fullscreen-editor) line-at-a-time body prompt --
# same shape as `netbbs.directory.MAX_BIO_LINES`, just sized for a
# letter rather than a short bio. `netbbs.mail.MAX_MAIL_BODY_BYTES` is
# still the one place actually enforcing a limit (checked by
# `send_mail` after the fact, same as post/bio validation elsewhere in
# this codebase) -- this is only a practical bound on the input loop
# itself.
_MAX_PLAIN_MAIL_LINES = 200


async def browse_mail(session: Session, db: Database, user: User) -> None:
    """Entry point from the main menu's `[E]-mail` option."""
    await _render_mail_menu(session, db, user)
    while True:
        choice = (await session.read_key()).lower()

        if choice == "b":
            await session.write_line("")
            return
        elif choice == "i":
            await session.write_line("")
            await _show_inbox(session, db, user)
            await _render_mail_menu(session, db, user)
        elif choice == "s":
            await session.write_line("")
            await _show_sent(session, db, user)
            await _render_mail_menu(session, db, user)
        elif choice == "c":
            await session.write_line("")
            await _compose_mail(session, db, user)
            await _render_mail_menu(session, db, user)
        else:
            await session.write(reject_keystroke())


async def _render_mail_menu(session: Session, db: Database, user: User) -> None:
    unread = unread_count(db, user)
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


async def _show_inbox(session: Session, db: Database, user: User) -> None:
    while True:
        messages = list_inbox(db, user)
        message = await pick_item(
            session,
            messages,
            name_of=lambda m: f"{'' if m.is_read else '* '}{m.subject}",
            description_of=lambda m: f"from {m.sender_label} ({format_for_display(m.created_at, db)})",
            stable_id_of=lambda m: m.id,
            title="Inbox",
            empty_message="Your inbox is empty.",
        )
        if message is None:
            return
        await _show_inbox_message(session, db, user, message)


async def _show_sent(session: Session, db: Database, user: User) -> None:
    while True:
        messages = list_sent(db, user)

        def _recipient_label(m: MailMessage) -> str:
            recipient = get_user_by_id(db, m.recipient_user_id)
            return recipient.username if recipient is not None else "(deleted account)"

        message = await pick_item(
            session,
            messages,
            name_of=lambda m: m.subject,
            description_of=lambda m: f"to {_recipient_label(m)} ({format_for_display(m.created_at, db)})",
            stable_id_of=lambda m: m.id,
            title="Sent Mail",
            empty_message="You haven't sent any mail.",
        )
        if message is None:
            return
        await _show_sent_message(session, db, user, message)


async def _render_message(session: Session, db: Database, *, message: MailMessage, to_label: str | None) -> None:
    header = colored(f"\r\nSubject: {sanitize_text(message.subject)}", fg_color=HEADER_COLOR, bold=True)
    await session.write_line(header)
    if to_label is not None:
        await session.write_line(f"To: {sanitize_text(to_label)}")
    else:
        await session.write_line(f"From: {sanitize_text(message.sender_label)}")
    await session.write_line(f"Date: {format_for_display(message.created_at, db)}")
    await session.write_line("")
    await session.write_line(reflow(sanitize_text(message.body, allow_newlines=True), width=session.terminal_width))


async def _show_inbox_message(session: Session, db: Database, user: User, message: MailMessage) -> None:
    message = mark_read(db, user, message)
    await _render_message(session, db, message=message, to_label=None)

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
            delete_for_recipient(db, user, message)
            await session.write_line("Message deleted.")
            return
        elif choice == "r":
            await session.write_line("")
            sender = get_user_by_id(db, message.sender_user_id) if message.sender_user_id is not None else None
            if sender is None:
                await session.write_line(
                    colored("That sender's account no longer exists -- can't reply.", fg_color=MUTED_COLOR)
                )
                continue
            reply_subject = message.subject if message.subject.lower().startswith("re:") else f"Re: {message.subject}"
            await _compose_mail(session, db, user, prefill_recipient=sender, prefill_subject=reply_subject)
        else:
            await session.write(reject_keystroke())


async def _show_sent_message(session: Session, db: Database, user: User, message: MailMessage) -> None:
    recipient = get_user_by_id(db, message.recipient_user_id)
    to_label = recipient.username if recipient is not None else "(deleted account)"
    await _render_message(session, db, message=message, to_label=to_label)

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
            delete_for_sender(db, user, message)
            await session.write_line("Message deleted.")
            return
        else:
            await session.write(reject_keystroke())


async def _compose_mail(
    session: Session,
    db: Database,
    user: User,
    *,
    prefill_recipient: User | None = None,
    prefill_subject: str = "",
) -> None:
    if prefill_recipient is not None:
        recipient = prefill_recipient
        await session.write_line(f"To: {sanitize_text(recipient.username)}")
    else:
        await session.write("\r\nTo (username): ")
        username = (await session.read_line()).strip()
        if not username:
            await session.write_line(colored("Cancelled.", fg_color=MUTED_COLOR))
            return
        try:
            recipient = get_user_by_username(db, username)
        except AuthError:
            await session.write_line(colored(f"No such user: {username!r}", fg_color=MUTED_COLOR))
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

    body = await _compose_mail_body(session, db, user)
    if body is None or not body.strip():
        await session.write_line(colored("Cancelled -- message body cannot be blank.", fg_color=MUTED_COLOR))
        return

    try:
        send_mail(db, user, recipient, subject, body)
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


async def _compose_mail_body(session: Session, db: Database, user: User) -> str | None:
    """The single place a mail body is actually entered -- the
    fullscreen prose editor if `user` has opted in
    (`netbbs.net.editor_preference`), otherwise a repeated-`read_line`-
    until-blank-line prompt, matching `netbbs.net.login_flow._edit_bio`'s
    own plain-path shape (a letter benefits from multiple lines the way
    a bio does, unlike a board post's single-line plain fallback)."""
    if fullscreen_editor_enabled(db, user):
        return await edit_prose(
            session, initial_text=None, draft_path=_mail_draft_path(db, user), max_bytes=MAX_MAIL_BODY_BYTES
        )
    await session.write_line("Enter your message. Blank line to finish.")
    lines: list[str] = []
    for _ in range(_MAX_PLAIN_MAIL_LINES):
        line = (await session.read_line()).strip()
        if not line:
            break
        lines.append(line)
    return "\n".join(lines)


def _mail_draft_path(db: Database, user: User) -> Path:
    directory = db.path.parent / f"{db.path.name}_drafts"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"mail_{user.id}.draft"

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
from netbbs.link.enforcement import LinkPolicyAction, decide_node_action
from netbbs.link.trust import TrustState
from netbbs.link.mail import LinkMailError, compose_link_message
from netbbs.link.node_profiles import (
    identity_for_fingerprint, latest_identity_observation, resolve_stored_peer_reference,
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
from netbbs.net.char_input import reject_unhandled_key
from netbbs.net.color_depth_preference import effective_truecolor
from netbbs.net.composition import (
    ReviewAction,
    characters_over,
    edit_line_body,
    read_prefilled_field,
    read_subject,
    review_composition,
    too_long_message,
)
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.editor_preference import fullscreen_editor_enabled
from netbbs.net.menu_description_preference import menu_description_level
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.net.node_theme import effective_accent_color_256, effective_header_color_256
from netbbs.net.picker import pick_item
from netbbs.net.prose_editor import edit_prose
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
    recognize a `user@node-name-or-dns` address and send a Link message
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
    await write_notices(session)
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


async def _display_sender_label(lane: DatabaseLane, message: MailMessage) -> str:
    """Resolve a Link sender's technical home node only at render time."""
    if "@" not in message.sender_label:
        return message.sender_label
    user_id, fingerprint = message.sender_label.split("@", 1)
    node_label = (await lane.run(identity_for_fingerprint, fingerprint)).label
    return f"{user_id}@{node_label}"


async def _link_mail_identity_warning(
    lane: DatabaseLane, technical_address: str,
) -> str | None:
    if "@" not in technical_address:
        return None
    _user_id, fingerprint = technical_address.split("@", 1)
    observation = await lane.run(latest_identity_observation, fingerprint)
    if observation is None or observation.severity != "security":
        return None
    return (
        "Caution: this familiar node name now has a different cryptographic identity. "
        "Mail remains available, but verify the change if it was unexpected."
    )


async def _show_inbox_message(session: Session, lane: DatabaseLane, user: User, message: MailMessage) -> None:
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
        sender = (
            await lane.run(get_user_by_id, message.sender_user_id)
            if message.sender_user_id is not None
            else None
        )
        if sender is None:
            announce(session, "That sender's account no longer exists -- can't reply.", tone="error")
            continue
        # The same subject rule and quote a board reply uses (issue #675).
        await _compose_mail(
            session, lane, user, prefill_recipient=sender,
            prefill_subject=reply_subject(message.subject, max_bytes=MAX_MAIL_SUBJECT_BYTES),
            prefill_body=quote_body(message.body, author=message.sender_label) or None,
        )


async def _show_sent_message(session: Session, lane: DatabaseLane, user: User, message: MailMessage) -> None:
    recipient = await lane.run(get_user_by_id, message.recipient_user_id)
    to_label = recipient.username if recipient is not None else "(deleted account)"
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
    prefill_subject: str = "",
    prefill_body: str | None = None,
    link_context: LinkContext | None = None,
) -> None:
    """
    `link_context`, if given, lets the "To:" prompt accept a `user@
    node-name-or-dns` address (design doc) in addition to a
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
        prompt = "username or user@node-name-or-dns" if link_context is not None else "username"
        while True:
            await write_prompt(session, f"\r\nTo ({prompt}): ")
            recipient_text = (await session.read_line()).strip()
            if not recipient_text:
                announce(session, "Cancelled.", tone="muted")
                return
            if link_context is not None and "@" in recipient_text:
                refusal = await _link_mail_refusal_for_address(lane, recipient_text)
                if refusal is not None:
                    await session.write_line(colored(refusal, fg_color=ERROR_COLOR))
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

    # Checked here rather than at Send (issue #812): an empty subject is
    # asked for again, one that is too long says by how much, and only
    # Esc on a fresh prompt gives up on the message.
    subject = await read_subject(session, max_bytes=MAX_MAIL_SUBJECT_BYTES, current=prefill_subject or None)
    if subject is None:
        announce(session, "Message cancelled.", tone="muted")
        return

    # A reply starts on the quote, with the cursor under it (issue #675).
    body = await _compose_mail_body(
        session, lane, user, initial_text=prefill_body, cursor_at_end=prefill_body is not None
    )
    if body is None or not body.strip():
        announce(session, "Message cancelled.", tone="muted")
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
        # Before Review, not at Send (issue #812): an editor stops the
        # body at the limit, but the signature is added afterwards and can
        # carry it over. Said on the review screen, where [B]ody and
        # [U]pdate subject fix it; Send is refused until then.
        too_long = _too_long_to_send(subject, body)
        if too_long is not None:
            announce(session, too_long, tone="error")
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
            announce(session, "Message cancelled.", tone="muted")
            return
        if action is ReviewAction.EDIT_RECIPIENT:
            recipient_text = await read_prefilled_field(session, "To", recipient_text)
            continue
        if action is ReviewAction.EDIT_SUBJECT:
            subject = await read_subject(session, max_bytes=MAX_MAIL_SUBJECT_BYTES, current=subject)
            continue
        if action is ReviewAction.EDIT_BODY:
            revised = await _compose_mail_body(session, lane, user, initial_text=body)
            if revised is not None:
                body = revised
            else:
                announce(session, "Body unchanged.", tone="muted")
            continue
        if too_long is not None:
            continue

        if link_context is not None and "@" in recipient_text:
            remote_user, node_reference = recipient_text.split("@", 1)
            resolved = await lane.run(resolve_stored_peer_reference, node_reference, met_only=True)
            if isinstance(resolved, list):
                if resolved:
                    candidates = []
                    for fingerprint in resolved[:5]:
                        identity = await lane.run(identity_for_fingerprint, fingerprint)
                        candidates.append(
                            f"{sanitize_text(identity.label)} [{sanitize_text(fingerprint)}]"
                        )
                    announce_styled(
                        session,
                        colored(
                            f"Could not send: {sanitize_text(node_reference)!r} matches more than one node "
                            f"({', '.join(candidates)}). Address the recipient as "
                            "user@technical-identity.",
                            fg_color=ERROR_COLOR,
                        ),
                    )
                else:
                    announce_styled(
                        session,
                        colored(
                            f"Could not send: no linked node is known as {sanitize_text(node_reference)!r}.",
                            fg_color=ERROR_COLOR,
                        ),
                    )
                continue
            # The To prompt checks this too; the address may have been
            # edited from the review screen since.
            refusal = await lane.run(_link_mail_refusal, resolved)
            if refusal is not None:
                announce_styled(session, colored(refusal, fg_color=ERROR_COLOR))
                continue
            technical_recipient = f"{remote_user}@{resolved}"
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
        announce(session, "Message sent.")
        return


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


async def _link_mail_refusal_for_address(lane: DatabaseLane, recipient_text: str) -> str | None:
    """`_link_mail_refusal` for a typed `user@node` address, at the To
    prompt. An address that does not name exactly one known node is left
    for the send step to explain."""
    node_reference = recipient_text.split("@", 1)[1]
    resolved = await lane.run(resolve_stored_peer_reference, node_reference, met_only=True)
    if isinstance(resolved, list):
        return None
    return await lane.run(_link_mail_refusal, resolved)


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
    session: Session, lane: DatabaseLane, user: User, *, initial_text: str | None, cursor_at_end: bool = False
) -> str | None:
    """Enter or revise one mail body through the user's chosen editor.

    Both paths accept the current draft and only return text/explicit cancel;
    the caller owns review and persistence.
    """
    if await lane.run(fullscreen_editor_enabled, user):
        return await edit_prose(
            session, initial_text=initial_text, draft_path=_mail_draft_path(lane, user), max_bytes=MAX_MAIL_BODY_BYTES,
            unicode_style=await lane.run(unicode_style_enabled, user), cursor_at_end=cursor_at_end,
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

"""
The SysOp console's Welcome letter screen (issue #1352), under Settings ›
Mastheads & banners › Session banners, beside the new-account banners: the
letter every new account finds in its mailbox (`netbbs.welcome_letter`).

One screen, the #282 model: what the letter is and what has become of it
drawn as a panel, every change a key on it, and each outcome carried into
the redraw. Every change is audited.
"""

from __future__ import annotations

from netbbs.auth.users import AuthError, User, get_user_by_username
from netbbs.config import set_config_without_commit
from netbbs.mail import MailError, validate_mail_fields
from netbbs.moderation.log import record_action_without_commit
from netbbs.net.admin_fields import _CLEAR_HINT, _read_seeded_line
from netbbs.net.admin_ui import _announce_line, _choice_prompt, _load_chrome, _write_sections
from netbbs.net.char_input import HELP_KEY, InputCancelled, reject_unhandled_key
from netbbs.net.draft_storage import delete_draft, drafts_directory
from netbbs.net.help_overlay import show_menu_help
from netbbs.net.location import crumbs, located
from netbbs.net.resource_editor import write_field_prompt
from netbbs.net.session import Session
from netbbs.rendering import (
    ERROR_COLOR,
    MUTED_COLOR,
    SUCCESS_COLOR,
    VALUE_COLOR,
    WARNING_COLOR,
    MenuEntry,
    action_bar,
    colored,
    menu_key,
    sanitize_text,
    screen_title,
)
from netbbs.rendering.detail import Field, Note, Section
from netbbs.rendering.menu import continue_prompt
from netbbs.rendering.pipe_codes import render_pipe_codes
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display
from netbbs.welcome_letter import (
    BODY_KEY,
    ENABLED_KEY,
    SENDER_KEY,
    SUBJECT_KEY,
    WelcomeLetter,
    fill_placeholders,
    load_welcome_letter,
    sender_problem,
)

#: How much of the letter the screen shows; [P]review shows all of it.
_TEXT_ROWS = 6
#: The name a preview fills `{user}` with.
_PREVIEW_NAME = "NewCaller"

_ACTIONS = (
    MenuEntry(label=menu_key("O", "n/off"), brief="Send it to new accounts, or stop"),
    MenuEntry(label=menu_key("E", "dit text"), brief="Write it in the mail editor"),
    MenuEntry(label=menu_key("S", "ubject"), brief="Change the letter's subject"),
    MenuEntry(label=menu_key("F", "rom"), brief="Who the letter comes from"),
    MenuEntry(label=menu_key("P", "review"), brief="Read it as a new caller will"),
    MenuEntry(label=menu_key("B", "ack"), brief="Return to Session banners"),
)


def _save(db: Database, actor: User, settings: dict[str, str], *, detail: str) -> None:
    """Store `settings` and their audit entry together."""
    db.connection.execute("BEGIN IMMEDIATE")
    try:
        for key, value in settings.items():
            set_config_without_commit(db, key, value)
        record_action_without_commit(db, actor=actor, action="set_welcome_letter", detail=detail)
    except BaseException:
        db.connection.rollback()
        raise
    db.connection.commit()


def _sections(db: Database, letter: WelcomeLetter) -> list[Section]:
    problem = sender_problem(db, letter.sender)
    if letter.enabled:
        status = Field("Status", "on: sent to every new account once it can sign in", color=SUCCESS_COLOR)
    else:
        status = Field("Status", "off: new accounts get no letter", color=MUTED_COLOR)
    sender = letter.sender.username if letter.sender is not None else "(none)"
    rows: list[Field | Note] = [
        status,
        Field("From", sender, color=VALUE_COLOR if problem is None else WARNING_COLOR),
        Field("Subject", sanitize_text(letter.subject)),
    ]
    if letter.sent:
        when = format_for_display(letter.last_sent_at, db) if letter.last_sent_at else "?"
        rows.append(Field("Sent", f"{letter.sent}, last to {letter.last_recipient} on {when}"))
    else:
        rows.append(Field("Sent", "none yet", color=MUTED_COLOR))
    if problem is not None and (letter.enabled or letter.sender_id is not None):
        rows.append(Note(f"{problem} Pick a sender with [F]rom.", color=WARNING_COLOR))
    if letter.last_failure:
        when = format_for_display(letter.last_failure_at, db) if letter.last_failure_at else "?"
        rows.append(Note(f"Last letter not sent ({when}): {sanitize_text(letter.last_failure)[:240]}",
                         color=WARNING_COLOR))
    lines = sanitize_text(letter.body, allow_newlines=True).splitlines() or [""]
    text: list[Field | Note] = [Note(line or " ") for line in lines[:_TEXT_ROWS]]
    if len(lines) > _TEXT_ROWS:
        text.append(Note(f"... {len(lines) - _TEXT_ROWS} more lines; [P]review shows them all.", color=MUTED_COLOR))
    text.append(Note("{user}, {node} and {date} are filled in for each new account. A letter already sent "
                     "stays as it was when you change this one.", color=MUTED_COLOR))
    return [Section("Letter", rows), Section("Text", text)]


@located("Welcome letter")
async def welcome_letter_screen(session: Session, lane: DatabaseLane, actor: User) -> None:
    """The console screen for the welcome letter."""
    while True:
        chrome = await _load_chrome(lane, actor)
        letter = await lane.run(load_welcome_letter)
        sections = await lane.run(_sections, letter)
        await session.write_line("\r\n" + screen_title(
            "Welcome letter", breadcrumb=crumbs(session, own=True), width=session.terminal_width,
            subtitle="A letter from you in every new account's mailbox.",
            clear=chrome.redraw_in_place, unicode_style=chrome.unicode_style, collapsed=chrome.collapsed,
            header_color=chrome.header_color, node_name_gradient=session.node_name_gradient,
        ))
        await _write_sections(session, sections, unicode_style=chrome.unicode_style)
        await session.write_line("\r\n" + action_bar(
            [entry.label for entry in _ACTIONS], width=session.terminal_width, height=session.terminal_height,
        ))
        await _choice_prompt(session)
        choice = (await session.read_key()).lower()
        if choice == "b":
            await session.write_line("")
            return
        if choice == "o":
            await _toggle(session, lane, actor, letter)
        elif choice == "e":
            await _edit_text(session, lane, actor, letter)
        elif choice == "s":
            await _edit_subject(session, lane, actor, letter)
        elif choice == "f":
            await _pick_sender(session, lane, actor, letter)
        elif choice == "p":
            await _preview(session, lane, letter)
        elif choice == HELP_KEY:
            await show_menu_help(
                session, "Welcome letter help", _ACTIONS,
                about="Every new account finds this letter unread in its mailbox once it can sign in: "
                      "straight away when anyone may sign up, on approval when you approve accounts, "
                      "and for accounts made here on the console. Existing accounts never get it. It "
                      "comes from the account under From, which must be you or a staff member who may "
                      "write mail, so a reply reaches a person. Changing the text, subject or sender "
                      "affects only letters sent afterwards.",
                header_color=chrome.header_color, unicode_style=chrome.unicode_style,
            )
        else:
            await session.write(reject_unhandled_key(choice))


async def _toggle(session: Session, lane: DatabaseLane, actor: User, letter: WelcomeLetter) -> None:
    await session.write_line("")
    if letter.enabled:
        await lane.run(_save, actor, {ENABLED_KEY: "0"}, detail="off")
        _announce_line(session, colored("Welcome letter off. New accounts get no letter.", fg_color=MUTED_COLOR))
        return
    settings = {ENABLED_KEY: "1", SUBJECT_KEY: letter.subject, BODY_KEY: letter.body}
    sender = letter.sender
    if sender is None or await lane.run(sender_problem, sender) is not None:
        # Whoever switches it on is the sender until they pick another.
        sender = actor
        settings[SENDER_KEY] = str(actor.id)
    problem = await lane.run(sender_problem, sender)
    if problem is not None:
        _announce_line(session, colored(f"{problem} The welcome letter stays off.", fg_color=ERROR_COLOR))
        return
    await lane.run(_save, actor, settings, detail=f"on, from {sender.username}")
    _announce_line(session, colored(
        f"Welcome letter on, from {sender.username}. Every new account gets it once it can sign in.",
        fg_color=SUCCESS_COLOR,
    ))


async def _edit_text(session: Session, lane: DatabaseLane, actor: User, letter: WelcomeLetter) -> None:
    from netbbs.net.mail_flow import _compose_mail_body

    await session.write_line("")
    draft_path = await lane.run(lambda db: drafts_directory(db) / f"welcome_letter_{actor.id}.draft")
    # A draft kept last time is where the text picks up (Claude review of
    # #1353: the draft is gone only once its text is saved).
    initial = draft_path.read_text(encoding="utf-8") if draft_path.exists() else letter.body
    body = await _compose_mail_body(session, lane, actor, initial_text=initial, draft_path=draft_path,
                                    cursor_at_end=True)
    if body is None:
        if draft_path.exists():
            _announce_line(session, colored("Draft kept; [E]dit text picks it up again. Letter unchanged.",
                                            fg_color=MUTED_COLOR))
        else:
            _announce_line(session, colored("Letter unchanged.", fg_color=MUTED_COLOR))
        return
    problem = None
    try:
        validate_mail_fields(letter.subject, body)
    except MailError as exc:
        problem = f"{exc}."
    if problem is None and not body.strip():
        problem = "A letter needs some text."
    if problem is not None:
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        draft_path.write_text(body, encoding="utf-8")
        _announce_line(session, colored(f"{problem} Letter unchanged; your text is kept as a draft for [E]dit text.",
                                        fg_color=ERROR_COLOR))
        return
    await lane.run(_save, actor, {BODY_KEY: body}, detail="text changed")
    delete_draft(draft_path)
    _announce_line(session, colored("Letter saved. New accounts get it from now on.", fg_color=SUCCESS_COLOR))


async def _edit_subject(session: Session, lane: DatabaseLane, actor: User, letter: WelcomeLetter) -> None:
    await session.write_line("")
    hint = "Enter saves, Esc keeps"
    await write_field_prompt(session, colored(f"Subject ({hint}):", fg_color=MUTED_COLOR), hint=hint)
    try:
        raw = await _read_seeded_line(session, initial=letter.subject)
    except InputCancelled:
        _announce_line(session, colored("Subject unchanged.", fg_color=MUTED_COLOR))
        return
    try:
        subject = validate_mail_fields(raw, letter.body)
    except MailError as exc:
        _announce_line(session, colored(f"{exc}. Subject unchanged.", fg_color=ERROR_COLOR))
        return
    await lane.run(_save, actor, {SUBJECT_KEY: subject}, detail=f"subject {subject!r}")
    _announce_line(session, colored("Subject saved.", fg_color=SUCCESS_COLOR))


async def _pick_sender(session: Session, lane: DatabaseLane, actor: User, letter: WelcomeLetter) -> None:
    await session.write_line("")
    hint = "Enter saves, Esc keeps"
    await write_field_prompt(
        session, colored(f"Send it from which SysOp or staff account? ({hint}):", fg_color=MUTED_COLOR), hint=hint,
    )
    current = letter.sender.username if letter.sender is not None else actor.username
    try:
        name = (await _read_seeded_line(session, initial=current)).strip()
    except InputCancelled:
        _announce_line(session, colored("Sender unchanged.", fg_color=MUTED_COLOR))
        return
    try:
        sender = await lane.run(get_user_by_username, name) if name else None
    except AuthError:
        sender = None
    if sender is None:
        _announce_line(session, colored(f"No account is called {sanitize_text(name)!r}. Sender unchanged.",
                                        fg_color=ERROR_COLOR))
        return
    problem = await lane.run(sender_problem, sender)
    if problem is not None:
        _announce_line(session, colored(f"{problem} Sender unchanged.", fg_color=ERROR_COLOR))
        return
    await lane.run(_save, actor, {SENDER_KEY: str(sender.id)}, detail=f"from {sender.username}")
    _announce_line(session, colored(f"The letter now comes from {sender.username}.", fg_color=SUCCESS_COLOR))


async def _preview(session: Session, lane: DatabaseLane, letter: WelcomeLetter) -> None:
    subject, body = await lane.run(lambda db: (
        fill_placeholders(db, letter.subject, _PREVIEW_NAME), fill_placeholders(db, letter.body, _PREVIEW_NAME),
    ))
    sender = letter.sender.username if letter.sender is not None else "(no sender)"
    await session.write_line(colored(f"\r\nThe letter as {_PREVIEW_NAME} would find it:", fg_color=MUTED_COLOR))
    await session.write_line(f"From: {sender}\r\nTo: {_PREVIEW_NAME}\r\nSubject: {sanitize_text(subject)}\r\n")
    for line in render_pipe_codes(sanitize_text(body, allow_newlines=True)).splitlines():
        await session.write_line(line)
    await session.write_line(continue_prompt())
    await session.read_any_key()

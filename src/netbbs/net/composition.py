"""Transport-independent line composition and pre-commit review.

The fullscreen prose editor owns a cursor-addressed screen model. This
module deliberately does not: it gives the default Telnet/SSH/web path a
caller-owned logical-line buffer with explicit operations, then provides the
shared review state used after either editor. Domain flows remain responsible
for validation and persistence; finishing an editor only returns a draft.
"""

from __future__ import annotations

from enum import Enum, auto
from pathlib import Path

from netbbs.net.char_input import CANCEL_KEY, HELP_KEY, EditorKey, EditorKeyKind, reject_unhandled_key
from netbbs.net.draft_storage import delete_draft, load_draft, offer_draft_recovery, save_draft
from netbbs.net.help_overlay import show_help
from netbbs.net.session import Session
from netbbs.rendering import (
    ACCENT_COLOR,
    HEADER_COLOR,
    LABEL_COLOR,
    MUTED_COLOR,
    MenuEntry,
    action_bar,
    colored,
    menu_grid,
    menu_key,
    reflow,
    sanitize_text,
    screen_title,
)


def _menu_row(entries: list[MenuEntry], *, width: int, height: int, description_level: str) -> str:
    """Compact `action_bar` packing when descriptions are off, `menu_grid`'s
    taller one-entry-per-line layout once the caller has opted into "brief"/
    "detailed" (issue #160's rollout) -- see `netbbs.net.resource_editor.
    edit_resource_draft`'s identical branch for why `menu_grid` alone isn't a
    byte-for-byte substitute for `action_bar`'s packed row at the off level."""
    if description_level == "off":
        return action_bar([e.label for e in entries], width=width)
    return menu_grid([("", entries)], width=width, height=height, description_level=description_level)


class ReviewAction(Enum):
    COMMIT = auto()
    EDIT_RECIPIENT = auto()
    EDIT_SUBJECT = auto()
    EDIT_BODY = auto()
    CANCEL = auto()


def _body_bytes(lines: list[str]) -> int:
    return len("\n".join(lines).encode("utf-8"))


async def _show_line_editor_help(session: Session, *, can_save_draft: bool) -> None:
    await session.write_line(colored("Line editor commands:", fg_color=HEADER_COLOR, bold=True))
    await session.write_line("  /done       finish editing and review the draft")
    await session.write_line("  /list       show all submitted lines")
    await session.write_line("  /insert N   insert a new line before line N")
    await session.write_line("  /edit N     replace line N")
    await session.write_line("  /delete N   delete line N")
    await session.write_line("  /cancel     discard the composition")
    if can_save_draft:
        # Dogfood feature request, issue #149: distinct from /cancel --
        # only offered when the caller passed a `draft_path` (persisted
        # posts, not e.g. mail, which has no resume mechanism to offer).
        await session.write_line("  /exit, /quit  save as a draft and leave -- resume it later")
    await session.write_line("  /help, /?   show these commands")
    await session.write_line("  //text      add a line beginning with /")


async def _show_lines(session: Session, lines: list[str]) -> None:
    if not lines:
        await session.write_line(colored("(body is empty)", fg_color=MUTED_COLOR))
        return
    width = max(1, session.terminal_width - 6)
    for number, line in enumerate(lines, start=1):
        safe = sanitize_text(line)
        wrapped = reflow(safe, width=width).splitlines() or [""]
        await session.write_line(f"{number:>3}: {wrapped[0]}")
        for continuation in wrapped[1:]:
            await session.write_line(f"     {continuation}")


def _parse_line_number(command: str, line_count: int, *, allow_end: bool = False) -> int | None:
    parts = command.split()
    if len(parts) != 2 or not parts[1].isdigit():
        return None
    number = int(parts[1])
    maximum = line_count + 1 if allow_end else line_count
    return number if 1 <= number <= maximum else None


async def edit_line_body(
    session: Session,
    *,
    initial_text: str | None,
    max_bytes: int,
    max_lines: int,
    draft_path: Path | None = None,
) -> str | None:
    """Edit a logical-line body without cursor-addressed terminal UI.

    Ordinary non-empty input appends one line; a blank line (or ``/done``)
    finishes into review. Blank paragraph lines remain expressible through
    ``/insert N``. Slash commands operate on the retained buffer; command
    follow-up prompts use ordinary ``read_line`` too, so behavior is identical
    on Telnet, SSH, and web sessions. ``None`` means either ``/cancel``
    (draft discarded) or ``/exit``/``/quit`` (draft saved) -- callers that
    need to tell the two apart check whether `draft_path` still exists.

    `draft_path` (dogfood feature request, issue #149), if given, is the
    same kind of caller-owned persistence target
    `netbbs.net.prose_editor.edit_prose` already uses for its own
    crash-recovery autosave -- see `netbbs.net.draft_storage`. A
    pre-existing draft there is offered for recovery on entry, same
    wording as the fullscreen editor; declining deletes it. `/cancel`
    always deletes it (nothing to keep). `/exit`/`/quit` are only
    recognized as commands at all when `draft_path` is given -- a
    caller with no resume mechanism to offer (e.g. mail composition)
    simply doesn't gain these two commands, same as before this
    parameter existed. Finishing normally (`/done`/blank line) deletes
    the draft too: the body is being handed back for real persistence,
    so the temporary autosave has nothing left to recover.
    """
    if draft_path is not None and draft_path.exists():
        if await offer_draft_recovery(session):
            initial_text = load_draft(draft_path)
        else:
            delete_draft(draft_path)
    lines = initial_text.split("\n") if initial_text is not None else []
    exit_hint = " /exit or /quit saves it as a draft;" if draft_path is not None else ""
    await session.write_line(
        f"Enter message text. Blank line or /done reviews the draft;{exit_hint} "
        "/help or /? shows editing commands."
    )
    if lines:
        await _show_lines(session, lines)

    async def apply(candidate: list[str]) -> bool:
        if len(candidate) > max_lines:
            await session.write_line(
                colored(f"Body cannot exceed {max_lines} logical lines.", fg_color=MUTED_COLOR)
            )
            return False
        size = _body_bytes(candidate)
        if size > max_bytes:
            await session.write_line(
                colored(f"Body cannot exceed {max_bytes} bytes (would be {size}).", fg_color=MUTED_COLOR)
            )
            return False
        lines[:] = candidate
        return True

    while True:
        await session.write(f"{len(lines) + 1}> ")
        raw = await session.read_line()
        command = raw.strip()
        lowered = command.lower()

        if raw == "" or lowered == "/done":
            body = "\n".join(lines)
            if not body.strip():
                await session.write_line(colored("Body cannot be blank.", fg_color=MUTED_COLOR))
                continue
            if draft_path is not None:
                delete_draft(draft_path)
            return body
        if lowered == "/cancel":
            if draft_path is not None:
                delete_draft(draft_path)
            return None
        if draft_path is not None and lowered in ("/exit", "/quit"):
            # No confirmation printed here on purpose -- the caller
            # (the only one who knows *where* this draft becomes
            # resumable, e.g. "next time you visit this board") owns
            # that message, the same way it already owns "Post
            # cancelled." Checking `draft_path.exists()` after a `None`
            # return is how a caller tells this apart from `/cancel`.
            save_draft(draft_path, "\n".join(lines))
            return None
        if lowered in ("/help", "/?"):
            await _show_line_editor_help(session, can_save_draft=draft_path is not None)
            continue
        if lowered == "/list":
            await _show_lines(session, lines)
            continue
        if lowered.startswith("/insert"):
            number = _parse_line_number(command, len(lines), allow_end=True)
            if number is None:
                await session.write_line(colored(f"Usage: /insert N (1-{len(lines) + 1})", fg_color=MUTED_COLOR))
                continue
            await session.write(f"New line {number}: ")
            text = await session.read_line()
            candidate = list(lines)
            candidate.insert(number - 1, text)
            await apply(candidate)
            continue
        if lowered.startswith("/edit"):
            number = _parse_line_number(command, len(lines))
            if number is None:
                await session.write_line(colored(f"Usage: /edit N (1-{len(lines)})", fg_color=MUTED_COLOR))
                continue
            await session.write_line(
                colored(
                    f"Current line {number}: {sanitize_text(lines[number - 1])}",
                    fg_color=MUTED_COLOR,
                )
            )
            await session.write(f"Replacement line {number}: ")
            text = await session.read_line()
            candidate = list(lines)
            candidate[number - 1] = text
            await apply(candidate)
            continue
        if lowered.startswith("/delete"):
            number = _parse_line_number(command, len(lines))
            if number is None:
                await session.write_line(colored(f"Usage: /delete N (1-{len(lines)})", fg_color=MUTED_COLOR))
                continue
            candidate = list(lines)
            deleted = candidate.pop(number - 1)
            if await apply(candidate):
                await session.write_line(
                    colored(f"Deleted line {number}: {sanitize_text(deleted)}", fg_color=MUTED_COLOR)
                )
            continue
        if raw.startswith("//"):
            raw = raw[1:]
        elif raw.startswith("/"):
            await session.write_line(
                colored(
                    "Unknown editor command. Type /help, or // to begin a text line with /.",
                    fg_color=MUTED_COLOR,
                )
            )
            continue

        await apply([*lines, raw])


def _preview_body(body: str, width: int) -> str:
    safe = sanitize_text(body, allow_newlines=True)
    return "\n".join(reflow(line, width=max(1, width)) if line else "" for line in safe.split("\n"))


def _review_field_line(
    hotkey: str, label: str, value: str, *, selected: str | None, bold_value: bool, accent: int
) -> str:
    """Dogfood feature request, issue #160's cursor-navigation follow-up
    (item 2 of the prioritized list): the same `>`-cursor/highlight
    convention `netbbs.net.resource_editor.edit_resource_draft` and
    `netbbs.net.admin_flow`'s own user-detail screen already render
    their fields with -- duplicated rather than imported, since this
    screen (like that one) is a bespoke dispatch loop, not a draft
    editor: `review_composition` is stateless and called fresh by each
    of its callers' own outer edit loops (mail/board/channel
    composition), unlike a draft this module owns end to end, so it has
    no `draft` dict of its own for a shared `FieldSpec` list to mutate.
    `label` already carries its own trailing punctuation (e.g. `"To: "`),
    matching this function's pre-existing labels exactly."""
    prefix = (
        colored(f"> {label}", fg_color=accent, bold=True)
        if selected == hotkey
        else colored(f"  {label}", fg_color=LABEL_COLOR)
    )
    return prefix + colored(value, fg_color=accent, bold=bold_value)


async def _read_review_key(session: Session) -> EditorKey:
    """`netbbs.net.resource_editor._read_navigable_key`'s own fallback
    shape, duplicated per this project's "duplicate rather than reach
    into another module's private helper" convention (see
    `netbbs.link.files._file_area_from_row`'s own docstring).

    `distinguish_ctrl_h=True` (dogfood feature request: this screen had
    no on-demand help at all until now) -- without it, real byte 0x08
    collapses into `BACKSPACE`, unreachable as help. This screen never
    needs a real Backspace at its own top level either."""
    read_editor_key = getattr(session, "read_editor_key", None)
    if read_editor_key is not None:
        try:
            return await read_editor_key(distinguish_ctrl_h=True)
        except NotImplementedError:
            pass
    raw = await session.read_key()
    return EditorKey(EditorKeyKind.CHAR, char=raw)


# Ctrl-H's own content for the arrow-selectable fields -- dogfood
# feature request, this screen had no on-demand help at all until now.
# Keyed the same as `field_order`'s own hotkeys, not a `FieldSpec` list,
# since this screen has no draft of its own (see `review_composition`'s
# own docstring for why it isn't an `edit_resource_draft` caller).
_REVIEW_HELP: dict[str, tuple[str, str]] = {
    "t": ("To", "The recipient this will be sent to."),
    "u": ("Subject", "A short one-line summary, shown wherever this ends up listed."),
    "b": (
        "Body",
        "The message text itself. Reopens whichever editor you're currently using (the "
        "simple line-by-line editor, or the fullscreen editor if you've turned it on in "
        "Your profile) with your draft intact.",
    ),
}


async def _show_review_help(
    session: Session, *, field_order: tuple[str, ...], selected: str | None,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
    unicode_style: bool = False,
) -> None:
    """Same "narrow to the highlighted field if one is selected, else
    list everything" shape `netbbs.net.resource_editor._show_field_help`
    already establishes for `edit_resource_draft`'s own Ctrl-H.
    `field_order` (the caller's own, already excluding "t" when there's
    no recipient) decides what "everything" means here -- this function
    has no independent opinion on which fields actually apply."""
    if selected is not None:
        label, help_text = _REVIEW_HELP[selected]
        await show_help(
            session, "Field help", [colored(label, fg_color=header_color, bold=True), f"  {help_text}"],
            header_color=header_color, unicode_style=unicode_style,
        )
        return
    lines: list[str] = []
    for key in field_order:
        label, help_text = _REVIEW_HELP[key]
        lines.append(colored(label, fg_color=header_color, bold=True))
        lines.append(f"  {help_text}")
        lines.append("")
    await show_help(session, "Field help", lines[:-1], header_color=header_color, unicode_style=unicode_style)


async def review_composition(
    session: Session,
    *,
    subject: str,
    body: str,
    recipient: str | None,
    commit_key: str,
    commit_label: str,
    commit_brief: str | None = None,
    description_level: str = "off",
    redraw_in_place: bool = False,
    unicode_style: bool = False,
    collapsed: bool = False,
    accent_color: int = ACCENT_COLOR,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
    truecolor: bool = False,
) -> ReviewAction:
    """Render a complete draft and return one explicit review action.

    `commit_brief` and `description_level` (issue #160's rollout to this
    screen) describe the caller-supplied commit action for `menu_grid`'s
    description text -- this module has no domain knowledge of its own
    (posting a board message, sending mail, etc.) to describe it with,
    unlike the other fixed T/U/B/C options below. `description_level`
    should be the caller's already-resolved `menu_description_level`
    preference, same caching rule as every other screen in this rollout.
    `redraw_in_place` (dogfood feature request, `netbbs.net.
    redraw_preference`) is the same shape -- the caller's already-
    resolved preference, not looked up here. `truecolor` is likewise
    already-resolved -- the caller's `netbbs.net.color_depth_preference.
    effective_truecolor(session, db, user)`, which honors that user's own
    `[C]olor depth` override rather than this module reading `session.
    supports_truecolor` directly and silently ignoring it.

    Dogfood feature request, issue #160's cursor-navigation follow-up
    (item 2 of the prioritized list): `[T]o`/`[U]pdate subject`/`[B]ody`
    are also reachable by moving a `>` cursor with Up/Down and
    activating the highlighted one with Space or Enter -- purely
    additive, every hotkey letter keeps working exactly as before. The
    commit action and `[C]ancel` are never arrow-selectable, the same
    "always hotkey-only" treatment `edit_resource_draft` gives Save/Back."""
    field_order = (("t",) if recipient is not None else ()) + ("u", "b")
    actions = {
        commit_key.lower(): ReviewAction.COMMIT,
        "u": ReviewAction.EDIT_SUBJECT,
        "b": ReviewAction.EDIT_BODY,
        "c": ReviewAction.CANCEL,
        # Issue #157: Ctrl-C as an incremental alias for [C]ancel.
        CANCEL_KEY: ReviewAction.CANCEL,
    }
    if recipient is not None:
        actions["t"] = ReviewAction.EDIT_RECIPIENT

    selected: str | None = None

    async def draw() -> None:
        heading = screen_title(
            "Review composition",
            breadcrumb=(session.node_display_name, "Compose"),
            subtitle="Check the draft before continuing",
            width=session.terminal_width,
            clear=redraw_in_place,
            unicode_style=unicode_style, collapsed=collapsed,
            header_color=header_color,
        node_name_gradient=session.node_name_gradient)
        await session.write_line(f"\r\n{heading}")
        if recipient is not None:
            await session.write_line(
                _review_field_line(
                    "t", "To: ", sanitize_text(recipient), selected=selected, bold_value=False, accent=accent_color
                )
            )
        await session.write_line(
            _review_field_line(
                "u", "Subject: ", sanitize_text(subject), selected=selected, bold_value=True, accent=accent_color
            )
        )
        body_prefix = (
            colored("> Body", fg_color=accent_color, bold=True)
            if selected == "b"
            else colored("  Body", fg_color=MUTED_COLOR, bold=True)
        )
        await session.write_line(body_prefix)
        rule_char = "─" if unicode_style else "-"
        divider_color = 238 if truecolor else MUTED_COLOR
        preview_rule = colored(rule_char * min(session.terminal_width, 78), fg_color=divider_color)
        await session.write_line(preview_rule)
        await session.write_line(_preview_body(body, session.terminal_width))
        await session.write_line(preview_rule)

        options = [MenuEntry(label=menu_key(commit_key.upper(), commit_label), brief=commit_brief)]
        if recipient is not None:
            options.append(MenuEntry(label=menu_key("T", "o"), brief="Change the recipient"))
        options.extend([
            MenuEntry(label=menu_key("U", "pdate subject"), brief="Change the subject"),
            MenuEntry(label=menu_key("B", "ody"), brief="Edit the body text"),
            MenuEntry(label=menu_key("C", "ancel"), brief="Discard this draft"),
        ])
        await session.write_line(
            f"\r\n{_menu_row(options, width=session.terminal_width, height=session.terminal_height, description_level=description_level)}"
        )
        await session.write_line(colored("(Ctrl-H for help on these fields)", fg_color=MUTED_COLOR))
        await session.write("Choice: ")

    await draw()
    while True:
        key = await _read_review_key(session)

        if key.kind == EditorKeyKind.UP:
            index = field_order.index(selected) if selected in field_order else 0
            selected = field_order[(index - 1) % len(field_order)]
            await draw()
            continue
        if key.kind == EditorKeyKind.DOWN:
            index = field_order.index(selected) if selected in field_order else -1
            selected = field_order[(index + 1) % len(field_order)]
            await draw()
            continue
        if key.kind == EditorKeyKind.ESCAPE:
            if selected is not None:
                selected = None
                await draw()
                continue
            await session.write("\a")
            continue
        if key.kind == EditorKeyKind.CTRL and key.char == "h":
            await _show_review_help(
                session, field_order=field_order, selected=selected, header_color=header_color,
                unicode_style=unicode_style,
            )
            await draw()
            continue
        if key.kind == EditorKeyKind.ENTER or (key.kind == EditorKeyKind.CHAR and key.char == " "):
            if selected is None:
                await session.write("\a")
                continue
            choice = selected
        elif key.kind == EditorKeyKind.CHAR and key.char is not None:
            choice = key.char.lower()
            if choice == HELP_KEY:
                # A session with no real `read_editor_key` (falls back
                # to plain `read_key()`) delivers Ctrl-H as an ordinary
                # character, never as `EditorKeyKind.CTRL` -- same dual
                # path `edit_resource_draft` itself handles.
                await _show_review_help(
                session, field_order=field_order, selected=selected, header_color=header_color,
                unicode_style=unicode_style,
            )
                await draw()
                continue
            if choice in field_order:
                selected = choice
        else:
            # Left/Right/Backspace/Tab/Home/End/Page Up/Page Down --
            # nothing on this screen defines a step, same silent no-op
            # `edit_resource_draft` gives Left/Right on a step-less field.
            continue

        action = actions.get(choice)
        if action is not None:
            await session.write_line("")
            return action
        await session.write(reject_unhandled_key(choice))

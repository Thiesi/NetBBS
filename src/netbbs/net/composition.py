"""Transport-independent line composition and pre-commit review.

The fullscreen prose editor owns a cursor-addressed screen model. This
module deliberately does not: it gives the default Telnet/SSH/web path a
caller-owned logical-line buffer with explicit operations, then provides the
shared review state used after either editor. Domain flows remain responsible
for validation and persistence; finishing an editor only returns a draft.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum, auto
from pathlib import Path

from netbbs.net.char_input import CANCEL_KEY, HELP_KEY, EditorKey, EditorKeyKind, InputCancelled, reject_unhandled_key
from netbbs.net.draft_storage import delete_draft, load_draft, offer_draft_recovery, save_draft
from netbbs.net.help_overlay import show_help
from netbbs.net.notices import take_notices, write_notices
from netbbs.net.session import Session, write_prompt
from netbbs.net.session_activity import records_activity
from netbbs.quoting import is_attribution
from netbbs.rendering.width import display_width
from netbbs.rendering.pipe_codes import PastedColor
from netbbs.rendering.post_body import post_body_rows
from netbbs.rendering.detail import Section, Styled, paginate, render_sections
from netbbs.rendering.reflow import wrap_terminal_text
from netbbs.rendering import (
    ACCENT_COLOR,
    ERROR_COLOR,
    HEADER_COLOR,
    LABEL_COLOR,
    MUTED_COLOR,
    RULE_COLOR,
    MenuEntry,
    action_bar,
    clear_screen,
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


async def show_compose_screen(
    session: Session,
    *,
    title: str,
    breadcrumb: Sequence[str],
    fields: Sequence[tuple[str, str]] = (),
    hint: str | None = None,
    redraw_in_place: bool = False,
    unicode_style: bool = False,
    collapsed: bool = False,
    header_color: int | tuple[int, int, int] = HEADER_COLOR,
    accent_color: int = ACCENT_COLOR,
) -> None:
    """The screen a composition's first prompts are asked on (issue #813):
    its own title -- "New message", "Reply", "New post" -- over the facts
    already settled (a reply's To) and a muted `hint` saying what to type.
    The To and Subject prompts used to appear under the menu they were
    chosen from, with nothing saying what was being written.

    `breadcrumb` is the path after the node's name. `fields` are plain
    `(label, value)` pairs, sanitized here. Pending outcomes are written
    last, directly above the prompt the caller asks next."""
    heading = screen_title(
        title,
        breadcrumb=(session.node_display_name, *breadcrumb),
        width=session.terminal_width,
        clear=redraw_in_place,
        unicode_style=unicode_style, collapsed=collapsed,
        header_color=header_color,
        node_name_gradient=session.node_name_gradient,
    )
    await session.write_line(f"\r\n{heading}")
    for label, value in fields:
        await session.write_line(
            colored(f"  {label}: ", fg_color=LABEL_COLOR) + colored(sanitize_text(value), fg_color=accent_color)
        )
    if hint:
        await session.write_line(colored(hint, fg_color=MUTED_COLOR))
    await write_notices(session)


async def read_prefilled_field(session: Session, label: str, current: str) -> str:
    """A required one-line field (a subject, a recipient) opened on its
    current value (design doc §3.5, issue #529's rule; applied here by issue
    #680): Enter saves what is shown, Esc leaves it unchanged. A field that
    may not be empty keeps its value when the line is emptied, rather than
    turning blank -- "keep" is Esc, not an empty answer."""
    prompt = f"{label}: "
    await write_prompt(session, prompt)
    # The editor echoes its initial buffer, and a subject carried over Link
    # can hold control sequences: it is shown sanitized, and handed back
    # untouched when the caller saves it without changing it.
    shown = sanitize_text(current)
    try:
        value = await session.read_line(
            initial=shown, cancellable=True,
            # The columns left after the label on this row: the viewport is
            # measured from the cursor, and a full-width one would wrap a
            # long subject and edit against the wrong row.
            viewport=lambda: max(1, session.terminal_width - display_width(prompt)),
        )
    except InputCancelled:
        await session.write_line("")
        return current
    value = value.strip()
    if not value or value == shown.strip():
        return current
    return value


def characters_over(text: str, max_bytes: int) -> int:
    """How many characters `text` has to lose from its end to fit
    `max_bytes` of UTF-8; 0 when it already fits (issue #812).

    Storage limits are counted in bytes, which mean nothing to a caller:
    200 bytes is 200 plain letters but as few as 100 accented ones. A
    refusal says how much to remove instead, in the unit the caller is
    typing in, and this is exact for the usual fix -- shortening the end."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return 0
    kept = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return len(text) - len(kept)


def too_long_message(what: str, over: int) -> str:
    """"<what> N characters too long" -- the one wording every
    composition refusal uses (issue #812), never a byte count."""
    return f"{what} {over} character{'' if over == 1 else 's'} too long"


async def read_subject(
    session: Session, *, max_bytes: int, current: str | None = None, blank_cancels: bool = False,
) -> str | None:
    """The Subject prompt of every composition (issue #812), which checks
    the subject where it is typed rather than after the body is written.

    With `current`, the field opens on it and behaves as
    `read_prefilled_field`: Enter keeps what is shown, an emptied line or
    Esc keeps `current`, and a string always comes back -- even an empty
    one, since a stored or carried post may have an empty subject (Codex
    review). With `current=None` it starts empty and Esc returns `None`,
    cancelling. An empty answer asks again, saying Esc cancels -- unless
    `blank_cancels`, for a prompt that already offers Enter as its way out
    (a board's "Subject (or press Enter to cancel)").

    A subject over `max_bytes` is refused with how many characters to
    remove, and the prompt reopens on it to be shortened (Ctrl-U clears
    it). The storage layer's byte check stays the backstop."""
    prefilled = current is not None
    if prefilled:
        prompt = "Subject: "
        escape_does = "keep the previous subject"
    elif blank_cancels:
        prompt = "Subject (or press Enter to cancel): "
        escape_does = "cancel"
    else:
        prompt = "Subject: "
        escape_does = "cancel"
    # Shown sanitized, and handed back untouched when saved unchanged --
    # the same reasoning as `read_prefilled_field`.
    shown = sanitize_text(current or "")
    seed = shown
    while True:
        await write_prompt(session, prompt)
        try:
            value = await session.read_line(
                initial=seed, cancellable=True,
                viewport=lambda: max(1, session.terminal_width - display_width(prompt)),
            )
        except InputCancelled:
            await session.write_line("")
            return current
        value = value.strip()
        if prefilled and (not value or value == shown.strip()):
            return current
        if not value:
            if blank_cancels:
                return None
            await session.write_line(
                colored(f"A subject is required -- type one, or press Esc to {escape_does}.", fg_color=ERROR_COLOR)
            )
            seed = ""
            continue
        over = characters_over(value, max_bytes)
        if over:
            await session.write_line(
                colored(
                    f"{too_long_message('That subject is', over)} -- shorten it, or press Esc to {escape_does}.",
                    fg_color=ERROR_COLOR,
                )
            )
            seed = value
            continue
        return value


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
    await session.write_line("  /done       finish editing and review the draft (so do two blank lines)")
    await session.write_line("  /list       show all lines")
    await session.write_line("  /insert N   write the next lines before line N")
    await session.write_line("  /end        write the next lines at the end again")
    await session.write_line("  /edit N     replace line N")
    await session.write_line("  /delete N   delete line N (/delete N-M deletes lines N to M)")
    await session.write_line("  /unquote    remove the quoted post (the \"> \" lines and who wrote them)")
    await session.write_line("  /cancel     discard the composition")
    if can_save_draft:
        # Dogfood feature request, issue #149: distinct from /cancel --
        # only offered when the caller passed a `draft_path`.
        await session.write_line("  /exit, /quit  save as a draft and leave -- resume it later")
    await session.write_line("  /help, /?   show these commands")
    await session.write_line("  //text      add a line beginning with /")
    await session.write_line("  A blank line starts a new paragraph.")
    await session.write_line(f"  {FULLSCREEN_EDITOR_HINT}")


# Where the other editor is (issue #837): the line editor is the default,
# and nothing in it said a cursor-addressed one exists.
FULLSCREEN_EDITOR_HINT = "Prefer arrow keys? Profile > [F]ullscreen editor switches to a fullscreen editor."


def _quoted_lines(lines: list[str]) -> set[int]:
    """The indexes of a reply's quote (issue #837): every "> " line, the
    "<author> wrote:" line directly above a run of them, and the blank line
    `quote_body` leaves under the run to write on. Removing them leaves only
    what the caller wrote, which is how a reply without the quote is made."""
    quoted = {index for index, line in enumerate(lines) if line.startswith(">")}
    for index in sorted(quoted):
        if index - 1 >= 0 and index - 1 not in quoted and is_attribution(lines[index - 1]):
            quoted.add(index - 1)
    for index in sorted(quoted):
        after = index + 1
        if after < len(lines) and after not in quoted and not lines[after].strip() and lines[index].startswith(">"):
            quoted.add(after)
    return quoted


async def _show_lines(session: Session, lines: list[str], *, point: int | None = None) -> None:
    """Every line, numbered; `point` (issue #814) marks where the next typed
    line goes when that is not the end."""
    if not lines:
        await session.write_line(colored("(body is empty)", fg_color=MUTED_COLOR))
        return
    width = max(1, session.terminal_width - 6)
    for number, line in enumerate(lines, start=1):
        if point is not None and number - 1 == point:
            await session.write_line(colored("     (new lines go here -- /end to write at the end)", fg_color=MUTED_COLOR))
        safe = sanitize_text(line)
        wrapped = reflow(safe, width=width).splitlines() or [""]
        await session.write_line(f"{number:>3}: {wrapped[0]}")
        for continuation in wrapped[1:]:
            await session.write_line(f"     {continuation}")


def _is_line_number(text: str) -> bool:
    """Digits `int()` accepts. `str.isdigit()` also takes "²" (AltGr+2 on a
    German keyboard), which `int()` rejects -- a crash that ended the
    session mid-composition (review on #902; as `picker._is_ascii_number`)."""
    return bool(text) and text.isascii() and text.isdigit()


def _parse_line_range(command: str, line_count: int) -> tuple[int, int] | None:
    """`/delete N` or `/delete N-M` (issue #837): trimming a 19-line quote
    took 17 commands, one per line."""
    parts = command.split()
    if len(parts) != 2:
        return None
    first, dash, last = parts[1].partition("-")
    if not _is_line_number(first) or (dash and not _is_line_number(last)):
        return None
    start, end = int(first), int(last) if dash else int(first)
    return (start, end) if 1 <= start <= end <= line_count else None


def _parse_line_number(command: str, line_count: int, *, allow_end: bool = False) -> int | None:
    parts = command.split()
    if len(parts) != 2 or not _is_line_number(parts[1]):
        return None
    number = int(parts[1])
    maximum = line_count + 1 if allow_end else line_count
    return number if 1 <= number <= maximum else None


@records_activity("Writing")
async def edit_line_body(
    session: Session,
    *,
    initial_text: str | None,
    max_bytes: int,
    max_lines: int,
    draft_path: Path | None = None,
    keep_pasted_color: bool = False,
    offer_recovery: bool = True,
    start_at: int | None = None,
) -> str | None:
    """Edit a logical-line body without cursor-addressed terminal UI.

    Ordinary input adds one line where the caller is writing: at the end,
    or before line N after ``/insert N`` until ``/end`` (issue #814). A
    blank line is a paragraph break; a second blank line in a row, or
    ``/done``, finishes into review, and that closing blank is not kept.
    Before #814 the first blank line finished, so a paragraph needed
    ``/insert N`` and so did every answer written between a reply's quoted
    lines, one line per command. Slash commands operate on the retained
    buffer; command follow-up prompts use ordinary ``read_line`` too, so
    behavior is identical on Telnet, SSH, and web sessions. ``None`` means
    either ``/cancel`` (draft discarded) or ``/exit``/``/quit`` (draft
    saved) -- callers that need to tell the two apart check whether
    `draft_path` still exists.

    `draft_path` (dogfood feature request, issue #149), if given, is the
    same kind of caller-owned persistence target
    `netbbs.net.prose_editor.edit_prose` already uses for its own
    crash-recovery autosave -- see `netbbs.net.draft_storage`. Every change
    to the text is written there (issue #814), so a dropped connection
    keeps what was typed. A pre-existing draft there is offered for
    recovery on entry, same wording as the fullscreen editor; declining
    deletes it. `offer_recovery` False leaves that decision to a caller
    that made it with its own Resume/Discard choice (a letter, issue
    #814): nothing is asked, `initial_text` is loaded, and the draft on
    disk stays until this session's first change replaces it. `/cancel`
    always deletes it (nothing to keep). `/exit`/`/quit` are only
    recognized as commands at all when `draft_path` is given -- a caller
    with no resume mechanism to offer simply doesn't gain these two
    commands. Finishing normally (`/done`/two blank lines) deletes the
    draft too: the body is being handed back for real persistence, so the
    temporary autosave has nothing left to recover.

    `keep_pasted_color` (issue #754) -- as for
    `netbbs.net.prose_editor.edit_prose`: pasted SGR color is typed in
    as pipe codes. One translator serves the whole body, so a color
    pasted on one line still counts on the next.

    `start_at` starts the caller writing before that line (0-based), as
    `/insert` would -- a forward's note goes above the letter it carries
    (issue #822). The listing marks the place, and `/end` leaves it.
    """
    if offer_recovery and draft_path is not None and draft_path.exists():
        if await offer_draft_recovery(session):
            initial_text = load_draft(draft_path)
        else:
            delete_draft(draft_path)
    lines = initial_text.split("\n") if initial_text is not None else []
    # Where the next typed line goes: the end, or before a line `/insert`
    # named (issue #814), or where the caller asked to start.
    point = len(lines) if start_at is None else max(0, min(start_at, len(lines)))
    # The line just typed was blank: another one finishes.
    blank_pending = False
    # ...and whether that blank went into the text (the cap may refuse it).
    blank_added = False
    paragraph_hint_shown = False
    # Passed only when asked for, so a Session that predates the option
    # still reads lines here.
    read_options = {"pasted_color": PastedColor()} if keep_pasted_color else {}
    exit_hint = " /exit or /quit saves it as a draft;" if draft_path is not None else ""
    await session.write_line(
        "Enter message text. A blank line starts a new paragraph; two blank lines or /done "
        f"review the draft;{exit_hint} /help or /? shows editing commands."
    )
    await session.write_line(colored(f"({FULLSCREEN_EDITOR_HINT})", fg_color=MUTED_COLOR))
    if lines:
        await _show_lines(session, lines, point=point if point < len(lines) else None)
        if _quoted_lines(lines):
            await session.write_line(
                colored("(/unquote removes the quote; /delete N-M removes some of its lines.)", fg_color=MUTED_COLOR)
            )

    async def apply(candidate: list[str]) -> bool:
        # A cap refuses growth past it, not every change to a body that is
        # already over it: a body written in the fullscreen editor (no line
        # cap) or carried over Link can arrive with more lines than this
        # editor allows, and must still be trimmable with /delete or /edit.
        if len(candidate) > max_lines and len(candidate) > len(lines):
            await session.write_line(
                colored(f"Body cannot exceed {max_lines} logical lines.", fg_color=MUTED_COLOR)
            )
            return False
        size = _body_bytes(candidate)
        if size > max_bytes and size > _body_bytes(lines):
            # In characters, not bytes (issue #812).
            over = characters_over("\n".join(candidate), max_bytes)
            await session.write_line(
                colored(f"{too_long_message('That would make the text', over)}.", fg_color=MUTED_COLOR)
            )
            return False
        lines[:] = candidate
        if draft_path is not None:
            # Kept as it is typed (issue #814): a dropped connection loses
            # nothing, as the fullscreen editor's autosave already did.
            save_draft(draft_path, "\n".join(lines))
        return True

    async def add_line(text: str) -> bool:
        nonlocal point
        candidate = list(lines)
        candidate.insert(point, text)
        if not await apply(candidate):
            return False
        point += 1
        return True

    while True:
        await session.write(f"{point + 1}> ")
        raw = await session.read_line(**read_options)
        command = raw.strip()
        lowered = command.lower()

        if raw == "" and not blank_pending and "\n".join(lines).strip():
            # A paragraph break; the next blank line finishes. At the line or
            # size cap the break is refused, said by `apply`, and the next
            # blank line still finishes (review on #873): the refusal is not
            # followed straight by review.
            blank_added = await add_line("")
            blank_pending = True
            if not paragraph_hint_shown:
                paragraph_hint_shown = True
                await session.write_line(
                    colored("(New paragraph. A second blank line, or /done, finishes.)", fg_color=MUTED_COLOR)
                )
            continue
        if raw == "" or lowered == "/done":
            if blank_pending and blank_added and (raw == "" or point == len(lines)):
                # The blank that asked to finish is not part of the text --
                # unless /done follows a blank typed mid-text, which is a
                # paragraph break the caller meant (review on #873).
                del lines[point - 1]
                point -= 1
                blank_pending = False
            body = "\n".join(lines)
            if not body.strip():
                await session.write_line(colored("Body cannot be blank.", fg_color=MUTED_COLOR))
                continue
            if draft_path is not None:
                delete_draft(draft_path)
            return body
        blank_pending = False
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
            await _show_lines(session, lines, point=point if point < len(lines) else None)
            continue
        if lowered == "/end":
            point = len(lines)
            continue
        if lowered.startswith("/insert"):
            number = _parse_line_number(command, len(lines), allow_end=True)
            if number is None:
                await session.write_line(colored(f"Usage: /insert N (1-{len(lines) + 1})", fg_color=MUTED_COLOR))
                continue
            # Stays there for the lines after it too (issue #814): answering
            # between a reply's quoted lines is one command per answer, not
            # one per line.
            point = number - 1
            if point < len(lines):
                await session.write_line(
                    colored(
                        f"Writing before line {number}: {sanitize_text(lines[point])} "
                        "-- /end goes back to the end.",
                        fg_color=MUTED_COLOR,
                    )
                )
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
            text = await session.read_line(**read_options)
            candidate = list(lines)
            candidate[number - 1] = text
            await apply(candidate)
            continue
        if lowered.startswith("/delete"):
            span = _parse_line_range(command, len(lines))
            if span is None:
                await session.write_line(
                    colored(f"Usage: /delete N or /delete N-M (1-{len(lines)})", fg_color=MUTED_COLOR)
                )
                continue
            start, end = span
            candidate = lines[:start - 1] + lines[end:]
            deleted = lines[start - 1:end]
            if await apply(candidate):
                point -= len(range(start - 1, min(end, point)))
                if start == end:
                    said = f"Deleted line {start}: {sanitize_text(deleted[0])}"
                else:
                    said = f"Deleted lines {start}-{end}."
                await session.write_line(colored(said, fg_color=MUTED_COLOR))
            continue
        if lowered == "/unquote":
            quoted = _quoted_lines(lines)
            if not quoted:
                await session.write_line(colored("There is no quote to remove.", fg_color=MUTED_COLOR))
                continue
            if await apply([line for index, line in enumerate(lines) if index not in quoted]):
                point -= sum(1 for index in quoted if index < point)
                count = len(quoted)
                await session.write_line(
                    colored(f"Removed the quote ({count} line{'' if count == 1 else 's'}).", fg_color=MUTED_COLOR)
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

        await add_line(raw)


# As on `show_detail`: a body squeezed below this many rows a page is no
# longer a page worth turning.
_MIN_PAGE_ROWS = 4


def _rows(text: str, width: int) -> list[str]:
    return wrap_terminal_text(text, width).split("\r\n")


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
    body_mode: str | None = None,
    body_layout: str = "prose",
    breadcrumb: Sequence[str] = ("Compose",),
    extra_rows: Sequence[str] = (),
    extra_actions: Sequence[tuple[str, str, str | None]] = (),
) -> ReviewAction | str:
    """Render a complete draft and return one explicit review action.

    `extra_rows` and `extra_actions` are a caller's own additions (issue
    #830: mail's attached files): rows, already styled, shown under the
    Subject on every page, and `(key, label, brief)` actions placed after
    `[B]ody`, the label already styled with `menu_key`. Pressing one returns
    its key, lower-cased, instead of a `ReviewAction`. Both count toward the
    page budget like the screen's own rows. Neither given, the screen is
    what it always was.

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

    `body_mode` (issue #711) previews a board post as its readers will see
    it -- `netbbs.rendering.post_body.post_body_mode`'s ``color``,
    ``plain`` or ``text``. Mail passes it too (issue #809). `None` keeps
    the plain preview. `body_layout` is the post's layout: ``art``
    keeping its lines, or ``lines`` for mail, wrapped at words.

    Dogfood feature request, issue #160's cursor-navigation follow-up
    (item 2 of the prioritized list): `[T]o`/`[U]pdate subject`/`[B]ody`
    are also reachable by moving a `>` cursor with Up/Down and
    activating the highlighted one with Space or Enter -- purely
    additive, every hotkey letter keeps working exactly as before. The
    commit action and `[C]ancel` are never arrow-selectable, the same
    "always hotkey-only" treatment `edit_resource_draft` gives Save/Back.

    The body is paged the way `netbbs.net.detail_view.show_detail` pages a
    message in the reader (issue #813), with the same pieces: the title,
    To and Subject stay on every page, and a body taller than the rows left
    over turns with `PgUp`/`PgDn` and `[N]ext`/`[P]rev page` -- `[>]`/`[<]`
    where the commit key already is `P` (a board's `[P]ost`). Printed whole,
    a long letter scrolled its own To and Subject off the screen before the
    menu appeared. `breadcrumb` is the path after the node's name."""
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
    extra_keys = {key.lower() for key, _label, _brief in extra_actions}

    selected: str | None = None
    width = max(1, session.terminal_width)
    next_key, prev_key = (">", "<") if {"n", "p"} & (set(actions) | extra_keys) else ("n", "p")
    if body_mode is None:
        body_rows = _preview_body(body, width).split("\n")
    else:
        body_rows = list(post_body_rows(body, width, body_mode, truecolor=truecolor, layout=body_layout))
    blocks = render_sections([Section(None, [Styled(body_rows)])], width=width, unicode_style=unicode_style)
    # An outcome carried in from the step before (a refused Send, a subject
    # too long) stays above the prompt until a page is turned, as on
    # `show_detail`; measured here, since it takes rows from the body.
    message_rows = [row for line in take_notices(session) for row in _rows(line, width)]
    rule_char = "─" if unicode_style else "-"
    divider_color = 238 if truecolor else RULE_COLOR
    preview_rule = colored(rule_char * min(width, 78), fg_color=divider_color)

    def _menu(paged: bool, packed: bool) -> list[str]:
        options = [MenuEntry(label=menu_key(commit_key.upper(), commit_label), brief=commit_brief)]
        if recipient is not None:
            options.append(MenuEntry(label=menu_key("T", "o"), brief="Change the recipient"))
        options.extend([
            MenuEntry(label=menu_key("U", "pdate subject"), brief="Change the subject"),
            MenuEntry(label=menu_key("B", "ody"), brief="Edit the body text"),
        ])
        options.extend(MenuEntry(label=label, brief=brief) for _key, label, brief in extra_actions)
        if paged:
            options.extend([
                MenuEntry(
                    label=menu_key(next_key.upper(), "ext page" if next_key == "n" else " Next page"),
                    brief="Show the next page of the body",
                ),
                MenuEntry(
                    label=menu_key(prev_key.upper(), "rev page" if prev_key == "p" else " Prev page"),
                    brief="Show the previous page of the body",
                ),
            ])
        options.append(MenuEntry(label=menu_key("C", "ancel"), brief="Discard this draft"))
        # A described menu takes the rows a long body needs: a body it does
        # not leave room for gets the packed bar instead, before any paging
        # (design doc §3.5's rule for a detail screen with a described menu).
        level = "off" if packed else description_level
        row = _menu_row(options, width=width, height=session.terminal_height, description_level=level)
        return _rows(row, width)

    def _head() -> list[str]:
        heading = screen_title(
            "Review composition",
            breadcrumb=(session.node_display_name, *breadcrumb),
            subtitle="Check the draft before continuing",
            width=width,
            clear=False,
            unicode_style=unicode_style, collapsed=collapsed,
            header_color=header_color,
            node_name_gradient=session.node_name_gradient,
        )
        rows = _rows(heading, width)
        if recipient is not None:
            rows.extend(_rows(
                _review_field_line(
                    "t", "To: ", sanitize_text(recipient), selected=selected, bold_value=False, accent=accent_color
                ),
                width,
            ))
        rows.extend(_rows(
            _review_field_line(
                "u", "Subject: ", sanitize_text(subject), selected=selected, bold_value=True, accent=accent_color
            ),
            width,
        ))
        for extra in extra_rows:
            rows.extend(_rows(extra, width))
        rows.append(
            colored("> Body", fg_color=accent_color, bold=True)
            if selected == "b"
            else colored("  Body", fg_color=MUTED_COLOR, bold=True)
        )
        return rows

    def _budget(paged: bool, packed: bool) -> int:
        # Lead-in, heading and fields, two rules, the blank row and menu,
        # the page line, the help hint, carried outcomes, the prompt.
        fixed = (
            (0 if redraw_in_place else 1) + len(_head()) + 2 + 1 + len(_menu(paged, packed))
            + (1 if paged else 0) + 1 + len(message_rows) + 1
        )
        return max(_MIN_PAGE_ROWS, session.terminal_height - fixed)

    # Each step is tried only when the one before does not fit, and the
    # screen is drawn with the layout its pages were cut for: the described
    # menu, then the packed bar, then the packed bar with pages (review on
    # #861: a body fitting only the packed bar was drawn under the
    # described menu, overflowing the terminal).
    paged = packed = False
    pages = paginate(blocks, budget=_budget(paged, packed)) or [[]]
    if len(pages) > 1 and description_level != "off":
        packed = True
        pages = paginate(blocks, budget=_budget(paged, packed))
    if len(pages) > 1:
        packed = paged = True
        pages = paginate(blocks, budget=_budget(paged, packed))
    page = 0

    async def draw() -> None:
        rows = [*_head(), preview_rule, *pages[page], preview_rule, "", *_menu(paged, packed)]
        if paged:
            rows.append(colored(f"(Page {page + 1} of {len(pages)} -- PgUp/PgDn to switch)", fg_color=MUTED_COLOR))
        rows.append(colored("(Ctrl-H for help on these fields)", fg_color=MUTED_COLOR))
        # A refused commit ("Could not create post: ...") returns here, and
        # this redraw would erase a line written before it (issue #680).
        rows.extend(message_rows)
        lead = clear_screen() if redraw_in_place else "\r\n"
        for index, row in enumerate(rows):
            await session.write_line((lead if index == 0 else "") + row)
        await session.write("Choice: ")

    await draw()
    while True:
        key = await _read_review_key(session)

        char = key.char.lower() if key.kind == EditorKeyKind.CHAR and key.char else ""
        if paged and (key.kind in (EditorKeyKind.PAGE_DOWN, EditorKeyKind.PAGE_UP) or char in (next_key, prev_key)):
            step = 1 if key.kind == EditorKeyKind.PAGE_DOWN or char == next_key else -1
            page = (page + step) % len(pages)
            # A one-off result belongs to the render that produced it.
            message_rows = []
            await draw()
            continue
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
        if choice in extra_keys:
            await session.write_line("")
            return choice
        await session.write(reject_unhandled_key(choice))

"""
The caller's side of the oneliner wall (issue #1391, design doc §3.3).

`add_oneliner_prompt` is `[A]dd a line`, offered beside `[Enter] Continue`
on the logon panel, on `[R]ecent callers` and on the full wall: one line
prompt, Esc or a blank answer cancels, and the outcome is carried into the
next redraw. `oneliner_wall_screen` is `[R]ecent callers ▸ [O]neliners`, the
whole wall, newest first, paged, with `[/] Find` and `[D]elete`.

The panel the logon and `[R]ecent callers` share is drawn by
`netbbs.net.profile_flow._render_previous_callers_panel`.
"""

from __future__ import annotations

from typing import Any, Callable

from netbbs.auth.users import User
from netbbs.digits import is_ascii_number
from netbbs.net.art_prompt import end_choice_line
from netbbs.net.breadcrumb_preference import breadcrumb_collapsed_enabled
from netbbs.net.char_input import (
    HELP_KEY,
    NEXT_PAGE_KEY,
    PREVIOUS_PAGE_KEY,
    InputCancelled,
    reject_unhandled_key,
)
from netbbs.net.confirm import prompt_yes_no
from netbbs.net.help_overlay import show_menu_help
from netbbs.net.location import crumbs, located
from netbbs.net.node_theme import effective_accent_color, effective_header_color
from netbbs.net.notices import announce, pending_notice_rows, write_notices
from netbbs.net.redraw_preference import redraw_in_place_enabled
from netbbs.net.session import Session, write_prompt
from netbbs.net.unicode_style_preference import unicode_style_enabled
from netbbs.oneliners import (
    MAX_ONELINER_COLUMNS,
    Oneliner,
    OnelinerError,
    add_oneliner,
    delete_oneliner,
    list_oneliners,
    may_delete_any_oneliner,
    may_delete_oneliner,
    may_write_oneliners,
    oneliner_refusal,
)
from netbbs.rendering import (
    METADATA_COLOR,
    MUTED_COLOR,
    VALUE_COLOR,
    action_bar,
    colored,
    cut_to_width,
    display_width,
    menu_key,
    sanitize_text,
    screen_title,
    wrap_to_width,
)
from netbbs.rendering.layout import MenuEntry
from netbbs.rendering.reflow import wrap_terminal_text
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.timeutil import format_for_display

#: The session attribute that says this call has added a line already.
_ADDED_THIS_CALL = "oneliner_added"
#: Rows the wall's own chrome needs besides its title and action bar: the
#: blank line before the bar, the `Choice:` row and one row of slack.
_WALL_FIXED_ROWS = 3
#: Numbers on a page run 01-99 (design doc §3.5).
_MAX_ROWS_PER_PAGE = 99
#: Below this many columns beside the number, name and time, a line's text
#: goes on rows of its own under them.
_MIN_INLINE_TEXT = 24


async def _run(lane: DatabaseLane | None, db: Database, func: Callable[..., Any], *args, **kwargs) -> Any:
    if lane is not None:
        return await lane.run(func, *args, **kwargs)
    return func(db, *args, **kwargs)


def added_a_line_this_call(session: Session) -> bool:
    return bool(getattr(session, _ADDED_THIS_CALL, False))


async def add_oneliner_prompt(
    session: Session, db: Database, user: User, *, lane: DatabaseLane | None = None
) -> bool:
    """`[A]dd a line`: refuse with the reason, or ask for the line and add
    it. Nothing is confirmed, since a line can be deleted again. The
    outcome is announced for the next redraw. True when a line was added."""
    refusal = await _run(lane, db, oneliner_refusal, user, added_this_call=added_a_line_this_call(session))
    if refusal is not None:
        announce(session, refusal, tone="error")
        return False
    # A hotkey followed by a line prompt: an Enter typed right behind the
    # "a" is not an answer (the same guard as mail's compose).
    discard_buffered_enter = getattr(session, "discard_buffered_enter", None)
    if discard_buffered_enter is not None:
        await discard_buffered_enter()
    prompt = f"Your line ({MAX_ONELINER_COLUMNS} max, Esc cancels): "
    await write_prompt(session, prompt)
    try:
        typed = await session.read_line(
            cancellable=True,
            # The columns left after the prompt on this row: the line
            # scrolls inside them rather than wrapping onto the next row.
            viewport=lambda: max(1, session.terminal_width - display_width(prompt)),
        )
    except InputCancelled:
        await session.write_line("")
        return False
    if not typed.strip():
        return False
    try:
        await _run(lane, db, add_oneliner, user, typed, added_this_call=added_a_line_this_call(session))
    except OnelinerError as error:
        announce(session, str(error), tone="error")
        return False
    setattr(session, _ADDED_THIS_CALL, True)
    announce(session, "Added to the wall.")
    return True


def _line_rows(
    lines: list[Oneliner], db: Database, *, width: int, accent_color
) -> list[list[str]]:
    """Each line's rendered rows, numbered from 01: the number, the
    author, the time and the text, wrapped rather than cut. The author
    and time columns are measured from these lines alone."""
    authors = [cut_to_width(sanitize_text(line.author_label), 12) for line in lines]
    times = [sanitize_text(format_for_display(line.created_at, db)).replace("\t", " ") for line in lines]
    author_width = max((display_width(a) for a in authors), default=0)
    time_width = max((display_width(t) for t in times), default=0)
    prefix_width = 4 + author_width + 2 + time_width + 2
    inline = width - prefix_width >= _MIN_INLINE_TEXT
    result = []
    for number, (line, author, when) in enumerate(zip(lines, authors, times), start=1):
        head = (
            colored(f" {number:02d} ", fg_color=METADATA_COLOR)
            + colored(author, fg_color=accent_color, bold=True)
            + " " * (author_width - display_width(author) + 2)
            + colored(when, fg_color=METADATA_COLOR)
        )
        text = sanitize_text(line.text)
        if inline:
            wrapped = wrap_to_width(text, width - prefix_width) or [""]
            pad = " " * (time_width - display_width(when) + 2)
            rows = [head + pad + colored(wrapped[0], fg_color=VALUE_COLOR)]
            rows += [" " * prefix_width + colored(part, fg_color=VALUE_COLOR) for part in wrapped[1:]]
        else:
            rows = [head]
            rows += [
                "    " + colored(part, fg_color=VALUE_COLOR)
                for part in wrap_to_width(text, max(1, width - 4))
            ]
        result.append(rows)
    return result


def _pages(lines: list[Oneliner], db: Database, *, width: int, rows: int, accent_color) -> list[list[Oneliner]]:
    """`lines` cut into pages of at most `rows` rendered rows each (and
    at most 99 lines), every page holding at least one line."""
    pages: list[list[Oneliner]] = []
    current: list[Oneliner] = []
    used = 0
    heights = [len(r) for r in _line_rows(lines, db, width=width, accent_color=accent_color)]
    for line, height in zip(lines, heights):
        if current and (used + height > rows or len(current) >= _MAX_ROWS_PER_PAGE):
            pages.append(current)
            current, used = [], 0
        current.append(line)
        used += height
    if current:
        pages.append(current)
    return pages


@located("Oneliners")
async def oneliner_wall_screen(
    session: Session, db: Database, user: User, *, lane: DatabaseLane | None = None
) -> None:
    """The whole wall, newest first (issue #1391): `<` `>` page, `[/] Find`
    matches a name or a line, `[A]dd a line` for a caller who meets the
    node's oneliner level, and `[D]elete` while the page shows a line of
    the viewer's own, or for a SysOp or "moderate everything" staff member,
    who may delete any line (audited). Deleting asks once, since it is
    irreversible. An empty wall says so and still offers `[A]dd a line`."""
    page_index = 0
    query = ""
    while True:
        all_lines = await _run(lane, db, list_oneliners)
        needle = query.lower()
        lines = [
            line for line in all_lines
            if not needle or needle in f"{line.author_label} {line.text}".lower()
        ]
        width = max(1, session.terminal_width)
        accent_color = effective_accent_color(session, db)
        unicode_style = unicode_style_enabled(db, user)
        header_color = effective_header_color(session, db)
        may_add = may_write_oneliners(db, user)
        moderator = may_delete_any_oneliner(user)

        def _bar(*, offers_delete: bool, has_prev: bool, has_next: bool) -> str:
            options = []
            if may_add:
                options.append(menu_key("A", "dd a line"))
            if offers_delete:
                options.append(menu_key("D", "elete"))
            if has_prev:
                options.append(menu_key("<", " Prev"))
            if has_next:
                options.append(menu_key(">", " Next"))
            options.append(menu_key("/", " Find"))
            options.append(menu_key("B", "ack"))
            return action_bar(options, width=width, height=session.terminal_height)

        def _title(subtitle: str) -> str:
            return screen_title(
                "Oneliners",
                breadcrumb=crumbs(session, own=True),
                subtitle=subtitle,
                width=width,
                clear=redraw_in_place_enabled(db, user),
                unicode_style=unicode_style,
                collapsed=breadcrumb_collapsed_enabled(db, user),
                header_color=header_color,
                node_name_gradient=session.node_name_gradient,
            )

        # The page budget: what the title, the widest bar this screen can
        # draw, the carried outcome and the prompt leave of the height.
        title_rows = wrap_terminal_text(_title("page 1/1"), width).count("\r\n") + 2
        bar_rows = wrap_terminal_text(
            _bar(offers_delete=True, has_prev=True, has_next=True), width
        ).count("\r\n") + 1
        budget = max(
            1,
            session.terminal_height - title_rows - bar_rows - pending_notice_rows(session) - _WALL_FIXED_ROWS,
        )
        pages = _pages(lines, db, width=width, rows=budget, accent_color=accent_color) or [[]]
        page_index = min(page_index, len(pages) - 1)
        page = pages[page_index]
        find_note = f", matching “{sanitize_text(query)}”" if query else ""
        subtitle = (
            f"page {page_index + 1}/{len(pages)}, {len(lines)} line{'s' if len(lines) != 1 else ''}{find_note}"
        )
        await session.write_line("\r\n" + _title(subtitle))
        if not page:
            await session.write_line(colored(
                f"No line matches “{sanitize_text(query)}”." if query else "Nobody has left a line yet.",
                fg_color=MUTED_COLOR,
            ))
        else:
            rendered = _line_rows(page, db, width=width, accent_color=accent_color)
            await session.write_line("\r\n".join(row for rows in rendered for row in rows))
        has_prev, has_next = page_index > 0, page_index < len(pages) - 1
        offers_delete = bool(page) and (moderator or any(line.author_user_id == user.id for line in page))
        await session.write_line(
            "\r\n" + _bar(offers_delete=offers_delete, has_prev=has_prev, has_next=has_next)
        )
        await write_notices(session)
        await write_prompt(session, "Choice: ")

        while True:
            action = (await session.read_key()).lower()
            if action == HELP_KEY:
                break
            if action == "b":
                await end_choice_line(session)
                return
            if action == "a" and may_add:
                break
            if action == "d" and offers_delete:
                break
            if action == PREVIOUS_PAGE_KEY and has_prev:
                break
            if action == NEXT_PAGE_KEY and has_next:
                break
            if action == "/":
                break
            await session.write(reject_unhandled_key(action))

        if action == HELP_KEY:
            entries = []
            if may_add:
                entries.append(MenuEntry(
                    label=menu_key("A", "dd a line"),
                    brief=f"Leave one line, up to {MAX_ONELINER_COLUMNS} columns",
                ))
            if offers_delete:
                entries.append(MenuEntry(
                    label=menu_key("D", "elete"),
                    brief="Remove any line on this page" if moderator else "Remove one of your own lines",
                ))
            entries.append(MenuEntry(label=menu_key("/", " Find"), brief="Show lines with a name or word"))
            await show_menu_help(
                session, "Oneliners help", entries,
                about=(
                    "Short lines callers left for the callers after them, newest first, each with the "
                    "account name that wrote it, even one hidden from Recent callers. You may add one line "
                    "per call and three a day; deleting one does not give it back."
                ),
                paged=True, header_color=header_color, unicode_style=unicode_style,
            )
            continue
        if action == PREVIOUS_PAGE_KEY:
            page_index -= 1
            continue
        if action == NEXT_PAGE_KEY:
            page_index += 1
            continue
        await end_choice_line(session)
        if action == "a":
            if await add_oneliner_prompt(session, db, user, lane=lane):
                # The new line is the newest: show the page it is on.
                page_index, query = 0, ""
            continue
        if action == "/":
            await write_prompt(session, "Find (blank shows every line): ")
            try:
                typed = (await session.read_line(cancellable=True)).strip()
            except InputCancelled:
                await session.write_line("")
                continue
            query, page_index = typed, 0
            continue

        # [D]elete: a row number on this page, then one confirmation.
        discard_buffered_enter = getattr(session, "discard_buffered_enter", None)
        if discard_buffered_enter is not None:
            await discard_buffered_enter()
        await write_prompt(session, f"Delete which line (01-{len(page):02d})? ")
        try:
            typed = (await session.read_line(cancellable=True)).strip()
        except InputCancelled:
            await session.write_line("")
            continue
        if not typed:
            continue
        if not is_ascii_number(typed) or not 1 <= int(typed) <= len(page):
            announce(session, f"There is no line {typed} on this page.", tone="error")
            continue
        target = page[int(typed) - 1]
        if not may_delete_oneliner(user, target):
            announce(session, "You can delete only your own lines.", tone="error")
            continue
        if not await prompt_yes_no(session, "Delete this line?", default=False):
            announce(session, "Nothing deleted.", tone="muted")
            continue
        try:
            await _run(lane, db, delete_oneliner, user, target.id)
        except OnelinerError as error:
            announce(session, str(error), tone="error")
            continue
        announce(session, "Line deleted.")

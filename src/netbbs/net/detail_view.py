"""A read-only detail screen that never scrolls its own top away.

`show_detail` draws a title, one page of a `netbbs.rendering.detail` panel, and
an action bar, then waits for a hotkey the calling screen owns. Paging is
handled here -- `PgUp`/`PgDn` always, plus `[N]ext`/`[P]rev` on the action bar
(or `[>]`/`[<]` where the screen already uses those letters) -- so a screen
only ever says *what* it shows, never how many rows of it fit.

It is also the answer to a quieter defect: a screen that printed a result and
returned had that result wiped by its parent menu's clear-and-redraw before it
could be read. A result shown through `show_detail` stays up until the SysOp
presses `[B]ack`.

Design doc §3.5: the panel is drawn in full before any key is read, `[B]ack`
leaves without writing anything, and nothing here ever asks a question.
"""

from __future__ import annotations

from collections.abc import Sequence

from netbbs.net.char_input import EditorKey, EditorKeyKind, reject_unhandled_key
from netbbs.net.session import Session, write_prompt
from netbbs.rendering import MUTED_COLOR, action_bar, clear_screen, colored, menu_key
from netbbs.rendering.detail import Section, paginate, render_sections
from netbbs.rendering.reflow import wrap_terminal_text

# Title, action bar and prompt are fixed furniture; a panel squeezed below this
# many rows a page is no longer a page worth turning.
_MIN_PAGE_ROWS = 4


def _rows(text: str, width: int) -> list[str]:
    return wrap_terminal_text(text, width).split("\r\n")


async def _read_key(session: Session) -> tuple[EditorKey, bool]:
    """Structured read so `PgUp`/`PgDn` arrive as keys, with the same plain
    `read_key` fallback `resource_editor._read_navigable_key` keeps for
    lightweight `Session` doubles. The flag says whether the key was echoed:
    `read_editor_key` never echoes, `read_key` does, and a rejected key is only
    erased if it was ever drawn."""
    read_editor_key = getattr(session, "read_editor_key", None)
    if read_editor_key is not None:
        try:
            return await read_editor_key(distinguish_ctrl_h=True), False
        except NotImplementedError:
            pass
    return EditorKey(EditorKeyKind.CHAR, char=await session.read_key()), True


async def show_detail(
    session: Session,
    *,
    title: str,
    sections: Sequence[Section],
    actions: Sequence[tuple[str, str]],
    redraw_in_place: bool,
    unicode_style: bool = False,
    page: int = 0,
    preamble: Sequence[str] = (),
    message: str | None = None,
) -> tuple[str, int]:
    """Show `sections` a page at a time and return `(hotkey, page)` once the
    SysOp presses one of `actions`' keys.

    `title` is a `screen_title(...)` block rendered with `clear=False`; this
    function owns the clear so that turning a page redraws in place too.
    `actions` is `(key, styled label)` pairs in display order. `preamble` rows
    (already styled) sit under the title on every page; `message` (already
    styled, `\r\n` between several) is the one-off outcome of whatever the
    SysOp just did, shown directly above the prompt until a page is turned.
    `page` lets a caller that redraws after an action come back to the page it
    left."""
    width = max(1, session.terminal_width)
    keys = {key.lower() for key, _label in actions}
    next_key, prev_key = ("n", "p") if not keys & {"n", "p"} else (">", "<")

    title_rows = _rows(title, width)
    preamble_rows = [row for line in preamble for row in _rows(line, width)]
    message_rows = _rows(message, width) if message else []
    blocks = render_sections(sections, width=width, unicode_style=unicode_style)

    def _bar(paged: bool) -> list[str]:
        labels = [label for _key, label in actions]
        if paged:
            paging = [
                menu_key(next_key.upper(), "ext page" if next_key == "n" else " Next page"),
                menu_key(prev_key.upper(), "rev page" if prev_key == "p" else " Prev page"),
            ]
            labels = [*labels[:-1], *paging, *labels[-1:]]
        return action_bar(labels, width=width).split("\r\n")

    def _budget(paged: bool) -> int:
        fixed = (
            (0 if redraw_in_place else 1) + len(title_rows) + len(preamble_rows) + 2
            + len(message_rows) + len(_bar(paged)) + (1 if paged else 0) + 1
        )
        return max(_MIN_PAGE_ROWS, session.terminal_height - fixed)

    pages = paginate(blocks, budget=_budget(False))
    if len(pages) > 1:
        pages = paginate(blocks, budget=_budget(True))
    paged = len(pages) > 1
    page = max(0, min(page, len(pages) - 1))

    while True:
        rows = [*title_rows, *preamble_rows, "", *pages[page], ""]
        rows.extend(_bar(paged))
        if paged:
            rows.append(colored(f"(Page {page + 1} of {len(pages)} -- PgUp/PgDn to switch)", fg_color=MUTED_COLOR))
        # Directly above the prompt, where the eye already is -- and where a
        # self-drawn console screen shows its own (`admin_flow._choice_prompt`).
        rows.extend(message_rows)
        lead = clear_screen() if redraw_in_place else "\r\n"
        for index, row in enumerate(rows):
            await session.write_line((lead if index == 0 else "") + row)
        await write_prompt(session, "Choice: ")

        while True:
            key, echoed = await _read_key(session)
            char = key.char.lower() if key.kind == EditorKeyKind.CHAR and key.char else ""
            if paged and (key.kind == EditorKeyKind.PAGE_DOWN or char == next_key):
                step = 1
            elif paged and (key.kind == EditorKeyKind.PAGE_UP or char == prev_key):
                step = -1
            elif char in keys:
                await session.write_line("")
                return char, page
            else:
                await session.write(reject_unhandled_key(key.char) if echoed and key.char else "\a")
                continue
            page = (page + step) % len(pages)
            # A one-off result belongs to the render that produced it.
            message_rows = []
            break

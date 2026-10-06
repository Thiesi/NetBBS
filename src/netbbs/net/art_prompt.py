"""A prompt drawn inside SysOp art, and leaving it (issue #1083).

Slot art (issue #929) can place the menu's or a list's `Choice:` prompt at
its `{prompt}` token, somewhere inside the art, while the notices and the
navigation lines sit below the art. The cursor then waits mid-screen. Every
answer to that prompt ends its line before saying anything more -- "Log
off? [y/N]", "Search:", "Which one" -- and that line started at the row
under the prompt: art, or a notice. "1 account awaiting approval: SysOp →
Users." became "Log off? [y/N]: ng approval: SysOp → Users.".

The screen that parks the prompt in its art marks the first free row below
everything it drew; `end_choice_line` ends the choice there instead.
"""

from __future__ import annotations

from netbbs.rendering.ansi import move_cursor

_ATTRIBUTE = "_netbbs_row_below_art_prompt"


def mark_prompt_in_art(session: object, free_row: int) -> None:
    """The prompt has just been moved into art; `free_row` (1-based) is the
    first row below everything the screen drew."""
    setattr(session, _ATTRIBUTE, free_row)


def clear_prompt_in_art(session: object) -> None:
    """The screen is being drawn again, or drawn without a prompt in art."""
    setattr(session, _ATTRIBUTE, None)


async def end_choice_line(session) -> None:
    """End the line a choice was typed on, as `write_line("")` does, from
    below the screen when the prompt was drawn inside art."""
    row = getattr(session, _ATTRIBUTE, None)
    if row is not None:
        clear_prompt_in_art(session)
        await session.write(move_cursor(row, 1))
        return
    await session.write_line("")

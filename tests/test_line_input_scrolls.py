"""A single-line prompt whose input outgrows the row (issue #964).

Deleting a door asks for its name after a long confirmation sentence.
The typed name wrapped onto a second row, and Backspace stopped at the
start of that row: a terminal's Backspace never moves up a line, so the
text before the wrap could not be corrected. Every `read_line` now
scrolls sideways within the columns left after its prompt -- the
window chat's input line got in #926 -- whether or not the caller asked
for one, and a masked field stops showing `*` at the edge.

These tests replay the written bytes onto `TerminalEmulator`, the same
model the SysOp's snoop view uses, because the claim is about what the
caller sees: **the input never leaves the prompt's row**.
"""

from __future__ import annotations

import asyncio

from netbbs.net.char_input import prompt_viewport, read_line
from netbbs.rendering.terminal_emulator import TerminalEmulator

_WIDTH = 80
_PROMPT = "Type the name 'The Scriptorium of Inks' to delete this door: "  # 62 columns
_NAME = "The Scriptorium of Inks and Other Very Long Door Names"  # 54 columns
_BACKSPACE = b"\x7f"
_HOME = b"\x1b[H"
_END = b"\x1b[F"
_LEFT = b"\x1b[D"


class _Terminal:
    """A `ByteSource` with a scripted keyboard and a screen: everything
    written lands on the emulator, which is also the session's screen
    copy, exactly as `Session.write` feeds it."""

    def __init__(self, script: bytes, *, width: int = _WIDTH, terminal_width: int | None = None):
        self._bytes = list(script)
        self.screen = TerminalEmulator(width, 24)
        self.terminal_width = terminal_width if terminal_width is not None else width

    def screen_copy(self) -> TerminalEmulator:
        return self.screen

    async def read_byte(self) -> int | None:
        return self._bytes.pop(0) if self._bytes else ord("\r")

    async def read_byte_with_timeout(self, timeout: float) -> int | None:
        return self._bytes.pop(0) if self._bytes else None

    async def write(self, text: str) -> None:
        self.screen.feed(text)


def _run(script: bytes, *, echo: bool = True, **kwargs) -> tuple[str, _Terminal]:
    terminal = _Terminal(script, **kwargs)
    terminal.screen.feed(_PROMPT)
    result = asyncio.run(read_line(terminal, terminal.write, echo=echo))
    return result, terminal


def test_a_long_answer_stays_on_the_prompts_row():
    result, terminal = _run(_NAME.encode())
    assert result == _NAME
    rows = terminal.screen.text_rows()
    assert rows[0].startswith(_PROMPT)
    # The Enter that ends the read moves to row 1; nothing was drawn there.
    assert rows[1].strip() == ""


def test_backspace_erases_the_whole_answer_even_past_the_row():
    result, terminal = _run(_NAME.encode() + _BACKSPACE * len(_NAME) + b"ok")
    assert result == "ok"
    assert terminal.screen.text_rows()[0].rstrip() == _PROMPT + "ok"


def test_editing_at_the_start_of_a_scrolled_answer_changes_the_value():
    result, terminal = _run(_NAME.encode() + _HOME + b"X" + _END + _LEFT + _BACKSPACE)
    assert result == "X" + _NAME[:-2] + _NAME[-1]
    assert terminal.screen.text_rows()[1].strip() == ""


def test_the_last_column_is_left_alone_on_a_terminal_that_wraps_at_once():
    # SyncTERM wraps as soon as its last column is written (#964,
    # finding 1): the layout width is one short of the physical one.
    result, terminal = _run(_NAME.encode(), terminal_width=_WIDTH - 1)
    assert result == _NAME
    rows = terminal.screen.text_rows()
    assert rows[0][_WIDTH - 1] == " "
    assert rows[0][: _WIDTH - 1].rstrip().endswith(_NAME[-2:])
    assert rows[1].strip() == ""


def test_a_masked_answer_stops_showing_stars_at_the_edge_and_backspace_reaches_them():
    password = "correct horse battery staple and then some more words"
    result, terminal = _run(password.encode(), echo=False)
    assert result == password
    rows = terminal.screen.text_rows()
    assert rows[0].startswith(_PROMPT + "*")
    assert rows[1].strip() == ""

    result, terminal = _run(password.encode() + _BACKSPACE * len(password) + b"pw", echo=False)
    assert result == "pw"
    assert terminal.screen.text_rows()[0].rstrip() == _PROMPT + "**"


def test_a_source_without_a_screen_copy_echoes_as_before():
    class _Plain:
        terminal_width = _WIDTH

    assert prompt_viewport(_Plain()) is None


def test_the_viewport_is_what_the_prompt_left():
    terminal = _Terminal(b"")
    terminal.screen.feed(_PROMPT)
    viewport = prompt_viewport(terminal)
    assert viewport is not None and viewport() == _WIDTH - len(_PROMPT)


def test_a_prompt_that_fills_the_row_gets_its_answer_on_a_fresh_row():
    # Review: the cursor sits on the prompt's last cell with a wrap
    # pending, so there is no room after it at all.
    prompt = "x" * _WIDTH
    terminal = _Terminal(b"abc")
    terminal.screen.feed(prompt)
    result = asyncio.run(read_line(terminal, terminal.write))
    assert result == "abc"
    rows = terminal.screen.text_rows()
    assert rows[0] == prompt
    assert rows[1].rstrip() == "abc"


def test_tab_listing_candidates_moves_the_line_to_its_own_row():
    # Review: Tab's candidate list reprints the line at column 0 of a
    # fresh row, so from then on the whole row is the room.
    def complete(word: str) -> list[str]:
        return ["Alpha" + "q" * 60, "Alpha" + "r" * 60]

    typed = b"Alpha\t\t" + b"z" * 70
    terminal = _Terminal(typed)
    terminal.screen.feed(_PROMPT)
    result = asyncio.run(read_line(terminal, terminal.write, completer=complete))
    assert result.startswith("Alpha") and result.endswith("z" * 70)
    rows = [row.rstrip() for row in terminal.screen.text_rows()]
    # Row 0 is the prompt, rows 1-2 the candidate list; the line is
    # reprinted on row 3 and fits the whole row there, so it shows in
    # full rather than scrolling within the prompt's old room.
    assert rows[3] == "Alpha" + "z" * 70
    assert rows[4] == ""


def test_a_reprint_that_wrapped_is_redrawn_from_its_first_row():
    # Re-review: when Tab's candidate list reprints a line that is wider
    # than the whole row and the cursor is mid-line, the reprint's `CSI D`
    # clamps on its last row. The window must count up from there, not
    # from where the logical cursor is.
    from netbbs.net.char_input import DeferredWindow, LineViewport

    terminal = _Terminal(b"")
    line = list("w" * 100)
    cursor = 10
    deferred = DeferredWindow(LineViewport(18), lambda: 18, lambda: _WIDTH)
    deferred.begin(line, cursor)

    async def replay() -> None:
        await deferred.write(terminal.write, "\r\nfirst  second\r\n")
        await deferred.write(terminal.write, "".join(line))
        await deferred.write(terminal.write, "\x1b[90D")
        await deferred.end(terminal.write, line, cursor)

    asyncio.run(replay())
    rows = [row.rstrip() for row in terminal.screen.text_rows()]
    assert rows[1] == "first  second"
    # One row, starting where the reprint started; the row below it is
    # cleared rather than left holding the reprint's wrapped tail.
    assert rows[2].strip().startswith("w") and len(rows[2]) <= _WIDTH
    assert rows[3] == ""

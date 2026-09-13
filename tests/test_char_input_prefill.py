"""A prompt that opens on the value you are editing (issue #529).

Dogfood report: "especially for longer strings like descriptions it's
annoying you have to type them in in entirety again". `read_line` grew
an `initial` buffer and, with it, a real answer for "leave this alone" —
because once the line starts populated, an empty submit stops being
something a caller reaches by accident and becomes a deliberate clear.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.net.char_input import InputCancelled, read_line
from tests.test_char_input import FakeByteSource, Writer

_LEFT = b"\x1b[D"
_BACKSPACE = b"\x7f"
_CRLF = b"\r\n"
_ESC = b"\x1b"


def _run(data: bytes, **kwargs) -> tuple[str, str]:
    async def scenario():
        source = FakeByteSource(data)
        writer = Writer()
        line = await read_line(source, writer, **kwargs)
        return line, writer.joined

    return asyncio.run(scenario())


# -- The buffer starts populated --------------------------------------


def test_enter_alone_returns_the_initial_value():
    line, _ = _run(_CRLF, initial="Weekly release builds")
    assert line == "Weekly release builds"


def test_the_initial_value_is_echoed_so_the_caller_can_see_what_they_are_editing():
    _, written = _run(_CRLF, initial="Weekly release builds")
    assert "Weekly release builds" in written


def test_typing_appends_to_the_initial_value():
    """The cursor starts at the end, which is where someone amending a
    description wants it."""
    line, _ = _run(b" 2026" + _CRLF, initial="Releases")
    assert line == "Releases 2026"


def test_backspace_edits_the_initial_value():
    line, _ = _run(_BACKSPACE * 3 + _CRLF, initial="Releases")
    assert line == "Relea"


def test_the_cursor_can_be_moved_back_into_the_initial_value():
    line, _ = _run(_LEFT * 8 + b"NetBBS " + _CRLF, initial="Releases")
    assert line == "NetBBS Releases"


def test_an_emptied_line_comes_back_empty():
    """The convention change this forces: with the buffer pre-filled, an
    empty result means the caller deliberately cleared it, not that they
    pressed Enter on an untouched prompt."""
    line, _ = _run(_BACKSPACE * 20 + _CRLF, initial="Releases")
    assert line == ""


def test_no_initial_value_behaves_exactly_as_before():
    line, written = _run(b"typed" + _CRLF)
    assert line == "typed"
    assert written.startswith("t")


# -- Escape cancels, but only for a caller that asked ------------------


def test_escape_cancels_when_the_caller_opted_in():
    with pytest.raises(InputCancelled):
        _run(_ESC, initial="Releases", cancellable=True)


def test_escape_cancels_even_after_editing():
    """Cancel means "forget all of it", not "keep what I typed"."""
    with pytest.raises(InputCancelled):
        _run(b" and more" + _ESC, initial="Releases", cancellable=True)


def test_escape_is_still_ignored_for_every_other_caller():
    """Every existing `read_line` call site is unchanged: a bare Escape
    falls through as it always did, changing nothing.

    Sent *after* the text, not before: an Escape immediately followed by
    a character is read as a possible Alt-combination and swallows it,
    which is long-standing behaviour this change does not touch.
    """
    line, _ = _run(b"abc" + _ESC + _CRLF)
    assert line == "abc"


def test_an_arrow_key_is_not_mistaken_for_a_cancel():
    """Arrows arrive as ESC-introduced sequences. Only a *bare* Escape
    cancels, or cursor navigation would abort the edit."""
    line, _ = _run(_LEFT + b"X" + _CRLF, initial="ab", cancellable=True)
    assert line == "aXb"

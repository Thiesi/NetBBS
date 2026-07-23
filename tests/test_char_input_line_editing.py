"""
Tests for cursor-addressable line editing in netbbs.net.char_input
(design doc §15 Phase 2) — Left/Right/Home/
End movement, mid-line Backspace/Delete, and Insert/overwrite toggling.
Command history is covered separately in
tests/test_char_input_history.py; the original (pre-cursor-editing)
append/Backspace-from-the-end/escape-discarding behavior is still
covered by tests/test_char_input.py and remains unchanged for masked
(echo=False) reads.
"""

from __future__ import annotations

import asyncio

from netbbs.net.char_input import read_line

from tests.test_char_input import FakeByteSource, Writer

_UP = b"\x1b[A"
_DOWN = b"\x1b[B"
_LEFT = b"\x1b[D"
_RIGHT = b"\x1b[C"
_HOME = b"\x1b[H"
_END = b"\x1b[F"
_HOME_TILDE = b"\x1b[1~"
_END_TILDE = b"\x1b[4~"
_DELETE = b"\x1b[3~"
_INSERT = b"\x1b[2~"
_CRLF = b"\r\n"


def _run(data: bytes, **kwargs) -> tuple[str, str]:
    async def scenario():
        source = FakeByteSource(data)
        writer = Writer()
        line = await read_line(source, writer, **kwargs)
        return line, writer.joined

    return asyncio.run(scenario())


# -- Left/Right/Home/End move the cursor without changing the buffer -------


def test_left_then_typing_inserts_before_the_last_character():
    line, _ = _run(b"abc" + _LEFT + b"X" + _CRLF)
    assert line == "abXc"


def test_left_twice_then_typing_inserts_further_back():
    line, _ = _run(b"abc" + _LEFT + _LEFT + b"X" + _CRLF)
    assert line == "aXbc"


def test_left_past_the_start_of_the_line_is_a_no_op():
    line, _ = _run(b"ab" + _LEFT * 5 + b"X" + _CRLF)
    assert line == "Xab"


def test_right_moves_forward_after_going_left():
    line, _ = _run(b"abc" + _LEFT + _LEFT + _RIGHT + b"X" + _CRLF)
    assert line == "abXc"


def test_right_past_the_end_of_the_line_is_a_no_op():
    line, _ = _run(b"ab" + _RIGHT * 5 + b"X" + _CRLF)
    assert line == "abX"


def test_home_then_typing_inserts_at_the_start():
    line, _ = _run(b"abc" + _HOME + b"X" + _CRLF)
    assert line == "Xabc"


def test_home_tilde_form_also_works():
    line, _ = _run(b"abc" + _HOME_TILDE + b"X" + _CRLF)
    assert line == "Xabc"


def test_end_after_home_returns_to_appending():
    line, _ = _run(b"abc" + _HOME + _END + b"X" + _CRLF)
    assert line == "abcX"


def test_end_tilde_form_also_works():
    line, _ = _run(b"abc" + _HOME + _END_TILDE + b"X" + _CRLF)
    assert line == "abcX"


# -- exact redraw byte sequences (a representative sample, not every case) --


def test_mid_line_insert_produces_the_expected_escape_sequence():
    # "abc", Left once (cursor -> 2, "ab|c"), type "X" -> "abXc".
    line, written = _run(b"abc" + _LEFT + b"X" + _CRLF)
    assert line == "abXc"
    # Left echoes a plain cursor-back-one; the insert then erases to EOL,
    # reprints the new tail ("Xc"), and repositions one column back
    # (the old tail "c" was 1 character).
    assert written == "abc" + "\x1b[1D" + "\x1b[K" + "Xc" + "\x1b[1D" + "\r\n"


def test_mid_line_backspace_produces_the_expected_escape_sequence():
    # "abc", Left once (cursor -> 2), Backspace removes "b" -> "ac".
    line, written = _run(b"abc" + _LEFT + b"\x08" + _CRLF)
    assert line == "ac"
    assert written == "abc" + "\x1b[1D" + "\x1b[1D" + "\x1b[K" + "c" + "\x1b[1D" + "\r\n"


def test_delete_forward_produces_the_expected_escape_sequence():
    # "abc", Home (cursor -> 0), Delete removes "a" -> "bc".
    line, written = _run(b"abc" + _HOME + _DELETE + _CRLF)
    assert line == "bc"
    assert written == "abc" + "\x1b[3D" + "\x1b[K" + "bc" + "\x1b[2D" + "\r\n"


def test_appending_at_the_end_writes_only_the_one_character():
    # The common case (typing normally) shouldn't trigger a full tail
    # reprint -- just the one character, no ESC[K, no cursor moves.
    line, written = _run(b"ab" + _CRLF)
    assert line == "ab"
    assert written == "ab" + "\r\n"


# -- Delete (forward) ---------------------------------------------------


def test_delete_at_end_of_line_is_a_no_op():
    line, _ = _run(b"abc" + _DELETE + _CRLF)
    assert line == "abc"


def test_delete_mid_line_removes_character_at_cursor_not_before_it():
    line, _ = _run(b"abc" + _LEFT + _LEFT + _DELETE + _CRLF)
    # cursor is at index 1 ("a|bc"); Delete removes "b", not "a".
    assert line == "ac"


# -- Insert / overwrite mode ---------------------------------------------


def test_insert_toggles_overwrite_mode():
    line, _ = _run(b"abc" + _HOME + _INSERT + b"X" + _CRLF)
    assert line == "Xbc"


def test_insert_twice_toggles_back_to_insert_mode():
    line, _ = _run(b"abc" + _HOME + _INSERT + _INSERT + b"X" + _CRLF)
    assert line == "Xabc"


def test_overwrite_at_end_of_line_still_appends():
    # Overwrite mode with nothing left to overwrite behaves like a
    # normal append, not a no-op.
    line, _ = _run(b"ab" + _INSERT + b"X" + _CRLF)
    assert line == "abX"


def test_overwrite_mode_does_not_change_line_length():
    line, _ = _run(b"abc" + _HOME + _INSERT + b"XY" + _CRLF)
    assert line == "XYc"
    assert len(line) == 3


# -- Backspace/Delete still work at the boundaries -----------------------


def test_backspace_at_start_of_line_is_a_no_op():
    line, _ = _run(b"abc" + _HOME + b"\x08" + _CRLF)
    assert line == "abc"


# -- masked (echo=False) reads are unaffected: no cursor movement, no ---
# -- recognition of the new escape sequences, matching the original -----
# -- append/Backspace-from-the-end-only behavior -------------------------


def test_masked_read_ignores_left_arrow_and_keeps_simple_behavior():
    line, written = _run(b"abc" + _LEFT + b"X" + _CRLF, echo=False)
    # If Left were honored here, "X" would land mid-line ("abXc"); the
    # masked path deliberately keeps append-only semantics instead.
    assert line == "abcX"
    assert written == "****" + "\r\n"


def test_masked_read_backspace_still_removes_from_the_end():
    line, _ = _run(b"secret" + b"\x08\x08" + _CRLF, echo=False)
    assert line == "secr"

"""
Tests for command history recall (Up/Down) in netbbs.net.char_input
(design doc §15 Phase 2, sign-off round 47/Track 5f) -- both the
library-level `InputHistory` class and its wiring into `read_line`.
Cursor-addressable editing itself is covered separately in
tests/test_char_input_line_editing.py.
"""

from __future__ import annotations

import asyncio

from netbbs.net.char_input import InputHistory, read_line

from tests.test_char_input import FakeByteSource, Writer

_UP = b"\x1b[A"
_DOWN = b"\x1b[B"
_CRLF = b"\r\n"


def _run(data: bytes, history: InputHistory | None) -> str:
    async def scenario():
        source = FakeByteSource(data)
        writer = Writer()
        return await read_line(source, writer, history=history)

    return asyncio.run(scenario())


# -- InputHistory in isolation --------------------------------------------


def test_record_then_entry_returns_the_most_recent_line():
    history = InputHistory()
    history.record("first")
    history.record("second")
    assert history.entry(1) == "second"
    assert history.entry(2) == "first"


def test_blank_lines_are_not_recorded():
    history = InputHistory()
    history.record("")
    assert len(history) == 0


def test_bounded_size_drops_the_oldest_entry():
    history = InputHistory(max_entries=2)
    history.record("first")
    history.record("second")
    history.record("third")
    assert len(history) == 2
    assert history.entry(1) == "third"
    assert history.entry(2) == "second"


# -- Up/Down recall wired into read_line -----------------------------------


def test_up_with_no_history_object_is_a_no_op():
    line = _run(b"ab" + _UP + b"c" + _CRLF, history=None)
    assert line == "abc"


def test_up_with_an_empty_history_is_a_no_op():
    history = InputHistory()
    line = _run(b"ab" + _UP + b"c" + _CRLF, history)
    assert line == "abc"


def test_up_recalls_the_most_recent_entry():
    history = InputHistory()
    history.record("/mute bob spamming")
    line = _run(_UP + _CRLF, history)
    assert line == "/mute bob spamming"


def test_up_twice_recalls_two_entries_back():
    history = InputHistory()
    history.record("first command")
    history.record("second command")
    line = _run(_UP + _UP + _CRLF, history)
    assert line == "first command"


def test_up_past_the_oldest_entry_stays_on_the_oldest():
    history = InputHistory()
    history.record("only command")
    line = _run(_UP + _UP + _UP + _CRLF, history)
    assert line == "only command"


def test_down_after_up_returns_to_the_more_recent_entry():
    history = InputHistory()
    history.record("first command")
    history.record("second command")
    line = _run(_UP + _UP + _DOWN + _CRLF, history)
    assert line == "second command"


def test_down_past_the_newest_recalled_entry_restores_the_in_progress_line():
    history = InputHistory()
    history.record("previous command")
    # Type "wip", recall history (replacing the buffer), then Down back
    # past it -- should restore "wip", not an empty line.
    line = _run(b"wip" + _UP + _DOWN + _CRLF, history)
    assert line == "wip"


def test_down_with_nothing_recalled_yet_is_a_no_op():
    history = InputHistory()
    history.record("previous command")
    line = _run(b"ab" + _DOWN + b"c" + _CRLF, history)
    assert line == "abc"


def test_recalled_entry_can_be_edited_before_submitting():
    history = InputHistory()
    history.record("/mute bob")
    # Recall it, then Backspace twice to trim " bob" down to "/mute bo",
    # confirming the recalled text is a real editable copy.
    line = _run(_UP + b"\x08\x08" + b"b" + _CRLF, history)
    assert line == "/mute bb"


def test_editing_a_recalled_entry_does_not_mutate_the_stored_history():
    history = InputHistory()
    history.record("/mute bob")
    _run(_UP + b"\x08\x08\x08\x08\x08\x08\x08\x08\x08" + _CRLF, history)
    assert history.entry(1) == "/mute bob"


def test_submitting_a_recalled_entry_records_it_again_as_the_newest():
    history = InputHistory()
    history.record("/mute bob")
    history.record("/whois alice")
    _run(_UP + _UP + _CRLF, history)  # recalls and resubmits "/mute bob"
    assert history.entry(1) == "/mute bob"


def test_history_persists_across_multiple_read_line_calls():
    # Simulates one connected session issuing several lines in a row --
    # the same InputHistory instance is reused (see
    # netbbs.net.login_flow.handle_session, constructed once per
    # session), not recreated per read_line call.
    history = InputHistory()
    _run(b"first line" + _CRLF, history)
    _run(b"second line" + _CRLF, history)
    line = _run(_UP + _UP + _CRLF, history)
    assert line == "first line"

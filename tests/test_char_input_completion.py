"""
Tests for Tab completion in netbbs.net.char_input (design doc round
49/Track 5g) -- the generic `apply_tab_completion` primitive and its
wiring into `read_line` via the `completer` parameter. The BBS-specific
completer built in `netbbs.net.chat_flow` is covered separately in
tests/test_chat_completion.py; this file only exercises the generic
word-replacement mechanics against small hand-written completers.
"""

from __future__ import annotations

import asyncio

from netbbs.net.char_input import read_line

from tests.test_char_input import FakeByteSource, Writer

_TAB = b"\t"
_CRLF = b"\r\n"


def _run(data: bytes, completer) -> tuple[str, str]:
    async def scenario():
        source = FakeByteSource(data)
        writer = Writer()
        line = await read_line(source, writer, completer=completer)
        return line, writer.joined

    return asyncio.run(scenario())


def _static(candidates: list[str]):
    """A completer stand-in that, like a real one, only matches against
    the last whitespace-delimited word of the text it's given -- not
    the whole line before the cursor (which would misfire on e.g.
    "/msg al", where only "al" should be matched, not the literal
    string "/msg al")."""

    def completer(text: str) -> list[str]:
        word = text.rsplit(" ", 1)[-1]
        return [c for c in candidates if c.lower().startswith(word.lower())]

    return completer


# -- no completer / no candidates -------------------------------------------


def test_tab_with_no_completer_is_a_no_op():
    line, _ = _run(b"ab" + _TAB + b"c" + _CRLF, completer=None)
    assert line == "abc"


def test_tab_with_zero_candidates_does_nothing():
    line, output = _run(b"zz" + _TAB + _CRLF, completer=_static(["alice", "bob"]))
    assert line == "zz"
    assert "\a" not in output


# -- single candidate ---------------------------------------------------


def test_single_candidate_replaces_the_current_word_with_a_trailing_space():
    line, _ = _run(b"al" + _TAB + _CRLF, completer=_static(["alice", "bob"]))
    assert line == "alice "


def test_single_candidate_completion_can_be_followed_by_more_typing():
    line, _ = _run(b"al" + _TAB + b"!" + _CRLF, completer=_static(["alice"]))
    assert line == "alice !"


def test_single_candidate_only_replaces_the_last_word():
    line, _ = _run(b"/msg al" + _TAB + _CRLF, completer=_static(["alice"]))
    assert line == "/msg alice "


# -- multiple candidates --------------------------------------------------


def test_multiple_candidates_extend_to_the_shared_prefix():
    # "alice" and "alicia" share the prefix "alic" -- typing "a" and
    # hitting Tab should extend the buffer that far, not complete
    # either name outright.
    line, output = _run(b"a" + _TAB + _CRLF, completer=_static(["alice", "alicia"]))
    assert line == "alic"
    assert "alice" in output
    assert "alicia" in output


def test_multiple_candidates_with_no_further_shared_prefix_just_lists_them():
    line, output = _run(b"a" + _TAB + _CRLF, completer=_static(["alice", "andy"]))
    assert line == "a"
    assert "alice" in output
    assert "andy" in output


def test_candidate_list_is_followed_by_the_in_progress_line_reprinted():
    _, output = _run(b"a" + _TAB + _CRLF, completer=_static(["alice", "andy"]))
    # The candidate list is printed, then the (possibly-extended)
    # in-progress line is echoed again so the user can keep typing.
    assert output.endswith("a") or "\r\na" in output


# -- repeated Tab presses: suppress a redundant identical reprint -----------

_BS = b"\x08"


def test_repeated_tab_with_nothing_typed_in_between_does_not_reprint():
    line, output = _run(b"a" + _TAB + _TAB + _CRLF, completer=_static(["alice", "andy"]))
    assert line == "a"
    assert output.count("alice") == 1
    assert output.count("andy") == 1


def test_a_third_candidate_list_is_also_suppressed_not_just_the_second():
    line, output = _run(b"a" + _TAB + _TAB + _TAB + _CRLF, completer=_static(["alice", "andy"]))
    assert line == "a"
    assert output.count("alice") == 1
    assert output.count("andy") == 1


def test_tab_reprints_after_any_other_keystroke_in_between():
    # Move the cursor and back (a keystroke that doesn't change `line`
    # at all) between two Tab presses -- still a real keystroke, so the
    # second Tab isn't treated as a no-progress repeat.
    left_then_right = b"\x1b[D\x1b[C"
    line, output = _run(
        b"a" + _TAB + left_then_right + _TAB + _CRLF, completer=_static(["alice", "andy"])
    )
    assert line == "a"
    assert output.count("alice") == 2
    assert output.count("andy") == 2


def test_tab_reprints_after_backspacing_the_word_away_and_retyping():
    # Thiesi's own scenario: backspace a fully-typed word back to
    # nothing, then press Tab again. The completion engine's own
    # common-prefix auto-extension reconstructs the identical "a" both
    # times (compare test_multiple_candidates_extend_to_the_shared_prefix),
    # so this specifically exercises that suppression is keyed on "did a
    # keystroke happen", not on whether the resulting word looks the same.
    line, output = _run(b"a" + _TAB + _BS + _TAB + _CRLF, completer=_static(["alice", "andy"]))
    assert line == "a"
    assert output.count("alice") == 2
    assert output.count("andy") == 2


def test_tab_reprints_once_the_word_is_narrowed_to_a_new_ambiguous_state():
    line, output = _run(
        b"a" + _TAB + b"l" + _TAB + _CRLF, completer=_static(["alice", "alicia", "andy"])
    )
    # First Tab (word "a"): all three share prefix "a" already typed,
    # so it lists all three, unextended. Typing "l" narrows to "al" --
    # a real edit -- and the second Tab extends the shared prefix to
    # "alic" and lists the two survivors.
    assert line == "alic"
    assert output.count("andy") == 1  # only the first, wider list
    assert output.count("alicia") == 2  # both lists


# -- mid-line completion (cursor not at the end) -----------------------------


def test_completion_operates_on_the_word_behind_the_cursor_not_the_whole_line():
    # Type "al bob", move left 4 to land right after "al" (before the
    # space) -- Tab there completes "al" to "alice " in place, without
    # touching " bob" sitting after the cursor.
    left = b"\x1b[D" * 4
    line, _ = _run(b"al bob" + left + _TAB + _CRLF, completer=_static(["alice"]))
    assert line == "alice  bob"

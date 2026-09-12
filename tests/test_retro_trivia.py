"""Retro Trivia's caller-facing contract (issue #514).

The door is loaded from its file path under a private module name rather than
through `netbbs.doors.bundled`, for the same reason `tests/voidrunner/support.py`
and `test_doors_runtime.py` do: this is the exact file NetBBS launches as a
standalone subprocess, not an ordinarily-imported library module.

Every test here drives the real screen functions and reads what they printed,
because the things being asserted -- that a key quits, that a chosen length is
honoured, that a box does not burst its terminal -- are only true of the output.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "src" / "netbbs" / "doors" / "bundled" / "retro_trivia.py"


def _load():
    spec = importlib.util.spec_from_file_location("retro_trivia_under_test", _PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


rt = _load()


@pytest.fixture
def keys(monkeypatch):
    """Feed `read_key` a scripted sequence, one key per call."""

    def script(*pressed: str):
        remaining = list(pressed)

        def read_key() -> str:
            if not remaining:
                raise EOFError("stdin closed")
            return remaining.pop(0)

        monkeypatch.setattr(rt, "read_key", read_key)

    return script


def _drawn(call) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        result = call()
    return result, _plain(buffer.getvalue())


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)


def _rows(text: str) -> list[str]:
    flat = text.replace("\r\n", "\n").replace("\r", "\n")
    return [row.rstrip() for row in flat.split("\n")]


# -- the bank ------------------------------------------------------------


def test_every_question_is_well_formed():
    for question, choices, answer in rt.QUESTIONS:
        assert question.strip(), question
        assert len(choices) == 4, question
        assert len(set(choices)) == 4, question
        assert 0 <= answer < 4, question


def test_no_question_is_asked_twice_in_the_bank():
    seen = [re.sub(r"[^a-z0-9]+", " ", q.lower()).strip() for q, _, _ in rt.QUESTIONS]
    assert len(seen) == len(set(seen))


def test_the_bank_is_deep_enough_for_the_longest_round():
    # A caller may ask for the longest offered round; `random.sample` cannot
    # draw more than the bank holds, so a bank shallower than this would
    # silently shorten the round the caller chose.
    assert len(rt.QUESTIONS) >= max(rt.ROUND_LENGTHS)


# -- quitting ------------------------------------------------------------


def test_quit_key_is_never_one_of_the_answer_letters():
    assert rt.QUIT_KEY not in rt.LETTERS


def test_quitting_a_question_returns_none_rather_than_an_answer(keys):
    keys(rt.QUIT_KEY)
    chosen, _ = _drawn(lambda: rt.ask_question(
        rt.Palette(truecolor=True), 1, 5, "Q?", ["a", "b", "c", "d"], width=78))
    assert chosen is None


def test_a_question_still_answers_normally(keys):
    keys("C")
    chosen, _ = _drawn(lambda: rt.ask_question(
        rt.Palette(truecolor=True), 1, 5, "Q?", ["a", "b", "c", "d"], width=78))
    assert chosen == 2


def test_the_quit_key_is_advertised_on_the_question(keys):
    keys("A")
    _, drawn = _drawn(lambda: rt.ask_question(
        rt.Palette(truecolor=True), 1, 5, "Q?", ["a", "b", "c", "d"], width=78))
    assert f"[{rt.QUIT_KEY}]" in drawn


def test_abandoning_reports_only_what_was_answered():
    _, drawn = _drawn(lambda: rt.draw_abandoned(rt.Palette(truecolor=True), 3, 5, width=78))
    assert "3" in drawn and "5" in drawn
    assert "abandoned" in drawn.lower()


# -- round length --------------------------------------------------------


def test_caller_chooses_the_round_length(keys):
    for index, expected in enumerate(rt.ROUND_LENGTHS, start=1):
        keys(str(index))
        chosen, _ = _drawn(lambda: rt.ask_round_length(rt.Palette(truecolor=True), width=78))
        assert chosen == expected


def test_quitting_the_length_picker_returns_none(keys):
    keys(rt.QUIT_KEY)
    chosen, _ = _drawn(lambda: rt.ask_round_length(rt.Palette(truecolor=True), width=78))
    assert chosen is None


def test_an_exhausted_input_falls_back_to_the_classic_round(keys):
    # A door whose caller has gone away should not raise out of a prompt.
    keys()
    chosen, _ = _drawn(lambda: rt.ask_round_length(rt.Palette(truecolor=True), width=78))
    assert chosen == rt.QUESTIONS_PER_ROUND


def test_the_masthead_does_not_name_a_length_before_one_is_chosen():
    _, drawn = _drawn(lambda: rt.draw_title(rt.Palette(truecolor=True), {}, width=78))
    assert "8 QUESTIONS" not in drawn
    _, chosen = _drawn(lambda: rt.draw_title(rt.Palette(truecolor=True), {}, width=78, questions=12))
    assert "12 QUESTIONS" in chosen


# -- fitting the terminal ------------------------------------------------


@pytest.fixture
def terminal(monkeypatch):
    """Draw at a real terminal width, as `main()` sets it from the drop file.

    Screens take a `width`, but `out_line` wraps against the module-level
    `_OUTPUT_WIDTH`. Leaving that at its 80-column default while passing
    `width=40` tests a combination no caller ever has.
    """

    def at(width: int) -> int:
        monkeypatch.setattr(rt, "_OUTPUT_WIDTH", width)
        return width

    return at


@pytest.mark.parametrize("width", [40, 64, 78])
def test_a_question_fits_its_terminal(keys, terminal, width):
    terminal(width)
    longest = max((c for _, choices, _ in rt.QUESTIONS for c in choices), key=len)
    question = max((q for q, _, _ in rt.QUESTIONS), key=len)
    keys("A")
    _, drawn = _drawn(lambda: rt.ask_question(
        rt.Palette(truecolor=True), 1, 20, question,
        [longest, "b", "c", "d"], width=width))
    for row in _rows(drawn):
        assert len(row) <= width, (width, len(row), row)


@pytest.mark.parametrize("width", [40, 64, 78])
def test_the_length_picker_fits_its_terminal(keys, terminal, width):
    terminal(width)
    keys("1")
    _, drawn = _drawn(lambda: rt.ask_round_length(rt.Palette(truecolor=True), width=width))
    for row in _rows(drawn):
        assert len(row) <= width, (width, len(row), row)


def test_a_long_choice_keeps_every_word(keys, terminal):
    # Truncating an answer can make a question unanswerable, so it is wrapped.
    terminal(40)
    choice = "A very long answer indeed which cannot possibly fit on one row here"
    keys("A")
    _, drawn = _drawn(lambda: rt.ask_question(
        rt.Palette(truecolor=True), 1, 5, "Q?", [choice, "b", "c", "d"], width=40))
    body = " ".join(row.strip("│ ") for row in _rows(drawn))
    assert all(word in body for word in choice.split())


def test_a_wrapped_choice_continues_under_its_own_marker(keys, terminal):
    # The point of wrapping here rather than leaving it to `_wrap_output`:
    # that fallback starts the continuation hard against the left border,
    # so the answer no longer reads as one block of text under its letter.
    terminal(40)
    choice = "Eight characters plus a three-character extension"
    keys("A")
    _, drawn = _drawn(lambda: rt.ask_question(
        rt.Palette(truecolor=True), 1, 5, "Q?", [choice, "b", "c", "d"], width=40))
    rows = [row for row in _rows(drawn) if row.startswith("│")]
    first = next(i for i, row in enumerate(rows) if "[A]" in row)
    # Where the answer's own text begins on its first row is the column its
    # continuation has to start in; deriving it beats hardcoding the marker.
    text_column = rows[first].index("Eight")
    continuation = rows[first + 1]
    assert continuation.strip("│ "), "the choice did not wrap at all"
    assert continuation.index("three-character") == text_column, continuation
    assert continuation[1:text_column].strip() == "", continuation


def test_the_question_header_keeps_its_box_on_one_row(keys, terminal):
    # Built as a single string, the header used to split its own border across
    # two rows at 40 columns: "╭── Question 1/8 ──── Progress" / "[...] 0% ──╮".
    terminal(40)
    keys("A")
    _, drawn = _drawn(lambda: rt.ask_question(
        rt.Palette(truecolor=True), 1, 8, "Q?", ["a", "b", "c", "d"], width=40))
    opening = [row for row in _rows(drawn) if row.startswith("╭")]
    assert len(opening) == 1, opening
    assert opening[0].endswith("╮"), opening[0]

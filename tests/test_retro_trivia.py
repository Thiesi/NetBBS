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
import os
import re
import select
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
    """Exact prompts only -- what this cannot catch is a paraphrase.

    Two entries asking the same fact in different words can still be drawn into
    one round, and three of those shipped in the first version of the expanded
    bank. Matching on the *answer* instead would be worse: "Zmodem" is correctly
    the answer to two unrelated questions already in the bank. So paraphrase is
    a review concern, not an assertable one; this only guards the literal case.
    """
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


def test_the_masthead_does_not_name_a_length_it_cannot_know():
    # The title is printed before the picker and the door never redraws it, so
    # a number in this chip would be a guess. An earlier revision took a
    # `questions` argument that nothing in `main()` ever passed, which made the
    # branch naming a length unreachable in play while a unit test called it
    # directly and looked green.
    _, drawn = _drawn(lambda: rt.draw_title(rt.Palette(truecolor=True), {}, width=78))
    assert "YOU CHOOSE" in drawn
    assert "QUESTIONS" not in drawn.replace("YOU CHOOSE", "")
    import inspect
    assert "questions" not in inspect.signature(rt.draw_title).parameters


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


# -- the launch splash ---------------------------------------------------

_CURSOR = re.compile(r"\x1b\[(\d+);(\d+)H|\x1b\[(\d+)C|\x1b\[[0-9;?]*[a-zA-Z]")


def _screen(frames: list[str]) -> dict[tuple[int, int], str]:
    """The glyph left in each cell once `frames` have all been drawn."""
    screen: dict[tuple[int, int], str] = {}
    row = col = 1
    for frame in frames:
        position = 0
        for match in _CURSOR.finditer(frame + "\x1b[0m"):
            for ch in frame[position:match.start()]:
                assert ch not in "\r\n", "a splash frame must not scroll the terminal"
                width = rt._char_width(ch)
                if width:
                    screen[(row, col)] = ch
                    _WRITES.append((row, col + width - 1))
                    col += width
            if match.group(1):
                row, col = int(match.group(1)), int(match.group(2))
            elif match.group(3):
                col += int(match.group(3))
            position = match.end()
    return screen


_WRITES: list[tuple[int, int]] = []


def _cells_written(frames: list[str]) -> list[tuple[int, int]]:
    """Every (row, col) a frame writes a glyph to, following its cursor moves."""
    _WRITES.clear()
    _screen(frames)
    return list(_WRITES)


def _screen_rows(frames: list[str]) -> list[str]:
    screen = _screen(frames)
    height = max(row for row, _ in screen)
    width = max(col for _, col in screen)
    return ["".join(screen.get((row, col), " ") for col in range(1, width + 1)) for row in range(1, height + 1)]


@pytest.mark.parametrize("width,height", [(40, 12), (60, 18), (80, 24), (132, 50)])
@pytest.mark.parametrize("truecolor", [True, False])
def test_every_splash_frame_stays_inside_its_terminal(width, height, truecolor):
    frames = rt.splash_frames(rt.Palette(truecolor=truecolor), {"handle": "keeper", "node_name": "Harbor Lights"},
                              width, height)
    assert frames and len(frames) == rt.SPLASH_FRAMES
    cells = _cells_written(frames)
    # The last column is never written: a glyph there leaves some terminals
    # waiting to wrap, and the next write scrolls the screen.
    assert all(1 <= row <= height and 1 <= col <= width - 1 for row, col in cells)
    assert sum(len(frame.encode()) for frame in frames) < 40_000 or width > 80


def test_the_splash_shows_the_stage_only_where_it_fits():
    p = rt.Palette(truecolor=True)
    full = "\n".join(_screen_rows(rt.splash_frames(p, {"handle": "keeper"}, 80, 24)))
    compact = "\n".join(_screen_rows(rt.splash_frames(p, {"handle": "keeper"}, 40, 12)))
    assert "TONIGHT'S CONTESTANT" in full and "keeper" in full and "Retro Computing Challenge" in full
    assert "keeper" in compact and "Retro Computing Challenge" not in compact
    assert rt.splash_frames(p, {}, 39, 24) is None
    assert rt.splash_frames(p, {}, 80, 11) is None


def test_a_hostile_handle_cannot_reach_the_terminal_through_the_splash():
    frames = rt.splash_frames(rt.Palette(truecolor=True),
                              {"handle": "\x1b[2Jx\x07" + "界" * 40, "node_name": "N\x1b]0;t\x07"}, 40, 12)
    drawn = "".join(frames)
    assert drawn.count("\x1b[2J") == 1  # the splash's own clear, nothing from the handle
    assert "\x07" not in drawn and "\x1b]" not in drawn
    assert all(col <= 39 for _, col in _cells_written(frames))


class _FakePoll:
    def __init__(self, *, typed_ahead=False, key_after=None):
        self.typed_ahead = typed_ahead
        self.key_after = key_after
        self.takes = 0

    def waiting(self, timeout):
        return self.typed_ahead

    def take(self, timeout):
        self.takes += 1
        return self.key_after is not None and self.takes > self.key_after


def test_a_key_ends_the_splash_at_once_and_hands_over_a_clear_screen():
    poll = _FakePoll(key_after=3)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert rt.play_splash(rt.Palette(truecolor=True), {"handle": "keeper"}, 80, 24, poll=poll)
    assert poll.takes == 4  # three frames ran out, the fourth was interrupted
    assert buffer.getvalue().endswith("\x1b[2J\x1b[H\x1b[?25h")


def test_typing_ahead_skips_the_splash_and_draws_nothing():
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        shown = rt.play_splash(rt.Palette(truecolor=True), {}, 80, 24, poll=_FakePoll(typed_ahead=True))
    assert not shown and buffer.getvalue() == ""


def test_without_a_live_input_stream_the_splash_is_not_drawn(monkeypatch):
    class NoFileno:
        def fileno(self):
            raise io.UnsupportedOperation("not a real stream")

    monkeypatch.setattr(rt.sys, "stdin", NoFileno())
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert not rt.play_splash(rt.Palette(truecolor=True), {}, 80, 24)
    assert buffer.getvalue() == ""


def _poll_modes():
    modes = []
    reader, writer = os.pipe()
    try:
        try:
            select.select([reader], [], [], 0)
            modes.append(True)
        except OSError:
            pass
        try:
            os.set_blocking(reader, False)
            os.set_blocking(reader, True)
            modes.append(False)
        except OSError:
            pass
    finally:
        os.close(reader)
        os.close(writer)
    return modes


@pytest.mark.parametrize("use_select", _poll_modes())
def test_the_key_that_skips_is_spent_whole(use_select):
    reader, writer = os.pipe()
    try:
        poll = rt._KeyPoll(reader, use_select)
        assert not poll.take(0.02)  # nothing typed yet
        os.write(writer, b"\x1b[A")  # an arrow key: three bytes, one key
        assert poll.take(0.2)
        assert not poll.waiting(0.05), "part of the key was left for the round-length picker"
    finally:
        os.close(reader)
        os.close(writer)
        rt._PUSHBACK.clear()


@pytest.mark.parametrize("use_select", _poll_modes())
def test_typing_ahead_is_kept_in_order(use_select):
    reader, writer = os.pipe()
    try:
        poll = rt._KeyPoll(reader, use_select)
        os.write(writer, b"2A")
        assert poll.waiting(0.2)
        kept = "".join(rt._PUSHBACK) + os.read(reader, 8).decode()
        assert kept == "2A"
    finally:
        os.close(reader)
        os.close(writer)
        rt._PUSHBACK.clear()

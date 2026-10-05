"""
War Dialer's launch splash: the dial-up, the masthead burning in, the ring.

The composer is pure, so its writes are checked against the terminal's
bounds at every size the door accepts. What matters as much as the picture
is when it is *not* drawn: under the presets that exist to drop motion, and
whenever a keystroke is already waiting or stdin cannot be polled -- the
splash must never eat or reorder input meant for the screens after it.
"""

from __future__ import annotations

import importlib.util
import io
import re
import sys
import unicodedata
from pathlib import Path

import pytest

_WAR_DIALER_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "netbbs" / "doors" / "bundled" / "war_dialer.py"
)
_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _load():
    spec = importlib.util.spec_from_file_location("war_dialer_splash_under_test", _WAR_DIALER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wd = _load()
INFO = {"handle": "alice", "node_name": "Harbor Lights"}


def _width(text: str) -> int:
    return sum(wd._char_width(ch) for ch in text)


def _palette(truecolor: bool = True):
    return wd.Palette(truecolor=truecolor)


@pytest.mark.parametrize("width,height", [(40, 12), (64, 20), (79, 23), (72, 24), (80, 24), (132, 50)])
@pytest.mark.parametrize("truecolor", [True, False])
def test_every_write_stays_inside_the_terminal(width, height, truecolor):
    frames = wd.splash_frames(_palette(truecolor), INFO, 3, width, height, seed=1)
    assert frames
    for ops in frames:
        for row, col, segments in ops:
            text = "".join(t for _, t in segments)
            assert "\r" not in text and "\n" not in text
            assert 1 <= row <= height, (row, text)
            assert col >= 1 and col + _width(text) - 1 <= width, (row, col, text)
            # The bottom-right cell would scroll some terminals; it is never written.
            assert not (row == height and col + _width(text) - 1 == width)


@pytest.mark.parametrize("width,height,kind", [(40, 12, "compact"), (80, 23, "compact"),
                                               (80, 24, "large"), (132, 50, "large")])
def test_the_composition_follows_the_room_it_has(width, height, kind):
    assert wd.splash_layout(width, height)[0] == kind


def test_a_long_wide_handle_and_node_name_are_fitted_not_wrapped():
    info = {"handle": "Ｗｉｄｅ" * 8, "node_name": "N\x1b[31mode " * 10}
    for width, height in ((40, 12), (80, 24)):
        for ops in wd.splash_frames(_palette(), info, 12, width, height, seed=2):
            for row, col, segments in ops:
                text = "".join(t for _, t in segments)
                assert "\x1b" not in text  # caller-derived text arrives plain
                assert col + _width(text) - 1 <= width


def test_it_animates_without_going_quiet_and_stays_small():
    frames = wd.splash_bytes(wd.splash_frames(_palette(), INFO, 3, 80, 24, seed=3))
    # About two and a half seconds, a write in every frame.
    assert 2.0 <= len(frames) * wd.SPLASH_FRAME_SECONDS <= 3.2
    assert all(frames[1:])
    assert sum(len(frame.encode()) for frame in frames) < 40_000


def _picture(frames, width, height) -> str:
    """The screen the frames leave behind, as plain rows."""
    grid = [[" "] * width for _ in range(height)]
    for ops in frames:
        for row, col, segments in ops:
            for offset, ch in enumerate("".join(t for _, t in segments)):
                grid[row - 1][col - 1 + offset] = ch
    return "\n".join("".join(row) for row in grid)


def test_the_finished_picture_carries_the_call_the_masthead_and_the_season():
    plain = _picture(wd.splash_frames(_palette(), INFO, 7, 80, 24, seed=4), 80, 24)
    for part in ("ATDT 555-0142", wd.SPLASH_CONNECT, "SEASON 7", "Harbor Lights", "alice", "█"):
        assert part in plain
    # Words other screens and their tests wait for never appear here.
    for marker in ("Press any key", "dial ", "W A R"):
        assert marker not in plain


class _Stdin(io.StringIO):
    def fileno(self):
        raise io.UnsupportedOperation("not pollable")


def test_nothing_is_drawn_when_stdin_cannot_be_polled(monkeypatch):
    written = []
    monkeypatch.setattr(wd, "out", written.append)
    monkeypatch.setattr(wd.sys, "stdin", _Stdin("q"))
    assert wd.play_splash(_palette(), INFO, 1, 80, 24) is False
    assert written == [] and wd._PENDING_INPUT == []


def test_nothing_is_drawn_when_input_is_already_waiting(monkeypatch):
    written = []
    monkeypatch.setattr(wd, "out", written.append)
    monkeypatch.setattr(wd.select, "select", lambda r, w, x, t: (r, [], []))
    monkeypatch.setattr(wd.sys, "stdin", io.StringIO("q"))
    assert wd.play_splash(_palette(), INFO, 1, 80, 24) is False
    assert written == []


@pytest.mark.parametrize("preset", ["fast", "monochrome", "ascii_art"])
def test_presets_without_motion_get_no_splash(monkeypatch, preset):
    written = []
    monkeypatch.setattr(wd, "out", written.append)
    monkeypatch.setattr(wd.select, "select", lambda r, w, x, t: ([], [], []))
    monkeypatch.setattr(wd.sys, "stdin", io.StringIO(""))
    p = _palette()
    setattr(p, preset, True)
    assert wd.play_splash(p, INFO, 1, 80, 24) is False
    assert written == []


def test_any_key_ends_it_at_once_and_is_consumed(monkeypatch):
    written = []
    monkeypatch.setattr(wd, "out", written.append)
    monkeypatch.setattr(wd.select, "select", lambda r, w, x, t: ([], [], []))
    monkeypatch.setattr(wd.sys, "stdin", io.StringIO(""))
    beats = []

    def beat(seconds, *, hand_back=True):
        beats.append(hand_back)
        return len(beats) == 3  # the caller presses a key during the third frame

    monkeypatch.setattr(wd, "_beat", beat)
    assert wd.play_splash(_palette(), INFO, 1, 80, 24) is True
    assert beats == [False, False, False]  # never handed back to the next screen
    assert written[0].startswith("\x1b[?25l\x1b[2J")
    assert written[-1].endswith("\x1b[2J\x1b[H\x1b[?25h")  # cleared, cursor restored
    assert len(written) == 1 + 3 + 1


def test_left_to_run_it_plays_every_frame_and_hands_over_cleared(monkeypatch):
    written = []
    monkeypatch.setattr(wd, "out", written.append)
    monkeypatch.setattr(wd.select, "select", lambda r, w, x, t: ([], [], []))
    monkeypatch.setattr(wd.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(wd, "_beat", lambda seconds, *, hand_back=True: False)
    assert wd.play_splash(_palette(), INFO, 2, 40, 12) is True
    frames = wd.splash_frames(_palette(), INFO, 2, 40, 12, seed=0)
    assert len(written) == len(frames) + 2
    assert written[-1].endswith("\x1b[?25h")


def test_the_large_masthead_spells_the_name():
    rows = wd._splash_word(wd._SPLASH_FONT, "WAR DIALER", 1, 3)
    assert len(rows) == 5 and len({len(row) for row in rows}) == 1
    assert len(rows[0]) + 1 <= 72  # the shadow included, inside the narrowest large layout
    small = wd._splash_word(wd._SPLASH_FONT_SMALL, "WAR DIALER", 1, 2)
    assert len(small[0]) <= 38
    for row in rows + small:
        assert all(unicodedata.east_asian_width(ch) != "W" for ch in row)

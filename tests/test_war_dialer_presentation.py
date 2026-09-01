"""Tests for the War Dialer door's presentation layer (netbbs.doors.
bundled.war_dialer) -- deliberately narrow, unlike test_war_dialer_
domain.py's broad domain-formula coverage. This door has no other
presentation-layer tests (matches Retro Trivia's own established
boundary: domain logic gets real regression coverage, the terminal-
driving `main()`/rendering glue doesn't) -- these exist specifically
because each pins a real bug Codex caught on PR #239 or its own PR #240
follow-up review, not a routine rendering check.

Loaded directly from its file path, same reasoning as test_war_dialer_
domain.py: this is the exact file NetBBS launches as a standalone
subprocess, not an ordinarily-imported library module.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

_WAR_DIALER_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "netbbs" / "doors" / "bundled" / "war_dialer.py"
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _load_war_dialer():
    spec = importlib.util.spec_from_file_location("war_dialer_presentation_under_test", _WAR_DIALER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wd = _load_war_dialer()


def test_draw_help_never_overflows_its_own_declared_width():
    # Codex review (PR #239): draw_help's body text used to be hand-
    # wrapped assuming a fixed ~78-column terminal, overflowing into
    # extra rows exactly at the narrow widths main() explicitly
    # supports (down to 40 columns) -- defeating the "one page" intent
    # right where it matters most. Checked across the full supported
    # range, not just one width, since the fix (_wrap) is a general
    # mechanism, not a special case for any one column count.
    for width in (40, 60, 78):
        written: list[str] = []
        original_out = wd.out
        try:
            wd.out = written.append
            wd.draw_help(wd.Palette(truecolor=False), width)
        finally:
            wd.out = original_out
        text = _ANSI_RE.sub("", "".join(written))
        for line in text.split("\r\n"):
            assert len(line) <= width, f"line exceeds width={width}: {line!r}"


def test_press_any_key_consumes_a_full_arrow_key_sequence():
    # Codex review (PR #239), a real bug: press_any_key() used to
    # consume only the leading ESC byte of a multi-byte arrow-key
    # sequence (ESC [ <letter>), leaving the rest in the input buffer
    # for the *next* read. read_menu_choice() silently ignored the
    # stray '[' but accepted the trailing letter as a real hotkey --
    # right-arrow's trailing 'C' silently spent cash and a turn on
    # Crew Recruit, an action the caller never chose to take. Confirms
    # the fix by scripting a right-arrow press (\x1b[C) at
    # press_any_key()'s own prompt and asserting nothing extra is
    # readable afterward.
    inputs = iter([wd.ESC, "[", "C", "b"])  # right-arrow, then a real "back-ish" byte

    class _FakeSession:
        def __init__(self):
            self.calls = 0

        def read(self):
            self.calls += 1
            return next(inputs)

    session = _FakeSession()
    original_read_key = wd.read_key
    original_has_pending = wd._stdin_has_pending_byte
    try:
        wd.read_key = session.read
        # A real CSI sequence's bytes are already sitting in the input
        # buffer by the time press_any_key() checks (PR #240 fix below)
        # -- true here since this FakeSession's whole sequence is
        # scripted up front, not arriving byte-by-byte from a real
        # socket.
        wd._stdin_has_pending_byte = lambda timeout: True
        written: list[str] = []
        original_out = wd.out
        try:
            wd.out = written.append
            wd.press_any_key(wd.Palette(truecolor=False))
        finally:
            wd.out = original_out
    finally:
        wd.read_key = original_read_key
        wd._stdin_has_pending_byte = original_has_pending

    # The whole 3-byte sequence (ESC, '[', 'C') must be consumed by
    # press_any_key() itself -- exactly 3 read_key() calls, leaving the
    # 4th scripted byte ('b') untouched for whatever reads next, not
    # already silently consumed as if it were a menu choice.
    assert session.calls == 3


def test_press_any_key_does_not_block_on_a_standalone_escape():
    # Codex review (PR #240), a real bug in the PR #239 fix above: a
    # standalone Escape press is an ordinary way to dismiss "Press any
    # key to continue..." -- but the unconditional second read_key()
    # call that fix added blocked waiting for a byte that was never
    # coming, then silently consumed whatever the caller typed *next*
    # (their real following menu choice) as if it might be the '[' of a
    # CSI sequence. Confirms the fix: when no further byte is available
    # (_stdin_has_pending_byte stubbed False, matching a real standalone
    # Escape with nothing queued behind it), press_any_key() must return
    # after exactly one read_key() call, never attempting a second.
    inputs = iter([wd.ESC])

    class _FakeSession:
        def __init__(self):
            self.calls = 0

        def read(self):
            self.calls += 1
            return next(inputs)

    session = _FakeSession()
    original_read_key = wd.read_key
    original_has_pending = wd._stdin_has_pending_byte
    try:
        wd.read_key = session.read
        wd._stdin_has_pending_byte = lambda timeout: False
        written: list[str] = []
        original_out = wd.out
        try:
            wd.out = written.append
            wd.press_any_key(wd.Palette(truecolor=False))
        finally:
            wd.out = original_out
    finally:
        wd.read_key = original_read_key
        wd._stdin_has_pending_byte = original_has_pending

    assert session.calls == 1

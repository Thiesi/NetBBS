"""
Tests for netbbs.net.char_input — the transport-agnostic character-mode
line/key reading extracted from netbbs.net.telnet once netbbs.net.ssh
needed identical logic against a completely different byte source.

Exercised here against a minimal fake ByteSource rather than a real
socket, unlike tests/test_telnet.py (which still covers this exact same
logic end-to-end over a real loopback connection via TelnetSession,
proving the extraction didn't change real-world behavior). These tests
exist to pin down the shared logic in isolation, independent of either
transport.
"""

from __future__ import annotations

import asyncio

import pytest

import netbbs.net.char_input as char_input_module
from netbbs.net.char_input import (
    CANCEL_KEY,
    HELP_KEY,
    REDRAW_KEY,
    REFRESH_KEY,
    EditorKeyKind,
    discard_buffered_enter,
    discard_buffered_input,
    read_any_key,
    read_editor_key,
    read_key,
    read_line,
    reject_unhandled_key,
)
from netbbs.net.session import SessionClosedError
from netbbs.rendering.ansi import reject_keystroke


class FakeByteSource:
    """Feeds a fixed sequence of bytes one at a time; raises
    SessionClosedError once exhausted, matching a real transport's
    behavior when the connection closes mid-read."""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    async def read_byte(self) -> int | None:
        if self._pos >= len(self._data):
            raise SessionClosedError("no more data")
        b = self._data[self._pos]
        self._pos += 1
        return b

    async def read_byte_with_timeout(self, timeout: float) -> int | None:
        if self._pos >= len(self._data):
            return None
        b = self._data[self._pos]
        self._pos += 1
        return b


class Writer:
    """Collects everything written via the write callback, for
    assertions on echo output."""

    def __init__(self):
        self.written: list[str] = []

    async def __call__(self, text: str) -> None:
        self.written.append(text)

    @property
    def joined(self) -> str:
        return "".join(self.written)


def test_read_line_returns_typed_text():
    async def scenario():
        source = FakeByteSource(b"hello\r\n")
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "hello"

    asyncio.run(scenario())


def test_read_line_echoes_each_character():
    async def scenario():
        source = FakeByteSource(b"hi\r\n")
        writer = Writer()
        await read_line(source, writer)
        assert writer.joined == "hi\r\n"

    asyncio.run(scenario())


def test_read_line_echo_false_masks_with_asterisk():
    async def scenario():
        source = FakeByteSource(b"secret\r\n")
        writer = Writer()
        line = await read_line(source, writer, echo=False)
        assert line == "secret"
        assert writer.joined == "******\r\n"

    asyncio.run(scenario())


def test_read_line_bare_cr_terminates():
    async def scenario():
        source = FakeByteSource(b"abc\r")
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "abc"

    asyncio.run(scenario())


def test_read_line_backspace_removes_last_character():
    async def scenario():
        source = FakeByteSource(b"abc\x08\r\n")  # "abc" + Backspace
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "ab"

    asyncio.run(scenario())


def test_read_line_backspace_on_empty_line_does_nothing():
    async def scenario():
        source = FakeByteSource(b"\x08a\r\n")
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "a"

    asyncio.run(scenario())


def test_read_line_delete_byte_also_works_as_backspace():
    async def scenario():
        source = FakeByteSource(b"abc\x7f\r\n")
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "ab"

    asyncio.run(scenario())


def test_read_line_decodes_two_byte_utf8():
    async def scenario():
        source = FakeByteSource("Müller".encode("utf-8") + b"\r\n")
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "Müller"

    asyncio.run(scenario())


def test_read_line_recovers_byte_after_malformed_utf8_lead_byte():
    async def scenario():
        # 0xD6 is a valid two-byte UTF-8 lead byte, but here it's followed
        # by an ordinary ASCII byte instead of a continuation byte -- e.g.
        # a client sending a Latin-1/CP1252 extended character as a single
        # byte instead of real UTF-8. The malformed sequence must be
        # discarded without losing the ASCII byte that broke it.
        source = FakeByteSource(bytes([0xD6, ord("A")]) + b"\r\n")
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "A"

    asyncio.run(scenario())


def test_read_line_recovers_repeated_malformed_utf8_lead_bytes():
    async def scenario():
        # Regression for the reported bug: repeatedly typing an umlaut
        # whose bytes aren't valid UTF-8 continuations must not desync the
        # rest of the line -- each malformed lead byte drops only itself.
        source = FakeByteSource(bytes([0xD6, 0xD6, 0xD6]) + b"ok\r\n")
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "ok"

    asyncio.run(scenario())


def test_read_line_discards_csi_escape_sequence():
    async def scenario():
        # ESC [ A (an up-arrow CSI sequence) shouldn't corrupt the line.
        source = FakeByteSource(b"ab\x1b[Ac\r\n")
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "abc"

    asyncio.run(scenario())


def test_read_line_discards_ss3_escape_sequence():
    async def scenario():
        source = FakeByteSource(b"ab\x1bOPc\r\n")
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "abc"

    asyncio.run(scenario())


def test_read_line_none_from_source_is_skipped():
    """A ByteSource returning None mid-stream (a transport-level action
    with no data, e.g. Telnet negotiation or an SSH resize notification)
    shouldn't appear in the line or need special handling by the reader
    -- both transports already resolve this internally per-byte."""

    _CR = 0x0D
    _LF = 0x0A

    class SourceWithNones:
        def __init__(self):
            self._bytes = iter([ord("a"), None, ord("b"), _CR, _LF])

        async def read_byte(self):
            return next(self._bytes)

        async def read_byte_with_timeout(self, timeout):
            return None

    async def scenario():
        writer = Writer()
        line = await read_line(SourceWithNones(), writer)
        assert line == "ab"

    asyncio.run(scenario())


def test_read_key_returns_immediately_no_enter_needed():
    async def scenario():
        source = FakeByteSource(b"q")
        writer = Writer()
        key = await read_key(source, writer)
        assert key == "q"

    asyncio.run(scenario())


def test_read_key_skips_control_bytes_and_returns_next_real_key():
    async def scenario():
        # \x08 (Backspace) deliberately excluded here -- issue #150
        # gave it new meaning (HELP_KEY) at this exact layer; its own
        # dedicated tests above cover that.
        source = FakeByteSource(b"\r\n\x7fz")
        writer = Writer()
        key = await read_key(source, writer)
        assert key == "z"

    asyncio.run(scenario())


def test_read_key_echo_false_masks_with_asterisk():
    async def scenario():
        source = FakeByteSource(b"x")
        writer = Writer()
        await read_key(source, writer, echo=False)
        assert writer.joined == "*"


# -- issue #102: Ctrl-L/Ctrl-R as returnable keys ------------------------


def test_read_key_returns_ctrl_l_as_redraw_key():
    async def scenario():
        source = FakeByteSource(b"\x0c")
        writer = Writer()
        key = await read_key(source, writer)
        assert key == REDRAW_KEY == "\x0c"

    asyncio.run(scenario())


def test_read_key_returns_ctrl_r_as_refresh_key():
    async def scenario():
        source = FakeByteSource(b"\x12")
        writer = Writer()
        key = await read_key(source, writer)
        assert key == REFRESH_KEY == "\x12"

    asyncio.run(scenario())


def test_read_key_does_not_echo_ctrl_l_or_ctrl_r():
    """Unlike every other returned key, these must never be echoed --
    writing a raw Ctrl-L byte back to a real terminal risks triggering
    its own local form-feed/clear behavior."""
    async def scenario():
        source = FakeByteSource(b"\x0c")
        writer = Writer()
        await read_key(source, writer)
        assert writer.joined == ""

    asyncio.run(scenario())


def test_read_key_still_skips_other_control_bytes_as_before():
    """Regression guard: the Ctrl-L/Ctrl-R carve-out must stay narrow --
    every other control byte keeps the old "no meaning, skip it"
    treatment, not a broadened blanket pass-through.

    \\x03 (Ctrl-C) deliberately excluded here -- issue #157 gave it new
    meaning (CANCEL_KEY) at this exact layer; its own dedicated tests
    above cover that."""
    async def scenario():
        source = FakeByteSource(b"\x01\x02z")
        writer = Writer()
        key = await read_key(source, writer)
        assert key == "z"

    asyncio.run(scenario())


# -- issue #150: Ctrl-H as a returnable key, reusing Backspace's byte ----


def test_read_key_returns_ctrl_h_as_help_key():
    async def scenario():
        source = FakeByteSource(b"\x08")
        writer = Writer()
        key = await read_key(source, writer)
        assert key == HELP_KEY == "\x08"

    asyncio.run(scenario())


def test_read_key_does_not_echo_ctrl_h():
    async def scenario():
        source = FakeByteSource(b"\x08")
        writer = Writer()
        await read_key(source, writer)
        assert writer.joined == ""

    asyncio.run(scenario())


def test_read_line_still_treats_the_same_byte_as_real_backspace():
    """The HELP_KEY carve-out is read_key()-only -- 0x08 must keep
    deleting a character in the editable read_line() path, unlike the
    single-keystroke menu context where Backspace already had no
    meaning to take away."""
    async def scenario():
        source = FakeByteSource(b"abc\x08\r\n")  # "abc" then real backspace
        writer = Writer()
        return await read_line(source, writer)

    line = asyncio.run(scenario())
    assert line == "ab"


# -- issue #157: Ctrl-C as a returnable key (incremental cancel) ---------


def test_read_key_returns_ctrl_c_as_cancel_key():
    async def scenario():
        source = FakeByteSource(b"\x03")
        writer = Writer()
        key = await read_key(source, writer)
        assert key == CANCEL_KEY == "\x03"

    asyncio.run(scenario())


def test_read_key_does_not_echo_ctrl_c():
    async def scenario():
        source = FakeByteSource(b"\x03")
        writer = Writer()
        await read_key(source, writer)
        assert writer.joined == ""

    asyncio.run(scenario())


def test_read_line_still_discards_ctrl_c_with_no_special_meaning():
    """This increment is read_key()-only -- read_line()'s editable path
    keeps discarding 0x03 exactly as before (no caller-agnostic safe
    meaning for it there yet, see CANCEL_KEY's own docstring)."""
    async def scenario():
        source = FakeByteSource(b"ab\x03c\r\n")
        writer = Writer()
        return await read_line(source, writer)

    line = asyncio.run(scenario())
    assert line == "abc"


# -- read_any_key: "press any key to continue" needs Enter to count too --
# -- (dogfood report: read_key's own CR/LF-skip above, correct for a -----
# -- hotkey menu, silently ate Enter at every "Press any key to ----------
# -- continue..." pause in the codebase too) ------------------------------


def test_read_any_key_returns_immediately_on_bare_cr():
    async def scenario():
        source = FakeByteSource(b"\r")
        writer = Writer()
        return await read_any_key(source, writer)

    key = asyncio.run(scenario())
    assert key == "\r"


def test_read_any_key_returns_immediately_on_bare_lf():
    async def scenario():
        source = FakeByteSource(b"\n")
        writer = Writer()
        return await read_any_key(source, writer)

    key = asyncio.run(scenario())
    assert key == "\n"


def test_read_any_key_consumes_a_paired_lf_after_cr_without_leaking_it():
    """A real Enter keypress often arrives as CRLF -- this must consume
    both bytes, not just the CR, or the leftover LF would silently get
    skipped by whatever `read_key` call comes next anyway (harmless) but
    could confuse a caller using `read_any_key` again immediately."""
    async def scenario():
        source = FakeByteSource(b"\r\nz")
        writer = Writer()
        first = await read_any_key(source, writer)
        second = await read_any_key(source, writer)
        return first, second

    first, second = asyncio.run(scenario())
    assert first == "\r"
    assert second == "z"


def test_read_any_key_does_not_skip_backspace_or_delete():
    async def scenario():
        source = FakeByteSource(b"\x08")
        writer = Writer()
        return await read_any_key(source, writer)

    key = asyncio.run(scenario())
    assert key == "\x08"


def test_read_any_key_returns_an_ordinary_key_and_echoes_it():
    async def scenario():
        source = FakeByteSource(b"q")
        writer = Writer()
        key = await read_any_key(source, writer)
        return key, writer.joined

    key, echoed = asyncio.run(scenario())
    assert key == "q"
    assert echoed == "q"


def test_read_any_key_echo_false_masks_with_asterisk():
    async def scenario():
        source = FakeByteSource(b"x")
        writer = Writer()
        await read_any_key(source, writer, echo=False)
        return writer.joined

    assert asyncio.run(scenario()) == "*"


def test_read_any_key_drains_a_full_escape_sequence_without_leaking_bytes():
    """An arrow-key press (CSI sequence) must dismiss without leaking its
    trailing bytes into whatever this screen redraws into next."""
    async def scenario():
        source = FakeByteSource(b"\x1b[Az")
        writer = Writer()
        first = await read_any_key(source, writer)
        second = await read_any_key(source, writer)
        return first, second

    first, second = asyncio.run(scenario())
    assert first == "\x1b"
    assert second == "z"


# -- reject_unhandled_key: real dogfood-reported bug fix -----------------
# -- (Ctrl-L/Ctrl-R erasing the previous on-screen character on a menu ---
# -- that doesn't specifically support them) ------------------------------


def test_reject_unhandled_key_bells_only_for_redraw_key():
    """REDRAW_KEY was returned unechoed by read_key() -- nothing was
    actually drawn for this keystroke, so there's nothing to erase."""
    assert reject_unhandled_key(REDRAW_KEY) == "\a"


def test_reject_unhandled_key_bells_only_for_refresh_key():
    """Same reasoning as REDRAW_KEY -- REFRESH_KEY is also unechoed."""
    assert reject_unhandled_key(REFRESH_KEY) == "\a"


def test_reject_unhandled_key_bells_only_for_help_key():
    """Same reasoning as REDRAW_KEY/REFRESH_KEY -- HELP_KEY is also
    unechoed, for a menu that doesn't specifically support it."""
    assert reject_unhandled_key(HELP_KEY) == "\a"


def test_reject_unhandled_key_bells_only_for_cancel_key():
    """Same reasoning as REDRAW_KEY/REFRESH_KEY/HELP_KEY -- CANCEL_KEY
    is also unechoed, for a menu that doesn't specifically support it
    (issue #157's incremental rollout: not every screen opts in)."""
    assert reject_unhandled_key(CANCEL_KEY) == "\a"


def test_reject_unhandled_key_matches_reject_keystroke_for_ordinary_keys():
    """Every other unrecognized key keeps today's erase-and-bell
    behavior, unchanged."""
    assert reject_unhandled_key("z") == reject_keystroke()
    assert reject_unhandled_key("9") == reject_keystroke()


def test_reject_unhandled_key_honors_a_custom_count_for_ordinary_keys():
    assert reject_unhandled_key("z", count=2) == reject_keystroke(2)


def test_reject_unhandled_key_ignores_count_for_redraw_and_refresh():
    """A bell-only response for these two doesn't scale with `count` --
    there's still nothing echoed to erase, regardless of how many
    characters a caller would otherwise have asked to erase."""
    assert reject_unhandled_key(REDRAW_KEY, count=2) == "\a"
    assert reject_unhandled_key(REFRESH_KEY, count=2) == "\a"


def test_connection_closed_mid_line_raises_session_closed_error():
    async def scenario():
        source = FakeByteSource(b"ab")  # no terminator -- source raises on next read
        writer = Writer()
        with pytest.raises(SessionClosedError):
            await read_line(source, writer)

    asyncio.run(scenario())


# -- bounded escape-sequence parsing (issue #5, CSI half) -------------------


def test_oversized_csi_sequence_raises_session_closed_error():
    async def scenario():
        # ESC [ followed by more non-terminating parameter bytes than the
        # cap allows -- none of these bytes fall in 0x40-0x7E, so the CSI
        # loop never finds a final byte and keeps consuming until the
        # length cap trips.
        oversized = b"9" * (char_input_module._MAX_ESCAPE_SEQUENCE_LENGTH + 1)
        source = FakeByteSource(b"a\x1b[" + oversized + b"Ab\r\n")
        writer = Writer()
        with pytest.raises(SessionClosedError, match="too long"):
            await read_line(source, writer)

    asyncio.run(scenario())


def test_csi_sequence_within_length_cap_still_works():
    async def scenario():
        # One byte under the cap, still non-terminating until the final
        # byte -- confirms the cap is a strict "more than N", not "N or
        # fewer also rejected".
        within_cap = b"9" * (char_input_module._MAX_ESCAPE_SEQUENCE_LENGTH - 1)
        source = FakeByteSource(b"a\x1b[" + within_cap + b"Ab\r\n")
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "ab"

    asyncio.run(scenario())


def test_csi_sequence_exceeding_total_deadline_raises_session_closed_error(monkeypatch):
    """A client that keeps a CSI sequence "alive" by continuously sending
    parameter bytes -- each one arriving well within the per-byte
    _FOLLOWUP_BYTE_TIMEOUT, so no individual read ever times out -- must
    still be bounded by one total deadline for the whole sequence, not
    just by how many bytes it sends. Isolated from the length cap by
    raising it out of the way, so only the timeout can trip here."""

    monkeypatch.setattr(char_input_module, "_ESCAPE_SEQUENCE_TIMEOUT", 0.05)
    monkeypatch.setattr(char_input_module, "_MAX_ESCAPE_SEQUENCE_LENGTH", 1_000_000)

    class SlowTrickleSource:
        """First byte enters CSI mode ('['); every byte after that is a
        real (non-terminating) parameter byte returned after a short
        delay, forever -- never times out an individual read, never
        sends a final byte in 0x40-0x7E."""

        def __init__(self):
            self._first = True

        async def read_byte(self) -> int | None:
            raise AssertionError("read_line should only use read_byte_with_timeout here")

        async def read_byte_with_timeout(self, timeout: float) -> int | None:
            await asyncio.sleep(0.02)
            if self._first:
                self._first = False
                return 0x5B  # '[' -- enter CSI parameter mode
            return ord("9")

    async def scenario():
        source = SlowTrickleSource()
        # _read_escape_sequence is what read_line/read_key dispatch to
        # after consuming the leading ESC byte itself -- called directly
        # here since SlowTrickleSource has no fixed buffer to pre-seed
        # with one.
        with pytest.raises(SessionClosedError, match="timed out"):
            await char_input_module._read_escape_sequence(source)

    asyncio.run(scenario())


def test_valid_csi_and_ss3_sequences_still_work_within_new_bounds():
    async def scenario():
        source = FakeByteSource(b"a\x1b[Ab\x1bOPc\r\n")
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "abc"

    asyncio.run(scenario())


# -- read_editor_key ---------------------------------------------------------


def test_read_editor_key_page_up_and_page_down():
    async def scenario():
        source = FakeByteSource(b"\x1b[5~\x1b[6~")
        assert (await read_editor_key(source)).kind == EditorKeyKind.PAGE_UP
        assert (await read_editor_key(source)).kind == EditorKeyKind.PAGE_DOWN

    asyncio.run(scenario())


def test_read_editor_key_arrows_and_home_end():
    async def scenario():
        source = FakeByteSource(b"\x1b[A\x1b[B\x1b[C\x1b[D\x1b[H\x1b[F")
        for expected in (
            EditorKeyKind.UP,
            EditorKeyKind.DOWN,
            EditorKeyKind.RIGHT,
            EditorKeyKind.LEFT,
            EditorKeyKind.HOME,
            EditorKeyKind.END,
        ):
            assert (await read_editor_key(source)).kind == expected

    asyncio.run(scenario())


def test_read_editor_key_plain_character():
    async def scenario():
        source = FakeByteSource(b"X")
        key = await read_editor_key(source)
        assert key.kind == EditorKeyKind.CHAR
        assert key.char == "X"

    asyncio.run(scenario())


def test_read_editor_key_utf8_character():
    async def scenario():
        source = FakeByteSource("ü".encode("utf-8"))
        key = await read_editor_key(source)
        assert key.kind == EditorKeyKind.CHAR
        assert key.char == "ü"

    asyncio.run(scenario())


def test_read_editor_key_enter_from_crlf():
    async def scenario():
        source = FakeByteSource(b"\r\n")
        key = await read_editor_key(source)
        assert key.kind == EditorKeyKind.ENTER

    asyncio.run(scenario())


def test_read_editor_key_enter_from_bare_lf():
    async def scenario():
        source = FakeByteSource(b"\n")
        key = await read_editor_key(source)
        assert key.kind == EditorKeyKind.ENTER

    asyncio.run(scenario())


def test_read_editor_key_backspace_and_delete_byte():
    async def scenario():
        source = FakeByteSource(b"\x08\x7f")
        assert (await read_editor_key(source)).kind == EditorKeyKind.BACKSPACE
        assert (await read_editor_key(source)).kind == EditorKeyKind.BACKSPACE

    asyncio.run(scenario())


def test_read_editor_key_distinguish_ctrl_h_off_by_default():
    """Dogfood-reported regression (netbbs.net.resource_editor.
    edit_resource_draft's Ctrl-H): 0x08 must keep meaning BACKSPACE for
    every caller that doesn't opt in -- the fullscreen ANSI/prose
    editors genuinely need it for real character deletion."""
    async def scenario():
        source = FakeByteSource(b"\x08")
        assert (await read_editor_key(source)).kind == EditorKeyKind.BACKSPACE

    asyncio.run(scenario())


def test_read_editor_key_distinguish_ctrl_h_splits_0x08_only():
    """With `distinguish_ctrl_h=True`, 0x08 becomes a real Ctrl+h event
    instead of BACKSPACE -- but 0x7F (the actual Backspace-key byte on
    virtually every modern terminal) is unaffected either way."""
    async def scenario():
        source = FakeByteSource(b"\x08\x7f")
        key = await read_editor_key(source, distinguish_ctrl_h=True)
        assert key.kind == EditorKeyKind.CTRL
        assert key.char == "h"
        assert (await read_editor_key(source, distinguish_ctrl_h=True)).kind == EditorKeyKind.BACKSPACE

    asyncio.run(scenario())


def test_read_editor_key_delete_key_via_csi_tilde():
    async def scenario():
        source = FakeByteSource(b"\x1b[3~")
        key = await read_editor_key(source)
        assert key.kind == EditorKeyKind.DELETE

    asyncio.run(scenario())


def test_read_editor_key_tab():
    async def scenario():
        source = FakeByteSource(b"\t")
        key = await read_editor_key(source)
        assert key.kind == EditorKeyKind.TAB

    asyncio.run(scenario())


def test_read_editor_key_lone_escape():
    async def scenario():
        source = FakeByteSource(b"\x1b")
        key = await read_editor_key(source)
        assert key.kind == EditorKeyKind.ESCAPE

    asyncio.run(scenario())


def test_read_editor_key_ctrl_letter():
    async def scenario():
        source = FakeByteSource(bytes([0x13]))  # Ctrl+S
        key = await read_editor_key(source)
        assert key.kind == EditorKeyKind.CTRL
        assert key.char == "s"

    asyncio.run(scenario())


def test_read_editor_key_unrecognized_escape_is_skipped_not_returned():
    async def scenario():
        # ESC[1;5A (Ctrl+Up, a modified combo -- outside this project's
        # recognized set) must not surface as anything; the next real
        # key after it is what read_editor_key returns.
        source = FakeByteSource(b"\x1b[1;5AZ")
        key = await read_editor_key(source)
        assert key.kind == EditorKeyKind.CHAR
        assert key.char == "Z"

    asyncio.run(scenario())


def test_read_editor_key_connection_closed_raises():
    async def scenario():
        source = FakeByteSource(b"")
        with pytest.raises(SessionClosedError):
            await read_editor_key(source)

    asyncio.run(scenario())


# -- discard_buffered_enter (dogfood follow-up: a hotkey typed as one --
# -- "C<Enter>" habit was leaking that Enter into the very next prompt) --


def test_discard_buffered_enter_consumes_a_trailing_cr_lf():
    async def scenario():
        # Simulates a hotkey byte (consumed by the caller before this
        # point, not part of this call) immediately followed by CRLF --
        # the same "typed as one burst" shape a habitual "C<Enter>"
        # keypress produces. Without discarding it, the very next
        # read_line() would see this CRLF as an immediate blank Enter.
        source = FakeByteSource(b"\r\nhello\r\n")
        await discard_buffered_enter(source)
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "hello"

    asyncio.run(scenario())


def test_discard_buffered_enter_consumes_a_bare_cr():
    async def scenario():
        # A bare CR with no following LF (some clients never send one) --
        # `_consume_optional_lf_or_nul`'s other half of the same job.
        source = FakeByteSource(b"\rhello\r\n")
        await discard_buffered_enter(source)
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "hello"

    asyncio.run(scenario())


def test_discard_buffered_enter_pushes_back_a_real_character_unharmed():
    async def scenario():
        # Nothing was actually buffered behind the hotkey -- the very
        # next byte is real, ordinary input for the next prompt, not a
        # leftover Enter. It must not be lost.
        source = FakeByteSource(b"hello\r\n")
        await discard_buffered_enter(source)
        writer = Writer()
        line = await read_line(source, writer)
        assert line == "hello"

    asyncio.run(scenario())


def test_read_line_bells_once_when_the_length_cap_drops_a_character():
    # Dogfood follow-up: a line past `_MAX_LINE_LENGTH` used to drop the
    # excess silently, with zero indication to the caller that anything
    # was lost -- worst for a pasted letter body far longer than the cap.
    # One bell, not one per dropped character (which would turn a long
    # paste into a bell storm) -- Backspace/movement/Enter still work
    # normally past the cap either way.
    async def scenario():
        overlong = b"x" * (char_input_module._MAX_LINE_LENGTH + 50) + b"\r\n"
        source = FakeByteSource(overlong)
        writer = Writer()
        line = await read_line(source, writer)
        assert len(line) == char_input_module._MAX_LINE_LENGTH
        assert writer.joined.count("\a") == 1

    asyncio.run(scenario())


def test_read_line_does_not_bell_for_a_line_within_the_length_cap():
    async def scenario():
        source = FakeByteSource(b"hello\r\n")
        writer = Writer()
        await read_line(source, writer)
        assert "\a" not in writer.joined

    asyncio.run(scenario())


def test_discard_buffered_enter_is_a_no_op_when_nothing_arrives_in_time():
    async def scenario():
        # No buffered byte at all (a genuinely idle connection, the
        # common case) -- must return promptly rather than hang, and
        # must not disturb whatever arrives later for real.
        source = FakeByteSource(b"")
        await discard_buffered_enter(source)

    asyncio.run(scenario())


# -- discard_buffered_input (dogfood follow-up: a moderation kick/ban --
# -- landing mid-keystroke used to leak already-typed input into the --
# -- next screen the eviction landed the caller on) --------------------


def test_discard_buffered_input_drains_multiple_buffered_bytes_not_just_one():
    async def scenario():
        # Simulates a caller who was mid-typing "still here" when
        # evicted -- every byte of it must be gone, not just the first
        # (which `discard_buffered_enter` alone would leave sitting
        # there for whatever screen comes next to consume one keystroke
        # at a time). `FakeByteSource` delivers its whole fixed byte
        # string with no real gap, so the only way to observe "was
        # everything actually consumed" here is the source's own
        # position, not a subsequent read (there's nothing left after
        # a genuine full drain for one to return).
        buffered = b"still here, mid-sentence when kicked"
        source = FakeByteSource(buffered)
        await discard_buffered_input(source)
        assert source._pos == len(buffered)

    asyncio.run(scenario())


def test_discard_buffered_input_is_a_no_op_when_nothing_arrives_in_time():
    async def scenario():
        # A genuinely idle connection (the common case: an ordinary
        # kick with nothing mid-typed) -- must return promptly, not
        # hang waiting for input that was never coming.
        source = FakeByteSource(b"")
        await discard_buffered_input(source)

    asyncio.run(scenario())



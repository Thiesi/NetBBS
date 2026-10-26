"""Quarter blocks for a UTF-8 terminal whose font lacks them (issue #1560):
the answerback probe, the caller's preference, the fallback to half
blocks, and what a door is told."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from netbbs.doors.runtime import DoorTerminal
from netbbs.net.session import Session
from netbbs.net.ssh import SSHSession
from netbbs.net.telnet import TelnetServer
from netbbs.net.terminal_detect import ANSWERBACK_REQUEST, POSITION_REQUEST, take_answerback, take_position_report
from netbbs.net.unicode_style_preference import (
    apply_quadrant_preference,
    effective_quadrants,
    quadrant_preference,
    set_quadrant_preference,
)
from netbbs.rendering.charset import CP437, QUADRANTS, UTF8, without_quadrants
from netbbs.rendering.width import display_width
from tests.test_doors_metadata import _info
from tests.test_doors_runtime import FakeSession, db, player  # noqa: F401 -- fixtures
from tests.test_session_charset import _DoorSession, _Process, _telnet
from tests.test_telnet import _FULL_NEGOTIATION_LEN, answer_terminal_type


# -- The fallback --------------------------------------------------------


def test_every_quarter_block_becomes_a_half_or_full_block_of_its_width():
    assert len(QUADRANTS) == 10
    for ch in QUADRANTS:
        out = without_quadrants(ch)
        assert out in "▀▄█▒", (ch, out)
        assert display_width(out) == display_width(ch) == 1


def test_the_fallback_is_the_picture_cp437_gets():
    assert without_quadrants("▘▝▖▗▛▜▙▟▚▞ ok") == "▀▀▄▄████▒▒ ok"


def test_a_session_without_quarter_blocks_is_sent_half_blocks():
    async def scenario():
        session, writer = _telnet(UTF8)
        session.quadrant_blocks = False
        await session.write("▗▟█")
        return writer.data, session.screen_copy().text_rows()[0].rstrip()

    data, row = asyncio.run(scenario())
    assert data == "▄██".encode("utf-8")
    assert row == "▄██", "the screen copy shows what the caller got"


def test_a_session_with_quarter_blocks_gets_them_unchanged():
    async def scenario():
        session, writer = _telnet(UTF8)
        await session.write("▗▟")
        return writer.data

    assert asyncio.run(scenario()) == "▗▟".encode("utf-8")


def test_a_utf8_doors_quarter_blocks_reach_a_caller_without_them_as_half_blocks():
    async def scenario():
        session = _DoorSession(UTF8)
        session.quadrant_blocks = False
        terminal = DoorTerminal(session, "utf-8")
        encoded = "x▙▞".encode("utf-8")
        # Split inside a character: the decoder carries it over.
        await terminal.write_raw(encoded[:3])
        await terminal.write_raw(encoded[3:])
        return session.sent

    assert asyncio.run(scenario()) == "x█▒".encode("utf-8")


def test_a_raw_doors_bytes_still_pass_through():
    async def scenario():
        session = _DoorSession(UTF8)
        session.quadrant_blocks = False
        terminal = DoorTerminal(session, "raw")
        await terminal.write_raw("▙".encode("utf-8"))
        return session.sent

    assert asyncio.run(scenario()) == "▙".encode("utf-8")


# -- The preference ------------------------------------------------------


def test_the_preference_defaults_to_auto_and_round_trips(db, player):
    assert quadrant_preference(db, player) == "auto"
    set_quadrant_preference(db, player, "off")
    assert quadrant_preference(db, player) == "off"
    with pytest.raises(ValueError):
        set_quadrant_preference(db, player, "maybe")


@pytest.mark.parametrize(("preference", "detected", "expected"), [
    ("auto", True, True),
    ("auto", False, False),
    ("on", False, True),
    ("off", True, False),
])
def test_auto_follows_detection_and_on_or_off_override_it(preference, detected, expected):
    session = SimpleNamespace(detected_quadrants=detected)
    assert effective_quadrants(preference, session) is expected
    apply_quadrant_preference(session, preference)
    assert session.quadrant_blocks is expected


# -- What a door is told -------------------------------------------------


@pytest.mark.parametrize(("charset", "blocks", "expected"), [
    (UTF8, True, True),
    (UTF8, False, False),
    # CP437 and ASCII have no quarter blocks, whatever the font.
    (CP437, True, False),
    ("ascii", True, False),
])
def test_door_info_says_whether_the_terminal_shows_quarter_blocks(db, tmp_path, player, charset, blocks, expected):
    session = FakeSession()
    session.output_charset = charset
    session.quadrant_blocks = blocks
    info = _info(db, tmp_path, session=session)
    assert info["quadrants"] is expected
    assert info["door_api"] == 5, "an additive field within door_api 5, not a bump"


# -- Reading the answer --------------------------------------------------


def test_only_a_known_answerback_is_taken_out_of_the_input():
    assert take_answerback(b"PuTTY") == ("PuTTY", b"")
    assert take_answerback(b"aPuTTYb") == ("PuTTY", b"ab")
    assert take_answerback(b"hello") == (None, b"hello")


def test_a_position_report_is_found_and_removed():
    assert take_position_report(b"\x1b[24;1R") == (True, b"")
    assert take_position_report(b"PuTTY\x1b[3;80Rx") == (True, b"PuTTYx")
    assert take_position_report(b"\x1b[A") == (False, b"\x1b[A")


# -- Telnet --------------------------------------------------------------


def _telnet_login(answerback: bytes, typed: bytes):
    """Connect as a client that answers ENQ with `answerback`, report
    `xterm`, type `typed`, and return what the session saw."""
    seen = {}

    async def handler(session: Session):
        seen["quadrants"] = session.quadrant_blocks
        seen["detected"] = session.detected_quadrants
        seen["answerback"] = session.answerback
        seen["line"] = await session.read_line()

    async def scenario():
        server = TelnetServer(host="127.0.0.1", port=0, session_handler=handler, terminal_type_wait_seconds=5)
        await server.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            negotiation = await reader.readexactly(_FULL_NEGOTIATION_LEN)
            seen["asked"] = negotiation.endswith(ANSWERBACK_REQUEST)
            # As PuTTY does: WILL TTYPE, then the answerback, then the
            # terminal type once asked.
            writer.write(answerback)
            await answer_terminal_type(reader, writer, "xterm")
            writer.write(typed)
            await writer.drain()
            while "line" not in seen:
                await asyncio.sleep(0.01)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    return seen


def test_telnet_asks_for_the_answerback_and_takes_puttys_out_of_the_input():
    seen = _telnet_login(b"PuTTY", b"alice\r\n")
    assert seen["asked"]
    assert seen["answerback"] == "PuTTY"
    assert seen["detected"] is False and seen["quadrants"] is False
    assert seen["line"] == "alice", "the answerback never reaches the first prompt"


def test_telnet_without_an_answerback_keeps_quarter_blocks():
    seen = _telnet_login(b"", b"alice\r\n")
    assert seen["answerback"] is None
    assert seen["detected"] is True and seen["quadrants"] is True
    assert seen["line"] == "alice"


# -- SSH -----------------------------------------------------------------


class _Stdin:
    def __init__(self, data: bytes, *, closed: bool = False):
        self.data = bytearray(data)
        self.closed = closed
        self.reads = 0

    async def read(self, n):
        self.reads += 1
        if not self.data:
            if self.closed:
                return b""
            await asyncio.sleep(3600)
        chunk = bytes(self.data[:n])
        del self.data[:n]
        return chunk


class _TerminalProcess(_Process):
    def __init__(self, answer: bytes, terminal_type: str = "xterm-256color"):
        super().__init__()
        self.stdin = _Stdin(answer)
        self._terminal_type = terminal_type

    def get_terminal_type(self):
        return self._terminal_type


def _ssh_probe(answer: bytes, terminal_type: str = "xterm-256color", timeout: float = 2.0):
    async def scenario():
        session = SSHSession(_TerminalProcess(answer, terminal_type))
        await session.probe_answerback(timeout=timeout)
        left = bytes(session._early_input)
        return session, session._process.stdout.data, left

    return asyncio.run(scenario())


def test_ssh_asks_for_the_answerback_and_a_position_report():
    session, sent, left = _ssh_probe(b"xPuTTY\x1b[1;1Ry")
    assert sent == ANSWERBACK_REQUEST + POSITION_REQUEST
    assert session.answerback == "PuTTY"
    assert session.detected_quadrants is False and session.quadrant_blocks is False
    assert left == b"x", "what the caller typed is kept for the first screen"
    assert bytes(session._process.stdin.data) == b"y", "the report ends the wait; the rest stays unread"


def test_ssh_keeps_quarter_blocks_for_a_terminal_without_puttys_answerback():
    session, _sent, left = _ssh_probe(b"\x1b[5;9R")
    assert session.answerback is None and session.quadrant_blocks is True
    assert left == b""


def test_ssh_does_not_wait_long_for_a_terminal_that_answers_nothing():
    session, _sent, left = _ssh_probe(b"", timeout=0.05)
    assert session.quadrant_blocks is True and left == b""


def test_ssh_stops_probing_at_once_when_the_caller_has_gone():
    """Review on #1563: an empty read is end of input, not "nothing yet",
    and retrying it spun until the deadline."""
    async def scenario():
        process = _TerminalProcess(b"")
        process.stdin.closed = True
        session = SSHSession(process)
        started = asyncio.get_running_loop().time()
        await session.probe_answerback(timeout=5.0)
        return asyncio.get_running_loop().time() - started, process.stdin.reads

    elapsed, reads = asyncio.run(scenario())
    assert reads == 1
    assert elapsed < 1.0


def test_ssh_does_not_probe_a_cp437_terminal():
    session, sent, _left = _ssh_probe(b"PuTTY", terminal_type="syncterm")
    assert sent == b"" and session.quadrant_blocks is True


def test_early_input_is_served_before_the_channel():
    async def scenario():
        session = SSHSession(_TerminalProcess(b"z"))
        session._early_input.extend(b"xy")
        return [await session._receive_byte() for _ in range(3)]

    assert asyncio.run(scenario()) == [ord("x"), ord("y"), ord("z")]


def test_a_session_starts_with_quarter_blocks():
    session, _writer = asyncio.run(_async_telnet())
    assert session.quadrant_blocks is True and session.detected_quadrants is True
    assert session.answerback is None


async def _async_telnet():
    return _telnet(UTF8)


@pytest.mark.parametrize(("charset", "expected"), [
    ("cp437", "Auto (off: CP437 has none)"),
    ("ascii", "Auto (off: ASCII has none)"),
])
def test_the_profile_says_a_cp437_or_ascii_caller_gets_none(charset, expected):
    """ReLink's check of v7.43.0: a SyncTERM caller read "Auto (now on)"."""
    from netbbs.net.profile_flow import _quadrant_label

    session = SimpleNamespace(output_charset=charset, quadrant_blocks=True)
    assert _quadrant_label("auto", session) == expected


def test_the_profile_label_on_a_unicode_terminal_keeps_its_sample():
    from netbbs.net.profile_flow import _quadrant_label

    session = SimpleNamespace(output_charset="utf-8", quadrant_blocks=False)
    assert _quadrant_label("auto", session) == "Auto (now off: PuTTY detected)  ▗▟█▙▖"
    assert _quadrant_label("on", None) == "On  ▗▟█▙▖"

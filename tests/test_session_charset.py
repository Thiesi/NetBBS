"""A session's output character set reaches the wire, the screen copy and
a door's stream (issue #929, design doc §3.2)."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.doors.runtime import DoorTerminal
from netbbs.net.ssh import SSHSession
from netbbs.net.telnet import TelnetSession
from netbbs.rendering.charset import ASCII, CP437, UTF8

_ESC = chr(27)
_NBSP = chr(0xA0)


class _Writer:
    def __init__(self):
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass

    def is_closing(self) -> bool:
        return False

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class _Stdout(_Writer):
    pass


class _Process:
    term_size = (80, 24, 0, 0)
    env: dict = {}

    def __init__(self):
        self.stdout = _Stdout()

    def get_extra_info(self, name, default=None):
        return default


def _telnet(charset):
    writer = _Writer()
    session = TelnetSession(asyncio.StreamReader(), writer)
    session.output_charset = charset
    return session, writer


def test_utf8_stays_what_it_always_was():
    async def scenario():
        session, writer = _telnet(UTF8)
        await session.write("╭─ café ★")
        return writer.data

    assert asyncio.run(scenario()) == "╭─ café ★".encode("utf-8")


def test_telnet_sends_cp437_and_doubles_its_0xff():
    async def scenario():
        session, writer = _telnet(CP437)
        await session.write(f"╭─ café{_NBSP}★\n")
        return writer.data

    # ┌ ─ space c a f é NBSP(0xFF, doubled for Telnet) * CR LF
    assert asyncio.run(scenario()) == bytes(
        [0xDA, 0xC4, 0x20, 0x63, 0x61, 0x66, 0x82, 0xFF, 0xFF, 0x2A, 0x0D, 0x0A])


def test_telnet_ascii_is_seven_bit():
    async def scenario():
        session, writer = _telnet(ASCII)
        await session.write(f"{_ESC}[1m╔═╗{_ESC}[0m café — ok…")
        return writer.data

    data = asyncio.run(scenario())
    assert data == f"{_ESC}[1m+=+{_ESC}[0m cafe - ok.".encode("ascii")


def test_ssh_encodes_in_the_session_charset():
    async def scenario():
        session = SSHSession(_Process())
        session.output_charset = CP437
        await session.write("é│")
        return session._process.stdout.data

    assert asyncio.run(scenario()) == bytes([0x82, 0xB3])


def test_the_screen_copy_shows_what_the_caller_got():
    async def scenario():
        session, _writer = _telnet(ASCII)
        await session.write("╭ café")
        return session.screen_copy().text_rows()[0].rstrip()

    assert asyncio.run(scenario()) == "+ cafe"


def test_raw_output_reaches_the_copy_in_the_session_charset():
    async def scenario():
        session, writer = _telnet(CP437)
        await session.write_raw(bytes([0xC9, 0xCD, 0xBB]))  # ╔═╗ in CP437
        return writer.data, session.screen_copy().text_rows()[0].rstrip()

    data, row = asyncio.run(scenario())
    assert data == bytes([0xC9, 0xCD, 0xBB])
    assert row == "╔═╗"


def test_a_binary_transfer_is_never_mapped():
    async def scenario():
        session, writer = _telnet(ASCII)
        frame = bytes(range(0x80, 0x100))
        with session.binary_transfer():
            await session.write_raw(frame)
        return frame, writer.data

    frame, data = asyncio.run(scenario())
    # Only Telnet's own IAC doubling applies.
    assert data == frame.replace(bytes([0xFF]), bytes([0xFF, 0xFF]))


class _DoorSession:
    def __init__(self, charset, typed=b""):
        self.output_charset = charset
        self.sent = b""
        self.typed = list(typed)

    async def write_raw(self, data):
        self.sent += data

    async def read_byte(self):
        return self.typed.pop(0)


def _door_out(door_encoding, charset, *chunks):
    async def scenario():
        session = _DoorSession(charset)
        terminal = DoorTerminal(session, door_encoding)
        for chunk in chunks:
            await terminal.write_raw(chunk)
        return session.sent

    return asyncio.run(scenario())


def _door_in(door_encoding, charset, typed, count):
    async def scenario():
        session = _DoorSession(charset, typed)
        terminal = DoorTerminal(session, door_encoding)
        return bytes([await terminal.read_byte() for _ in range(count)])

    return asyncio.run(scenario())


_CP437_ART = bytes([0xC9, 0xCD, 0xBB, 0x20, 0x82])  # ╔═╗ é


def test_a_cp437_door_reaches_a_cp437_terminal_unchanged():
    assert _door_out("cp437", CP437, _CP437_ART) == _CP437_ART


def test_a_cp437_door_is_still_transcoded_for_utf8():
    assert _door_out("cp437", UTF8, _CP437_ART) == "╔═╗ é".encode("utf-8")


def test_a_cp437_door_is_mapped_for_ascii():
    assert _door_out("cp437", ASCII, _CP437_ART) == b"+=+ e"


def test_a_utf8_door_is_mapped_for_cp437_even_split_mid_character():
    data = "╭─ é".encode("utf-8")
    assert _door_out("utf-8", CP437, data[:1], data[1:4], data[4:]) == bytes([0xDA, 0xC4, 0x20, 0x82])


def test_raw_doors_pass_bytes_through():
    assert _door_out("raw", ASCII, bytes([0x80, 0xFF])) == bytes([0x80, 0xFF])


@pytest.mark.parametrize(
    ("door_encoding", "charset", "typed", "expected"),
    [
        ("cp437", CP437, bytes([0x82]), bytes([0x82])),
        ("cp437", UTF8, "é".encode("utf-8"), bytes([0x82])),
        ("utf-8", CP437, bytes([0x82]), "é".encode("utf-8")),
        ("utf-8", UTF8, "é".encode("utf-8"), "é".encode("utf-8")),
        ("utf-8", ASCII, b"e", b"e"),
        # An ASCII terminal still types UTF-8 (review on #940).
        ("cp437", ASCII, "é".encode("utf-8"), bytes([0x82])),
    ],
)
def test_keystrokes_reach_the_door_in_its_encoding(door_encoding, charset, typed, expected):
    assert _door_in(door_encoding, charset, typed, len(expected)) == expected

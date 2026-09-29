"""A CP437 terminal types one byte per character (issue #929)."""

from __future__ import annotations

import asyncio

from netbbs.net import break_in
from netbbs.net.session import Session
from netbbs.net.telnet import IAC
from netbbs.rendering.charset import ASCII, CP437, UTF8
from tests.test_telnet import _run_server, skip_initial_negotiation


def _typed(charset, keystrokes: bytes, *, read_key: bool = False):
    results = []

    async def handler(session: Session):
        session.output_charset = charset
        if read_key:
            results.append(await session.read_key(echo=False))
        else:
            results.append(await session.read_line())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader, writer)
            writer.write(keystrokes)
            await writer.drain()
            for _ in range(200):
                if results:
                    break
                await asyncio.sleep(0.01)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    return results[0]


def test_a_cp437_terminal_types_accented_letters():
    # c a f é(0x82) space Z ü(0x81) r i c h
    assert _typed(CP437, b"caf" + bytes([0x82]) + b" Z" + bytes([0x81]) + b"rich\r") == "café Zürich"


def test_a_cp437_high_byte_is_not_mistaken_for_utf8():
    # 0xC3 starts a UTF-8 sequence; in CP437 it is a box piece on its own,
    # and the letters after it must not be swallowed as its continuation.
    assert _typed(CP437, bytes([0xC3]) + b"ab\r") == "├ab"


def test_a_cp437_single_key():
    assert _typed(CP437, bytes([0x84]), read_key=True) == "ä"


def test_telnets_escaped_0xff_is_cp437s_nbsp():
    assert _typed(CP437, b"a" + bytes([IAC, IAC]) + b"b\r") == "a" + chr(0xA0) + "b"


def test_utf8_terminals_are_unchanged():
    assert _typed(UTF8, "café\r".encode("utf-8")) == "café"


def test_a_lone_high_byte_on_an_ascii_terminal_is_dropped_not_fatal():
    # An ASCII session still reads UTF-8: a stray 0x84 is no lead byte,
    # so it is skipped and the rest of the line survives.
    assert _typed(ASCII, b"a" + bytes([0x84]) + b"b\r") == "ab"


def test_a_cursor_navigated_screen_reads_cp437_characters():
    keys = []

    async def handler(session: Session):
        session.output_charset = CP437
        for _ in range(2):
            keys.append(await session.read_editor_key())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader, writer)
            writer.write(bytes([0x82, 0xC3]))  # é, then a lone box piece
            await writer.drain()
            for _ in range(200):
                if len(keys) == 2:
                    break
                await asyncio.sleep(0.01)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert [key.char for key in keys] == ["é", "├"]


class _Session:
    output_charset = CP437
    reading_secret = False


def test_a_break_in_chat_reads_cp437_typing():
    pane = break_in.Pane()
    keys = break_in._CallerKeys(pane, _Session())
    for value in b"gr" + bytes([0x81]) + b"n":
        keys.feed(value)
    assert pane.typing == "grün"

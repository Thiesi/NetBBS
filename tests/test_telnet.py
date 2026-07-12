"""
Integration tests for the Telnet transport.

These spin up a real `TelnetServer` on an OS-assigned loopback port and
connect a plain `asyncio.open_connection` client to it — exercising the
actual network path and byte-level IAC handling, rather than mocking
`StreamReader`/`StreamWriter`. The client side deliberately does *not*
implement full Telnet negotiation; it only needs to send/receive the
specific byte sequences each test cares about.
"""

from __future__ import annotations

import asyncio

from netbbs.net.session import Session
from netbbs.net.telnet import DO, ECHO, IAC, SB, SE, SUPPRESS_GO_AHEAD, WILL, WONT, TelnetServer


async def _run_server(session_handler):
    server = TelnetServer(host="127.0.0.1", port=0, session_handler=session_handler)
    await server.start()
    return server


def test_server_runs_handler_and_sends_output():
    calls = []

    async def handler(session: Session):
        calls.append("called")
        await session.write_line("hello")

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(3)  # consume the initial SGA negotiation
            line = await reader.readline()
            assert line == b"hello\r\n"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert calls == ["called"]


def test_server_negotiates_suppress_go_ahead_on_connect():
    async def handler(session: Session):
        await session.write_line("done")

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            # First bytes off the wire should be the IAC WILL SGA
            # negotiation, sent before the handler's own output.
            first_bytes = await reader.readexactly(3)
            assert first_bytes == bytes([IAC, WILL, SUPPRESS_GO_AHEAD])
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_read_line_strips_will_wont_do_dont_sequences():
    received = []

    async def handler(session: Session):
        line = await session.read_line()
        received.append(line)
        await session.write_line("ack")

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(3)  # consume the initial SGA negotiation

            # Send a line with an embedded (client-initiated) negotiation
            # sequence in the middle, which the server must strip rather
            # than treat as text content.
            payload = b"hel" + bytes([IAC, DO, ECHO]) + b"lo\r\n"
            writer.write(payload)
            await writer.drain()

            ack = await reader.readline()
            assert ack == b"ack\r\n"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["hello"]


def test_read_line_strips_subnegotiation_sequences():
    received = []

    async def handler(session: Session):
        line = await session.read_line()
        received.append(line)

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(3)

            # A NAWS-shaped subnegotiation (window size) embedded in the
            # input — content we deliberately don't parse (see module
            # docstring) but must still correctly skip over.
            naws_subneg = bytes([IAC, SB, 31, 0, 80, 0, 24, IAC, SE])
            payload = b"foo" + naws_subneg + b"bar\r\n"
            writer.write(payload)
            await writer.drain()
            await asyncio.sleep(0.05)  # let the handler finish reading before closing
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["foobar"]


def test_read_line_handles_escaped_iac_as_literal_data():
    """
    IAC IAC (RFC 854's escape for a literal 0xFF data byte) is correctly
    passed through by the byte-level reader — but note what it becomes
    once decoded: 0xFF is not a valid standalone UTF-8 byte (UTF-8 never
    uses byte values 0xF5-0xFF at all), so `decode("utf-8",
    errors="replace")` cannot recover a "0xFF character" — there isn't
    one in UTF-8. It becomes U+FFFD (the replacement character), same as
    any other invalid byte would. This is correct, expected behavior for
    UTF-8 decoding, not a bug — but it does mean literal 8-bit binary
    data (as RFC 854's IAC-escaping was originally designed for, and as
    classic CP437-codepage ANSI art historically relies on) cannot
    survive this layer intact while we're committed to interpreting all
    Telnet input as UTF-8 text. Worth a deliberate decision once the
    ANSI-art rendering work happens, not something to silently paper
    over here.
    """
    received = []

    async def handler(session: Session):
        line = await session.read_line()
        received.append(line)

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(3)

            # IAC IAC is the RFC 854 escape for a literal 0xFF data byte.
            payload = b"a" + bytes([IAC, IAC]) + b"b\r\n"
            writer.write(payload)
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["a\ufffdb"]


def test_read_line_echo_false_sends_will_echo_and_wont_echo():
    async def handler(session: Session):
        await session.read_line(echo=False)

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(3)  # initial SGA negotiation

            will_echo = await reader.readexactly(3)
            assert will_echo == bytes([IAC, WILL, ECHO])

            writer.write(b"secret\r\n")
            await writer.drain()

            wont_echo = await reader.readexactly(3)
            assert wont_echo == bytes([IAC, WONT, ECHO])
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_write_never_produces_invalid_utf8_or_stray_iac():
    async def handler(session: Session):
        await session.write_line("hello world")

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(3)  # consume the initial SGA negotiation
            data = await reader.readline()
            assert data == b"hello world\r\n"
            assert 0xFF not in data
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_server_port_property_before_start_raises():
    import pytest

    async def handler(session: Session):
        pass

    server = TelnetServer(host="127.0.0.1", port=0, session_handler=handler)
    with pytest.raises(RuntimeError):
        _ = server.port

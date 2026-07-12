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
from netbbs.net.telnet import DO, ECHO, IAC, NAWS, SB, SE, SUPPRESS_GO_AHEAD, WILL, WONT, TelnetServer


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
            await reader.readexactly(6)  # consume the initial SGA + DO NAWS negotiation
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
            # negotiation, sent before IAC DO NAWS and before the
            # handler's own output. Only reading these first 3 bytes is
            # fine — the connection closes right after this assertion,
            # so the trailing DO NAWS bytes never need to be consumed.
            first_bytes = await reader.readexactly(3)
            assert first_bytes == bytes([IAC, WILL, SUPPRESS_GO_AHEAD])
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_server_negotiates_naws_on_connect():
    async def handler(session: Session):
        await session.write_line("done")

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(3)  # SGA, sent first
            do_naws = await reader.readexactly(3)
            assert do_naws == bytes([IAC, DO, NAWS])
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
            await reader.readexactly(6)  # consume the initial SGA + DO NAWS negotiation

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


def test_read_line_strips_non_naws_subnegotiation_sequences():
    """
    Non-NAWS subnegotiations (any option we don't specifically handle)
    are still correctly skipped over without being treated as line
    content — NAWS itself is covered by its own dedicated tests below,
    since it's no longer just "discarded" the way this test's name might
    otherwise imply.
    """
    received = []

    async def handler(session: Session):
        line = await session.read_line()
        received.append(line)

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(6)

            # A subnegotiation for some option we don't implement at all
            # (a made-up option byte, 99) embedded in the input.
            unknown_subneg = bytes([IAC, SB, 99, 1, 2, 3, IAC, SE])
            payload = b"foo" + unknown_subneg + b"bar\r\n"
            writer.write(payload)
            await writer.drain()
            await asyncio.sleep(0.05)  # let the handler finish reading before closing
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["foobar"]


def test_naws_subnegotiation_updates_session_terminal_dimensions():
    captured = {}

    async def handler(session: Session):
        line = await session.read_line()
        captured["width"] = session.terminal_width
        captured["height"] = session.terminal_height

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(6)

            naws_subneg = bytes([IAC, SB, NAWS, 0, 132, 0, 43, IAC, SE])  # 132x43
            payload = b"foo" + naws_subneg + b"bar\r\n"
            writer.write(payload)
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert captured["width"] == 132
    assert captured["height"] == 43


def test_naws_subnegotiation_correctly_unescapes_0xff_in_body():
    """
    A terminal that's exactly 255 columns wide has a literal 0xFF byte in
    its NAWS payload, which per RFC 854 must arrive doubled (IAC IAC).
    Verifies the un-escaping in _read_subnegotiation_body actually works
    for this case, not just small width/height values that never happen
    to collide with the IAC byte value.
    """
    captured = {}

    async def handler(session: Session):
        line = await session.read_line()
        captured["width"] = session.terminal_width

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(6)

            # width=255 (0x00FF): low byte is 0xFF, which must be
            # IAC-doubled per RFC 854 when it appears as literal data —
            # i.e. two consecutive 0xFF bytes represent one literal 0xFF,
            # not three (an earlier version of this test accidentally
            # wrote three in a row here, which under-counted the parsed
            # body by one byte and made the test fail — a bug in the
            # test's manual byte construction, not in the parser; caught
            # by actually running this rather than assuming it was
            # correct).
            naws_subneg = bytes([IAC, SB, NAWS, 0x00, 0xFF, 0xFF, 0x00, 24, IAC, SE])
            payload = naws_subneg + b"x\r\n"
            writer.write(payload)
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert captured["width"] == 255


def test_naws_zero_dimension_does_not_override_default():
    """Some clients report 0 to mean "unknown" — must not overwrite the
    sane 80x24 default with a useless 0."""
    captured = {}

    async def handler(session: Session):
        line = await session.read_line()
        captured["width"] = session.terminal_width
        captured["height"] = session.terminal_height

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(6)
            writer.write(bytes([IAC, WILL, NAWS]))
            writer.write(bytes([IAC, SB, NAWS, 0, 0, 0, 0, IAC, SE]))
            writer.write(b"hello\r\n")
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert captured["width"] == 80
    assert captured["height"] == 24


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
            await reader.readexactly(6)

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
            await reader.readexactly(6)  # initial SGA + DO NAWS negotiation

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
            await reader.readexactly(6)  # consume the initial SGA + DO NAWS negotiation
            data = await reader.readline()
            assert data == b"hello world\r\n"
            assert 0xFF not in data
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_write_normalizes_internal_bare_lf_to_crlf():
    """
    Regression test for a real gap found while verifying the rendering
    framework end-to-end: netbbs.rendering.reflow correctly produces
    multi-line text using plain '\\n' internally (it's a transport-
    agnostic utility, not specific to Telnet), but Session.write_line()
    only appends '\\r\\n' once, at the end. Without normalization at the
    transport boundary, every internal line break in something like a
    reflowed post body would reach the wire as a bare LF — tolerated by
    lenient modern terminals (which auto-CR on LF) but not correct
    Telnet per RFC 854, and a risk on stricter/older clients.
    """
    async def handler(session: Session):
        await session.write_line("first line\nsecond line\nthird line")

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(6)
            data = await reader.read(1024)
            assert data == b"first line\r\nsecond line\r\nthird line\r\n"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_write_normalization_is_idempotent_for_already_crlf_text():
    """Text that already uses \\r\\n internally shouldn't end up
    double-CR'd by the normalization pass."""
    async def handler(session: Session):
        await session.write_line("first line\r\nsecond line")

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(6)
            data = await reader.read(1024)
            assert data == b"first line\r\nsecond line\r\n"
            assert b"\r\r" not in data
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

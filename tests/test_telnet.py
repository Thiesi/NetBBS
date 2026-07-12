"""
Integration tests for the Telnet transport, including server-driven
character-mode input (see module docstring in netbbs.net.telnet for why
this replaced client-side line editing).

These spin up a real `TelnetServer` on an OS-assigned loopback port and
connect a plain `asyncio.open_connection` client to it — exercising the
actual network path and byte-level IAC/character handling, rather than
mocking `StreamReader`/`StreamWriter`. The client side deliberately does
*not* implement full Telnet negotiation; it only needs to send/receive
the specific byte sequences each test cares about.
"""

from __future__ import annotations

import asyncio
import time

from netbbs.net.session import Session
from netbbs.net.telnet import DO, ECHO, IAC, NAWS, SB, SE, SUPPRESS_GO_AHEAD, WILL, WONT, TelnetServer

# The full 9-byte initial negotiation every connection now sends:
# IAC WILL SGA, IAC WILL ECHO, IAC DO NAWS, in that order.
_INITIAL_NEGOTIATION = bytes(
    [IAC, WILL, SUPPRESS_GO_AHEAD, IAC, WILL, ECHO, IAC, DO, NAWS]
)


async def _run_server(session_handler):
    server = TelnetServer(host="127.0.0.1", port=0, session_handler=session_handler)
    await server.start()
    return server


# -- initial negotiation -----------------------------------------------


def test_server_sends_full_initial_negotiation_on_connect():
    async def handler(session: Session):
        await session.write_line("done")

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            data = await reader.readexactly(9)
            assert data == _INITIAL_NEGOTIATION
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_server_runs_handler_and_sends_output():
    calls = []

    async def handler(session: Session):
        calls.append("called")
        await session.write_line("hello")

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            line = await reader.readline()
            assert line == b"hello\r\n"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert calls == ["called"]


# -- character-mode echo & Enter handling ------------------------------


def test_each_character_is_echoed_as_typed():
    received = []

    async def handler(session: Session):
        received.append(await session.read_line())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            writer.write(b"hi\r\n")
            await writer.drain()
            echoed = await reader.readexactly(4)  # 'h' 'i' '\r' '\n'
            assert echoed == b"hi\r\n"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["hi"]


def test_password_mode_masks_each_character_with_asterisk():
    received = []

    async def handler(session: Session):
        received.append(await session.read_line(echo=False))

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            writer.write(b"secret\r\n")
            await writer.drain()
            echoed = await reader.readexactly(8)  # 6 asterisks + CRLF
            assert echoed == b"******\r\n"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["secret"]


def test_crlf_pair_is_one_line_terminator_not_two():
    received = []

    async def handler(session: Session):
        received.append(await session.read_line())
        received.append(await session.read_line())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            writer.write(b"first\r\nsecond\r\n")
            await writer.drain()
            await reader.readexactly(len(b"first\r\nsecond\r\n"))
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["first", "second"]


def test_bare_cr_terminates_line_without_hanging():
    """
    Regression test for a real latent bug fixed while building character
    mode: a lone CR with nothing following it must resolve on a bounded
    timeout, not hang forever waiting for a byte that may never come.
    """
    received = []

    async def handler(session: Session):
        received.append(await session.read_line())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            writer.write(b"hi")
            await writer.drain()
            await reader.readexactly(2)
            writer.write(bytes([0x0D]))  # bare CR, nothing after it
            await writer.drain()

            start = time.monotonic()
            echoed = await asyncio.wait_for(reader.readexactly(2), timeout=2.0)
            elapsed = time.monotonic() - start
            assert echoed == b"\r\n"
            assert elapsed < 1.0, f"took too long ({elapsed}s) — did it hang?"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["hi"]


# -- Backspace / Delete --------------------------------------------------


def test_backspace_removes_last_character_and_erases_visually():
    received = []

    async def handler(session: Session):
        received.append(await session.read_line())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)

            writer.write(b"helz")
            await writer.drain()
            assert await reader.readexactly(4) == b"helz"

            writer.write(bytes([0x08]))  # Backspace
            await writer.drain()
            assert await reader.readexactly(3) == b"\b \b"

            writer.write(b"lo\r\n")
            await writer.drain()
            assert await reader.readexactly(4) == b"lo\r\n"

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["hello"]


def test_delete_byte_also_works_as_backspace():
    received = []

    async def handler(session: Session):
        received.append(await session.read_line())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)

            writer.write(b"abx")
            await writer.drain()
            assert await reader.readexactly(3) == b"abx"

            writer.write(bytes([0x7F]))  # DEL
            await writer.drain()
            assert await reader.readexactly(3) == b"\b \b"

            writer.write(b"c\r\n")
            await writer.drain()
            assert await reader.readexactly(3) == b"c\r\n"

            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["abc"]


def test_backspace_on_empty_line_does_nothing():
    received = []

    async def handler(session: Session):
        received.append(await session.read_line())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            writer.write(bytes([0x08]) + b"ok\r\n")
            await writer.drain()
            # No erase sequence should appear — just "ok\r\n".
            echoed = await reader.readexactly(4)
            assert echoed == b"ok\r\n"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["ok"]


# -- UTF-8 multi-byte characters -----------------------------------------


def test_two_byte_utf8_character_umlaut():
    received = []

    async def handler(session: Session):
        received.append(await session.read_line())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            text = "grüße"
            payload = text.encode("utf-8")
            writer.write(payload + b"\r\n")
            await writer.drain()
            echoed = await reader.readexactly(len(payload) + 2)
            assert echoed.decode("utf-8") == text + "\r\n"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["grüße"]


def test_three_byte_utf8_character():
    received = []

    async def handler(session: Session):
        received.append(await session.read_line())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            text = "€100"  # Euro sign is 3-byte UTF-8
            payload = text.encode("utf-8")
            writer.write(payload + b"\r\n")
            await writer.drain()
            echoed = await reader.readexactly(len(payload) + 2)
            assert echoed.decode("utf-8") == text + "\r\n"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["€100"]


# -- escape sequences (arrow keys etc.) ----------------------------------


def test_csi_escape_sequence_discarded_without_corrupting_line():
    received = []

    async def handler(session: Session):
        received.append(await session.read_line())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            writer.write(b"ab")
            await writer.drain()
            assert await reader.readexactly(2) == b"ab"

            writer.write(bytes([0x1B, ord("["), ord("A")]))  # up arrow, CSI form
            await writer.drain()

            writer.write(b"cd\r\n")
            await writer.drain()
            # Nothing from the arrow key should be echoed — just "cd\r\n".
            echoed = await reader.readexactly(4)
            assert echoed == b"cd\r\n"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["abcd"]


def test_ss3_escape_sequence_discarded():
    received = []

    async def handler(session: Session):
        received.append(await session.read_line())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            writer.write(b"x")
            await writer.drain()
            assert await reader.readexactly(1) == b"x"

            writer.write(bytes([0x1B, ord("O"), ord("A")]))  # up arrow, SS3 form
            await writer.drain()

            writer.write(b"y\r\n")
            await writer.drain()
            echoed = await reader.readexactly(3)
            assert echoed == b"y\r\n"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["xy"]


# -- negotiation sequences mid-input -------------------------------------


def test_negotiation_sequence_mid_input_produces_no_echo():
    received = []

    async def handler(session: Session):
        received.append(await session.read_line())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            writer.write(b"a")
            await writer.drain()
            assert await reader.readexactly(1) == b"a"

            writer.write(bytes([IAC, DO, ECHO]))  # client-initiated negotiation
            await writer.drain()
            await asyncio.sleep(0.05)

            writer.write(b"b\r\n")
            await writer.drain()
            echoed = await reader.readexactly(3)
            assert echoed == b"b\r\n"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["ab"]


def test_naws_subnegotiation_still_works_during_character_mode():
    captured = {}

    async def handler(session: Session):
        await session.read_line()
        captured["width"] = session.terminal_width
        captured["height"] = session.terminal_height

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            writer.write(bytes([IAC, WILL, NAWS]))
            writer.write(bytes([IAC, SB, NAWS, 0, 100, 0, 30, IAC, SE]))
            writer.write(b"x\r\n")
            await writer.drain()
            await reader.readexactly(3)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert captured["width"] == 100
    assert captured["height"] == 30


def test_naws_handles_width_containing_0xff_byte():
    """
    A terminal exactly 255 columns wide has a literal 0xFF byte in its
    NAWS payload, which per RFC 854 must arrive IAC-doubled. Verifies the
    un-escaping in _read_subnegotiation_body handles this correctly.
    """
    captured = {}

    async def handler(session: Session):
        await session.read_line()
        captured["width"] = session.terminal_width

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            # width=255 (0x00FF): low byte 0xFF must be doubled (two
            # consecutive 0xFF bytes represent one literal 0xFF).
            naws_subneg = bytes([IAC, SB, NAWS, 0x00, 0xFF, 0xFF, 0x00, 24, IAC, SE])
            writer.write(naws_subneg + b"x\r\n")
            await writer.drain()
            await reader.readexactly(3)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert captured["width"] == 255


def test_naws_zero_dimension_does_not_override_default():
    captured = {}

    async def handler(session: Session):
        await session.read_line()
        captured["width"] = session.terminal_width
        captured["height"] = session.terminal_height

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            writer.write(bytes([IAC, WILL, NAWS]))
            writer.write(bytes([IAC, SB, NAWS, 0, 0, 0, 0, IAC, SE]))
            writer.write(b"x\r\n")
            await writer.drain()
            await reader.readexactly(3)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert captured["width"] == 80
    assert captured["height"] == 24


# -- line length cap -------------------------------------------------------


def test_line_length_is_capped():
    """
    Characters beyond the cap are neither stored nor echoed — confirmed
    deliberately, not just "doesn't crash": echoing characters we then
    silently drop would show the user a complete line while actually
    storing a truncated one, a display/storage mismatch worse than the
    truncation itself.
    """
    received = []

    async def handler(session: Session):
        received.append(await session.read_line())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            writer.write(b"a" * 5000 + b"\r\n")
            await writer.drain()
            echoed = await reader.readexactly(4096 + 2)
            assert echoed == b"a" * 4096 + b"\r\n"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert len(received[0]) == 4096


# -- write() correctness (unchanged behavior, still verified) -------------


def test_write_never_produces_invalid_utf8_or_stray_iac():
    async def handler(session: Session):
        await session.write_line("hello world")

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            data = await reader.readline()
            assert data == b"hello world\r\n"
            assert 0xFF not in data
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_write_normalizes_internal_bare_lf_to_crlf():
    async def handler(session: Session):
        await session.write_line("first line\nsecond line\nthird line")

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            data = await reader.read(1024)
            assert data == b"first line\r\nsecond line\r\nthird line\r\n"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_write_normalization_is_idempotent_for_already_crlf_text():
    async def handler(session: Session):
        await session.write_line("first line\r\nsecond line")

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
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

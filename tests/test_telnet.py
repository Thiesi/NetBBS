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
import socket
import time

from netbbs.net.confirm import prompt_yes_no
from netbbs.net.session import Session
from netbbs.net.telnet import (
    BINARY,
    DO,
    ECHO,
    IAC,
    NAWS,
    NEW_ENVIRON,
    NEW_ENVIRON_IS,
    NEW_ENVIRON_SEND,
    NEW_ENVIRON_VALUE,
    NEW_ENVIRON_VAR,
    SB,
    SE,
    SUPPRESS_GO_AHEAD,
    WILL,
    WONT,
    TelnetServer,
    TelnetSession,
)

# The full 9-byte initial negotiation every connection now sends:
# IAC WILL SGA, IAC WILL ECHO, IAC DO NAWS, in that order.
_INITIAL_NEGOTIATION = bytes(
    [IAC, WILL, SUPPRESS_GO_AHEAD, IAC, WILL, ECHO, IAC, DO, NAWS]
)

# Immediately follows _INITIAL_NEGOTIATION: IAC DO NEW-ENVIRON, then a
# subnegotiation requesting just the COLORTERM variable (SEND VAR
# "COLORTERM"), for truecolor detection.
_NEW_ENVIRON_REQUEST = bytes([IAC, DO, NEW_ENVIRON]) + bytes(
    [IAC, SB, NEW_ENVIRON, NEW_ENVIRON_SEND, NEW_ENVIRON_VAR]
) + b"COLORTERM" + bytes([IAC, SE])

# Immediately follows _NEW_ENVIRON_REQUEST: IAC WILL BINARY, IAC DO
# BINARY (RFC 856) -- see negotiate_initial_options's docstring for why
# this matters for non-ASCII input (issue #152).
_BINARY_REQUEST = bytes([IAC, WILL, BINARY, IAC, DO, BINARY])

# Every connection now sends _INITIAL_NEGOTIATION followed immediately by
# _NEW_ENVIRON_REQUEST and then _BINARY_REQUEST -- tests that don't care
# about the negotiation bytes themselves (the overwhelming majority) skip
# past all three as one chunk before reading application-level data.
_FULL_NEGOTIATION_LEN = (
    len(_INITIAL_NEGOTIATION) + len(_NEW_ENVIRON_REQUEST) + len(_BINARY_REQUEST)
)


async def skip_initial_negotiation(reader: asyncio.StreamReader) -> None:
    """Consume exactly the bytes `TelnetSession.negotiate_initial_options()`
    sends on every connection, for the overwhelming majority of real-socket
    integration tests (here and in other test modules) that don't care about
    the negotiation bytes themselves, only about getting past them before
    asserting on application-level data.

    Issue #105: adding NEW-ENVIRON negotiation for truecolor detection broke
    roughly 70 tests that each hardcoded their own now-stale byte count
    (`readexactly(9)`, the length before that addition). Routing every such
    call through this one function means the next legitimate negotiation
    addition only requires updating `_FULL_NEGOTIATION_LEN` above, not
    re-auditing every integration test file for its own magic number."""
    await reader.readexactly(_FULL_NEGOTIATION_LEN)


async def _run_server(session_handler):
    server = TelnetServer(host="127.0.0.1", port=0, session_handler=session_handler)
    await server.start()
    return server


def test_single_key_confirmation_rejects_invalid_input_and_ends_its_row():
    """The Telnet byte decoder preserves Enter through the structured-key
    path while the confirmation helper owns echo/newline rendering."""
    results = []

    async def handler(session: Session):
        results.append(await prompt_yes_no(session, "Confirm?", default=False))
        results.append(await prompt_yes_no(session, "Again?", default=True))
        await session.write_line("NEXT")

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            prompt = await reader.readuntil(b": ")
            # The CR after Y models a caller's habitual "Y then Enter".
            # It belongs to the first response and must not select the
            # second prompt's True default; the following N answers that.
            writer.write(b"xY\rN")
            await writer.drain()
            remainder = await reader.readuntil(b"NEXT\r\n")
            writer.close()
            await writer.wait_closed()
            return prompt + remainder
        finally:
            await server.stop()

    output = asyncio.run(scenario())
    assert results == [True, False]
    assert output == (
        b"Confirm? \x1b[1m\x1b[38;5;75m[\x1b[0my/\x1b[1m\x1b[38;5;46mN\x1b[0m\x1b[1m\x1b[38;5;75m]\x1b[0m: \x07Y\r\n"
        b"Again? \x1b[1m\x1b[38;5;75m[\x1b[0m\x1b[1m\x1b[38;5;46mY\x1b[0m/n\x1b[1m\x1b[38;5;75m]\x1b[0m: N\r\nNEXT\r\n"
    )


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
            await skip_initial_negotiation(reader)
            line = await reader.readline()
            assert line == b"hello\r\n"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert calls == ["called"]


def test_accepted_connections_have_tcp_nodelay_set():
    """
    Nagle's algorithm is on by default and, left on, is a well-known
    source of interactive small-write traffic (a single echoed
    character, a bare bell) not going out promptly on a real client --
    exactly the traffic shape this server-driven character-mode
    transport produces on every keystroke. Regression coverage for
    that fix: every accepted connection's socket must have
    `TCP_NODELAY` enabled.
    """
    seen_nodelay = []

    async def handler(session: Session):
        sock = session._writer.get_extra_info("socket")
        seen_nodelay.append(sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY))

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            await asyncio.sleep(0.05)  # let the handler run and record the option
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    # Nonzero, not `== [1]`: a platform's getsockopt is free to return
    # whatever nonzero representation it stores a boolean option as --
    # confirmed on Thiesi's real NetBSD deployment target, where this
    # came back as `4`, not `1`. Only "was it actually enabled" matters.
    assert seen_nodelay and all(value != 0 for value in seen_nodelay)


# -- character-mode echo & Enter handling ------------------------------


def test_each_character_is_echoed_as_typed():
    received = []

    async def handler(session: Session):
        received.append(await session.read_line())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
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
            await skip_initial_negotiation(reader)
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
            await skip_initial_negotiation(reader)
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
            await skip_initial_negotiation(reader)
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
            await skip_initial_negotiation(reader)

            writer.write(b"helz")
            await writer.drain()
            assert await reader.readexactly(4) == b"helz"

            writer.write(bytes([0x08]))  # Backspace
            await writer.drain()
            # Cursor-addressable editing erases via move-left + ESC[K
            # rather than the old "\b \b"
            # trick, since the same redraw primitive also has to work
            # for a Backspace in the *middle* of a line, not just at
            # the end.
            assert await reader.readexactly(7) == b"\x1b[1D\x1b[K"

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
            await skip_initial_negotiation(reader)

            writer.write(b"abx")
            await writer.drain()
            assert await reader.readexactly(3) == b"abx"

            writer.write(bytes([0x7F]))  # DEL
            await writer.drain()
            assert await reader.readexactly(7) == b"\x1b[1D\x1b[K"

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
            await skip_initial_negotiation(reader)
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
            await skip_initial_negotiation(reader)
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
            await skip_initial_negotiation(reader)
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
            await skip_initial_negotiation(reader)
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
            await skip_initial_negotiation(reader)
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
            await skip_initial_negotiation(reader)
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
            await skip_initial_negotiation(reader)
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
            await skip_initial_negotiation(reader)
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


def test_naws_maximum_16_bit_size_is_clamped():
    """Regression test for GitHub issue #33: NAWS width/height are each
    16-bit values (max 65535), which alone would already force a
    ScreenBuffer allocation of well over 4 billion cells -- clamped to
    the shared sane ceiling before ever reaching terminal_width/height."""
    captured = {}

    async def handler(session: Session):
        await session.read_line()
        captured["width"] = session.terminal_width
        captured["height"] = session.terminal_height

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            # 0xFFFF for both width and height -- each byte doubled per
            # NAWS's IAC-escaping rule (0xFF is the IAC byte itself).
            writer.write(bytes([IAC, SB, NAWS, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, IAC, SE]))
            writer.write(b"x\r\n")
            await writer.drain()
            await reader.readexactly(3)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert captured["width"] <= 500
    assert captured["height"] <= 200


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
            await skip_initial_negotiation(reader)
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


# -- NEW-ENVIRON / truecolor detection --------------------------------------


def test_server_requests_colorterm_via_new_environ_after_naws():
    async def handler(session: Session):
        await session.write_line("done")

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            data = await reader.readexactly(len(_NEW_ENVIRON_REQUEST))
            assert data == _NEW_ENVIRON_REQUEST
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_server_requests_binary_transmission_after_new_environ():
    async def handler(session: Session):
        await session.write_line("done")

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            await reader.readexactly(len(_NEW_ENVIRON_REQUEST))
            data = await reader.readexactly(len(_BINARY_REQUEST))
            assert data == _BINARY_REQUEST
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())


def _new_environ_scenario(colorterm_value: bytes | None):
    captured = {}

    async def handler(session: Session):
        await session.read_line()
        captured["supports_truecolor"] = session.supports_truecolor
        captured["diagnostic"] = session.truecolor_diagnostic

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            await reader.readexactly(len(_NEW_ENVIRON_REQUEST))
            if colorterm_value is not None:
                body = (
                    bytes([NEW_ENVIRON_IS, NEW_ENVIRON_VAR])
                    + b"COLORTERM"
                    + bytes([NEW_ENVIRON_VALUE])
                    + colorterm_value
                )
                writer.write(bytes([IAC, SB, NEW_ENVIRON]) + body + bytes([IAC, SE]))
            writer.write(b"x\r\n")
            await writer.drain()
            await reader.readexactly(3)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    return captured["supports_truecolor"], captured["diagnostic"]


def test_new_environ_is_with_colorterm_truecolor_sets_supports_truecolor():
    assert _new_environ_scenario(b"truecolor") == (
        True, "Telnet NEW-ENVIRON reported COLORTERM=truecolor; truecolor available"
    )


def test_new_environ_is_with_colorterm_24bit_sets_supports_truecolor():
    assert _new_environ_scenario(b"24bit") == (
        True, "Telnet NEW-ENVIRON reported COLORTERM=24bit; truecolor available"
    )


def test_new_environ_is_with_other_colorterm_value_leaves_default():
    assert _new_environ_scenario(b"256color") == (
        False, "Telnet NEW-ENVIRON reported COLORTERM=256color; using 256-color"
    )


def test_no_new_environ_reply_leaves_default():
    supported, diagnostic = _new_environ_scenario(None)
    assert supported is False
    assert "negotiation pending" in diagnostic


def test_malformed_new_environ_subnegotiation_does_not_raise():
    captured = {}

    async def handler(session: Session):
        await session.read_line()
        captured["supports_truecolor"] = session.supports_truecolor

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            await reader.readexactly(len(_NEW_ENVIRON_REQUEST))
            # Garbage body, not even starting with an IS marker.
            writer.write(bytes([IAC, SB, NEW_ENVIRON, 99, 99, 99, IAC, SE]))
            writer.write(b"x\r\n")
            await writer.drain()
            await reader.readexactly(3)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert captured["supports_truecolor"] is False


# -- line length cap -------------------------------------------------------


def test_line_length_is_capped():
    """
    Characters beyond the cap are neither stored nor echoed as
    themselves — confirmed deliberately, not just "doesn't crash":
    echoing characters we then silently drop would show the user a
    complete line while actually storing a truncated one, a
    display/storage mismatch worse than the truncation itself.

    A single bell (`\\a`) *is* echoed the moment the cap is first hit
    (dogfood follow-up: silent truncation gave zero indication anything
    was lost) -- distinct from echoing the dropped character itself, so
    it doesn't reintroduce the display/storage mismatch this test
    guards against; every character after the first over the cap is
    still dropped with no further echo of any kind.
    """
    received = []

    async def handler(session: Session):
        received.append(await session.read_line())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            writer.write(b"a" * 5000 + b"\r\n")
            await writer.drain()
            echoed = await reader.readexactly(4096 + 1 + 2)
            assert echoed == b"a" * 4096 + b"\a" + b"\r\n"
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
            await skip_initial_negotiation(reader)
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
            await skip_initial_negotiation(reader)
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
            await skip_initial_negotiation(reader)
            data = await reader.read(1024)
            assert data == b"first line\r\nsecond line\r\n"
            assert b"\r\r" not in data
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())


# -- read_key: immediate single-keystroke dispatch (no Enter needed) ------


def test_read_key_returns_immediately_no_enter_needed():
    received = []

    async def handler(session: Session):
        received.append(await session.read_key())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            writer.write(b"b")  # deliberately no Enter/CR/LF sent at all
            await writer.drain()
            assert await reader.readexactly(1) == b"b"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["b"]


def test_read_key_skips_enter_bytes():
    """
    Junk control bytes (CR, LF) sent before a real key press are
    silently skipped, not returned as "the key" — Enter has no special
    meaning when already responding to the very next keystroke. (0x08/
    Backspace used to be part of this same "junk, skip it" set, but
    issue #150 gave it real meaning -- see HELP_KEY's own tests in
    tests/test_char_input.py and tests/test_telnet.py's own
    test_read_key_returns_help_key_for_backspace_byte below.)
    """
    received = []

    async def handler(session: Session):
        received.append(await session.read_key())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            writer.write(bytes([0x0D, 0x0A]))
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.write(b"q")
            await writer.drain()
            assert await reader.readexactly(1) == b"q"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["q"]


def test_read_key_returns_help_key_for_backspace_byte():
    """Issue #150: 0x08 (Backspace) is repurposed as HELP_KEY at this
    single-keystroke layer, unechoed -- see char_input.HELP_KEY's own
    docstring for why this is safe specifically here."""
    from netbbs.net.char_input import HELP_KEY

    received = []

    async def handler(session: Session):
        received.append(await session.read_key())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            writer.write(bytes([0x08]))
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == [HELP_KEY]


def test_read_key_echo_false_masks_with_asterisk():
    """
    Regression test for a real bug caught while building this: the first
    implementation only echoed when echo=True and wrote nothing at all
    for echo=False, instead of masking with '*' the way read_line does.
    Caught by actually running this exact test before it was formalized.
    """
    received = []

    async def handler(session: Session):
        received.append(await session.read_key(echo=False))

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            writer.write(b"x")
            await writer.drain()
            echoed = await reader.readexactly(1)
            assert echoed == b"*"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["x"]


def test_read_key_ignores_negotiation_sequences():
    received = []

    async def handler(session: Session):
        received.append(await session.read_key())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            writer.write(bytes([IAC, DO, ECHO]))
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.write(b"z")
            await writer.drain()
            assert await reader.readexactly(1) == b"z"
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["z"]


def test_concurrent_writes_from_two_tasks_do_not_interleave_bytes():
    """
    Stress test backing the concurrency-safety claim documented in
    netbbs.net.chat_flow._chat_loop (two asyncio tasks — send_loop and
    receive_loop — both call session.write()/write_line() on the same
    connection). Confirms TelnetSession.write()'s single synchronous
    self._writer.write() call before any await means one logical message
    can never be interleaved mid-write by another concurrently-running
    task, only reordered relative to it — verified here, not assumed.
    """
    async def handler(session: Session):
        async def writer_a():
            for _ in range(20):
                await session.write_line("A" * 50)

        async def writer_b():
            for _ in range(20):
                await session.write_line("B" * 50)

        await asyncio.gather(writer_a(), writer_b())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            chunks = []
            try:
                while True:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=0.5)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except asyncio.TimeoutError:
                pass
            data = b"".join(chunks)
            lines = [line for line in data.decode().split("\r\n") if line]
            assert len(lines) == 40
            assert all(line == "A" * 50 or line == "B" * 50 for line in lines)
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


# -- raw byte I/O (netbbs.net.zmodem's transport, not character-mode) --


def test_write_raw_doubles_literal_iac_bytes_per_rfc_854():
    """write_raw carries arbitrary binary data (netbbs.net.zmodem's
    actual use), unlike write()'s UTF-8 text -- a literal 0xFF byte can
    genuinely appear and must be doubled so a real Telnet client doesn't
    misinterpret it as an IAC command byte."""

    async def handler(session: Session):
        await session.write_raw(bytes([0x01, 0xFF, 0x02, 0xFF, 0xFF, 0x03]))

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)  # initial negotiation
            data = await reader.readexactly(9)  # 6 bytes + 3 doubled IACs
            assert data == bytes([0x01, IAC, IAC, 0x02, IAC, IAC, IAC, IAC, 0x03])
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_read_byte_undoubles_iac_and_roundtrips_all_byte_values():
    """The receiving half of the same RFC 854 rule, exercised through
    every byte value 0-255 (not just 0xFF) -- read_byte is the same
    primitive netbbs.net.zmodem reads raw protocol/file bytes with."""
    received = []

    async def handler(session: Session):
        for _ in range(256):
            received.append(await session.read_byte())

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await skip_initial_negotiation(reader)
            payload = bytes(range(256))
            escaped = payload.replace(bytes([IAC]), bytes([IAC, IAC]))
            writer.write(escaped)
            await writer.drain()
            await asyncio.sleep(0.3)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == list(range(256))


# -- shutdown with lingering connections ------------------------------------


def test_stop_aborts_a_connection_the_client_never_closes():
    """Same shutdown hang class the SSH listener had (see
    tests/test_ssh.py's sibling): `Server.close()` leaves admitted
    connections open and Python 3.12+'s `wait_closed()` waits for every
    one of them, so a client that never disconnects -- a dead peer, or
    one still in option negotiation that never reached the session
    registry -- held shutdown indefinitely. Without the bounded wait +
    `abort()` this test hangs (bounded here only by the outer
    `wait_for`)."""
    release = asyncio.Event()

    async def handler(session: Session):
        await release.wait()

    async def scenario():
        server = TelnetServer(
            host="127.0.0.1", port=0, session_handler=handler, stop_timeout_seconds=0.3
        )
        await server.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        try:
            await skip_initial_negotiation(reader)
            started = time.monotonic()
            await asyncio.wait_for(server.stop(), timeout=5)
            elapsed = time.monotonic() - started
            # The server dropped the transport: the client reads EOF (or
            # a reset, depending on the platform) without having closed.
            try:
                remainder = await asyncio.wait_for(reader.read(), timeout=2)
            except ConnectionError:
                remainder = b""
            assert remainder == b""
            return elapsed
        finally:
            release.set()
            writer.close()

    elapsed = asyncio.run(scenario())
    assert elapsed < 3


def test_session_close_aborts_a_transport_that_never_drains():
    """`StreamWriter.close()` flushes buffered output first, and a peer
    whose network silently vanished never ACKs, so `wait_closed()` on
    such a writer lasts the kernel's TCP retransmission timeout. Shutdown
    gathers every session's close, so one such peer stalled the node."""
    events = []

    class _Transport:
        def abort(self):
            events.append("abort")

    class _StuckWriter:
        transport = _Transport()

        def is_closing(self):
            return False

        def close(self):
            events.append("close")

        async def wait_closed(self):
            await asyncio.Event().wait()

    async def scenario():
        session = TelnetSession(
            asyncio.StreamReader(), _StuckWriter(), close_timeout_seconds=0.05
        )
        await asyncio.wait_for(session.close(), timeout=2)

    asyncio.run(scenario())
    assert events == ["close", "abort"]

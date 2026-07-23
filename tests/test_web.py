"""
Integration tests for the web transport.

These spin up a real `WebServer` on an OS-assigned loopback port and
connect a real `aiohttp` client websocket to it — exercising the actual
HTTP/websocket path and the structured JSON wire protocol, rather than
mocking anything.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from netbbs.net.session import Session, SessionClosedError
from netbbs.net.web import WebServer


async def _run_server(session_handler):
    server = WebServer(host="127.0.0.1", port=0, session_handler=session_handler)
    await server.start()
    return server


def test_session_handler_is_called_on_connect():
    calls = []

    async def handler(session: Session):
        calls.append("called")

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    await ws.close()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert calls == ["called"]


def test_write_sends_output_message():
    async def handler(session: Session):
        await session.write_line("hello")

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    msg = await ws.receive_json(timeout=2)
                    assert msg == {"type": "output", "data": "hello\r\n"}
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_write_normalizes_bare_lf_to_crlf():
    async def handler(session: Session):
        await session.write("first\nsecond")

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    msg = await ws.receive_json(timeout=2)
                    assert msg == {"type": "output", "data": "first\r\nsecond"}
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_read_line_returns_typed_text_and_echoes_it():
    received = []

    async def handler(session: Session):
        received.append(await session.read_line())

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    await ws.send_json({"type": "key", "data": "hi\r"})
                    echoed = []
                    for _ in range(3):
                        echoed.append(await ws.receive_json(timeout=2))
                    assert echoed == [
                        {"type": "output", "data": "h"},
                        {"type": "output", "data": "i"},
                        {"type": "output", "data": "\r\n"},
                    ]
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["hi"]


def test_read_line_echo_false_masks_with_asterisk():
    async def handler(session: Session):
        await session.read_line(echo=False)

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    await ws.send_json({"type": "key", "data": "x\r"})
                    first = await ws.receive_json(timeout=2)
                    assert first == {"type": "output", "data": "*"}
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_backspace_removes_last_character():
    received = []

    async def handler(session: Session):
        received.append(await session.read_line())

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    await ws.send_json({"type": "key", "data": "abc\x7f\r"})
                    # "abc" echoed, then a backspace erase sequence, then
                    # the line terminator -- five output messages total,
                    # not asserted individually here (only the final
                    # read_line() result matters for this test).
                    for _ in range(5):
                        await ws.receive_json(timeout=2)
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["ab"]


def test_read_key_returns_immediately_no_enter_needed():
    received = []

    async def handler(session: Session):
        received.append(await session.read_key())

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    await ws.send_json({"type": "key", "data": "q"})
                    await ws.receive_json(timeout=2)
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["q"]


def test_resize_updates_terminal_dimensions():
    sizes = []

    async def handler(session: Session):
        await session.read_key()
        sizes.append((session.terminal_width, session.terminal_height))

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    await ws.send_json({"type": "resize", "cols": 120, "rows": 40})
                    await asyncio.sleep(0.1)
                    await ws.send_json({"type": "key", "data": "x"})
                    await ws.receive_json(timeout=2)
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert sizes == [(120, 40)]


def test_resize_with_an_absurd_size_is_clamped():
    """Regression test for GitHub issue #33: unlike Telnet NAWS (16-bit)
    or SSH's PTY window-size channel, this transport accepts any JSON
    integer here -- a malicious client can report a value neither of
    the other two protocols could even encode."""
    sizes = []

    async def handler(session: Session):
        await session.read_key()
        sizes.append((session.terminal_width, session.terminal_height))

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    await ws.send_json({"type": "resize", "cols": 10_000_000, "rows": 10_000_000})
                    await asyncio.sleep(0.1)
                    await ws.send_json({"type": "key", "data": "x"})
                    await ws.receive_json(timeout=2)
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert sizes[0][0] <= 500
    assert sizes[0][1] <= 200


def test_resize_with_boolean_values_is_ignored():
    """bool is an int subclass in Python -- `true`/`false` in the JSON
    payload decode to Python's True/False, which must not be treated as
    real dimensions (GitHub issue #33)."""
    sizes = []

    async def handler(session: Session):
        await session.read_key()
        sizes.append((session.terminal_width, session.terminal_height))

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    await ws.send_json({"type": "resize", "cols": True, "rows": False})
                    await asyncio.sleep(0.1)
                    await ws.send_json({"type": "key", "data": "x"})
                    await ws.receive_json(timeout=2)
        finally:
            await server.stop()

    asyncio.run(scenario())
    # Neither boolean was accepted -- dimensions stay at the untouched default.
    assert sizes == [(80, 24)]


def test_escape_sequence_is_stripped_from_typed_line():
    received = []

    async def handler(session: Session):
        received.append(await session.read_line())

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    # An up-arrow CSI sequence arriving mid-line shouldn't
                    # corrupt it -- xterm.js delivers this as one onData
                    # event, forwarded here as one "key" message.
                    await ws.send_json({"type": "key", "data": "ab\x1b[Ac\r"})
                    for _ in range(4):
                        await ws.receive_json(timeout=2)
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["abc"]


def test_disconnect_mid_read_raises_session_closed_error():
    outcomes = []

    async def handler(session: Session):
        try:
            await session.read_line()
            outcomes.append("no exception")
        except SessionClosedError:
            outcomes.append("closed")

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                ws = await client.ws_connect(f"http://127.0.0.1:{server.port}/ws")
                await ws.send_json({"type": "key", "data": "a"})
                await asyncio.sleep(0.2)
                await ws.close()
                await asyncio.sleep(0.3)
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert outcomes == ["closed"]


def test_write_raw_raises_not_implemented():
    outcomes = []

    async def handler(session: Session):
        try:
            await session.write_raw(b"data")
        except NotImplementedError:
            outcomes.append("raised")

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws"):
                    await asyncio.sleep(0.2)
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert outcomes == ["raised"]


def test_read_byte_raises_not_implemented():
    outcomes = []

    async def handler(session: Session):
        try:
            await session.read_byte()
        except NotImplementedError:
            outcomes.append("raised")

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws"):
                    await asyncio.sleep(0.2)
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert outcomes == ["raised"]


def test_index_page_is_served():
    async def handler(session: Session):
        pass

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.get(f"http://127.0.0.1:{server.port}/") as resp:
                    assert resp.status == 200
                    body = await resp.text()
                    assert "netbbs-terminal.js" in body
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_static_assets_are_served():
    async def handler(session: Session):
        pass

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.get(f"http://127.0.0.1:{server.port}/static/xterm.js") as resp:
                    assert resp.status == 200
                async with client.get(
                    f"http://127.0.0.1:{server.port}/static/netbbs-terminal.js"
                ) as resp:
                    assert resp.status == 200
        finally:
            await server.stop()

    asyncio.run(scenario())


def test_server_port_property_before_start_raises():
    async def handler(session: Session):
        pass

    server = WebServer(host="127.0.0.1", port=0, session_handler=handler)
    with pytest.raises(RuntimeError):
        _ = server.port

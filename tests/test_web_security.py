"""Security and resource-bound regression tests for the web transport."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from netbbs.net import web as web_transport
from netbbs.net.session import Session, SessionClosedError
from netbbs.net.web import WebServer


async def _run_server(session_handler, *, allowed_origins=None):
    server = WebServer(
        host="127.0.0.1",
        port=0,
        session_handler=session_handler,
        allowed_origins=allowed_origins,
    )
    await server.start()
    return server


def test_same_host_origin_is_accepted():
    calls = []

    async def handler(session: Session):
        calls.append("called")

    async def scenario():
        server = await _run_server(handler)
        try:
            url = f"http://127.0.0.1:{server.port}/ws"
            origin = f"http://127.0.0.1:{server.port}"
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(url, origin=origin):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert calls == ["called"]


def test_foreign_origin_is_rejected_before_session_handler_runs():
    calls = []

    async def handler(session: Session):
        calls.append("called")

    async def scenario():
        server = await _run_server(handler)
        try:
            url = f"http://127.0.0.1:{server.port}/ws"
            async with aiohttp.ClientSession() as client:
                with pytest.raises(aiohttp.WSServerHandshakeError) as exc_info:
                    await client.ws_connect(url, origin="https://attacker.example")
                assert exc_info.value.status == 403
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert calls == []


def test_explicit_origin_allowlist_supports_reverse_proxy_origin():
    calls = []
    public_origin = "https://bbs.example"

    async def handler(session: Session):
        calls.append("called")

    async def scenario():
        server = await _run_server(handler, allowed_origins={public_origin})
        try:
            url = f"http://127.0.0.1:{server.port}/ws"
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(url, origin=public_origin):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert calls == ["called"]


def test_absent_origin_is_deliberately_allowed_for_non_browser_clients():
    calls = []

    async def handler(session: Session):
        calls.append("called")

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws"):
                    pass
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert calls == ["called"]


def test_oversized_key_event_closes_session_predictably(monkeypatch):
    outcomes = []

    async def handler(session: Session):
        try:
            await session.read_line()
        except SessionClosedError as exc:
            outcomes.append(str(exc))

    async def scenario():
        monkeypatch.setattr(web_transport, "_MAX_KEY_EVENT_LENGTH", 8)
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    await ws.send_json({"type": "key", "data": "x" * 9})
                    message = await ws.receive(timeout=2)
                    assert message.type in {
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                    }
                    assert ws.close_code == 1009
            await asyncio.sleep(0.05)
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert outcomes == ["web terminal key event is too large"]


def test_repeated_input_closes_when_character_queue_saturates(monkeypatch):
    async def handler(session: Session):
        # Deliberately do not consume input, modelling a client flooding while
        # the application is busy elsewhere.
        await asyncio.sleep(1)

    async def scenario():
        monkeypatch.setattr(web_transport, "_MAX_QUEUED_CHARS", 4)
        monkeypatch.setattr(web_transport, "_MAX_KEY_EVENT_LENGTH", 4)
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    await ws.send_json({"type": "key", "data": "abcd"})
                    await ws.send_json({"type": "key", "data": "e"})
                    message = await ws.receive(timeout=2)
                    assert message.type in {
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                    }
                    assert ws.close_code == 1009
        finally:
            await server.stop()

    asyncio.run(scenario())

"""
Real-socket verification that wrapping `TelnetSession.read_line()` in an
outer `asyncio.wait_for` (as `netbbs.net.login_flow._login` now does for
issue #3's unauthenticated idle timeout) is reliable.

Not redundant with tests/test_login_throttling.py's idle-timeout tests,
which use a `FakeSession` and therefore can't exercise this: this
this test suite's own sign-off note records a real, empirically-found
asyncio bug where a *nested* `asyncio.wait_for` (an outer one wrapping a
call chain that itself uses `asyncio.wait_for` internally --
`TelnetSession.read_byte_with_timeout` does, for CSI escape-sequence
handling) intermittently misbehaved when both timeouts were tuned to
fire around the same narrow window. The margin here (a 60s-scale idle
timeout vs. sub-second internal timeouts) should make that race
irrelevant in practice, but "should, by reasoning" is exactly the
standard this project's CLAUDE.md says isn't good enough on its own --
this test suite actually exercises it, including the specific case that
triggers TelnetSession's internal wait_for (an arrow-key escape
sequence) while the outer idle-timeout wait_for is also armed.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.net.session import Session, SessionClosedError
from netbbs.net.telnet import TelnetServer


async def _run_server(session_handler):
    server = TelnetServer(host="127.0.0.1", port=0, session_handler=session_handler)
    await server.start()
    return server


def test_outer_timeout_fires_when_client_sends_nothing():
    outcomes = []

    async def handler(session: Session):
        try:
            await asyncio.wait_for(session.read_line(), timeout=0.2)
        except asyncio.TimeoutError:
            outcomes.append("timed out")
        except SessionClosedError:
            outcomes.append("closed")

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)  # initial negotiation
            await asyncio.sleep(0.5)  # send nothing -- let the idle timeout fire
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert outcomes == ["timed out"]


def test_outer_timeout_does_not_fire_for_normal_typing():
    results = []

    async def handler(session: Session):
        try:
            line = await asyncio.wait_for(session.read_line(), timeout=2.0)
            results.append(line)
        except asyncio.TimeoutError:
            results.append("TIMED OUT")

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            writer.write(b"alice\r\n")
            await writer.drain()
            await asyncio.sleep(0.2)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert results == ["alice"]


@pytest.mark.parametrize("run", range(5))
def test_outer_timeout_survives_an_escape_sequence_mid_read(run):
    """
    The specific scenario that stresses nested asyncio.wait_for: the
    client sends an up-arrow (CSI) escape sequence, which internally
    drives TelnetSession.read_byte_with_timeout's own asyncio.wait_for
    calls (netbbs.net.char_input._discard_escape_sequence), *while* this
    test's own outer asyncio.wait_for around the whole read_line() call
    is also armed. Repeated 5x (parametrized, not looped, so pytest -v
    shows each run and a flaky one doesn't get silently averaged away)
    to catch intermittent failures the way an empirically-found nested-wait_for
    bug originally surfaced -- only visible under repetition.
    """
    results = []

    async def handler(session: Session):
        try:
            line = await asyncio.wait_for(session.read_line(), timeout=2.0)
            results.append(line)
        except asyncio.TimeoutError:
            results.append("TIMED OUT")
        except SessionClosedError as exc:
            results.append(f"CLOSED: {exc}")

    async def scenario():
        server = await _run_server(handler)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            await reader.readexactly(9)
            # "a" + up-arrow (ESC [ A, discarded without corrupting the
            # line, per netbbs.net.char_input) + "b" + Enter.
            writer.write(b"a\x1b[Ab\r\n")
            await writer.drain()
            await asyncio.sleep(0.2)
            writer.close()
            await writer.wait_closed()
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert results == ["ab"]

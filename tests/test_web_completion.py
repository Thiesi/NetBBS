"""
Integration tests for Tab completion on the web transport (design doc
round 49/Track 5g) — `WebSession`'s own parallel implementation of the
same behavior `tests/test_char_input_completion.py` already proves for
Telnet/SSH, driven here through a real websocket connection. Reuses
`netbbs.net.char_input.apply_tab_completion` directly (see that
module), so this mostly confirms the wiring (Tab byte recognized,
completer invoked, result reaches the client) rather than re-proving
the completion arithmetic itself.
"""

from __future__ import annotations

import asyncio

import aiohttp

from netbbs.net.session import Session
from netbbs.net.web import WebServer

_TAB = "\t"


async def _run_server(session_handler):
    server = WebServer(host="127.0.0.1", port=0, session_handler=session_handler)
    await server.start()
    return server


def _static(candidates: list[str]):
    def completer(text: str) -> list[str]:
        word = text.rsplit(" ", 1)[-1]
        return [c for c in candidates if c.lower().startswith(word.lower())]

    return completer


def _read_line_result(data: str, completer) -> str:
    received = []

    async def handler(session: Session):
        received.append(await session.read_line(completer=completer))

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    await ws.send_json({"type": "key", "data": data})
                    while True:
                        msg = await ws.receive_json(timeout=2)
                        if msg["data"].endswith("\r\n"):
                            break
        finally:
            await server.stop()

    asyncio.run(scenario())
    return received[0]


def test_tab_with_no_completer_is_a_no_op():
    assert _read_line_result("ab" + _TAB + "c\r", completer=None) == "abc"


def test_single_candidate_replaces_the_current_word():
    assert _read_line_result("al" + _TAB + "\r", _static(["alice", "bob"])) == "alice "


def test_zero_candidates_does_nothing():
    assert _read_line_result("zz" + _TAB + "\r", _static(["alice", "bob"])) == "zz"


def test_multiple_candidates_extend_to_the_shared_prefix():
    result = _read_line_result("a" + _TAB + "\r", _static(["alice", "alicia"]))
    assert result == "alic"


def test_completion_only_replaces_the_last_word():
    result = _read_line_result("/msg al" + _TAB + "\r", _static(["alice"]))
    assert result == "/msg alice "

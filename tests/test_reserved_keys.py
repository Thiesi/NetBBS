"""The keys that work everywhere (issue #1158, design doc §3.5): a hotkey
read turns Esc into Back, PgUp/Left and PgDn/Right into `<` and `>`, and
F1 and `?` into help, on Telnet/SSH (`char_input.read_key`) and in the
browser terminal alike, so every menu that answers `b`, `<`, `>` or Ctrl-H
answers them too."""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from netbbs.net.char_input import BACK_KEY, HELP_KEY, NEXT_PAGE_KEY, PREVIOUS_PAGE_KEY, read_key
from netbbs.net.session import Session
from netbbs.net.web import _CLICK_KEY
from netbbs.rendering.menu import highlight_hotkeys
from tests.test_backspace_not_help import SYNCTERM, XTERM, _Terminal
from tests.test_char_input import Writer
from tests.test_web import _run_server


def _key(data: bytes, terminal_types=XTERM) -> str:
    async def scenario():
        return await read_key(_Terminal(data, terminal_types=terminal_types), Writer())

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    "data, expected",
    [
        (b"\x1b", BACK_KEY),            # a bare Esc
        (b"\x1b[5~", PREVIOUS_PAGE_KEY),  # PgUp
        (b"\x1b[D", PREVIOUS_PAGE_KEY),   # Left
        (b"\x1bOD", PREVIOUS_PAGE_KEY),   # Left, application cursor mode
        (b"\x1b[6~", NEXT_PAGE_KEY),      # PgDn
        (b"\x1b[C", NEXT_PAGE_KEY),       # Right
        (b"\x1b[11~", HELP_KEY),          # F1
        (b"\x1bOP", HELP_KEY),            # F1, VT220 style
        (b"?", HELP_KEY),
        (b"\x1b[1;5Ax", "x"),             # Ctrl+Up means nothing; the next key counts
        (b"\x1b[Ax", "x"),                # Up means nothing on a menu
    ],
)
def test_a_hotkey_read_maps_the_keys_that_work_everywhere(data, expected):
    assert _key(data) == expected


def test_syncterms_page_keys_page_too():
    assert _key(b"\x1b[V", SYNCTERM) == PREVIOUS_PAGE_KEY
    assert _key(b"\x1b[U", SYNCTERM) == NEXT_PAGE_KEY


@pytest.mark.parametrize(
    "data, expected",
    [
        ("\x1b", BACK_KEY),
        ("\x1b[5~", PREVIOUS_PAGE_KEY),
        ("\x1b[D", PREVIOUS_PAGE_KEY),
        ("\x1b[6~", NEXT_PAGE_KEY),
        ("\x1b[C", NEXT_PAGE_KEY),
        ("\x1b[11~", HELP_KEY),
        ("\x1bOP", HELP_KEY),
        ("?", HELP_KEY),
        ("q", "q"),
    ],
)
def test_the_browser_terminal_maps_them_the_same_way(data, expected):
    received = []

    async def handler(session: Session):
        received.append(await session.read_key())
        await session.write_line("done")

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    await ws.send_json({"type": "key", "data": data})
                    while True:
                        message = await ws.receive_json(timeout=2)
                        if "done" in message.get("data", ""):
                            break
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == [expected]


@pytest.mark.parametrize("key", ["<", ">", "/", "?"])
def test_the_keys_can_be_clicked_and_are_highlighted(key):
    assert _CLICK_KEY.fullmatch(key)
    assert highlight_hotkeys(f"[{key}] go") != f"[{key}] go"

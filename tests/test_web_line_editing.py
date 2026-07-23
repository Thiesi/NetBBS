"""
Integration tests for cursor-addressable editing and history recall on
the web transport — `WebSession`'s own
parallel implementation of the same behavior
`tests/test_char_input_line_editing.py`/`test_char_input_history.py`
already prove for Telnet/SSH, driven here through a real websocket
connection rather than a fake `ByteSource`. Mostly checks the final
`read_line()` result (the redraw arithmetic itself is already proven
correct in the `char_input` tests this reuses `move_cursor`/
`redraw_tail` from); a couple of tests also check the exact JSON
message sequence, confirming the escape-sequence recognition and
wiring specifically.
"""

from __future__ import annotations

import asyncio

import aiohttp

from netbbs.net.char_input import InputHistory
from netbbs.net.session import Session
from netbbs.net.web import WebServer

_UP = "\x1b[A"
_DOWN = "\x1b[B"
_LEFT = "\x1b[D"
_RIGHT = "\x1b[C"
_HOME = "\x1b[H"
_END = "\x1b[F"
_DELETE = "\x1b[3~"
_INSERT = "\x1b[2~"


async def _run_server(session_handler):
    server = WebServer(host="127.0.0.1", port=0, session_handler=session_handler)
    await server.start()
    return server


def _read_line_result(data: str, *, history: InputHistory | None = None) -> str:
    received = []

    async def handler(session: Session):
        received.append(await session.read_line(history=history))

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    await ws.send_json({"type": "key", "data": data})
                    # Deterministically wait for read_line() to actually
                    # finish -- its last action before returning is
                    # always writing "\r\n" -- rather than a fixed
                    # sleep, which this project's own history has
                    # repeatedly flagged as a hazard (design doc rounds
                    # 20/28/44).
                    while True:
                        msg = await ws.receive_json(timeout=2)
                        if msg["data"].endswith("\r\n"):
                            break
        finally:
            await server.stop()

    asyncio.run(scenario())
    return received[0]


# -- Left/Right/Home/End -----------------------------------------------


def test_left_then_typing_inserts_before_the_last_character():
    assert _read_line_result("abc" + _LEFT + "X\r") == "abXc"


def test_home_then_typing_inserts_at_the_start():
    assert _read_line_result("abc" + _HOME + "X\r") == "Xabc"


def test_end_after_home_returns_to_appending():
    assert _read_line_result("abc" + _HOME + _END + "X\r") == "abcX"


def test_right_past_the_end_of_the_line_is_a_no_op():
    assert _read_line_result("ab" + _RIGHT * 3 + "X\r") == "abX"


# -- Delete / Insert -------------------------------------------------------


def test_delete_forward_removes_character_at_cursor():
    assert _read_line_result("abc" + _HOME + _DELETE + "\r") == "bc"


def test_insert_toggles_overwrite_mode():
    assert _read_line_result("abc" + _HOME + _INSERT + "X\r") == "Xbc"


# -- history ----------------------------------------------------------------


def test_up_recalls_the_most_recent_history_entry():
    history = InputHistory()
    history.record("/mute bob spamming")
    assert _read_line_result(_UP + "\r", history=history) == "/mute bob spamming"


def test_down_past_the_newest_recalled_entry_restores_the_in_progress_line():
    history = InputHistory()
    history.record("previous command")
    assert _read_line_result("wip" + _UP + _DOWN + "\r", history=history) == "wip"


def test_history_persists_across_multiple_reads_of_the_same_object():
    history = InputHistory()
    history.record("first line")
    assert _read_line_result("second line\r", history=history) == "second line"
    assert _read_line_result(_UP + _UP + "\r", history=history) == "first line"


# -- exact wire message sequence for a representative redraw ---------------


def test_mid_line_insert_produces_the_expected_wire_messages():
    received = []

    async def handler(session: Session):
        received.append(await session.read_line())

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    await ws.send_json({"type": "key", "data": "abc" + _LEFT + "X\r"})
                    messages = []
                    for _ in range(7):
                        messages.append((await ws.receive_json(timeout=2))["data"])
                    return messages
        finally:
            await server.stop()

    messages = asyncio.run(scenario())
    # "a", "b", "c" echoed one at a time, Left moves back one column,
    # then the insert erases to end-of-line, reprints the new tail
    # ("Xc"), and repositions one column back -- the exact same
    # sequence tests/test_char_input_line_editing.py already proves for
    # Telnet/SSH, confirming WebSession's parallel implementation
    # produces identical output.
    assert messages == ["a", "b", "c", "\x1b[1D", "\x1b[K", "Xc", "\x1b[1D"]

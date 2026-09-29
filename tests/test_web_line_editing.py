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
import re

import aiohttp

from netbbs.net.char_input import InputHistory
from netbbs.net.confirm import prompt_yes_no
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


def test_web_session_reports_built_in_truecolor_capability():
    captured = []

    async def handler(session: Session):
        captured.append((session.supports_truecolor, session.truecolor_diagnostic))
        await session.write_line("done")

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    await ws.receive_json(timeout=2)
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert captured == [(True, "NetBBS web/xterm.js client has built-in truecolor support")]


def test_single_key_confirmation_echoes_uppercase_and_ends_its_row():
    results = []

    async def handler(session: Session):
        results.append(await prompt_yes_no(session, "Confirm?", default=False))
        results.append(await prompt_yes_no(session, "Again?", default=True))
        await session.write_line("NEXT")

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    prompt = await ws.receive_json(timeout=2)
                    await ws.send_json({"type": "key", "data": "Y\rN"})
                    accepted = await ws.receive_json(timeout=2)
                    second_prompt = await ws.receive_json(timeout=2)
                    second_accepted = await ws.receive_json(timeout=2)
                    next_row = await ws.receive_json(timeout=2)
                    return [
                        prompt["data"], accepted["data"], second_prompt["data"],
                        second_accepted["data"], next_row["data"],
                    ]
        finally:
            await server.stop()

    output = asyncio.run(scenario())
    assert results == [True, False]
    assert output == [
        "Confirm? \x1b[1m\x1b[38;5;75m[\x1b[0my/\x1b[1m\x1b[38;5;46mN\x1b[0m\x1b[1m\x1b[38;5;75m]\x1b[0m: ", "Y\r\n",
        "Again? \x1b[1m\x1b[38;5;75m[\x1b[0m\x1b[1m\x1b[38;5;46mY\x1b[0m/n\x1b[1m\x1b[38;5;75m]\x1b[0m: ", "N\r\n", "NEXT\r\n",
    ]


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


# -- CJK text moves the real cursor by display columns, not characters --
# (design doc, dogfood feature request: international users found
# non-ASCII handling poor) -- WebSession's parallel implementation of
# the same fix tests/test_char_input_line_editing.py already proves.


def test_left_over_a_cjk_character_moves_before_it():
    assert _read_line_result("你" + _LEFT + "X\r") == "X你"


def test_mid_line_insert_between_cjk_characters_produces_the_expected_wire_messages():
    received = []

    async def handler(session: Session):
        received.append(await session.read_line())

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    await ws.send_json({"type": "key", "data": "你好" + _LEFT + "X\r"})
                    messages = []
                    for _ in range(6):
                        messages.append((await ws.receive_json(timeout=2))["data"])
                    return messages
        finally:
            await server.stop()

    messages = asyncio.run(scenario())
    # Each CJK character is 2 display columns -- the move-back before
    # erasing and the final reposition after reprinting the tail ("好")
    # must both be "\x1b[2D", not "\x1b[1D" the way a character-count
    # would produce.
    assert messages == ["你", "好", "\x1b[2D", "\x1b[K", "X好", "\x1b[2D"]


# -- Completion in a scrolling line (issue #926) -----------------------


def test_tab_completion_in_a_scrolling_line_completes_and_never_overruns_the_row():
    # Chat completes and, since #926, scrolls: the web editor must do both
    # at once, the same way `char_input._read_line_editable` does.
    received, drawn = [], []

    async def handler(session: Session):
        received.append(await session.read_line(
            completer=lambda text: ["/whois"] if text == "/whoi" else [], viewport=20,
        ))

    async def scenario():
        server = await _run_server(handler)
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    await ws.send_json({"type": "key", "data": "x" * 40 + _HOME + "/whoi\t\r"})
                    while True:
                        msg = await ws.receive_json(timeout=2)
                        drawn.append(msg["data"])
                        if msg["data"].endswith("\r\n"):
                            break
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["/whois " + "x" * 40]
    printed_runs = re.split(r"\x1b\[[0-9;]*[A-Za-z]|\r\n", "".join(drawn))
    assert max(len(run) for run in printed_runs) < 20

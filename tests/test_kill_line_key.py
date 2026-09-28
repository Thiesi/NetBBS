"""Ctrl-U empties the line being typed (issue #812).

A long value opened for editing -- a mail subject under [U]pdate subject,
a resource description -- used to take one Backspace per character to
clear, because neither line editor gave any key that meaning. Both
editors (`char_input` for Telnet/SSH, `WebSession`'s copy for the web
client) and both of their masked (password) paths honor it now.
"""

from __future__ import annotations

import asyncio

import aiohttp

from netbbs.net.char_input import KILL_LINE_KEY, LiveInputBuffer, read_line
from netbbs.net.session import Session
from netbbs.net.web import WebServer

from tests.test_char_input import FakeByteSource, Writer
from tests.test_line_editor_viewport import _CSI, _LONG, _WIDTH, _edit

_KILL = KILL_LINE_KEY.encode()
_LEFT = b"\x1b[D"
_CRLF = b"\r\n"


def _run(data: bytes, **kwargs) -> tuple[str, str]:
    async def scenario():
        writer = Writer()
        line = await read_line(FakeByteSource(data), writer, **kwargs)
        return line, writer.joined

    return asyncio.run(scenario())


def test_the_key_is_ctrl_u():
    assert KILL_LINE_KEY == "\x15"


def test_ctrl_u_empties_what_was_typed():
    line, _ = _run(b"hello" + _KILL + b"bye" + _CRLF)
    assert line == "bye"


def test_ctrl_u_empties_the_whole_line_from_mid_line():
    """A field is cleared, not split: the text after the caret goes too."""
    line, _ = _run(b"hello" + _LEFT * 2 + _KILL + b"X" + _CRLF)
    assert line == "X"


def test_ctrl_u_erases_the_text_on_screen():
    """Back to where the text started, then erase to the end of the row --
    with a wide character counted as the two columns it takes."""
    _, output = _run("ab中".encode() + _KILL + _CRLF)
    assert output.endswith("\x1b[4D\x1b[K\r\n")


def test_ctrl_u_on_an_empty_line_writes_nothing():
    _, output = _run(_KILL + _CRLF)
    assert output == "\r\n"


def test_ctrl_u_empties_a_prefilled_value():
    line, _ = _run(_KILL + b"new" + _CRLF, initial="old subject")
    assert line == "new"


def test_ctrl_u_clears_a_long_value_in_a_scrolled_window():
    """The audit's case: a value wider than the row, edited through the
    one-row window. Emptied in one key, and still never more than a row."""
    result, recorder = _edit(_KILL + b"short\r")
    assert result == "short"
    assert recorder.widest_run() <= _WIDTH
    # chunks[0] opened the window on the long value; chunks[1] is the
    # render Ctrl-U caused, which erases the row and draws nothing.
    assert "once more" in recorder.chunks[0]
    assert "\x1b[K" in recorder.chunks[1]
    assert _CSI.sub("", recorder.chunks[1]).strip() == ""
    assert _CSI.sub("", recorder.chunks[-2]).strip() == "short"


def test_ctrl_u_keeps_the_live_buffer_in_step():
    """Chat's pinned input row redraws from the live buffer."""
    buffer = LiveInputBuffer()

    async def scenario():
        source = FakeByteSource(b"hello" + _KILL)
        try:
            await read_line(source, Writer(), live_buffer=buffer)
        except Exception:
            pass

    asyncio.run(scenario())
    assert (buffer.text, buffer.cursor) == ("", 0)


def test_ctrl_u_empties_a_masked_line():
    line, output = _run(b"secret" + _KILL + b"pw" + _CRLF, echo=False)
    assert line == "pw"
    assert "\b \b" * 6 in output


# -- web ------------------------------------------------------------------


def _web_read_line(data: str, *, echo: bool = True, initial: str = "") -> str:
    received = []

    async def handler(session: Session):
        received.append(await session.read_line(echo=echo, initial=initial))

    async def scenario():
        server = WebServer(host="127.0.0.1", port=0, session_handler=handler)
        await server.start()
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


def test_web_ctrl_u_empties_what_was_typed():
    assert _web_read_line("hello\x1b[D" + KILL_LINE_KEY + "bye\r") == "bye"


def test_web_ctrl_u_empties_a_prefilled_value():
    assert _web_read_line(KILL_LINE_KEY + "new\r", initial="old subject") == "new"


def test_web_ctrl_u_empties_a_masked_line():
    assert _web_read_line("secret" + KILL_LINE_KEY + "pw\r", echo=False) == "pw"

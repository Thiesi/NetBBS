"""
Pasted color in the post editors (issue #754).

The key reader used to discard every CSI sequence it did not know as a
key, so text pasted with its color arrived plain. A pasted SGR is now
recognized, and a post editor on a board that allows color types it in
as the equivalent pipe codes. Every other reader still drops it.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

import netbbs.net.char_input as char_input
from netbbs.net.char_input import EditorKey, EditorKeyKind, read_editor_key, read_line
from netbbs.net.composition import edit_line_body
from netbbs.net.prose_editor import edit_prose
from netbbs.net.session import Session, SessionClosedError
from netbbs.net.web import WebServer, _parse_input_events
from netbbs.rendering.pipe_codes import PastedColor

_CTRL_O = b"\x0f"


def _translate(*sequences: str) -> list[str]:
    pasted = PastedColor()
    return [pasted.translate(params) for params in sequences]


# -- translation --------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "pipes"),
    [
        ("31", "|04"),  # ANSI red is CGA 4
        ("34", "|01"),  # ANSI blue is CGA 1
        ("33", "|06"),  # ANSI yellow is CGA brown
        ("37", "|07"),
        ("91", "|12"),  # bright red
        ("1;31", "|12"),  # bold becomes the bright foreground
        ("1", "|15"),  # bold on the default foreground
        ("44", "|17"),
        ("101", "|20"),  # a bright background becomes its base color
        ("0;1;33;44", "|14|17"),
    ],
)
def test_sgr_becomes_the_equivalent_pipe_codes(params, pipes):
    assert _translate(params) == [pipes]


def test_translation_keeps_the_color_state_between_sequences():
    # A bold after a red is light red; switching bold off is red again;
    # the same color twice says nothing the second time.
    assert _translate("31", "1", "22", "31") == ["|04", "|12", "|04", ""]
    # Bold set first carries into the next color.
    assert _translate("1", "34") == ["|15", "|09"]


def test_reset_returns_to_the_default_colors():
    assert _translate("31;44", "0") == ["|04|17", "|07|16"]
    assert _translate("31;44", "") == ["|04|17", "|07|16"]  # ESC[m is a reset
    assert _translate("31", "39") == ["|04", "|07"]
    assert _translate("44", "49") == ["|17", "|16"]
    # A reset with nothing set changes nothing.
    assert _translate("0") == [""]


def test_codes_without_a_pipe_equivalent_are_dropped():
    assert _translate("4", "5", "24", "25", "7") == ["", "", "", "", ""]
    assert _translate("38;5;196") == [""]
    assert _translate("38;2;255;0;0") == [""]
    # The extended color's own numbers are not read as codes of their
    # own: `38;5;1` is not bold, and the code after it still counts.
    assert _translate("38;5;1;41") == ["|20"]
    assert _translate("48;2;1;31;4;32") == ["|02"]


# -- the Telnet/SSH key reader -------------------------------------------


class FakeByteSource:
    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    async def read_byte(self) -> int | None:
        if self._pos >= len(self._data):
            raise SessionClosedError("no more data")
        self._pos += 1
        return self._data[self._pos - 1]

    async def read_byte_with_timeout(self, timeout: float) -> int | None:
        if self._pos >= len(self._data):
            return None
        self._pos += 1
        return self._data[self._pos - 1]


async def _ignore(text: str) -> None:
    return None


_PASTE = b"plain \x1b[1;31mred\x1b[0m and \x1b[44mblue\x1b[m\r"


def test_read_line_types_pasted_color_as_pipe_codes():
    line = asyncio.run(read_line(FakeByteSource(_PASTE), _ignore, pasted_color=PastedColor()))
    assert line == "plain |12red|07 and |17blue|16"


def test_read_line_still_drops_pasted_color_by_default():
    assert asyncio.run(read_line(FakeByteSource(_PASTE), _ignore)) == "plain red and blue"


def test_pasted_pipe_codes_are_inserted_at_the_cursor():
    # Home, then a pasted red: the codes land where the cursor is.
    line = asyncio.run(read_line(FakeByteSource(b"abc\x1b[H\x1b[31m\r"), _ignore, pasted_color=PastedColor()))
    assert line == "|04abc"


def test_a_key_is_not_mistaken_for_pasted_color():
    # Private-marker sequences ending in `m` (SGR mouse reports) are not
    # color, and arrows still move the cursor.
    line = asyncio.run(
        read_line(FakeByteSource(b"ab\x1b[<0;1;2mc\x1b[DX\r"), _ignore, pasted_color=PastedColor())
    )
    assert line == "abXc"


def test_a_truecolor_sgr_does_not_end_the_session():
    # 34 bytes of parameters: over the old 32-byte cap, which ended the
    # session for anyone pasting it.
    paste = b"\x1b[38;2;255;255;255;48;2;255;255;255mx\r"
    assert asyncio.run(read_line(FakeByteSource(paste), _ignore, pasted_color=PastedColor())) == "x"
    assert asyncio.run(read_line(FakeByteSource(paste), _ignore)) == "x"


def test_read_editor_key_types_pasted_color_one_char_at_a_time():
    async def scenario(pasted_color):
        source = FakeByteSource(b"\x1b[31mA")
        keys = []
        while True:
            key = await read_editor_key(source, pasted_color=pasted_color)
            keys.append(key.char)
            if key.char == "A":
                return keys, key.kind

    assert asyncio.run(scenario(PastedColor())) == (["|", "0", "4", "A"], EditorKeyKind.CHAR)
    assert asyncio.run(scenario(None)) == (["A"], EditorKeyKind.CHAR)


# -- the post editors -------------------------------------------------------


class ByteSession(Session):
    """A Session whose reads go through the real key reader."""

    def __init__(self, data: bytes):
        self._source = FakeByteSource(data)
        self.terminal_width = 80
        self.terminal_height = 24
        self.node_display_name = "NetBBS"
        self.peer_address = None

    async def read_byte(self) -> int | None:
        return await self._source.read_byte()

    async def read_byte_with_timeout(self, timeout: float) -> int | None:
        return await self._source.read_byte_with_timeout(timeout)

    async def write(self, text: str) -> None:
        return None

    async def write_raw(self, data: bytes) -> None:
        return None

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        return await char_input.read_line(self, self.write, echo, history, completer, **kwargs)

    async def read_key(self, echo: bool = True) -> str:
        return await char_input.read_key(self, self.write, echo)

    async def read_editor_key(self, **kwargs):
        return await char_input.read_editor_key(self, **kwargs)

    async def close(self) -> None:
        return None


@pytest.mark.parametrize(("keep", "body"), [(True, "|04red\nstill red, |12bright"), (False, "red\nstill red, bright")])
def test_line_editor_keeps_pasted_color_only_when_asked(keep, body):
    # One translator for the whole body: the bold on line two knows the
    # red from line one.
    session = ByteSession(b"\x1b[31mred\rstill red, \x1b[1mbright\r\r")
    result = asyncio.run(
        edit_line_body(session, initial_text=None, max_bytes=1_000, max_lines=20, keep_pasted_color=keep)
    )
    assert result == body


@pytest.mark.parametrize(("keep", "body"), [(True, "|02green|07 text"), (False, "green text")])
def test_prose_editor_keeps_pasted_color_only_when_asked(tmp_path, keep, body):
    session = ByteSession(b"\x1b[32mgreen\x1b[0m text" + _CTRL_O)
    result = asyncio.run(
        edit_prose(session, initial_text=None, draft_path=tmp_path / "d.draft", max_bytes=1_000, keep_pasted_color=keep)
    )
    assert result == body


# -- the web transport ---------------------------------------------------------


def test_web_parser_keeps_a_pasted_sgr_and_nothing_else_ending_in_m():
    items = _parse_input_events("a\x1b[1;31mb\x1b[<0;1;2mc")
    assert items == ["a", char_input.ColorCode("1;31"), "b", "c"]


def _web_read_line(data: str, **read_options) -> str:
    received = []

    async def handler(session: Session):
        received.append(await session.read_line(**read_options))

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


def test_web_read_line_types_pasted_color_as_pipe_codes():
    paste = "plain \x1b[1;31mred\x1b[0m and \x1b[44mblue\x1b[m\r"
    assert _web_read_line(paste, pasted_color=PastedColor()) == "plain |12red|07 and |17blue|16"
    assert _web_read_line(paste) == "plain red and blue"


def test_web_read_editor_key_types_pasted_color_one_char_at_a_time():
    received = []

    async def handler(session: Session):
        pasted_color = PastedColor()
        for _ in range(4):
            received.append((await session.read_editor_key(pasted_color=pasted_color)).char)
        received.append((await session.read_editor_key()).char)
        await session.write_line("done")

    async def scenario():
        server = WebServer(host="127.0.0.1", port=0, session_handler=handler)
        await server.start()
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    await ws.send_json({"type": "key", "data": "\x1b[31mA\x1b[32mB"})
                    await ws.receive_json(timeout=2)
        finally:
            await server.stop()

    asyncio.run(scenario())
    # Without a translator the second color is dropped and B comes next.
    assert received == ["|", "0", "4", "A", "B"]


# -- which boards ask for it -----------------------------------------------------


class ScriptedSession(Session):
    """Scripted lines; records which reads asked for pasted color."""

    def __init__(self, inputs: list[str]):
        self._inputs = list(inputs)
        self.asked: list[tuple[str, bool]] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.node_display_name = "NetBBS"
        self.peer_address = None

    async def write(self, text: str) -> None:
        return None

    async def write_raw(self, data: bytes) -> None:
        return None

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        value = self._inputs.pop(0)
        self.asked.append((value, isinstance(kwargs.get("pasted_color"), PastedColor)))
        return value

    async def read_key(self, echo: bool = True) -> str:
        return self._inputs.pop(0)

    async def read_editor_key(self, **kwargs):
        return EditorKey(EditorKeyKind.CHAR, char=self._inputs.pop(0))

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def close(self) -> None:
        return None


@pytest.fixture
def db(tmp_path):
    from netbbs.storage.database import Database

    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.mark.parametrize("allow_color", [True, False])
def test_only_a_board_that_allows_color_keeps_pasted_color(db, allow_color):
    from netbbs.auth.users import create_user
    from netbbs.boards.boards import create_board
    from netbbs.boards.posts import create_post
    from netbbs.net import board_flow

    alice = create_user(db, "alice", password="hunter2", user_level=10)
    board = create_board(db, "general", creator=alice, allow_color=allow_color)
    create_post(db, board, alice, "Existing", "Existing body")
    # A new post (subject, body), then an edit of the existing one
    # (subject kept, body), each left as a draft.
    session = ScriptedSession(["p", "Subject", "new body", "/exit", "1", "e", "", "edited body", "/exit", "b", "b"])
    asyncio.run(board_flow._show_board(session, db, board, alice))
    asked = dict(session.asked)
    assert asked["Subject"] is False  # a subject has no use for pipe codes
    assert asked["new body"] is allow_color
    assert asked["edited body"] is allow_color


@pytest.mark.parametrize("translate", [True, False])
def test_web_read_editor_key_survives_a_flood_of_color_codes(translate):
    # 1,300 no-op SGRs fit one permitted key event (Codex review on #779):
    # skipping them must not cost call stack.
    received = []

    async def handler(session: Session):
        options = {"pasted_color": PastedColor()} if translate else {}
        received.append((await session.read_editor_key(**options)).char)
        await session.write_line("done")

    async def scenario():
        server = WebServer(host="127.0.0.1", port=0, session_handler=handler)
        await server.start()
        try:
            async with aiohttp.ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{server.port}/ws") as ws:
                    await ws.send_json({"type": "key", "data": "\x1b[m" * 1300 + "A"})
                    await ws.receive_json(timeout=5)
        finally:
            await server.stop()

    asyncio.run(scenario())
    assert received == ["A"]

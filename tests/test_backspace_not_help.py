"""Backspace is not help on a terminal whose Backspace sends 0x08 (issue #1119).

Ctrl-H and Backspace are the same byte, 0x08. NetBBS reads a bare 0x08 on a
menu, a list or a screen of fields as Ctrl-H, help, because PuTTY and xterm
send 0x7F for Backspace. SyncTERM sends 0x08 for Backspace (its CTerm manual,
DECBKM set by default; #964), so a SyncTERM caller pressing Backspace on the
"From disk" list got the help screen. On such a terminal 0x08 is Backspace
everywhere, and help is `[?]` or F1 (``ESC[11~``, which SyncTERM sends, or
``ESC O P``)."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.net.char_input import (
    HELP_KEY,
    EditorKey,
    EditorKeyKind,
    read_editor_key,
    read_key,
    read_line,
)
from netbbs.net.picker import pick_item
from netbbs.net.session import Session, SessionClosedError
from tests.test_char_input import FakeByteSource, Writer

SYNCTERM = ("syncterm",)
XTERM = ("xterm",)


class _Terminal(FakeByteSource):
    def __init__(self, data: bytes, *, terminal_types: tuple[str, ...]):
        super().__init__(data)
        self.terminal_types = terminal_types


def _key(data: bytes, terminal_types: tuple[str, ...]) -> str:
    async def scenario():
        return await read_key(_Terminal(data, terminal_types=terminal_types), Writer())

    return asyncio.run(scenario())


def _navigation_key(data: bytes, terminal_types: tuple[str, ...]) -> EditorKey:
    async def scenario():
        return await read_editor_key(_Terminal(data, terminal_types=terminal_types), distinguish_ctrl_h=True)

    return asyncio.run(scenario())


# -- menus (read_key) ---------------------------------------------------------


def test_a_menu_ignores_syncterms_backspace():
    # Backspace means nothing on a menu: the next real key is what comes back.
    assert _key(b"\x08x", SYNCTERM) == "x"


def test_a_menu_still_reads_ctrl_h_as_help_elsewhere():
    assert _key(b"\x08", XTERM) == HELP_KEY


@pytest.mark.parametrize("terminal_types", [SYNCTERM, XTERM])
@pytest.mark.parametrize("f1", [b"\x1b[11~", b"\x1bOP"])
def test_f1_is_help_on_a_menu(terminal_types, f1):
    assert _key(f1, terminal_types) == HELP_KEY


# -- lists and screens of fields (read_editor_key with distinguish_ctrl_h) ---


def test_a_list_reads_syncterms_backspace_as_backspace():
    assert _navigation_key(b"\x08", SYNCTERM) == EditorKey(EditorKeyKind.BACKSPACE)


def test_a_list_still_reads_ctrl_h_as_help_elsewhere():
    assert _navigation_key(b"\x08", XTERM) == EditorKey(EditorKeyKind.CTRL, char="h")


@pytest.mark.parametrize("terminal_types", [SYNCTERM, XTERM])
@pytest.mark.parametrize("f1", [b"\x1b[11~", b"\x1bOP"])
def test_f1_is_help_on_a_list(terminal_types, f1):
    assert _navigation_key(f1, terminal_types) == EditorKey(EditorKeyKind.CTRL, char="h")


# -- line prompts ---------------------------------------------------------------


@pytest.mark.parametrize("terminal_types", [SYNCTERM, XTERM])
def test_a_line_prompt_still_erases_with_0x08(terminal_types):
    async def scenario():
        return await read_line(_Terminal(b"abc\x08\r", terminal_types=terminal_types), Writer())

    assert asyncio.run(scenario()) == "ab"


def test_f1_types_nothing_at_a_line_prompt():
    async def scenario():
        return await read_line(_Terminal(b"ab\x1b[11~c\r", terminal_types=SYNCTERM), Writer())

    assert asyncio.run(scenario()) == "abc"


# -- the "From disk" list, through a real picker ------------------------------


class _ByteSession(Session):
    """A session whose keys come from real bytes, decoded by char_input the
    way the transports decode them."""

    def __init__(self, data: bytes, *, terminal_types: tuple[str, ...]):
        self._source = _Terminal(data, terminal_types=terminal_types)
        self.terminal_types = terminal_types
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.node_display_name = "NetBBS"
        self.peer_address = None

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        return await read_line(self._source, self.write, echo=echo)

    async def read_key(self, echo: bool = True) -> str:
        return await read_key(self._source, self.write, echo=echo)

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
        return await read_editor_key(self._source, distinguish_ctrl_h=distinguish_ctrl_h)

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        return await self._source.read_byte()

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError


def _from_disk(data: bytes, terminal_types: tuple[str, ...]) -> tuple[object, str]:
    session = _ByteSession(data, terminal_types=terminal_types)
    files = ["nib.ans", "quill.ans"]

    async def scenario():
        try:
            return await pick_item(
                session,
                files,
                name_of=lambda name: name,
                stable_id_of=files.index,
                title="Pick an .ans file",
                empty_message="No .ans files found.",
            )
        except SessionClosedError:
            return "ran out of input"

    chosen = asyncio.run(scenario())
    return chosen, "".join(session.written)


_HELP_MARK = "Move one page forward/back"  # a line only the list's help screen shows


def test_syncterms_backspace_on_the_from_disk_list_does_not_open_help():
    chosen, screen = _from_disk(b"\x08b", SYNCTERM)
    assert chosen is None  # [B]ack left the list
    assert _HELP_MARK not in screen


def test_ctrl_h_still_opens_the_lists_help_elsewhere():
    chosen, screen = _from_disk(b"\x08\rb", XTERM)
    assert _HELP_MARK in screen


def test_f1_opens_the_lists_help_on_syncterm():
    chosen, screen = _from_disk(b"\x1b[11~\rb", SYNCTERM)
    assert _HELP_MARK in screen

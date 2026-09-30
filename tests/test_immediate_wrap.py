"""Terminals that wrap as soon as they write the last column (issue #964).

SyncTERM, like DOS ANSI.SYS, moves to the next line the moment a character
lands in the last column. A row exactly as wide as the screen followed by
CR LF therefore leaves a blank line, and writing the bottom-right cell
scrolls the screen. xterm and its descendants wait for the next character
instead, so the same output looks right there.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.net.ansi_editor import edit_ansi_art
from netbbs.net.session import Session, physical_terminal_width, preformatted_rows
from netbbs.net.ssh import SSHSession
from netbbs.net.terminal_detect import terminal_wraps_immediately
from netbbs.rendering.ansi_art import decode_banner_bytes, trim_row_ends
from netbbs.rendering.charset import CP437, UTF8
from netbbs.rendering.reflow import fills_last_column
from tests.test_ansi_editor import FakeSession as EditorSession
from tests.test_terminal_detect import _Process, _connect
from tests.test_telnet import answer_terminal_type


class _Session(Session):
    def __init__(self, *, width: int = 80, wraps: bool = False) -> None:
        self.terminal_width = width
        self.terminal_wraps_immediately = wraps
        self.written: list[str] = []

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def read_line(self, *args, **kwargs) -> str:
        raise AssertionError("unused")

    async def read_key(self, *args, **kwargs) -> str:
        raise AssertionError("unused")

    async def read_editor_key(self):
        raise AssertionError("unused")

    async def close(self) -> None:
        pass


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        (["syncterm"], True),
        (["ansi-bbs"], True),
        (["ansi"], True),
        (["dumb"], True),
        ([], True),
        (["xterm-256color"], False),
        (["putty"], False),
    ],
)
def test_only_a_known_modern_terminal_is_trusted_to_wait(names, expected):
    assert terminal_wraps_immediately(names) is expected


def test_layout_width_is_a_column_short_on_a_terminal_that_wraps_immediately():
    session = _Session(width=80, wraps=True)
    assert session.terminal_width == 79
    assert session.physical_width == 80
    assert physical_terminal_width(session) == 80
    session.terminal_width = 132
    assert (session.terminal_width, session.physical_width) == (131, 132)


def test_a_modern_terminal_keeps_its_full_width():
    session = _Session(width=80, wraps=False)
    assert session.terminal_width == 80 == session.physical_width


def test_choosing_cp437_counts_as_a_classic_terminal():
    session = _Session(width=80, wraps=False)
    session.output_charset = CP437
    assert session.wraps_immediately
    assert session.terminal_width == 79
    session.output_charset = UTF8
    assert session.terminal_width == 80


def test_syncterm_over_telnet_lays_out_one_column_short():
    async def client(reader, writer):
        await answer_terminal_type(reader, writer, "syncterm")
        return await reader.readuntil(b"\r\n")

    assert _connect(client)["width"] == 79


def test_xterm_over_telnet_keeps_the_full_width():
    async def client(reader, writer):
        await answer_terminal_type(reader, writer, "xterm")
        return await reader.readuntil(b"\r\n")

    assert _connect(client)["width"] == 80


@pytest.mark.parametrize(("terminal_type", "width"), [("syncterm", 79), ("xterm-256color", 80)])
def test_ssh_follows_the_pty_terminal_type(terminal_type, width):
    assert SSHSession(_Process(terminal_type)).terminal_width == width


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ("x" * 80, True),
        ("x" * 79, False),
        ("x" * 80 + "\x1b[0m", True),
        ("\x1b[44m" + "x" * 80 + "\x1b[0m", True),
        ("\x1b[10C" + "x" * 70, True),
        ("x" * 80 + "\x1b[1G", False),
        ("x" * 80 + "\r", False),
        ("x" * 160, True),
        ("x" * 81, False),
        ("", False),
    ],
)
def test_fills_last_column(row, expected):
    assert fills_last_column(row, 80) is expected


def test_trailing_plain_spaces_are_trimmed_from_each_row():
    art = "  /\\" + " " * 76 + "\r\n" + " THE NIB" + " " * 72 + "\r\n"
    assert trim_row_ends(art) == "  /\\\r\n THE NIB\r\n"


def test_painted_trailing_spaces_are_kept():
    art = "ab\x1b[44m   \x1b[0m   \r\ncd\r\n"
    assert trim_row_ends(art) == "ab\x1b[44m   \x1b[0m\r\ncd\r\n"


def test_a_background_set_on_an_earlier_row_still_paints():
    art = "\x1b[41mab  \r\ncd    \x1b[0m  \r\n"
    assert trim_row_ends(art) == "\x1b[41mab  \r\ncd    \x1b[0m\r\n"


def test_a_banner_from_the_editor_no_longer_fills_the_width():
    # The art editor saves every row of its 80-column canvas in full.
    rows = ["  /\\", " THE NIB & QUILL", ""]
    data = "".join(row.ljust(80) + "\r\n" for row in rows).encode("cp437")
    decoded = decode_banner_bytes(data)
    assert [len(row) for row in decoded.split("\r\n")] == [4, 16]


def test_full_width_art_rows_lose_their_line_break_on_a_classic_terminal():
    session = _Session(width=80, wraps=True)
    art = "\x1b[44m" + " " * 80 + "\x1b[0m\r\nshort\r\n" + "#" * 80
    sent = preformatted_rows(session, art)
    assert sent == "\x1b[44m" + " " * 80 + "\x1b[0mshort\r\n" + "#" * 80


def test_art_keeps_its_full_width_on_a_classic_terminal():
    # Art is drawn for the terminal's real width, not the layout width.
    session = _Session(width=80, wraps=True)
    sent = preformatted_rows(session, "#" * 80)
    assert sent == "#" * 80


def test_a_modern_terminal_gets_art_unchanged():
    session = _Session(width=80, wraps=False)
    art = "#" * 80 + "\r\nshort"
    assert preformatted_rows(session, art) == art + "\r\n"


def test_art_editor_status_line_never_reaches_the_last_column(tmp_path):
    async def scenario():
        session = EditorSession(["A", "CTRL+O"])
        session.terminal_wraps_immediately = True
        await edit_ansi_art(session, initial_bytes=None, draft_path=tmp_path / "d.draft",
                            autosave_interval_seconds=9999)
        return session

    session = asyncio.run(scenario())
    status = [
        re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", chunk)
        for chunk in session.written
        if "Ctrl+G help" in chunk
    ]
    assert status
    assert all(len(line) <= 79 for line in status)

"""SyncTERM's editing keys (issue #964, finding 5).

SyncTERM sends the BBS-convention sequences its CTerm manual lists under
"Sequences sent by SyncTERM": ``ESC[V`` Page Up, ``ESC[U`` Page Down,
``ESC[K`` End, ``ESC[@`` Insert, and with DECBKM set (its default) 0x08
for Backspace and 0x7F for Delete. PuTTY and xterm send 0x7F for
Backspace, so 0x7F only means Delete on a terminal that said it is
SyncTERM."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.net.char_input import EditorKeyKind, read_editor_key, read_line
from netbbs.net.terminal_detect import sends_syncterm_keys
from tests.test_char_input import FakeByteSource, Writer


class _Terminal(FakeByteSource):
    def __init__(self, data: bytes, *, terminal_types: tuple[str, ...] = ()):
        super().__init__(data)
        self.terminal_types = terminal_types


def _line(data: bytes, *, terminal_types: tuple[str, ...] = ()) -> str:
    async def scenario():
        return await read_line(_Terminal(data, terminal_types=terminal_types), Writer())

    return asyncio.run(scenario())


def _editor_keys(data: bytes, count: int, *, terminal_types: tuple[str, ...] = ()) -> list[EditorKeyKind]:
    async def scenario():
        source = _Terminal(data, terminal_types=terminal_types)
        return [(await read_editor_key(source)).kind for _ in range(count)]

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    ("sequence", "kind"),
    [
        (b"\x1b[V", EditorKeyKind.PAGE_UP),
        (b"\x1b[U", EditorKeyKind.PAGE_DOWN),
        (b"\x1b[K", EditorKeyKind.END),
        (b"\x1b[H", EditorKeyKind.HOME),
    ],
)
def test_syncterms_navigation_keys_are_read_in_an_editor(sequence, kind):
    assert _editor_keys(sequence, 1, terminal_types=("syncterm",)) == [kind]


def test_end_moves_to_the_end_of_a_line():
    # "abc", Home, "X", End, "Y": X lands at the start, Y at the end.
    assert _line(b"abc\x1b[HX\x1b[KY\r", terminal_types=("syncterm",)) == "XabcY"


def test_insert_toggles_overwrite():
    # "abc", Home, Insert (overwrite on), "X": X replaces a.
    assert _line(b"abc\x1b[H\x1b[@X\r", terminal_types=("syncterm",)) == "Xbc"


def test_syncterms_delete_removes_the_character_under_the_cursor():
    # "abc", Home, Delete: the a goes, not nothing.
    assert _line(b"abc\x1b[H\x7f\r", terminal_types=("syncterm",)) == "bc"


def test_syncterms_backspace_still_removes_the_character_before_the_cursor():
    assert _line(b"abc\x08\r", terminal_types=("syncterm",)) == "ab"


def test_ansi_bbs_is_syncterm_too():
    assert _line(b"abc\x1b[H\x7f\r", terminal_types=("ansi-bbs",)) == "bc"


@pytest.mark.parametrize("terminal_types", [(), ("xterm",), ("putty",), ("ansi",)])
def test_0x7f_stays_backspace_everywhere_else(terminal_types):
    assert _line(b"abc\x7f\r", terminal_types=terminal_types) == "ab"


def test_syncterms_delete_is_delete_in_an_editor():
    assert _editor_keys(b"\x7f\x08", 2, terminal_types=("syncterm",)) == [
        EditorKeyKind.DELETE,
        EditorKeyKind.BACKSPACE,
    ]


def test_0x7f_is_backspace_in_an_editor_elsewhere():
    assert _editor_keys(b"\x7f", 1, terminal_types=("xterm",)) == [EditorKeyKind.BACKSPACE]


def test_syncterms_delete_at_a_password_prompt_deletes_nothing():
    # A masked prompt has no cursor to delete under: Delete does nothing
    # there rather than eating the last typed character.
    async def scenario():
        return await read_line(_Terminal(b"secret\x7f\r", terminal_types=("syncterm",)), Writer(), echo=False)

    assert asyncio.run(scenario()) == "secret"


def test_the_decision_follows_any_reported_name():
    assert sends_syncterm_keys(_Terminal(b"", terminal_types=("SyncTERM",)))
    assert sends_syncterm_keys(_Terminal(b"", terminal_types=("some-unknown", "syncterm")))
    # The first name we recognise decides, as for the character set.
    assert not sends_syncterm_keys(_Terminal(b"", terminal_types=("xterm", "syncterm")))
    assert not sends_syncterm_keys(_Terminal(b"", terminal_types=("xterm",)))
    assert not sends_syncterm_keys(FakeByteSource(b""))


@pytest.mark.parametrize("terminal_types", [(), ("xterm",), ("putty",)])
def test_other_terminals_discard_bare_erase_and_insert_sequences(terminal_types):
    # Pasted ANSI output: ESC[K (erase in line) and ESC[@ (insert character)
    # are not End and Insert there, so neither moves the cursor nor turns
    # on overwrite.
    assert _line(b"abc\x1b[H\x1b[KX\x1b[@Y\r", terminal_types=terminal_types) == "XYabc"

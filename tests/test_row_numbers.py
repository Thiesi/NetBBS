"""Two-digit row numbers on the lists that draw their own screen (issue
#1158, design doc §3.5): `05`, or `5` and Enter, as the picker reads them."""

from __future__ import annotations

import asyncio

from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.row_numbers import read_row_number, row_number_label, row_range_label


class _Screen:
    def __init__(self) -> None:
        self.written: list[str] = []

    async def write(self, text: str) -> None:
        self.written.append(text)


def _reader(*keys: EditorKey, echoed: bool = False):
    queue = list(keys)

    async def read():
        return queue.pop(0), echoed

    return read


def _char(char: str) -> EditorKey:
    return EditorKey(EditorKeyKind.CHAR, char=char)


def _read(first: str, *keys: EditorKey, row_count: int = 30, first_echoed: bool = False, echoed: bool = False):
    screen = _Screen()
    number = asyncio.run(read_row_number(
        screen, first, row_count=row_count, first_echoed=first_echoed, read=_reader(*keys, echoed=echoed),
    ))
    return number, "".join(screen.written)


def test_two_digits_pick_the_row_and_both_are_shown():
    assert _read("1", _char("2")) == (12, "12")
    assert _read("0", _char("5")) == (5, "05")


def test_one_digit_and_enter_pick_that_row():
    assert _read("5", EditorKey(EditorKeyKind.ENTER))[0] == 5
    assert _read("5", _char("\r"))[0] == 5


def test_a_row_past_the_ninth_has_a_number():
    assert _read("2", _char("7"), row_count=27)[0] == 27


def test_a_number_beyond_the_page_or_zero_is_refused_with_a_bell():
    number, shown = _read("1", _char("2"), row_count=9)
    assert number is None and shown.endswith("\a")
    assert _read("0", _char("0"))[0] is None
    assert _read("0", EditorKey(EditorKeyKind.ENTER))[0] is None


def test_a_second_key_that_is_no_digit_is_refused_and_erased():
    number, shown = _read("1", _char("x"))
    assert number is None
    assert shown.startswith("1x") and shown.count("\b") == 4 and shown.endswith("\a")
    number, shown = _read("1", EditorKey(EditorKeyKind.UP))
    assert number is None and shown.count("\b") == 2


def test_keys_the_reader_already_echoed_are_not_drawn_twice():
    number, shown = _read("1", _char("2"), first_echoed=True, echoed=True)
    assert number == 12 and shown == ""


def test_labels():
    assert row_number_label(3) == "03"
    assert row_range_label(1) == "01"
    assert row_range_label(12) == "01-12"

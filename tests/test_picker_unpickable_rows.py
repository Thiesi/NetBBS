"""
`pick_item`'s rows that are shown but cannot be picked (`selectable_of`,
issue #920): no number, muted, skipped by the highlight and by numbers,
and never returned by a search.
"""

from __future__ import annotations

import asyncio

from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.picker import pick_item
from netbbs.rendering import MUTED_COLOR, fg
from tests.test_mail_flow import FakeSession, _visible_text

DOWN = EditorKey(EditorKeyKind.DOWN)
UP = EditorKey(EditorKeyKind.UP)
ENTER = EditorKey(EditorKeyKind.ENTER)
HELP = EditorKey(EditorKeyKind.CTRL, char="h")

ITEMS = ["ann", "ben (no)", "cat", "dan (no)"]


class KeySession(FakeSession):
    """Scripted structured keys: a string is typed as a character."""

    def __init__(self, keys, lines=None):
        super().__init__(lines=lines)
        self._editor_keys = iter(keys)

    async def read_editor_key(self, distinguish_ctrl_h: bool = False) -> EditorKey:
        key = next(self._editor_keys, None)
        if key is None:
            raise AssertionError("no more scripted keys")
        return key if isinstance(key, EditorKey) else EditorKey(EditorKeyKind.CHAR, char=key)

    async def read_key(self, echo: bool = True) -> str:
        return "q"

    async def discard_buffered_input(self) -> None:
        pass

    async def read_any_key(self) -> str:
        return " "


def _pick(keys, lines=None, items=ITEMS):
    session = KeySession(keys, lines)
    picked = asyncio.run(pick_item(
        session, items,
        name_of=lambda item: item,
        stable_id_of=items.index,
        selectable_of=lambda item: "(no)" not in item,
        title="People", empty_message="Nobody.",
    ))
    return picked, session


def test_rows_that_cannot_be_picked_take_no_number_and_are_muted():
    picked, session = _pick(["0", "2"])

    assert picked == "cat"
    text = _visible_text(session)
    assert "  01. ann\n" in text and "   -  ben (no)\n" in text and "  02. cat\n" in text
    assert "   -  dan (no)\n" in text
    assert fg(MUTED_COLOR) + "   -  " in "".join(session.written)


def test_a_number_past_the_pickable_rows_picks_nothing():
    picked, _ = _pick(["0", "3", "b"])
    assert picked is None


def test_the_highlight_steps_over_rows_that_cannot_be_picked():
    # Down: ann; Down: cat (over ben); Down: nothing below -- bell; Enter.
    assert _pick([DOWN, DOWN, DOWN, ENTER])[0] == "cat"
    # Up from nothing starts at the last pickable row, not at dan.
    assert _pick([UP, ENTER])[0] == "cat"
    assert _pick([UP, UP, ENTER])[0] == "ann"


def test_a_search_whose_one_match_cannot_be_picked_shows_it():
    picked, session = _pick(["s", "b"], lines=["ben"])

    assert picked is None
    assert _visible_text(session).count("   -  ben (no)\n") == 2


def test_help_explains_the_dash_only_where_there_is_one():
    line = "A row with - in place of a number can't be chosen"
    _, session = _pick([HELP, "b"])
    assert line in " ".join(_visible_text(session).split())
    _, session = _pick([HELP, "b"], items=["ann", "cat"])
    assert line not in " ".join(_visible_text(session).split())

"""`pick_item`'s `item_keys` (issue #710): a caller's key that acts on
one row -- the highlighted one, or the one whose reference is typed when
nothing is highlighted -- and keeps the page and the highlight."""

from __future__ import annotations

import asyncio

from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.picker import pick_item

_EDITOR_KEYS = {"UP": EditorKeyKind.UP, "DOWN": EditorKeyKind.DOWN, "ENTER": EditorKeyKind.ENTER}


class FakeSession:
    def __init__(self, keys):
        self._keys = iter(keys)
        self.written: list[str] = []
        self.terminal_width = 80
        self.terminal_height = 24
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.supports_truecolor = False

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self) -> str:
        return next(self._keys, "b")

    async def read_line(self, echo: bool = True, **kwargs) -> str:
        return next(self._keys, "b")

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
        raw = next(self._keys, "b")
        if raw in _EDITOR_KEYS:
            return EditorKey(_EDITOR_KEYS[raw])
        return EditorKey(EditorKeyKind.CHAR, char=raw)


def _pick(keys):
    acted_on: list[str] = []

    async def _mark(item: str):
        acted_on.append(item)
        return None

    session = FakeSession(keys)
    selected = asyncio.run(pick_item(
        session, ["alpha", "beta", "gamma"],
        name_of=lambda item: item,
        stable_id_of=lambda item: ["alpha", "beta", "gamma"].index(item) + 1,
        title="Things",
        empty_message="Nothing here.",
        item_keys={"m": _mark},
    ))
    return acted_on, selected


def test_an_item_key_acts_on_the_highlighted_row_and_keeps_the_highlight():
    acted_on, selected = _pick(["DOWN", "DOWN", "m", "ENTER"])

    assert acted_on == ["beta"]
    assert selected == "beta"


def test_an_item_key_asks_for_the_row_when_nothing_is_highlighted():
    acted_on, _selected = _pick(["m", "3", "b"])

    assert acted_on == ["gamma"]


def test_an_unknown_reference_acts_on_nothing():
    acted_on, _selected = _pick(["m", "9", "b"])

    assert acted_on == []

"""`pick_item`'s `item_keys` (issue #710): a caller's key that acts on
one row -- the highlighted one, or the one whose number on the page is
typed when nothing is highlighted -- and keeps the page and the highlight."""

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


def test_the_typed_number_is_the_rows_place_on_this_page_not_its_id():
    """Issue #838: rows show only the number that selects them, so that is
    the number a row key asks for -- on page 2, "01" is that page's first
    row, whatever its id."""
    names = [f"item {i:02d}" for i in range(40)]
    acted_on: list[str] = []

    async def _mark(item: str):
        acted_on.append(item)
        return None

    session = FakeSession(["n", "m", "01", "b"])
    asyncio.run(pick_item(
        session, names,
        name_of=lambda item: item,
        stable_id_of=lambda item: 1000 + names.index(item),
        title="Things",
        empty_message="Nothing here.",
        item_keys={"m": _mark},
    ))

    written = "".join(session.written)
    assert "Which one (01-" in written
    assert acted_on and acted_on[0] != "item 00"
    assert written.index(acted_on[0]) > written.index("page 2/")


def test_an_unknown_reference_acts_on_nothing():
    acted_on, _selected = _pick(["m", "9", "b"])

    assert acted_on == []


def test_an_outcome_an_item_key_announces_shows_on_the_next_redraw():
    """The picker reads pending outcomes at each render, not once when it
    opens, so its own keys' outcomes show at once (Codex review on #723)."""
    from netbbs.net.notices import announce

    session = FakeSession(["m", "2", "b"])

    async def _mark(item: str):
        announce(session, f"{item}: done.")
        return None

    asyncio.run(pick_item(
        session, ["alpha", "beta"],
        name_of=lambda item: item,
        stable_id_of=lambda item: ["alpha", "beta"].index(item) + 1,
        title="Things",
        empty_message="Nothing here.",
        item_keys={"m": _mark},
    ))

    written = "".join(session.written)
    assert "beta: done." in written
    # Drawn by the picker's own redraw, above its next prompt.
    assert written.index("beta: done.") < written.rindex("Choice")


def test_the_acted_on_row_stays_highlighted_when_its_outcome_shrinks_the_page():
    """A full page, its last row highlighted: the outcome line takes a row,
    and the row acted on must stay on the page and highlighted (Codex
    review on #723)."""
    from netbbs.net.notices import announce

    names = [f"item {i:02d}" for i in range(40)]
    session = FakeSession(["UP", "m", "ENTER"])
    acted_on: list[str] = []

    async def _mark(item: str):
        acted_on.append(item)
        announce(session, f"{item}: done.")
        return None

    selected = asyncio.run(pick_item(
        session, names,
        name_of=lambda item: item,
        stable_id_of=lambda item: names.index(item) + 1,
        title="Things",
        empty_message="Nothing here.",
        item_keys={"m": _mark},
    ))

    assert acted_on and selected == acted_on[0]


def test_shifting_the_window_for_the_acted_on_row_skips_no_row():
    """On a page after the first, the outcome line shifts the window to keep
    the acted-on row; [P]rev then shows the rows the shift moved off the
    top, not the page before them (Codex review on #723)."""
    import re

    from netbbs.net.notices import announce

    names = [f"item {i:02d}" for i in range(60)]
    session = FakeSession(["n", "UP", "m", "p", "b"])

    async def _mark(item: str):
        announce(session, f"{item}: done.")
        return None

    asyncio.run(pick_item(
        session, names,
        name_of=lambda item: item,
        stable_id_of=lambda item: names.index(item) + 1,
        title="Things",
        empty_message="Nothing here.",
        item_keys={"m": _mark},
    ))

    renders = "".join(session.written).split("Things")
    second_page_first = re.findall(r"item \d\d", renders[2])[0]
    after_prev = re.findall(r"item \d\d", renders[-1])
    assert second_page_first in after_prev

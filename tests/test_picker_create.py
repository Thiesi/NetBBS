"""A picker can offer what does not exist yet (issue #530).

Dogfood report: assigning a leaf resource to a Community that has not
been created yet meant leaving the editor, creating the Community from
the console, and coming back. The picker had no route to it.

The original reasoning -- that this only bites once, before the first
Community exists -- was wrong, so `[C]reate` is offered on a populated
list too: wanting to file a board under a *new* Community is an
ordinary thing to want at any point.
"""

from __future__ import annotations

import asyncio
import re

from netbbs.net.picker import pick_item

_SGR = re.compile(r"\x1b\[[0-9;]*m")


class FakeSession:
    def __init__(self, keys, width=80, height=24):
        self._keys = iter(keys)
        self.written: list[str] = []
        self.terminal_width = width
        self.terminal_height = height
        self.node_display_name = "ReLink"
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

    @property
    def plain(self) -> str:
        return _SGR.sub("", "".join(self.written))


class Item:
    def __init__(self, id_, name):
        self.id = id_
        self.name = name


def _pick(keys, items, *, on_create=None, **kwargs):
    session = FakeSession(keys)
    result = asyncio.run(
        pick_item(
            session, items,
            name_of=lambda i: i.name, stable_id_of=lambda i: i.id,
            title="Community", empty_message="No Communities exist yet.",
            on_create=on_create, **kwargs,
        )
    )
    return result, session


# -- The reported dead end --------------------------------------------


def test_an_empty_picker_stays_open_when_there_is_something_to_create():
    """It used to return the instant it found nothing, so the field
    silently resolved to "none" and the SysOp was bounced out."""
    made = Item(1, "Retro Computing")

    async def create():
        return made

    result, session = _pick(["c"], [], on_create=create)
    assert result is made


def test_an_empty_picker_says_create_is_available():
    async def create():
        return None

    _, session = _pick(["b"], [], on_create=create)
    assert "reate" in session.plain


def test_an_empty_picker_with_no_create_still_returns_immediately():
    """Every existing caller is unchanged: nothing to do here but
    leave, so leaving is automatic as before."""
    result, session = _pick([], [])
    assert result is None
    assert "No Communities exist yet." in session.plain


# -- Also on a populated list -----------------------------------------


def test_create_is_offered_when_items_already_exist():
    """The half the original reasoning missed."""
    async def create():
        return None

    _, session = _pick(["b"], [Item(1, "Politics")], on_create=create)
    assert "reate" in session.plain


def test_creating_from_a_populated_list_returns_the_new_item():
    made = Item(9, "Retro Computing")

    async def create():
        return made

    result, _ = _pick(["c"], [Item(1, "Politics")], on_create=create)
    assert result is made


def test_a_new_item_is_selected_rather_than_merely_listed():
    """The caller opened this picker to choose something and said "none
    of these". Making them pick it again by hand would answer a question
    they have already answered."""
    made = Item(9, "Retro Computing")
    calls = {"n": 0}

    async def create():
        calls["n"] += 1
        return made

    result, _ = _pick(["c"], [], on_create=create)
    assert result is made and calls["n"] == 1


def test_backing_out_of_create_returns_to_the_list():
    """`on_create` returning `None` means they changed their mind, not
    that they picked nothing."""
    async def create():
        return None

    result, session = _pick(["c", "b"], [Item(1, "Politics")], on_create=create)
    assert result is None
    assert "Politics" in session.plain


def test_the_create_key_is_rejected_when_no_caller_supplied_one():
    """`c` must not become a silent no-op on every other picker."""
    result, session = _pick(["c", "b"], [Item(1, "Politics")])
    assert result is None
    assert "\a" in "".join(session.written)


# -- Codex review -----------------------------------------------------


def test_an_interactive_empty_picker_clears_when_redrawing_in_place():
    """The empty branch used to be a dead end that printed one line and
    returned, so nothing depended on it clearing -- the clear rode along
    inside `_masthead_prefix`, which returns "" when no masthead is
    configured. The Community and category pickers have none, so an
    empty list was appended below the editor instead of replacing it."""
    async def create():
        return None

    _, session = _pick(["b"], [], on_create=create, redraw_in_place=True)
    assert "\x1b[2J" in "".join(session.written)


def test_a_dead_end_empty_picker_is_unchanged():
    """No `on_create` means it still returns immediately, and still
    writes nothing it did not write before."""
    _, session = _pick([], [], redraw_in_place=True)
    assert "\x1b[2J" not in "".join(session.written)

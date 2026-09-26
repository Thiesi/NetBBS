"""Tests for netbbs.net.picker.pick_item's `masthead` parameter (GitHub
issue #176) -- the mechanism board_list_banner/file_area_banner/
chat_channel_picker_banner build on to prepend an optional SysOp-authored
masthead above every redraw of a shared picker. Uses the lightweight
FakeSession pattern tests/test_picker_refresh_regressions.py already
established for this module, not the full Telnet-socket integration
suite in tests/test_picker.py -- these are unit tests for one parameter,
not a protocol-level concern.
"""

from __future__ import annotations

import asyncio

from netbbs.net.char_input import REFRESH_KEY
from netbbs.net.picker import pick_item
from netbbs.net.session import Session
from netbbs.rendering.ansi import clear_screen


class FakeSession(Session):
    def __init__(self, keys: list[str]):
        self._keys = list(keys)
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.terminal_height = 24
        self.peer_address = None

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        return self._keys.pop(0)

    async def read_key(self, echo: bool = True) -> str:
        return self._keys.pop(0)

    async def read_editor_key(self):
        raise NotImplementedError

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError


def _written(session: FakeSession) -> str:
    return "".join(session.written)


def test_no_masthead_by_default_renders_byte_for_byte_unchanged():
    with_default = FakeSession(["b"])
    without_param = FakeSession(["b"])

    async def _pick(session):
        return await pick_item(
            session, ["alpha", "beta"], name_of=lambda v: v, stable_id_of=lambda v: hash(v),
            title="Test", empty_message="none",
        )

    asyncio.run(_pick(without_param))
    asyncio.run(
        pick_item(
            with_default, ["alpha", "beta"], name_of=lambda v: v, stable_id_of=lambda v: hash(v),
            title="Test", empty_message="none", masthead="",
        )
    )
    assert _written(with_default) == _written(without_param)


def test_masthead_appears_above_the_populated_list():
    session = FakeSession(["b"])

    asyncio.run(
        pick_item(
            session, ["alpha", "beta"], name_of=lambda v: v, stable_id_of=lambda v: hash(v),
            title="Test", empty_message="none", masthead="=== MY MASTHEAD ===",
        )
    )
    text = _written(session)
    assert "=== MY MASTHEAD ===" in text
    assert text.index("=== MY MASTHEAD ===") < text.index("Test")


def test_a_truly_empty_non_refreshable_list_announces_and_draws_nothing():
    """Issue #680: with nothing to pick and nothing to do but leave, the
    picker returns straight into its parent's redraw. Anything it drew --
    masthead or message -- would be erased by that redraw's clear, so it
    draws nothing and announces the message for the parent screen instead."""
    from netbbs.net.notices import take_notices

    session = FakeSession([])

    asyncio.run(
        pick_item(
            session, [], name_of=lambda v: v, stable_id_of=lambda v: hash(v),
            title="Test", empty_message="Nothing here.", masthead="=== MY MASTHEAD ===",
        )
    )
    assert _written(session) == ""
    assert any("Nothing here." in line for line in take_notices(session))


def test_masthead_appears_above_a_refreshable_empty_list():
    session = FakeSession(["b"])

    async def refresh():
        return []

    asyncio.run(
        pick_item(
            session, [], name_of=lambda v: v, stable_id_of=lambda v: hash(v),
            title="Test", empty_message="No one here yet.", refresh=refresh, masthead="=== MY MASTHEAD ===",
        )
    )
    text = _written(session)
    assert "=== MY MASTHEAD ===" in text
    assert "No one here yet." in text


def test_masthead_persists_across_an_internal_redraw_from_paging():
    # 40 items at the default page size forces at least two pages;
    # "n" (next page) triggers _render() a second time from *inside*
    # pick_item's own loop, not a fresh top-level call -- proving the
    # masthead is threaded through the closure, not just written once
    # before the first render.
    items = [f"item{i}" for i in range(40)]
    session = FakeSession(["n", "b"])

    asyncio.run(
        pick_item(
            session, items, name_of=lambda v: v, stable_id_of=lambda v: hash(v),
            title="Test", empty_message="none", masthead="=== MY MASTHEAD ===",
        )
    )
    text = _written(session)
    assert text.count("=== MY MASTHEAD ===") == 2


def test_masthead_with_redraw_in_place_issues_clear_before_the_masthead_not_after():
    session = FakeSession(["b"])

    asyncio.run(
        pick_item(
            session, ["alpha"], name_of=lambda v: v, stable_id_of=lambda v: hash(v),
            title="Test", empty_message="none", masthead="=== MY MASTHEAD ===", redraw_in_place=True,
        )
    )
    text = _written(session)
    assert text.index(clear_screen()) < text.index("=== MY MASTHEAD ===")
    # screen_title's own clear=True path must never fire alongside a
    # masthead (it would wipe the just-written masthead -- the exact
    # hazard this parameter's own docstring documents) -- so there must
    # be exactly one clear_screen() in the whole render, not two.
    assert text.count(clear_screen()) == 1

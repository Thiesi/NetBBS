"""Focused regressions for issue #112's shared-picker edge cases."""

from __future__ import annotations

import asyncio

from netbbs.net.char_input import REDRAW_KEY, REFRESH_KEY
from netbbs.net.picker import pick_item
from netbbs.net.session import Session


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
        # Same ordered queue read_key() serves from -- the on_sort tests
        # below need search's own "Search: " prompt to work.
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


def test_refreshable_picker_can_start_empty_and_populate_without_reentry():
    session = FakeSession([REFRESH_KEY, "0", "1"])
    refresh_calls = 0

    async def refresh():
        nonlocal refresh_calls
        refresh_calls += 1
        return ["bob"]

    result = asyncio.run(
        pick_item(
            session,
            [],
            name_of=lambda value: value,
            stable_id_of=lambda _value: 1,
            title="Who's online",
            empty_message="No one else is online right now.",
            refresh=refresh,
        )
    )

    assert refresh_calls == 1
    assert result == "bob"
    text = _written(session)
    assert "No one else is online right now." in text
    assert "Ctrl-R: refresh" in text


def test_unsupported_ctrl_r_bells_without_erasing_existing_screen_content():
    session = FakeSession([REFRESH_KEY, "b"])

    result = asyncio.run(
        pick_item(
            session,
            ["alpha"],
            name_of=lambda value: value,
            stable_id_of=lambda _value: 1,
            title="Items",
            empty_message="none",
        )
    )

    assert result is None
    text = _written(session)
    assert "\a" in text
    assert "\b \b" not in text


def test_unechoed_control_as_second_selection_key_erases_only_first_digit():
    for control in (REDRAW_KEY, REFRESH_KEY):
        session = FakeSession(["0", control, "b"])

        result = asyncio.run(
            pick_item(
                session,
                ["alpha"],
                name_of=lambda value: value,
                stable_id_of=lambda _value: 1,
                title="Items",
                empty_message="none",
            )
        )

        assert result is None
        text = _written(session)
        assert "\b \b\a" in text
        assert "\b \b\b \b\a" not in text


# -- on_sort / sort_label (design doc, dogfood feature request) -------------


def test_order_command_is_absent_without_on_sort():
    session = FakeSession(["b"])
    asyncio.run(
        pick_item(
            session, ["alpha"], name_of=lambda v: v, stable_id_of=lambda _v: 1,
            title="Items", empty_message="none",
        )
    )
    assert "rder" not in _written(session)


def test_o_is_rejected_like_any_unrecognized_key_without_on_sort():
    session = FakeSession(["o", "b"])
    asyncio.run(
        pick_item(
            session, ["alpha"], name_of=lambda v: v, stable_id_of=lambda _v: 1,
            title="Items", empty_message="none",
        )
    )
    assert "\a" in _written(session)


def test_on_sort_replaces_items_and_working_set_and_resets_to_page_one():
    """A re-sort must be reflected by a later search too, not just the
    immediate redraw -- proven here by paging to page 2 first, then
    re-sorting, then searching for an item only present in the *new*
    list (a single match selects it)."""
    session = FakeSession(["n", "o", "/", "zzz"])
    on_sort_calls = 0

    async def on_sort():
        nonlocal on_sort_calls
        on_sort_calls += 1
        return ["zzz"]

    result = asyncio.run(
        pick_item(
            session,
            [f"item{n}" for n in range(1, 200)],
            name_of=lambda v: v,
            stable_id_of=lambda v: 9 if v == "zzz" else int(v.removeprefix("item")),
            title="Items",
            empty_message="none",
            on_sort=on_sort,
        )
    )

    assert on_sort_calls == 1
    assert result == "zzz"


def test_on_sort_returning_none_leaves_the_current_list_unchanged():
    session = FakeSession(["o", "0", "1"])

    async def on_sort():
        return None

    result = asyncio.run(
        pick_item(
            session, ["alpha", "beta"], name_of=lambda v: v, stable_id_of=lambda _v: 1,
            title="Items", empty_message="none", on_sort=on_sort,
        )
    )
    assert result == "alpha"


def test_sort_label_is_read_fresh_on_every_render():
    session = FakeSession(["o", "b"])
    label_calls = 0

    async def on_sort():
        return ["alpha"]

    def sort_label():
        nonlocal label_calls
        label_calls += 1
        return "Activity" if label_calls == 1 else "Alphabetical"

    asyncio.run(
        pick_item(
            session, ["alpha"], name_of=lambda v: v, stable_id_of=lambda _v: 1,
            title="Items", empty_message="none", on_sort=on_sort, sort_label=sort_label,
        )
    )
    text = _written(session)
    assert "Sort: Activity" in text
    assert "Sort: Alphabetical" in text

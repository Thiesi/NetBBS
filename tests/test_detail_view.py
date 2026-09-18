"""`netbbs.net.detail_view.show_detail`, and the two SysOp-console defects it exists for.

1. A status screen taller than the terminal scrolled its own top away. Link
   status was twenty-odd rows of `Label: value` on a 24-row terminal: the
   node's identity had gone by the time `Choice:` appeared.
2. A screen that printed a result and returned had that result wiped by its
   parent menu's clear-and-redraw before it could be read (redraw-in-place is
   the default for new accounts). Prune drafts, GC storage, Repair carried
   posts, and the empty Outbox and Diagnostics all did this.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.net.admin_flow import admin_menu
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.detail_view import show_detail
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.net.session import Session
from netbbs.rendering import clear_screen, menu_key
from netbbs.rendering.detail import Field, Section
from netbbs.rendering.width import display_width
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane

_SGR = re.compile(r"\x1b\[[0-9;]*m")
_CLEAR = clear_screen()
_KEYS = {kind.name: kind for kind in EditorKeyKind}
_BACK = ("b", menu_key("B", "ack"))


class _Exhausted(Exception):
    """The script ran out: whatever is on the terminal now is what the SysOp is looking at."""


class ScriptedSession(Session):
    def __init__(self, inputs, *, width=80, height=24):
        self._inputs = list(inputs)
        self.written: list[str] = []
        self.terminal_width = width
        self.terminal_height = height
        self.node_display_name = "NetBBS"
        self.peer_address = None

    def _next(self) -> str:
        if not self._inputs:
            raise _Exhausted()
        return self._inputs.pop(0)

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def read_line(self, echo=True, history=None, completer=None, **kwargs) -> str:
        return self._next()

    async def read_key(self, echo=True) -> str:
        return self._next()

    async def read_editor_key(self, *, distinguish_ctrl_h=False) -> EditorKey:
        raw = self._next()
        if raw in _KEYS and len(raw) > 1:
            return EditorKey(_KEYS[raw])
        return EditorKey(EditorKeyKind.CHAR, char=raw)

    async def close(self) -> None:
        pass

    async def read_byte(self):
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError

    def on_terminal(self) -> list[str]:
        """The rows on the terminal now: everything since the last clear."""
        text = "".join(self.written)
        text = text[text.rfind(_CLEAR) + len(_CLEAR):] if _CLEAR in text else text
        return _SGR.sub("", text).split("\r\n")


def _sections(count: int, rows: int = 4) -> list[Section]:
    return [
        Section(f"Group {index}", [Field(f"Fact {index}.{row}", f"value-{index}-{row}") for row in range(rows)])
        for index in range(count)
    ]


def _show(session, **kwargs):
    kwargs.setdefault("title", "NetBBS / Status\r\n---------------")
    kwargs.setdefault("actions", [_BACK])
    kwargs.setdefault("redraw_in_place", True)
    return asyncio.run(show_detail(session, **kwargs))


def test_a_panel_that_fits_is_one_page_with_no_paging_keys():
    session = ScriptedSession(["b"])
    assert _show(session, sections=_sections(2)) == ("b", 0)
    screen = "\n".join(session.on_terminal())
    assert "GROUP 0" in screen and "GROUP 1" in screen
    assert "Page " not in screen and "[N]ext" not in screen


@pytest.mark.parametrize("height", [12, 24, 40])
def test_no_page_is_ever_taller_than_the_terminal(height):
    session = ScriptedSession(["PAGE_DOWN"] * 12 + ["b"], height=height)
    _show(session, sections=_sections(8))
    renders = "".join(session.written).split(_CLEAR)[1:]
    assert len(renders) == 13
    # Leaving the screen ends the prompt's row; that newline is not a row of the page.
    renders[-1] = renders[-1].removesuffix("\r\n")
    for render in renders:
        assert len(render.split("\r\n")) <= height


def test_paging_reaches_every_group_and_wraps_around():
    session = ScriptedSession(["PAGE_DOWN", "PAGE_DOWN", "PAGE_DOWN", "b"], height=14)
    _key, page = _show(session, sections=_sections(3))
    everything = _SGR.sub("", "".join(session.written))
    assert all(f"GROUP {index}" in everything for index in range(3))
    assert "(Page 1 of 3 -- PgUp/PgDn to switch)" in everything
    assert page == 0  # three pages, three PgDn: back where it started


def test_n_and_p_page_when_the_screen_does_not_use_them_itself():
    session = ScriptedSession(["n", "p", "p", "b"], height=14)
    _key, page = _show(session, sections=_sections(3))
    assert "[N]ext page" in _SGR.sub("", "".join(session.written))
    assert page == 2


def test_a_screen_that_owns_p_keeps_it_and_pages_with_angle_brackets():
    session = ScriptedSession([">", "p"], height=14)
    key, page = _show(session, sections=_sections(3), actions=[("p", menu_key("P", "eers")), _BACK])
    assert (key, page) == ("p", 1)
    bar = _SGR.sub("", "".join(session.written))
    assert "[>] Next page" in bar and "[N]ext page" not in bar


def test_a_key_the_screen_does_not_offer_bells_and_redraws_nothing():
    session = ScriptedSession(["z", "PAGE_DOWN", "b"])
    assert _show(session, sections=_sections(1)) == ("b", 0)
    assert "".join(session.written).count(_CLEAR) == 1
    assert "\a" in "".join(session.written)


def test_a_result_message_is_shown_once_directly_above_the_prompt():
    session = ScriptedSession(["PAGE_DOWN", "b"], height=14)
    _show(session, sections=_sections(3), message="Identity changes acknowledged.")
    first, second = (_SGR.sub("", render) for render in "".join(session.written).split(_CLEAR)[1:])
    assert first.index("[B]ack") < first.index("Identity changes acknowledged.") < first.index("Choice: ")
    assert first.rstrip().split("\r\n")[-2] == "Identity changes acknowledged."
    assert "Identity changes acknowledged." not in second


def test_without_redraw_in_place_it_prints_below_instead_of_clearing_but_still_pages():
    session = ScriptedSession(["PAGE_DOWN", "b"], height=14)
    _show(session, sections=_sections(3), redraw_in_place=False)
    text = "".join(session.written)
    assert _CLEAR not in text
    assert "(Page 2 of 3" in _SGR.sub("", text)


def test_the_page_a_caller_left_is_where_a_redraw_comes_back_to():
    session = ScriptedSession(["b"], height=14)
    _key, page = _show(session, sections=_sections(3), page=2)
    assert page == 2
    assert "GROUP 2" in "\n".join(session.on_terminal())


# -- the two defects, against the real console -----------------------------------


@pytest.fixture
def lane(tmp_path):
    database = Database(tmp_path / "node.db")
    sysop = create_user(database, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    set_redraw_in_place_enabled(database, sysop, True)
    database.close()
    database_lane = DatabaseLane(tmp_path / "node.db")
    database_lane.sysop = sysop
    yield database_lane
    database_lane.close()


def _link_context():
    from netbbs.link.boards import LinkConfigSnapshot, LinkContext
    from netbbs.link.node_identity import bootstrap_node_identity
    from netbbs.link.protocol import LinkNode

    identity = bootstrap_node_identity("roanoke")
    return LinkContext(
        node_identity=identity, link_node=LinkNode(identity=identity),
        link_config=LinkConfigSnapshot(
            outgoing_only=False, advertised_host="roanoke.example", advertised_port=7862,
            seeds=("http://seed.example:7862",), sync_interval_seconds=60.0, relay_serving_enabled=True,
            max_relay_clients=8, max_peers=64, max_carried_boards=32, max_carried_channels=32,
        ),
    )


def _walk(lane, keys, *, width=80, height=24, link_context=None) -> ScriptedSession:
    session = ScriptedSession(keys, width=width, height=height)
    with pytest.raises(_Exhausted):
        asyncio.run(admin_menu(session, lane, lane.sysop, link_context=link_context))
    return session


@pytest.mark.parametrize("width,height", [(80, 24), (40, 24), (132, 50)])
def test_link_status_never_scrolls_its_own_top_off_the_terminal(lane, width, height):
    pages = []
    keys = ["l"]
    for _ in range(6):
        session = _walk(lane, keys, width=width, height=height, link_context=_link_context())
        rows = session.on_terminal()
        assert len(rows) <= height, f"{len(rows)} rows on a {height}-row terminal"
        assert all(display_width(row) <= width for row in rows)
        assert "Link status" in rows[0]  # the title is still on screen
        pages.append("\n".join(rows))
        keys = keys + ["PAGE_DOWN"]
    everything = "\n".join(pages)
    for heading in ("IDENTITY", "PEERS AND SEEDS", "RELAYS", "CONTENT"):
        assert heading in everything
    for label in ("Technical identity:", "Verified peers:", "Relay mailbox:", "Known events:"):
        assert label in everything


@pytest.mark.parametrize("keys,expected", [
    (["o", "p"], "Would delete"),
    (["c", "f", "g"], "Would reclaim"),
])
def test_a_maintenance_report_stays_on_screen_until_it_is_left(lane, keys, expected):
    session = _walk(lane, keys)
    screen = "\n".join(session.on_terminal())
    assert expected in screen
    assert "[B]ack" in screen
    assert "[O]perations" not in screen and "[G]C storage" not in screen  # the menu has not redrawn over it


def test_repair_carried_posts_holds_its_result(lane):
    session = _walk(lane, ["o", "r"], link_context=_link_context())
    screen = "\n".join(session.on_terminal())
    assert "nothing to do" in screen and "[B]ack" in screen


def test_an_empty_outbox_and_an_empty_diagnostic_log_are_screens_not_flashes(lane):
    for keys, expected in ((["x"], "No outbound work items recorded yet."), (["o", "d"], "Nothing logged yet.")):
        session = _walk(lane, keys, link_context=_link_context())
        screen = "\n".join(session.on_terminal())
        assert expected in screen and "[B]ack" in screen

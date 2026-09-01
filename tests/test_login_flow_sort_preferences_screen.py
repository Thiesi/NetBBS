"""
Tests for the "Your sort preferences" review/clear screen
(`netbbs.net.login_flow._sort_preferences_screen`, reached from
`_edit_profile`'s own `[S]ort preferences` option) -- design doc,
dogfood feature request: the discoverability half of the `[O]rder`
command, so a saved override is never a silent, forgotten surprise.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.communities import create_community
from netbbs.net import login_flow
from netbbs.net.session import Session
from netbbs.sort_preferences import get_effective_sort_mode, set_sort_preference
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


class FakeSession(Session):
    """One ordered input queue serves both `read_key()` (pick_item's
    browsing/selection) and `read_line()` (prompt_yes_no's line-
    oriented fallback, since read_editor_key isn't implemented here) --
    same shape tests/test_chat_flow_picker_authorization.py's own
    FakeSession already established for this exact mixed-input need."""

    def __init__(self, inputs: list[str] | None = None):
        self._inputs = list(inputs or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        return self._inputs.pop(0)

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        return self._inputs.pop(0)

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False):
        raise NotImplementedError

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        raise NotImplementedError

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _visible_text(session: FakeSession) -> str:
    return _ANSI_ESCAPE_RE.sub("", _written_text(session))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


def test_no_preferences_shows_a_friendly_empty_message(db, lane, alice):
    session = FakeSession([])
    asyncio.run(login_flow._sort_preferences_screen(session, lane, alice))
    text = _written_text(session)
    assert "no saved sort preferences" in text


def test_global_preference_is_listed_and_can_be_cleared(db, lane, alice):
    # "board" (default "activity"), not "channel" -- channels default to
    # "alphabetical" (DEFAULT_SORT_MODE_BY_KIND), so setting that exact
    # mode wouldn't let clearing be observed against the fallback.
    set_sort_preference(db, alice, "board", "alphabetical")
    session = FakeSession(["0", "1", "y"])
    asyncio.run(login_flow._sort_preferences_screen(session, lane, alice))
    text = _written_text(session)
    assert "Message boards" in text
    assert "Global default" in text
    assert get_effective_sort_mode(db, alice, "board") == "activity"  # cleared, back to default


def test_declining_the_clear_confirmation_leaves_the_preference_intact(db, lane, alice):
    set_sort_preference(db, alice, "board", "alphabetical")
    # The screen loops back to the picker after a decline (the
    # preference is still there to show) -- "b" backs out of that
    # second pass.
    session = FakeSession(["0", "1", "n", "b"])
    asyncio.run(login_flow._sort_preferences_screen(session, lane, alice))
    assert get_effective_sort_mode(db, alice, "board") == "alphabetical"  # not cleared


def test_community_scoped_preference_shows_the_communitys_real_name(db, lane, alice):
    community = create_community(db, "Retro Computing", creator=alice)
    set_sort_preference(db, alice, "board", "volume", community_id=community.id)
    session = FakeSession(["b"])
    asyncio.run(login_flow._sort_preferences_screen(session, lane, alice))
    text = _written_text(session)
    assert "Community: Retro Computing" in text


def test_category_scoped_preference_shows_the_categorys_real_name(db, lane, alice):
    from netbbs.boards.categories import create_category

    category = create_category(db, "Amiga", created_by=alice)
    set_sort_preference(db, alice, "board", "recent", category_id=category.id)
    session = FakeSession(["b"])
    asyncio.run(login_flow._sort_preferences_screen(session, lane, alice))
    text = _written_text(session)
    assert "Category: Amiga" in text


def test_profile_screen_shows_the_saved_preference_count_and_offers_the_menu_option(db, lane, alice):
    # Profile pagination follow-up: Sort preferences lives on ACCOUNT,
    # Profile's 4th page (of 4) once its 14 fields no longer fit
    # unpaginated at a real 80x24 terminal -- not visible on the
    # initial (Identity) render this test used to check directly. This
    # FakeSession's own `read_editor_key` isn't implemented (falls back
    # to plain single-character `read_key()`), so it can't script a
    # `PAGE_DOWN` press the way a full navigable session could -- the
    # only way to reach the Account page here is a hotkey jump (every
    # hotkey works regardless of current page, by design), so this
    # presses [S]ort preferences itself (which also opens its own
    # picker screen), backs out of that picker, and checks the
    # resulting Profile re-render -- now sitting on the Account page --
    # instead of the very first, pre-navigation one.
    set_sort_preference(db, alice, "channel", "alphabetical")
    set_sort_preference(db, alice, "board", "volume")
    session = FakeSession(["s", "b", "b"])
    asyncio.run(login_flow._edit_profile(session, lane, alice))
    text = _visible_text(session)
    assert "Sort preferences: 2 saved" in text
    assert "ort preferences" in text  # the [S]ort preferences menu option itself

"""
Tests for the main menu's content entries and the Communities path
(design doc §16, issue #838): [M]essage boards/[C]hat/[F]iles/[G]ames
over the whole node, C[o]mmunities with each Community's own page, and
category leak prevention. The
underlying data model/core logic (netbbs.communities) is covered
separately in tests/test_communities.py; these drive the real
netbbs.net.login_flow entry points.
"""

from __future__ import annotations

import asyncio
import re

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.boards.categories import create_category as create_board_category
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.communities import create_community
from netbbs.doors.registry import create_door
from netbbs.files.areas import create_file_area
from netbbs.net.char_input import InputHistory
from netbbs.net.main_menu import _main_menu
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


class FakeSession:
    def __init__(self, keys=None, lines=None):
        self._keys = iter(keys or [])
        self._lines = iter(lines or [])
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        key = next(self._keys, None)
        if key is None:
            raise AssertionError("FakeSession.read_key() called with no more scripted keys")
        return key

    async def read_line(self, echo: bool = True, history=None, completer=None, *, live_buffer=None, lock=None, **kwargs) -> str:
        # Every scenario in this file ends by pressing "l" to leave the
        # main menu cleanly; default the now-required logoff
        # confirmation to "y" once the scripted lines run out, rather
        # than making every call site thread an explicit confirmation
        # answer through just to reach its real assertions.
        return next(self._lines, "y")


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _visible_text(session: FakeSession) -> str:
    return _ANSI_ESCAPE_RE.sub("", _written_text(session))


def _run_main_menu(session, db, user):
    # File areas (like mail) are reachable through _main_menu only with
    # a real lane -- constructed here, once,
    # so every existing call site in this file exercises the real
    # lane-is-present path rather than the lane=None degrade.
    lane = DatabaseLane(db.path)
    try:
        asyncio.run(
            _main_menu(
                session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), user, lane=lane
            )
        )
    finally:
        lane.close()


# -- main-menu content entries ------------------------------------------------


def test_main_menu_always_offers_boards_chat_and_files(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    session = FakeSession(keys=["l"])

    _run_main_menu(session, db, bob)

    text = _visible_text(session)
    assert "[M]essage boards" in text
    assert "[C]hat" in text
    assert "[F]iles" in text
    assert "[/] Find" in text
    # Issue #838: the old type picker and the "outside a Community"
    # bucket are gone from the menu entirely.
    assert "ncategorized" not in text
    assert "ump to" not in text
    assert "mmunities" not in text
    db.close()


def test_main_menu_hides_games_with_no_doors(tmp_path):
    # Issue #838 (F108): "Games" led to "No doors are available to you yet".
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    session = FakeSession(keys=["g", "l"])

    _run_main_menu(session, db, bob)

    text = _visible_text(session)
    assert "[G]ames" not in text
    assert "No doors" not in text
    db.close()


def test_main_menu_shows_games_once_a_door_exists(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    create_door(db, "Retro Trivia", "/bin/true", creator=bob)
    session = FakeSession(keys=["l"])

    _run_main_menu(session, db, bob)

    assert "[G]ames" in _visible_text(session)
    db.close()


def test_main_menu_shows_communities_when_one_exists(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    create_community(db, "Vintage Computing", creator=bob)
    session = FakeSession(keys=["l"])

    _run_main_menu(session, db, bob)

    assert "C[o]mmunities" in _visible_text(session)
    db.close()


def test_main_menu_hides_communities_that_are_all_hidden_from_a_regular_user(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    create_community(db, "Secret Club", hidden=True, creator=bob)
    session = FakeSession(keys=["l"])

    _run_main_menu(session, db, bob)

    assert "C[o]mmunities" not in _visible_text(session)
    db.close()


def test_main_menu_shows_hidden_community_to_a_sysop(tmp_path):
    db = Database(tmp_path / "node.db")
    sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    create_community(db, "Secret Club", hidden=True, creator=sysop)
    session = FakeSession(keys=["l"])

    _run_main_menu(session, db, sysop)

    assert "C[o]mmunities" in _visible_text(session)
    db.close()


def test_boards_entry_lists_every_board_whatever_its_community(tmp_path):
    # Issue #838 (F043): a board outside every Community is simply a
    # board -- no "Uncategorized" bucket to find it in.
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    community = create_community(db, "Vintage Computing", creator=bob)
    create_board(db, "amiga", community_id=community.id, creator=bob)
    create_board(db, "general", creator=bob)

    session = FakeSession(keys=["m", "b", "l"])

    _run_main_menu(session, db, bob)

    text = _visible_text(session)
    assert "amiga" in text
    assert "general" in text
    assert "Available message boards" in text
    assert "ncategorized" not in text
    db.close()


def test_chat_and_files_entries_list_every_channel_and_area(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    community = create_community(db, "Vintage Computing", creator=bob)
    create_channel(db, "amiga-chat", community_id=community.id, creator=bob)
    create_channel(db, "general-chat", creator=bob)
    create_file_area(db, "amiga-files", community_id=community.id, creator=bob)
    create_file_area(db, "general-files", creator=bob)

    session = FakeSession(keys=["c", "b", "f", "b", "l"])

    _run_main_menu(session, db, bob)

    text = _written_text(session)
    for name in ("amiga-chat", "general-chat", "amiga-files", "general-files"):
        assert name in text
    db.close()


def test_sysop_on_an_empty_node_is_told_where_to_create_content(tmp_path):
    # Issue #838 (F017).
    db = Database(tmp_path / "node.db")
    sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    session = FakeSession(keys=["l"])

    _run_main_menu(session, db, sysop)

    assert "No boards yet: create one under SysOp" in _visible_text(session)
    db.close()


def test_empty_node_hint_is_for_the_sysop_only_and_goes_once_content_exists(tmp_path):
    db = Database(tmp_path / "node.db")
    sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    session = FakeSession(keys=["l"])
    _run_main_menu(session, db, bob)
    assert "No boards yet" not in _visible_text(session)

    create_file_area(db, "uploads", creator=sysop)
    session = FakeSession(keys=["l"])
    _run_main_menu(session, db, sysop)
    assert "No boards yet" not in _visible_text(session)
    db.close()


# -- entering a Community: its page, scoped browsing -------------------------


def test_community_page_offers_only_kinds_it_holds_with_counts(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    community = create_community(db, "Vintage Computing", description="Old machines, new tricks", creator=bob)
    create_board(db, "amiga", community_id=community.id, creator=bob)
    create_board(db, "c64", community_id=community.id, creator=bob)
    # No channel or file area in this Community.
    session = FakeSession(keys=["o", "0", "1", "b", "b", "l"])

    _run_main_menu(session, db, bob)

    text = _visible_text(session)
    page = text[text.index("NetBBS › Communities › Vintage Computing"):]
    assert "Old machines, new tricks" in page
    assert "[M]essage boards" in page
    assert "2 boards" in page
    assert "[C]hat" not in page.split("Choice:")[0]
    assert "[F]iles" not in page.split("Choice:")[0]
    db.close()


def test_back_from_a_community_page_returns_to_the_communities_list(tmp_path):
    # Issue #838 (F045): one level down, so Back goes one level up.
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    vintage = create_community(db, "Vintage Computing", creator=bob)
    create_board(db, "amiga", community_id=vintage.id, creator=bob)
    create_community(db, "Politics", creator=bob)
    session = FakeSession(keys=["o", "0", "2", "b", "b", "l"])

    _run_main_menu(session, db, bob)

    text = _visible_text(session)
    after_page = text[text.index("NetBBS › Communities › Vintage Computing"):]
    # The list is drawn again, on the Community just left.
    assert "Communities" in after_page
    assert "> 02." in after_page
    db.close()


def test_community_scoped_board_browsing_excludes_other_communities_and_uncategorized(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    vintage = create_community(db, "Vintage Computing", creator=bob)
    politics = create_community(db, "Politics", creator=bob)
    create_board(db, "amiga", community_id=vintage.id, creator=bob)
    create_board(db, "elections", community_id=politics.id, creator=bob)
    create_board(db, "general", creator=bob)  # no Community

    # Alphabetical: Politics is #01.
    session = FakeSession(keys=["o", "0", "1", "m", "b", "b", "b", "l"])

    _run_main_menu(session, db, bob)

    text = _written_text(session)
    assert "elections" in text
    assert "amiga" not in text
    assert "general" not in text
    db.close()


def test_community_scoped_board_browsing_shows_community_name_in_title(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    community = create_community(db, "Vintage Computing", creator=bob)
    create_board(db, "amiga", community_id=community.id, creator=bob)

    session = FakeSession(keys=["o", "0", "1", "m", "b", "b", "b", "l"])

    _run_main_menu(session, db, bob)

    # Dogfood-reported bug: this used to be baked into the title text
    # itself ("Vintage Computing › message boards"), which visually
    # mimicked screen_title's own ancestor/current-location color split
    # without actually being one -- the whole string rendered in one
    # flat color. It's now a real breadcrumb ancestor segment, muted
    # like every other ancestor, with only "Message boards" itself in
    # the current-location color.
    assert "NetBBS › Vintage Computing › Message boards" in _visible_text(session)
    db.close()


# -- category leak prevention (design doc §16) -------------------------------


def test_category_used_only_by_another_communitys_board_does_not_leak(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    vintage = create_community(db, "Vintage Computing", creator=bob)
    politics = create_community(db, "Politics", creator=bob)
    category = create_board_category(db, "Hardware", created_by=bob)
    # The category is used by a board in `politics`, not `vintage`.
    create_board(db, "elections", community_id=politics.id, category_id=category.id, creator=bob)
    create_board(db, "amiga", community_id=vintage.id, creator=bob)  # uncategorized within vintage

    # Enter `vintage` specifically (need to know which pick index it is
    # -- alphabetically "Politics" < "Vintage Computing", so vintage is
    # #02).
    session = FakeSession(keys=["o", "0", "2", "m", "b", "b", "b", "l"])

    _run_main_menu(session, db, bob)

    text = _written_text(session)
    assert "[Hardware]" not in text  # category picker line format: "[Name]"
    assert "amiga" in text
    db.close()


def test_category_used_by_a_board_in_this_community_is_shown(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    community = create_community(db, "Vintage Computing", creator=bob)
    category = create_board_category(db, "Hardware", created_by=bob)
    create_board(db, "amiga", community_id=community.id, category_id=category.id, creator=bob)

    session = FakeSession(keys=["o", "0", "1", "m", "b", "b", "b", "l"])

    _run_main_menu(session, db, bob)

    assert "[Hardware]" in _written_text(session)
    db.close()


# -- channels and file areas get the same treatment (spot-check) ------------


def test_community_scoped_channel_and_area_browsing_are_filtered_too(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    community = create_community(db, "Vintage Computing", creator=bob)
    create_channel(db, "amiga-chat", community_id=community.id, creator=bob)
    create_channel(db, "general-chat", creator=bob)  # no Community
    create_file_area(db, "amiga-files", community_id=community.id, creator=bob)
    create_file_area(db, "general-files", creator=bob)  # no Community

    session = FakeSession(keys=["o", "0", "1", "c", "b", "f", "b", "b", "b", "l"])

    _run_main_menu(session, db, bob)

    text = _written_text(session)
    assert "amiga-chat" in text
    assert "general-chat" not in text
    assert "amiga-files" in text
    assert "general-files" not in text
    db.close()

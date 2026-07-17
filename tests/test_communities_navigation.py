"""
Tests for the Communities main-menu navigation restructuring (design
doc §16, round 84/107): [C]ommunities/[U]ncategorized/[J]ump to...
replacing the flat [M]essage Boards/[C]hat/[F]ile areas split, the
shared resource-type sub-menu, and category leak prevention. The
underlying data model/core logic (netbbs.communities) is covered
separately in tests/test_communities.py; these drive the real
netbbs.net.login_flow entry points.
"""

from __future__ import annotations

import asyncio

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.boards.categories import create_category as create_board_category
from netbbs.chat.channels import create_channel
from netbbs.chat.hub import ChatHub
from netbbs.chat.mailbox import MessageMailbox
from netbbs.chat.presence import PresenceRegistry
from netbbs.communities import create_community
from netbbs.files.areas import create_file_area
from netbbs.net.char_input import InputHistory
from netbbs.net.login_flow import _main_menu
from netbbs.storage.database import Database


class FakeSession:
    def __init__(self, keys=None, lines=None):
        self._keys = iter(keys or [])
        self._lines = iter(lines or [])
        self.written: list[str] = []
        self.terminal_width = 80
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

    async def read_line(self, echo: bool = True, history=None, completer=None, *, live_buffer=None, lock=None) -> str:
        return next(self._lines, "")


def _written_text(session: FakeSession) -> str:
    return "".join(session.written)


def _run_main_menu(session, db, user):
    asyncio.run(
        _main_menu(session, db, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), user)
    )


# -- main-menu conditional visibility ----------------------------------------


def test_main_menu_hides_communities_and_uncategorized_with_nothing_to_show(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    session = FakeSession(keys=["l"])

    _run_main_menu(session, db, bob)

    text = _written_text(session)
    assert "ommunities" not in text
    assert "ncategorized" not in text
    assert "ump to..." in text  # always shown
    db.close()


def test_main_menu_shows_communities_when_one_exists(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    create_community(db, "Vintage Computing", creator=bob)
    session = FakeSession(keys=["l"])

    _run_main_menu(session, db, bob)

    assert "ommunities" in _written_text(session)
    db.close()


def test_main_menu_shows_uncategorized_when_an_uncategorized_board_exists(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    create_board(db, "general", creator=bob)  # no community_id
    session = FakeSession(keys=["l"])

    _run_main_menu(session, db, bob)

    assert "ncategorized" in _written_text(session)
    db.close()


def test_main_menu_hides_uncategorized_when_every_board_belongs_to_a_community(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    community = create_community(db, "Vintage Computing", creator=bob)
    create_board(db, "amiga", community_id=community.id, creator=bob)
    session = FakeSession(keys=["l"])

    _run_main_menu(session, db, bob)

    assert "ncategorized" not in _written_text(session)
    db.close()


def test_main_menu_hides_communities_that_are_all_hidden_from_a_regular_user(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    create_community(db, "Secret Club", hidden=True, creator=bob)
    session = FakeSession(keys=["l"])

    _run_main_menu(session, db, bob)

    assert "ommunities" not in _written_text(session)
    db.close()


def test_main_menu_shows_hidden_community_to_a_sysop(tmp_path):
    db = Database(tmp_path / "node.db")
    sysop = create_user(db, "sysop", password="hunter2pw", user_level=SYSOP_LEVEL)
    create_community(db, "Secret Club", hidden=True, creator=sysop)
    session = FakeSession(keys=["l"])

    _run_main_menu(session, db, sysop)

    assert "ommunities" in _written_text(session)
    db.close()


# -- entering a Community: scoped browsing + resource-type sub-menu ---------


def test_entering_a_community_only_offers_resource_types_with_matching_items(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    community = create_community(db, "Vintage Computing", creator=bob)
    create_board(db, "amiga", community_id=community.id, creator=bob)
    # No channel or file area in this Community -- sub-menu should only
    # offer [M]essage Boards. "c","0","1" enters the Community picker
    # and selects the only one; "b" backs out of the resource-type
    # sub-menu straight back to the main menu (_enter_communities isn't
    # itself a loop, so no extra "b" is needed there).
    session = FakeSession(keys=["c", "0", "1", "b", "l"])

    _run_main_menu(session, db, bob)

    text = _written_text(session)
    assert "essage Boards" in text
    assert "hat" not in text  # [C]hat never rendered -- no channel in this Community
    db.close()


def test_community_scoped_board_browsing_excludes_other_communities_and_uncategorized(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    vintage = create_community(db, "Vintage Computing", creator=bob)
    politics = create_community(db, "Politics", creator=bob)
    create_board(db, "amiga", community_id=vintage.id, creator=bob)
    create_board(db, "elections", community_id=politics.id, creator=bob)
    create_board(db, "general", creator=bob)  # uncategorized

    # main menu -> Communities -> pick #01 (alphabetical: Politics is
    # created second but let's just pick and check which board shows)
    session = FakeSession(keys=["c", "0", "1", "m", "b", "b", "b", "l"])

    _run_main_menu(session, db, bob)

    text = _written_text(session)
    # Exactly one of the two Community boards should appear (whichever
    # Community sorts first alphabetically), and neither the other
    # Community's board nor the uncategorized one should ever appear.
    assert ("amiga" in text) != ("elections" in text)  # exactly one, not both
    assert "general" not in text
    db.close()


def test_uncategorized_board_browsing_shows_only_uncategorized_boards(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    community = create_community(db, "Vintage Computing", creator=bob)
    create_board(db, "amiga", community_id=community.id, creator=bob)
    create_board(db, "general", creator=bob)  # uncategorized

    session = FakeSession(keys=["u", "m", "b", "b", "l"])

    _run_main_menu(session, db, bob)

    text = _written_text(session)
    assert "general" in text
    assert "amiga" not in text
    assert "Uncategorized" in text
    db.close()


def test_jump_shows_the_full_unfiltered_list(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    community = create_community(db, "Vintage Computing", creator=bob)
    create_board(db, "amiga", community_id=community.id, creator=bob)
    create_board(db, "general", creator=bob)

    session = FakeSession(keys=["j", "m", "b", "b", "l"])

    _run_main_menu(session, db, bob)

    text = _written_text(session)
    assert "amiga" in text
    assert "general" in text
    assert "Available message boards" in text  # unchanged title, per round 84
    db.close()


def test_community_scoped_board_browsing_shows_community_name_in_title(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    community = create_community(db, "Vintage Computing", creator=bob)
    create_board(db, "amiga", community_id=community.id, creator=bob)

    session = FakeSession(keys=["c", "0", "1", "m", "b", "b", "b", "l"])

    _run_main_menu(session, db, bob)

    assert "Vintage Computing — message boards" in _written_text(session)
    db.close()


def test_uncategorized_browsing_shows_uncategorized_in_title(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    create_board(db, "general", creator=bob)

    session = FakeSession(keys=["u", "m", "b", "b", "l"])

    _run_main_menu(session, db, bob)

    assert "Uncategorized — message boards" in _written_text(session)
    db.close()


# -- category leak prevention (design doc §16, round 84) --------------------


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
    session = FakeSession(keys=["c", "0", "2", "m", "b", "b", "b", "l"])

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

    session = FakeSession(keys=["c", "0", "1", "m", "b", "b", "b", "l"])

    _run_main_menu(session, db, bob)

    assert "[Hardware]" in _written_text(session)
    db.close()


# -- channels and file areas get the same treatment (spot-check) ------------


def test_community_scoped_channel_and_area_browsing_are_filtered_too(tmp_path):
    db = Database(tmp_path / "node.db")
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    community = create_community(db, "Vintage Computing", creator=bob)
    create_channel(db, "amiga-chat", community_id=community.id, creator=bob)
    create_channel(db, "general-chat", creator=bob)  # uncategorized
    create_file_area(db, "amiga-files", community_id=community.id, creator=bob)
    create_file_area(db, "general-files", creator=bob)  # uncategorized

    session = FakeSession(keys=["c", "0", "1", "c", "b", "f", "b", "b", "l"])

    _run_main_menu(session, db, bob)

    text = _written_text(session)
    assert "amiga-chat" in text
    assert "general-chat" not in text
    assert "amiga-files" in text
    assert "general-files" not in text
    db.close()

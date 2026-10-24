"""
Breadcrumbs come from where the session is (issue #1246): each screen's
path is the screens that led to it, not a path typed at its call site.

These walk the SysOp console the way a SysOp does and check the exact
breadcrumb line, including the screens the issue reported as drifted.
"""

from __future__ import annotations

import asyncio

from netbbs.link.work_items import KIND_LINK_MAIL_DELIVERY, _MAX_ATTEMPTS, enqueue_work_item_without_commit, record_failure
from netbbs.net.admin_flow import admin_menu
from tests.test_admin_flow import (  # noqa: F401 -- fixtures
    FakeSession,
    _link_context,
    _visible,
    _written_text,
    db,
    isolated_door_career_directory,
    lane,
    sysop,
)


def _lines(session: FakeSession) -> list[str]:
    return [line.strip() for line in _visible(_written_text(session)).splitlines()]


def _walk(lane, sysop, keys, **kwargs) -> list[str]:
    session = FakeSession(keys)
    asyncio.run(admin_menu(session, lane, sysop, **kwargs))
    assert session.location == (), "the trail unwinds on the way out"
    return _lines(session)


def test_the_console_and_its_menus(db, lane, sysop):
    lines = _walk(lane, sysop, ["s", "b", "b"])
    assert "NetBBS › SysOp operations console" in lines
    assert "NetBBS › SysOp › Settings" in lines


def test_settings_screens_sit_under_sysop_settings(db, lane, sysop):
    assert "NetBBS › SysOp › Settings › Operating limits" in _walk(lane, sysop, ["s", "o", "b", "b", "b"])
    assert "NetBBS › SysOp › Settings › Mastheads & banners" in _walk(lane, sysop, ["s", "m", "b", "b", "b"])
    assert "NetBBS › SysOp › Settings › Timestamp display" in _walk(lane, sysop, ["s", "t", "b", "b", "b"])


def test_a_picker_inside_an_editor_is_its_child(db, lane, sysop):
    lines = _walk(lane, sysop, ["s", "t", "0", "2", "/", "Europe/Berlin", "b", "b", "b"])
    assert "NetBBS › SysOp › Settings › Timestamp display › Timezone" in lines


def test_policy_trust_screens(db, lane, sysop):
    lines = _walk(lane, sysop, ["s", "p", "s", "b", "b", "b", "b"])
    assert "NetBBS › SysOp › Settings › Policy trust" in lines
    assert "NetBBS › SysOp › Settings › Policy trust › Trust subjects" in lines
    assert "NetBBS › SysOp › Settings › Policy trust › Vouches" in _walk(lane, sysop, ["s", "p", "v", "b", "b", "b", "b"])


def test_outbox_item_picker_sits_under_the_outbox(db, lane, sysop):
    item = enqueue_work_item_without_commit(db, kind=KIND_LINK_MAIL_DELIVERY, reference_id="m1", target_fingerprint="fp1")
    for _ in range(_MAX_ATTEMPTS):
        item = record_failure(db, item, error="unreachable")
    lines = _walk(lane, sysop, ["o", "o", "i", "b", "b", "b", "b"], link_context=_link_context())
    assert "NetBBS › SysOp › Operations › Outbox" in lines
    assert "NetBBS › SysOp › Operations › Outbox › Work items needing attention" in lines


def test_a_console_shortcut_shows_the_way_it_was_taken(db, lane, sysop):
    # [Q] opens the Outbox straight from the console: Back returns there,
    # and the breadcrumb says so.
    lines = _walk(lane, sysop, ["q", "b", "b"], link_context=_link_context())
    assert "NetBBS › SysOp › Outbox" in lines


def test_no_screen_names_itself_twice(db, lane, sysop):
    walks = [
        ["s", "o", "b", "b", "b"], ["s", "m", "s", "b", "b", "b", "b"], ["s", "t", "b", "b", "b"],
        ["s", "p", "s", "b", "b", "b", "b"], ["s", "p", "v", "b", "b", "b", "b"], ["u", "b", "b"],
        ["o", "b", "b"], ["c", "b", "b"],
    ]
    for keys in walks:
        crumbs = [line for line in _walk(lane, sysop, keys) if line.startswith("NetBBS › ")]
        assert len(crumbs) >= 2, keys
        for line in crumbs:
            segments = line.split(" › ")
            assert len(segments) == len(set(segments)), line


# -- caller side --------------------------------------------------------------
#
# Walked from the main menu, which is the root: its entries' words name
# what they open ("Message boards", "Files", "Mail", "Directory").

import re  # noqa: E402

from netbbs.auth.users import create_user  # noqa: E402
from netbbs.boards.boards import create_board  # noqa: E402
from netbbs.boards.categories import create_category as create_board_category  # noqa: E402
from netbbs.chat.hub import ChatHub  # noqa: E402
from netbbs.chat.mailbox import MessageMailbox  # noqa: E402
from netbbs.chat.presence import PresenceRegistry  # noqa: E402
from netbbs.communities import create_community  # noqa: E402
from netbbs.files.areas import create_file_area  # noqa: E402
from netbbs.net.char_input import InputHistory  # noqa: E402
from netbbs.net.main_menu import _main_menu  # noqa: E402
from netbbs.storage.database import Database  # noqa: E402
from netbbs.storage.execution import DatabaseLane  # noqa: E402


class _CallerSession:
    """A duck-typed session, as most caller tests use: no `location` of its
    own until a screen names itself."""

    def __init__(self, keys):
        self._keys = iter(keys)
        self.written: list[str] = []
        self.terminal_width = 100
        self.terminal_height = 30
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_key(self, echo: bool = True) -> str:
        key = next(self._keys, None)
        if key is None:
            raise AssertionError("no more scripted keys")
        return key

    async def read_line(self, echo: bool = True, history=None, completer=None, **kwargs) -> str:
        return "y"  # the logoff confirmation


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _caller_paths(session: _CallerSession) -> list[list[str]]:
    """Every breadcrumb line drawn, as its segments."""
    text = _ANSI.sub("", "".join(session.written))
    return [
        [segment.strip() for segment in line.strip().split("›")]
        for line in text.replace("\r", "").split("\n")
        if line.strip().startswith("NetBBS › ")
    ]


def _caller_walk(tmp_path, keys, setup) -> list[list[str]]:
    database = Database(tmp_path / "node.db")
    bob = create_user(database, "bob", password="hunter2pw", user_level=10)
    setup(database, bob)
    session = _CallerSession(keys)
    caller_lane = DatabaseLane(database.path)
    try:
        asyncio.run(_main_menu(
            session, database, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), bob,
            lane=caller_lane,
        ))
    finally:
        caller_lane.close()
        database.close()
    assert getattr(session, "location", ()) == (), "the trail unwinds on the way out"
    return _caller_paths(session)


def test_a_board_is_under_message_boards(tmp_path):
    paths = _caller_walk(
        tmp_path, ["m", "0", "1", "b", "b", "l"], lambda db, bob: create_board(db, "general", creator=bob),
    )
    assert ["NetBBS", "Available message boards"] in paths
    assert ["NetBBS", "Message boards", "general"] in paths


def test_a_category_is_on_the_path_to_its_boards(tmp_path):
    def setup(db, bob):
        category = create_board_category(db, "Vintage", created_by=bob)
        create_board(db, "amiga", category_id=category.id, creator=bob)

    paths = _caller_walk(tmp_path, ["m", "0", "1", "0", "1", "b", "b", "b", "l"], setup)
    assert ["NetBBS", "Message boards", "Vintage"] in paths
    assert ["NetBBS", "Message boards", "Vintage", "amiga"] in paths


def test_a_community_board_is_under_the_community(tmp_path):
    def setup(db, bob):
        community = create_community(db, "Retro", creator=bob)
        create_board(db, "amiga", community_id=community.id, creator=bob)

    paths = _caller_walk(tmp_path, ["t", "0", "1", "m", "0", "1", "b", "b", "b", "b", "l"], setup)
    assert ["NetBBS", "Communities"] in paths
    assert ["NetBBS", "Communities", "Retro"] in paths
    assert ["NetBBS", "Communities", "Retro", "Message boards"] in paths
    assert ["NetBBS", "Communities", "Retro", "Message boards", "amiga"] in paths


def test_a_file_area_is_under_files(tmp_path):
    paths = _caller_walk(
        tmp_path, ["f", "0", "1", "b", "b", "l"], lambda db, bob: create_file_area(db, "uploads", creator=bob),
    )
    assert ["NetBBS", "Available file areas"] in paths
    assert ["NetBBS", "Files", "uploads"] in paths


def test_the_mailbox_is_under_mail(tmp_path):
    assert ["NetBBS", "Mail", "Inbox"] in _caller_walk(tmp_path, ["e", "b", "l"], lambda db, bob: None)


def test_a_member_card_is_under_the_directory(tmp_path):
    paths = _caller_walk(
        tmp_path, ["d", "0", "1", "b", "b", "l"],
        lambda db, bob: create_user(db, "alice", password="hunter2pw", user_level=10),
    )
    assert ["NetBBS", "User directory"] in paths
    assert ["NetBBS", "Directory", "alice"] in paths


def test_no_caller_screen_shows_a_name_twice(tmp_path):
    def setup(db, bob):
        community = create_community(db, "Retro", creator=bob)
        create_board(db, "amiga", community_id=community.id, creator=bob)
        create_file_area(db, "uploads", creator=bob)

    paths = _caller_walk(
        tmp_path, ["t", "0", "1", "m", "0", "1", "b", "b", "b", "b", "f", "0", "1", "b", "b", "e", "b", "l"], setup,
    )
    assert len(paths) >= 6
    for path in paths:
        assert len(set(path)) == len(path), path

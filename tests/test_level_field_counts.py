"""
Every level field in an editor says what its value means (design doc §5.7,
issue #1008): where an inherited level comes from, and how many enabled,
approved accounts it lets in, recounted as the SysOp types.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.access_map import level_context
from netbbs.auth.users import SYSOP_LEVEL, create_user, set_user_disabled
from netbbs.boards.boards import create_board
from netbbs.communities import create_community
from netbbs.files.areas import create_file_area
from netbbs.net.admin_flow import (
    _community_default_label,
    _plain_level_label,
    _resource_level_label,
    _setting_level_label,
    admin_menu,
)
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_detail_view import ScriptedSession, _Exhausted


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def sysop(db):
    user = create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)
    set_redraw_in_place_enabled(db, user, True)
    return user


@pytest.fixture
def market(db, sysop):
    """A Community defaulting reads to 10 and writes to 50, two boards and an
    area inheriting from it, and accounts at 0, 10, 10, 50 and 255 (plus a
    disabled one at 50, which is not counted)."""
    community = create_community(
        db, "Market", default_min_read_level=10, default_min_write_level=50, creator=sysop
    )
    create_board(db, "Trading Post", min_read_level=None, min_write_level=None, community_id=community.id, creator=sysop)
    create_board(db, "Wanted", min_read_level=None, min_write_level=5, community_id=community.id, creator=sysop)
    create_file_area(db, "Catalogues", min_read_level=None, min_write_level=None, community_id=community.id,
                     creator=sysop)
    for name, level in (("guest", 0), ("alice", 10), ("bob", 10), ("carol", 50)):
        create_user(db, name, password="hunter2", user_level=level)
    dave = create_user(db, "dave", password="hunter2", user_level=50)
    set_user_disabled(db, dave, True, changed_by=sysop)
    return community


def test_the_context_counts_usable_accounts_at_or_above_a_level(db, market):
    levels = level_context(db)

    assert [levels.users_at_or_above(level) for level in (0, 1, 10, 11, 50, 255, 256)] == [5, 4, 4, 2, 2, 1, 0]
    assert levels.inheriting[(market.id, "read")] == {"board": 2, "file area": 1}
    assert levels.inheriting[(market.id, "write")] == {"board": 1, "file area": 1}


def test_a_resource_level_says_where_an_inherited_one_comes_from(db, market):
    levels = level_context(db)
    draft = {"community_id": market.id, "min_read_level": None, "min_write_level": None}

    assert _resource_level_label(levels, draft, "min_read_level") == "none: 10 from Community Market · 4 users"
    assert _resource_level_label(levels, draft, "min_write_level") == "none: 50 from Community Market · 2 users"
    assert _resource_level_label(levels, {**draft, "min_read_level": 0}, "min_read_level") == "0 · 5 users"
    orphan = {"community_id": None, "min_read_level": None, "min_write_level": None}
    assert _resource_level_label(levels, orphan, "min_read_level") == "none: 0 the default · 5 users"


def test_a_write_level_below_the_read_level_is_counted_at_the_read_level(db, market):
    levels = level_context(db)
    draft = {"community_id": market.id, "min_read_level": None, "min_write_level": 5}

    assert _resource_level_label(levels, draft, "min_write_level") == "5 · 4 users (reading needs 10)"


def test_the_count_follows_the_draft_not_the_saved_resource(db, market):
    """The draft is what the editor renders, so a typed value is counted
    before it is saved, and so is a Community changed in the same draft."""
    levels = level_context(db)

    assert _resource_level_label(levels, {"community_id": market.id, "min_read_level": 50}, "min_read_level") == (
        "50 · 2 users"
    )
    assert _resource_level_label(levels, {"community_id": None, "min_read_level": None}, "min_read_level") == (
        "none: 0 the default · 5 users"
    )


def test_a_community_default_says_what_inherits_it(db, market):
    levels = level_context(db)
    draft = {"default_min_read_level": 10, "default_min_write_level": None}

    assert _community_default_label(levels, draft, "default_min_read_level", market.id) == (
        "10 · 4 users · inherited by 2 boards, 1 file area"
    )
    assert _community_default_label(levels, draft, "default_min_write_level", market.id) == (
        "none · inherited by 1 board, 1 file area"
    )
    assert _community_default_label(levels, draft, "default_min_read_level", None) == "10 · 4 users"


def test_plain_and_setting_levels_count_too(db, market):
    levels = level_context(db)

    assert _plain_level_label(levels, 10) == "10 · 4 users"
    assert _plain_level_label(levels, 255) == "255 (SysOp) · 1 user"
    assert _setting_level_label(levels, 0) == "level 0 and up · 5 users"


def test_without_a_context_the_old_value_is_shown(db, market):
    assert _resource_level_label(None, {"min_read_level": None}, "min_read_level") == "none"
    assert _plain_level_label(None, 10) == "10"


def test_the_board_editor_shows_the_source_and_the_count(db, sysop, market):
    lane = DatabaseLane(db.path)
    try:
        # Content > Message boards > List > 01 (Trading Post) > Edit.
        session = ScriptedSession(["c", "m", "l", "0", "1", "e"], height=40)
        with pytest.raises(_Exhausted):
            asyncio.run(admin_menu(session, lane, sysop))
    finally:
        lane.close()
    text = "\n".join(" ".join(row.split()) for row in session.on_terminal())

    assert "Min read level: none: 10 from Community Market · 4 users" in text
    assert "Min write level: none: 50 from Community Market · 2 users" in text

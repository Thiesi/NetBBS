"""Board and file-area lists hold still (issue #839).

The field test's caller remembered "03" for a board and found another board
there a minute later: the lists re-sorted by activity on every visit. They now
follow the SysOp's order by default -- a `position` the SysOp sets with
`[U]p`/`[D]own` on the board's or area's console screen -- and activity is an
`[O]rder` choice a caller can still make.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3

import pytest

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board, get_board_by_name, list_boards, move_board
from netbbs.boards.categories import create_category
from netbbs.boards.posts import create_post
from netbbs.communities import create_community
from netbbs.files.areas import create_file_area, get_file_area_by_name, list_file_areas, move_file_area
from netbbs.moderation.log import list_actions_for_object
from netbbs.net import board_flow
from netbbs.net.admin_flow import admin_menu
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.sort_preferences import get_effective_sort_mode, set_sort_preference
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from netbbs.storage.migrations import MIGRATIONS
from tests.test_detail_view import ScriptedSession, _Exhausted
from tests.test_login_flow_board_picker_sort import FakeSession, _visible_text


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
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


def _names(items) -> list[str]:
    return [item.name for item in items]


# -- the order itself ----------------------------------------------------------


def test_a_board_with_a_new_post_keeps_its_place(db, sysop):
    for name in ("Newcomers", "Fountain Pens", "Inks"):
        create_board(db, name, creator=sysop)
    create_post(db, get_board_by_name(db, "Inks"), sysop, "Fresh", "x")

    assert _names(list_boards(db)) == ["Newcomers", "Fountain Pens", "Inks"]


def test_a_new_board_or_area_goes_last(db, sysop):
    create_board(db, "Zebra", creator=sysop)
    create_board(db, "Apple", creator=sysop)
    create_file_area(db, "Zebra files", creator=sysop)
    create_file_area(db, "Apple files", creator=sysop)

    assert _names(list_boards(db)) == ["Zebra", "Apple"]
    assert _names(list_file_areas(db)) == ["Zebra files", "Apple files"]


def test_moving_a_board_changes_the_order_and_is_logged(db, sysop):
    for name in ("Alpha", "Beta", "Gamma"):
        create_board(db, name, creator=sysop)
    gamma = get_board_by_name(db, "Gamma")

    assert move_board(db, gamma, -1, moved_by=sysop)
    assert move_board(db, gamma, -1, moved_by=sysop)
    assert not move_board(db, gamma, -1, moved_by=sysop)
    assert _names(list_boards(db)) == ["Gamma", "Alpha", "Beta"]
    assert not move_board(db, get_board_by_name(db, "Beta"), 1, moved_by=sysop)
    assert any(e.action == "move_board" for e in list_actions_for_object(db, "board", gamma.id))


def test_a_board_moves_only_among_its_own_category(db, sysop):
    pens = create_category(db, "Pens", created_by=sysop)
    create_board(db, "Vintage", creator=sysop, category_id=pens.id)
    create_board(db, "Chatter", creator=sysop)
    create_board(db, "Modern", creator=sysop, category_id=pens.id)
    modern = get_board_by_name(db, "Modern")

    # "Chatter" sits between them in the global order but in no category:
    # one move up passes "Vintage", its only neighbour.
    assert move_board(db, modern, -1, moved_by=sysop)
    in_pens = [b.name for b in list_boards(db) if b.category_id == pens.id]
    assert in_pens == ["Modern", "Vintage"]
    assert not move_board(db, get_board_by_name(db, "Modern"), -1, moved_by=sysop)


def test_a_board_moves_only_among_its_own_community(db, sysop):
    retro = create_community(db, "Retro", creator=sysop)
    create_board(db, "Amiga", creator=sysop, community_id=retro.id)
    create_board(db, "Elsewhere", creator=sysop)
    create_board(db, "Atari", creator=sysop, community_id=retro.id)

    # "Elsewhere" sits between them, but Retro's list never shows it: one
    # move up must pass "Amiga", or Retro's callers would see nothing move.
    assert move_board(db, get_board_by_name(db, "Atari"), -1, moved_by=sysop)
    in_retro = [b.name for b in list_boards(db) if b.community_id == retro.id]
    assert in_retro == ["Atari", "Amiga"]
    assert not move_board(db, get_board_by_name(db, "Atari"), -1, moved_by=sysop)


def test_pinned_boards_stay_first_and_move_among_themselves(db, sysop):
    create_board(db, "Chatter", creator=sysop)
    create_board(db, "Rules", creator=sysop, pinned=True)
    create_board(db, "News", creator=sysop, pinned=True)

    assert _names(list_boards(db)) == ["Rules", "News", "Chatter"]
    assert not move_board(db, get_board_by_name(db, "Chatter"), -1, moved_by=sysop)
    assert move_board(db, get_board_by_name(db, "News"), -1, moved_by=sysop)
    assert _names(list_boards(db)) == ["News", "Rules", "Chatter"]


def test_moving_a_file_area(db, sysop):
    for name in ("Scans", "Manuals"):
        create_file_area(db, name, creator=sysop)

    assert move_file_area(db, get_file_area_by_name(db, "Manuals"), -1, moved_by=sysop)
    assert _names(list_file_areas(db)) == ["Manuals", "Scans"]


def test_activity_stays_available_as_a_choice(db, sysop):
    create_board(db, "Quiet", creator=sysop)
    busy = create_board(db, "Busy", creator=sysop)
    create_post(db, busy, sysop, "Fresh", "x")
    db.connection.execute("UPDATE boards SET created_at = '2020-01-01T00:00:00.000000Z'")
    db.connection.commit()

    assert _names(list_boards(db, order_by="activity")) == ["Busy", "Quiet"]


# -- the migration -------------------------------------------------------------


def _migration():
    [migration] = [m for m in MIGRATIONS if m.description.startswith("Issue #839: `position` on boards")]
    return migration


def test_upgrading_keeps_creation_order_and_every_saved_preference(tmp_path, monkeypatch):
    """The real upgrade: a node on the schema before this migration, with
    boards, areas and saved sort preferences, opened by this build."""
    from netbbs.storage import database as database_module
    from tests.legacy_schema import insert_user_on_old_schema

    index = MIGRATIONS.index(_migration())
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:index])
    path = tmp_path / "node.db"
    old = Database(path)
    sysop = insert_user_on_old_schema(old, "sysop", user_level=SYSOP_LEVEL)
    caller = insert_user_on_old_schema(old, "caller", user_level=10)
    retro = create_community(old, "Retro", creator=sysop)
    for name in ("zebra", "Apple", "mike"):
        create_board(old, name, creator=sysop)
    for name in ("Scans", "Manuals"):
        create_file_area(old, name, creator=sysop)
    set_sort_preference(old, caller, "board", "activity")
    set_sort_preference(old, caller, "file_area", "volume", community_id=retro.id)
    set_sort_preference(old, caller, "channel", "recent")
    old.close()
    monkeypatch.undo()

    upgraded = Database(path)
    try:
        assert _names(list_boards(upgraded)) == ["zebra", "Apple", "mike"]
        assert _names(list_file_areas(upgraded)) == ["Scans", "Manuals"]
        # Every preference survived the table rebuild, at its own scope...
        assert get_effective_sort_mode(upgraded, caller, "board") == "activity"
        assert get_effective_sort_mode(upgraded, caller, "file_area", community_id=retro.id) == "volume"
        assert get_effective_sort_mode(upgraded, caller, "file_area") == "sysop"
        assert get_effective_sort_mode(upgraded, caller, "channel") == "recent"
        # ...and the rebuilt unique indexes still make a second save an update.
        set_sort_preference(upgraded, caller, "board", "sysop")
        count = upgraded.connection.execute(
            "SELECT COUNT(*) FROM user_sort_preferences WHERE user_id = ? AND resource_kind = 'board'", (caller.id,)
        ).fetchone()[0]
        assert count == 1
        # A board made after the upgrade goes last (the trigger).
        create_board(upgraded, "Aardvark", creator=sysop)
        create_file_area(upgraded, "Audio", creator=sysop)
        assert _names(list_boards(upgraded))[-1] == "Aardvark"
        assert _names(list_file_areas(upgraded))[-1] == "Audio"
    finally:
        upgraded.close()


def test_a_saved_preference_survives_and_sysop_is_a_mode_boards_accept(db, sysop):
    set_sort_preference(db, sysop, "board", "activity")
    assert get_effective_sort_mode(db, sysop, "board") == "activity"

    set_sort_preference(db, sysop, "board", "sysop")
    assert get_effective_sort_mode(db, sysop, "board") == "sysop"
    with pytest.raises(sqlite3.IntegrityError):
        db.connection.execute(
            "INSERT INTO user_sort_preferences (user_id, resource_kind, sort_mode, created_at) "
            "VALUES (?, 'board', 'nonsense', 'x')",
            (sysop.id,),
        )


def test_channels_have_no_sysop_order(db, sysop):
    with pytest.raises(ValueError):
        set_sort_preference(db, sysop, "channel", "sysop")


# -- the caller's list -----------------------------------------------------------


def test_the_callers_list_follows_the_sysops_order_and_offers_it_under_order(db, sysop):
    create_board(db, "Zebra", creator=sysop)
    create_board(db, "Apple", creator=sysop)

    session = FakeSession(["o", "n", "j", "o", "s", "j", "b"])
    asyncio.run(board_flow._browse_boards(session, db, sysop))
    text = _visible_text(session)

    assert re.search(r"01\.\s*Zebra", text)
    assert "[S]ysOp's order" in text
    # After alphabetical, back to the SysOp's order: Zebra first again.
    assert text.rstrip().rsplit("Sort: Alphabetical", 1)[1].count("Sort: SysOp's order") >= 1
    assert re.search(r"01\.\s*Zebra", text.rsplit("Sort: Alphabetical", 1)[1])


# -- the SysOp console -------------------------------------------------------------


def _screen(lane, sysop, keys) -> list[str]:
    session = ScriptedSession(keys)
    with pytest.raises(_Exhausted):
        asyncio.run(admin_menu(session, lane, sysop))
    return session.on_terminal()


def test_the_console_moves_a_board_with_up_and_down(db, lane, sysop):
    for name in ("Alpha", "Beta", "Gamma"):
        create_board(db, name, creator=sysop)

    # Content, Message boards, List, board 03 (Gamma), Up.
    rows = _screen(lane, sysop, ["c", "m", "l", "0", "3", "u"])
    screen = " ".join(" ".join(row.split()) for row in rows)

    assert _names(list_boards(db)) == ["Alpha", "Gamma", "Beta"]
    assert "place 2 of 3" in screen
    assert "[U]p" in screen and "[D]own" in screen and "[R]emove" in screen


def test_the_console_board_list_is_in_the_callers_order(db, lane, sysop):
    create_board(db, "Zebra", creator=sysop)
    create_board(db, "Apple", creator=sysop)

    rows = _screen(lane, sysop, ["c", "m", "l"])

    assert re.search(r"01\.\s*Zebra", "\n".join(rows))


def test_the_last_board_offers_no_down(db, lane, sysop):
    create_board(db, "Alpha", creator=sysop)
    create_board(db, "Beta", creator=sysop)

    rows = _screen(lane, sysop, ["c", "m", "l", "0", "2", "d"])
    screen = " ".join(" ".join(row.split()) for row in rows)

    assert _names(list_boards(db)) == ["Alpha", "Beta"]
    assert "[D]own" not in screen and "place 2 of 2" in screen


def test_the_console_moves_a_file_area(db, lane, sysop):
    create_file_area(db, "Scans", creator=sysop)
    create_file_area(db, "Manuals", creator=sysop)

    _screen(lane, sysop, ["c", "f", "l", "0", "1", "d"])

    assert _names(list_file_areas(db)) == ["Manuals", "Scans"]

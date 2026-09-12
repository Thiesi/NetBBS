"""What a SysOp resource row actually says (issue #528).

The reported row was

    02. (#1) Test - read 100/write 100, open

for a file area that was *also* age-gated and name-gated. Neither gate
appeared anywhere, so an area most of the node cannot enter read
identically to one that turns nobody away. These tests pin what each
resource kind now puts in its columns.
"""

from __future__ import annotations

from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.chat.channels import create_channel
from netbbs.communities import create_community
from netbbs.files.areas import create_file_area
from netbbs.net.admin_flow import (
    _AREA_COLUMNS,
    _BOARD_COLUMNS,
    _CHANNEL_COLUMNS,
    _COMMUNITY_COLUMNS,
    _area_columns,
    _board_columns,
    _channel_columns,
    _community_columns,
    _gate_cell,
    _level_cell,
)
from netbbs.boards.boards import create_board
from netbbs.rendering import GATE_COLOR, MUTED_COLOR
from netbbs.storage.database import Database


def _db(tmp_path):
    db = Database(tmp_path / "node.db")
    return db, create_user(db, "sysop", password="hunter2", user_level=SYSOP_LEVEL)


# -- The cells themselves ---------------------------------------------


def test_a_level_that_is_inherited_says_so():
    """`None` is a real answer -- "whatever the Community says" -- and
    a different one from any number, so it is spelled out rather than
    left blank."""
    assert _level_cell(None) == "inherit"
    assert _level_cell(0) == "0"


def test_an_ungated_resource_shows_a_quiet_dash():
    assert _gate_cell(None, None) == ("-", MUTED_COLOR)


def test_each_gate_gets_a_tag_and_the_colour():
    assert _gate_cell(18, None) == ("18+", GATE_COLOR)
    assert _gate_cell(None, "verified") == ("name", GATE_COLOR)
    assert _gate_cell(21, "verified") == ("21+ name", GATE_COLOR)


def test_a_displayed_name_requirement_is_distinguishable_from_a_plain_one():
    """'verified' and 'verified_and_displayed' gate the same people but
    do visibly different things to a post, so they cannot share a tag."""
    plain, _ = _gate_cell(None, "verified")
    displayed, _ = _gate_cell(None, "verified_and_displayed")
    assert plain != displayed
    assert displayed == "name+"


def test_every_gate_tag_fits_its_column():
    """The widest realistic combination still has to fit, or the column
    silently truncates the very thing it was added to show."""
    width = next(c.width for c in _AREA_COLUMNS if c.header == "gates")
    worst, _ = _gate_cell(100, "verified_and_displayed")
    assert len(worst) <= width


# -- Per-kind rows ----------------------------------------------------


def test_the_reported_area_row_now_shows_both_its_gates(tmp_path):
    db, sysop = _db(tmp_path)
    area = create_file_area(
        db, "Test", creator=sysop, min_read_level=100, min_write_level=100,
        min_age=18, name_requirement="verified",
    )
    assert _area_columns(area) == ["100", "100", "open", ("18+ name", GATE_COLOR)]
    db.close()


def test_an_ungated_area_is_visibly_different_from_a_gated_one(tmp_path):
    db, sysop = _db(tmp_path)
    open_area = create_file_area(db, "Releases", creator=sysop, min_read_level=0)
    gated = create_file_area(db, "Adults", creator=sysop, min_read_level=0, min_age=18)
    assert _area_columns(open_area)[-1] != _area_columns(gated)[-1]
    db.close()


def test_a_board_row_carries_the_same_four_fields(tmp_path):
    db, sysop = _db(tmp_path)
    board = create_board(db, "News", creator=sysop, moderated=True, name_requirement="verified")
    cells = _board_columns(board)
    assert len(cells) == len(_BOARD_COLUMNS)
    assert cells[2] == "moderated"
    assert cells[3] == ("name", GATE_COLOR)
    db.close()


def test_a_channel_reports_visibility_and_membership_together(tmp_path):
    db, sysop = _db(tmp_path)
    public = create_channel(db, "lobby", creator=sysop, min_level=0)
    staff = create_channel(db, "staff", creator=sysop, min_level=100, members_only=True, hidden=True)
    assert _channel_columns(public)[:2] == ["0", "open"]
    assert _channel_columns(staff)[:2] == ["100", "members+hidden"]
    assert len(_channel_columns(staff)) == len(_CHANNEL_COLUMNS)
    db.close()


def test_the_widest_channel_access_value_fits_its_column(tmp_path):
    db, sysop = _db(tmp_path)
    both = create_channel(db, "x", creator=sysop, min_level=0, members_only=True, hidden=True)
    width = next(c.width for c in _CHANNEL_COLUMNS if c.header == "access")
    assert len(_channel_columns(both)[1]) <= width
    db.close()


def test_a_community_row_shows_the_defaults_its_children_inherit(tmp_path):
    db, sysop = _db(tmp_path)
    community = create_community(
        db, "Retro", creator=sysop, default_min_read_level=10, default_min_age=18,
    )
    cells = _community_columns(community)
    assert len(cells) == len(_COMMUNITY_COLUMNS)
    assert cells[0] == "10"
    assert cells[1] == "inherit"
    assert cells[2] == "yes"  # listed
    assert cells[3] == ("18+", GATE_COLOR)
    db.close()


def test_a_hidden_community_says_it_is_not_listed(tmp_path):
    db, sysop = _db(tmp_path)
    hidden = create_community(db, "Private", creator=sysop, hidden=True)
    assert _community_columns(hidden)[2] == "no"
    db.close()

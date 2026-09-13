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
    _effective_for,
    _BOARD_COLUMNS,
    _CHANNEL_COLUMNS,
    _COMMUNITY_COLUMNS,
    _area_columns,
    _area_description,
    _board_description,
    _channel_description,
    _community_description,
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


def test_a_community_with_no_default_level_says_none_not_inherit():
    """A Community is the top of the cascade. "inherit" there would
    point at a parent that does not exist; `None` means it sets no
    default for its children."""
    assert _level_cell(None) == "none"
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
    assert _area_columns(area, _effective_for(db, area)) == [
        "100", "100", "open", ("18+ name", GATE_COLOR),
    ]
    db.close()


def test_an_ungated_area_is_visibly_different_from_a_gated_one(tmp_path):
    db, sysop = _db(tmp_path)
    open_area = create_file_area(db, "Releases", creator=sysop, min_read_level=0)
    gated = create_file_area(db, "Adults", creator=sysop, min_read_level=0, min_age=18)
    assert (_area_columns(open_area, _effective_for(db, open_area))[-1]
            != _area_columns(gated, _effective_for(db, gated))[-1])
    db.close()


def test_a_board_row_carries_the_same_four_fields(tmp_path):
    db, sysop = _db(tmp_path)
    board = create_board(db, "News", creator=sysop, moderated=True, name_requirement="verified")
    cells = _board_columns(board, _effective_for(db, board))
    assert len(cells) == len(_BOARD_COLUMNS)
    assert cells[2] == "moderated"
    assert cells[3] == ("name", GATE_COLOR)
    db.close()


def test_a_channel_reports_visibility_and_membership_together(tmp_path):
    db, sysop = _db(tmp_path)
    public = create_channel(db, "lobby", creator=sysop, min_level=0)
    staff = create_channel(db, "staff", creator=sysop, min_level=100, members_only=True, hidden=True)
    assert _channel_columns(public, _effective_for(db, public, levels=False))[:2] == ["0", "open"]
    assert _channel_columns(staff, _effective_for(db, staff, levels=False))[:2] == ["100", "members+hidden"]
    assert len(_channel_columns(staff, _effective_for(db, staff, levels=False))) == len(_CHANNEL_COLUMNS)
    db.close()


def test_the_widest_channel_access_value_fits_its_column(tmp_path):
    db, sysop = _db(tmp_path)
    both = create_channel(db, "x", creator=sysop, min_level=0, members_only=True, hidden=True)
    width = next(c.width for c in _CHANNEL_COLUMNS if c.header == "access")
    assert len(_channel_columns(both, _effective_for(db, both, levels=False))[1]) <= width
    db.close()


def test_a_community_row_shows_the_defaults_its_children_inherit(tmp_path):
    db, sysop = _db(tmp_path)
    community = create_community(
        db, "Retro", creator=sysop, default_min_read_level=10, default_min_age=18,
    )
    cells = _community_columns(community)
    assert len(cells) == len(_COMMUNITY_COLUMNS)
    assert cells[0] == "10"
    assert cells[1] == "none"
    assert cells[2] == "yes"  # listed
    assert cells[3] == ("18+", GATE_COLOR)
    db.close()


def test_a_hidden_community_says_it_is_not_listed(tmp_path):
    db, sysop = _db(tmp_path)
    hidden = create_community(db, "Private", creator=sysop, hidden=True)
    assert _community_columns(hidden)[2] == "no"
    db.close()


# -- Codex review: what enforcement actually does ---------------------


def test_an_inherited_gate_is_shown_not_swallowed(tmp_path):
    """The finding that mattered most. A resource leaving its gates
    unset inherits its Community's, and `meets_age`/`meets_name_
    requirement` enforce the inherited value -- so rendering the
    resource's own raw `None` as "-" told the SysOp an area was open
    when callers genuinely cannot enter it. Same bug as the one filed,
    one level up the cascade."""
    db, sysop = _db(tmp_path)
    community = create_community(
        db, "Adults only", creator=sysop,
        default_min_age=18, default_name_requirement="verified",
    )
    area = create_file_area(db, "Inherits", creator=sysop, community_id=community.id)
    assert area.min_age is None and area.name_requirement is None
    assert _area_columns(area, _effective_for(db, area))[-1] == ("18+ name", GATE_COLOR)
    db.close()


def test_an_inherited_level_resolves_too(tmp_path):
    """One column silently resolving its inherited value while the next
    still said "inherit" would be the more confusing half-measure."""
    db, sysop = _db(tmp_path)
    community = create_community(db, "Staff", creator=sysop, default_min_read_level=50)
    # Explicit None: `create_board` defaults the level to 0, so a board
    # only inherits one when it is deliberately cleared (which is what
    # the editor's "'none' = clear" answer does). Gates are the common
    # inheriting case -- they default to None already.
    board = create_board(db, "Notices", creator=sysop, community_id=community.id, min_read_level=None)
    assert board.min_read_level is None
    assert _board_columns(board, _effective_for(db, board))[0] == "50"
    db.close()


def test_an_explicit_zero_age_is_not_a_gate(tmp_path):
    """`meets_age` opens with `if not min_age: return True` and
    documents "unset/0 -> always passes". Zero is a supported way to
    override an inherited gate, so showing "0+" would advertise a
    restriction that admits everyone."""
    db, sysop = _db(tmp_path)
    community = create_community(db, "Adults only", creator=sysop, default_min_age=18)
    area = create_file_area(db, "Open again", creator=sysop, community_id=community.id, min_age=0)
    assert _area_columns(area, _effective_for(db, area))[-1] == ("-", MUTED_COLOR)
    assert _gate_cell(0, None) == ("-", MUTED_COLOR)
    db.close()


def test_the_narrow_fallback_still_names_the_gates(tmp_path):
    """Below the table width the row returns to prose, and that prose
    omitted the gates entirely -- recreating the reported bug on every
    supported narrow terminal, against design doc §3.6's promise that
    gates appear wherever a resource is listed."""
    db, sysop = _db(tmp_path)
    community = create_community(
        db, "Adults only", creator=sysop,
        default_min_age=18, default_name_requirement="verified",
    )
    area = create_file_area(db, "Inherits", creator=sysop, community_id=community.id)
    board = create_board(db, "Notices", creator=sysop, min_age=21)
    channel = create_channel(db, "staff", creator=sysop, min_level=0, name_requirement="verified")

    assert "18+ name" in _area_description(area, _effective_for(db, area))
    assert "21+" in _board_description(board, _effective_for(db, board))
    assert "name" in _channel_description(channel, _effective_for(db, channel, levels=False))
    assert "default 18+ name" in _community_description(community)
    db.close()


def test_the_gates_lead_the_fallback_so_truncation_takes_the_levels(tmp_path):
    """A narrow terminal is exactly where this string gets cut. Putting
    the gates last meant a 50-column row lost the one field the issue
    was about; whoever can enter is the least guessable fact in the
    row, so it survives truncation first."""
    db, sysop = _db(tmp_path)
    area = create_file_area(db, "Adults", creator=sysop, min_read_level=10, min_age=18)
    text = _area_description(area, _effective_for(db, area))
    assert text.startswith("18+")
    assert text.index("18+") < text.index("read")
    db.close()


def test_an_ungated_resource_keeps_its_original_fallback_wording(tmp_path):
    """The overwhelmingly common case must not grow a noisy prefix."""
    db, sysop = _db(tmp_path)
    area = create_file_area(db, "Plain", creator=sysop, min_read_level=0, min_write_level=0)
    assert _area_description(area, _effective_for(db, area)) == "read 0/write 0, open"
    db.close()

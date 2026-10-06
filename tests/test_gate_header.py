"""
Issue #1105: a gated board, file area or chat channel names its gates in
its header when a caller enters it.

The line lists only gates that restrict someone, with the effective values
after the Community cascade, and an ungated resource gets no line at all.
It fits one row at the session's width (79 on a terminal that wraps at
once) and falls back to plain separators and three dots for ASCII and
CP437 callers.
"""

from __future__ import annotations

import asyncio

import pytest

from netbbs.age_requirement import VERIFIED
from netbbs.auth.users import SYSOP_LEVEL, create_user
from netbbs.boards.boards import create_board
from netbbs.chat.channels import create_channel
from netbbs.communities import create_community
from netbbs.files.areas import create_file_area
from netbbs.gate_summary import gates_line, resource_gates, resource_gates_line
from netbbs.net import board_flow, file_flow
from netbbs.rendering.ansi import strip_ansi
from netbbs.rendering.width import display_width
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_file_flow_remote import FakeSession as AreaSession
from tests.test_outcomes_carried_into_redraw import FakeSession as BoardSession


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


@pytest.fixture
def alice(db):
    """A SysOp: creates every resource, and passes every gate (#1096), so
    the screens open whatever the gates say."""
    return create_user(db, "alice", password="hunter2pw", user_level=SYSOP_LEVEL)


# -- what counts as a gate ----------------------------------------------------


def test_an_ungated_resource_has_no_gates_and_no_line(db, alice):
    board = create_board(db, "open", creator=alice)
    assert resource_gates(db, board) == ()
    assert resource_gates_line(db, board, width=80) is None


def test_each_board_gate_is_named(db, alice):
    board = create_board(
        db, "adults", creator=alice, min_read_level=10, min_write_level=20, min_age=18,
        age_requirement=VERIFIED, name_requirement="verified",
    )
    assert resource_gates(db, board) == (
        "age 18+ verified", "verified name", "level 10+ to read", "level 20+ to post",
    )


def test_a_self_attested_age_and_a_shown_name_read_differently(db, alice):
    board = create_board(db, "pens", creator=alice, min_age=16, name_requirement="verified_and_displayed")
    assert resource_gates(db, board) == ("age 16+", "verified name, shown")


def test_a_write_level_no_higher_than_the_read_level_is_not_repeated(db, alice):
    board = create_board(db, "same", creator=alice, min_read_level=20, min_write_level=20)
    assert resource_gates(db, board) == ("level 20+ to read",)


def test_an_explicit_zero_minimum_age_is_no_gate(db, alice):
    board = create_board(db, "zero", creator=alice, min_age=0)
    assert resource_gates(db, board) == ()


def test_file_area_levels_say_browse_and_upload(db, alice):
    area = create_file_area(db, "warez", creator=alice, min_read_level=5, min_write_level=30)
    assert resource_gates(db, area) == ("level 5+ to browse", "level 30+ to upload")


def test_a_channel_names_its_level_and_members_only(db, alice):
    channel = create_channel(db, "inner", creator=alice, min_level=40, members_only=True, min_age=21)
    assert resource_gates(db, channel) == ("age 21+", "level 40+", "members only")


def test_the_community_cascade_supplies_inherited_gates(db, alice):
    community = create_community(
        db, "Afterhours", creator=alice, default_min_age=18, default_age_requirement=VERIFIED,
        default_min_read_level=10, default_name_requirement="verified",
    )
    board = create_board(db, "late", creator=alice, community_id=community.id, min_read_level=None, min_write_level=None)
    assert resource_gates(db, board) == ("age 18+ verified", "verified name", "level 10+ to read")


# -- one line, at the session's width -----------------------------------------


def _long_gates():
    return ("age 18+ verified", "verified name, shown", "level 200+ to read", "level 250+ to post", "x" * 40)


@pytest.mark.parametrize("width", [80, 79])
def test_the_line_fits_the_width(width):
    line = strip_ansi(gates_line(_long_gates(), width=width, ellipsis="…"))
    assert display_width(line) <= width
    assert line.startswith("Requires: age 18+ verified")
    assert line.endswith("…")


def test_ascii_uses_plain_separators_and_three_dots():
    line = strip_ansi(gates_line(_long_gates(), width=79, unicode_style=False, ellipsis="..."))
    assert " - " in line and "·" not in line
    assert line.endswith("...")
    assert line.isascii()
    assert display_width(line) <= 79


# -- where callers see it -----------------------------------------------------


def test_entering_a_gated_board_shows_its_gates(db, alice):
    board = create_board(db, "adults", creator=alice, min_age=18, min_write_level=20)
    session = BoardSession(["b"])
    asyncio.run(board_flow._show_board(session, db, board, alice))
    assert "Requires: age 18+ · level 20+ to post" in session.visible()


def test_entering_an_open_board_shows_no_gate_line(db, alice):
    board = create_board(db, "open", creator=alice)
    session = BoardSession(["b"])
    asyncio.run(board_flow._show_board(session, db, board, alice))
    assert "Requires:" not in session.visible()


def test_a_board_with_posts_shows_its_gates_above_the_list(db, alice):
    from netbbs.boards.posts import create_post

    board = create_board(db, "adults", creator=alice, name_requirement="verified")
    create_post(db, board, alice, "Hello", "Body")
    session = BoardSession(["b"])
    asyncio.run(board_flow._show_board(session, db, board, alice))
    assert "Requires: verified name" in session.visible()


def test_entering_a_gated_file_area_shows_its_gates(db, lane, alice):
    area = create_file_area(db, "pens", creator=alice, min_write_level=30)
    session = AreaSession(["b"])
    asyncio.run(file_flow._show_area(session, lane, area, alice))
    assert "Requires: level 30+ to upload" in strip_ansi("".join(session.written))


def _chat(db, lane, channel, user):
    from netbbs.chat.hub import ChatHub
    from netbbs.chat.mailbox import MessageMailbox
    from netbbs.chat.presence import PresenceRegistry
    from netbbs.net import chat_flow
    from netbbs.net.char_input import InputHistory
    from tests.test_chat_flow_moderation import FakeSession as ChatSession

    session = ChatSession(["/quit"])

    async def scenario():
        await asyncio.wait_for(
            chat_flow._chat_loop(
                session, lane, ChatHub(), PresenceRegistry(), MessageMailbox(), InputHistory(), channel, user
            ),
            timeout=5,
        )

    asyncio.run(scenario())
    return strip_ansi("\n".join(session.written))


def test_entering_a_gated_channel_shows_its_gates(db, lane, alice):
    channel = create_channel(db, "inner", creator=alice, min_level=40, name_requirement="verified")
    assert "Requires: verified name · level 40+" in _chat(db, lane, channel, alice)


def test_entering_an_open_channel_shows_no_gate_line(db, lane, alice):
    channel = create_channel(db, "general", creator=alice)
    assert "Requires:" not in _chat(db, lane, channel, alice)


def test_a_redrawn_empty_gated_file_area_keeps_its_gates(db, lane, alice):
    """Ctrl-L, an upload and a transfer link redraw the empty area in its
    own loop (`_still_empty`); the gate line comes back with it (review
    on #1107)."""
    from netbbs.net.file_flow import REDRAW_KEY

    area = create_file_area(db, "pens", creator=alice, min_write_level=30)
    session = AreaSession([REDRAW_KEY, "b"])
    asyncio.run(file_flow._show_area(session, lane, area, alice))
    assert strip_ansi("".join(session.written)).count("Requires: level 30+ to upload") == 2


# -- the caller's own unmet gates (issue #1115) ---------------------------------


def _member(db, level=10):
    return create_user(db, "member", password="hunter2pw", user_level=level)


def test_unmet_gates_are_the_ones_the_caller_fails(db, alice):
    from netbbs.gate_summary import unmet_gates

    board = create_board(db, "news", creator=alice, min_write_level=255, min_age=18, age_requirement=VERIFIED)
    member = _member(db)
    assert unmet_gates(db, member, board) == {"age 18+ verified", "level 255+ to post"}
    # A SysOp passes the age gate (#1096) and the level.
    assert unmet_gates(db, alice, board) == frozenset()


def test_an_unmet_gate_is_drawn_in_the_error_colour():
    from netbbs.rendering.ansi import colored
    from netbbs.rendering.theme import ERROR_COLOR, GATE_COLOR

    line = gates_line(("age 18+", "level 255+ to post"), width=80, unmet={"level 255+ to post"})
    assert colored("level 255+ to post", fg_color=ERROR_COLOR) in line
    assert colored("age 18+", fg_color=GATE_COLOR) in line
    assert strip_ansi(line) == "Requires: age 18+ · level 255+ to post"


def test_ascii_says_not_met_in_words():
    line = strip_ansi(gates_line(("level 255+ to post",), width=80, unicode_style=False, unmet={"level 255+ to post"}))
    assert line == "Requires: level 255+ to post (not met)"


@pytest.mark.parametrize("width", [80, 79, 30])
def test_a_marked_line_still_fits(width):
    gates = _long_gates()
    line = gates_line(gates, width=width, unicode_style=False, unmet=set(gates[2:4]))
    assert display_width(strip_ansi(line)) <= width
    assert strip_ansi(line).endswith("...")


def test_a_read_only_caller_sees_one_line_not_two(db, alice):
    board = create_board(db, "news", creator=alice, min_write_level=255)
    member = _member(db)
    session = BoardSession(["b"])
    asyncio.run(board_flow._show_board(session, db, board, member))
    screen = session.visible()
    assert "Requires: level 255+ to post" in screen
    assert "Read only" not in screen
    assert "[P]ost" not in screen


def test_a_caller_who_can_post_sees_nothing_marked(db, alice):
    from netbbs.rendering.theme import ERROR_COLOR

    board = create_board(db, "club", creator=alice, min_write_level=5)
    member = _member(db)
    session = BoardSession(["b"])
    asyncio.run(board_flow._show_board(session, db, board, member))
    assert "Requires: level 5+ to post" in session.visible()
    assert f"38;5;{ERROR_COLOR}m" not in "".join(session.written)


def test_an_upload_level_the_caller_lacks_is_marked(db, lane, alice):
    from netbbs.rendering.ansi import colored
    from netbbs.rendering.theme import ERROR_COLOR

    area = create_file_area(db, "pens", creator=alice, min_write_level=30)
    member = _member(db)
    session = AreaSession(["b"])
    asyncio.run(file_flow._show_area(session, lane, area, member))
    written = "".join(session.written)
    assert "Requires: level 30+ to upload" in strip_ansi(written)
    assert colored("level 30+ to upload", fg_color=ERROR_COLOR) in written

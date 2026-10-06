"""
Issue #1081: a console resource opens on its own fields. A Community's,
category's, message board's, file area's, chat channel's and door's screen
is its editor -- a read-only header above the fields, the cursor on the first
field, actions keeping their letters, and a changed draft offering only Save
and Back.
"""

from __future__ import annotations

import re

from netbbs.boards import categories as board_categories
from netbbs.communities import create_community, list_communities
from tests.test_admin_flow import FakeSession, _run, _visible, _written_text, db, lane, sysop  # noqa: F401 -- fixtures


def _screen_with(text: str, marker: str) -> str:
    """The first redraw (each ends at its Choice prompt) matching `marker`."""
    return next(screen for screen in text.split("Choice:") if re.search(marker, screen))


def test_a_communitys_screen_opens_on_its_fields_with_its_actions(db, lane, sysop):
    create_community(db, "Pens", creator=sysop)
    create_community(db, "Paper", creator=sysop)
    # Content > Communities > List > 02: its screen, then Back out.
    session = FakeSession(["m", "o", "l", "0", "2", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    screen = _screen_with(_visible(_written_text(session)), "Place 2 of 2")
    assert "in the callers' Communities list" in screen
    assert "> Name:" in screen
    assert "[U]p" in screen and "[R]emove" in screen and "[D]own" not in screen
    assert "[E]dit" not in screen and "[S]ave" not in screen


def test_a_changed_community_offers_only_save_and_back(db, lane, sysop):
    create_community(db, "Pens", creator=sysop)
    create_community(db, "Paper", creator=sysop)
    second = list_communities(db)[1]
    # 02; Down twice to Hidden, Right steps it; "u" is refused while it
    # waits; Save; then Up runs; Back out.
    session = FakeSession([
        "m", "o", "l", "0", "2", "DOWN", "DOWN", "RIGHT", "u", "s", "u", "b", "b", "b", "b", "b",
    ])
    _run(session, lane, sysop)
    changed = _screen_with(_visible(_written_text(session)), r"Hidden:\s+yes")
    assert "[S]ave" in changed and "[U]p" not in changed
    communities = list_communities(db)
    assert communities[0].id == second.id and communities[0].hidden


def test_a_categorys_screen_opens_on_its_fields_with_its_actions(db, lane, sysop):
    board_categories.create_category(db, "Retro", created_by=sysop)
    board_categories.create_category(db, "News", created_by=sysop)
    # Content > Categories > Message board > List > 01, Back out.
    session = FakeSession(["m", "c", "m", "l", "0", "1", "b", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    screen = _screen_with(_visible(_written_text(session)), "among its siblings")
    assert "Place 1 of 2" in screen and "Sub-categories: none" in screen
    assert "> Name:" in screen
    assert "[D]own" in screen and "[R]emove" in screen and "[E]dit" not in screen


def test_a_boards_screen_opens_on_its_fields_with_its_actions(db, lane, sysop):
    from netbbs.boards.boards import create_board

    create_board(db, "Pen Repair", creator=sysop)
    create_board(db, "Inks", creator=sysop)
    # Content > Message boards > List > 01, Back out.
    session = FakeSession(["m", "m", "l", "0", "1", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    screen = _screen_with(_visible(_written_text(session)), "0 posts")
    assert "place 1 of 2" in screen and "> Name:" in screen
    for action in ("[D]own", "[R]emove", "[P]ending posts", "[H]istory"):
        assert action in screen
    assert "[E]dit" not in screen and "[U]p" not in screen and "[S]ave" not in screen


def test_a_changed_board_hides_its_actions_until_saved(db, lane, sysop):
    from netbbs.boards.boards import create_board, get_board_by_name

    create_board(db, "Pen Repair", creator=sysop)
    # 01; Down nine times to Moderated, Enter toggles it; "r" is refused while
    # it waits (no removal prompt); Save; Back out.
    session = FakeSession(["m", "m", "l", "0", "1", *(["DOWN"] * 9), "ENTER", "r", "s", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _visible(_written_text(session))
    changed = _screen_with(text, r"Moderated:\s+yes")
    assert "[S]ave" in changed and "[R]emove" not in changed
    assert "Type the message board name" not in text
    assert get_board_by_name(db, "Pen Repair").moderated


def test_a_file_areas_screen_opens_on_its_fields_with_its_actions(db, lane, sysop):
    from netbbs.files.areas import create_file_area

    create_file_area(db, "Manuals", creator=sysop)
    # Content > File areas > List > 01, Back out.
    session = FakeSession(["m", "f", "l", "0", "1", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    screen = _screen_with(_visible(_written_text(session)), "0 files")
    assert "place 1 of 1" in screen and "> Name:" in screen
    for action in ("[R]emove", "[P]ending files", "E[x]pired files", "[H]istory"):
        assert action in screen
    assert "[E]dit" not in screen


def test_a_chat_channels_screen_opens_on_its_fields_with_its_actions(db, lane, sysop):
    from netbbs.chat.channels import create_channel

    create_channel(db, "Lobby", creator=sysop)
    # Content > Chat channels > List > 01, Back out.
    session = FakeSession(["m", "n", "l", "0", "1", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    screen = _screen_with(_visible(_written_text(session)), "no MRC room")
    assert "> Name:" in screen
    for action in ("[D]elete", "[R]estrictions", "[M]RC room"):
        assert action in screen
    assert "[E]dit" not in screen and "[S]ave" not in screen


def test_a_doors_screen_opens_on_its_fields_and_a_changed_one_hides_delete(db, lane, sysop):
    import sys

    from netbbs.doors import create_door, list_doors

    create_door(db, "Lotto", sys.executable, creator=sysop)
    # Content > Doors > List > 01; Down six times to Pinned, Enter toggles
    # it; "d" is refused while it waits (no delete prompt); Save; Back out.
    session = FakeSession(["m", "d", "l", "0", "1", *(["DOWN"] * 6), "ENTER", "d", "s", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    text = _visible(_written_text(session))
    screen = _screen_with(text, "Compatibility: NetBBS native API")
    assert "> Name:" in screen
    for action in ("[C]ompatibility", "[L]ast diagnostic", "[O]utbound", "[D]elete"):
        assert action in screen
    assert "[E]dit" not in screen
    changed = _screen_with(text, r"Pinned:\s+yes")
    assert "[S]ave" in changed and "[D]elete" not in changed
    assert "Type the door name" not in text
    assert list_doors(db)[0].pinned


def test_a_renamed_community_and_category_are_titled_by_their_new_name(db, lane, sysop):
    create_community(db, "Politics", creator=sysop)
    session = FakeSession(["m", "o", "l", "0", "1", "ENTER", "Civics", "s", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    saved = _screen_with(_visible(_written_text(session)), "Updated 'Civics'")
    assert "› Civics" in saved and "Politics" not in saved

    board_categories.create_category(db, "Retro", created_by=sysop)
    session = FakeSession(["m", "c", "m", "l", "0", "1", "ENTER", "Vintage", "s", "b", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    # The last redraw showing the new name is the one after the save (the one
    # before it shows the unsaved draft under the stored name).
    saved = [s for s in _visible(_written_text(session)).split("Choice:") if re.search(r"Name:\s+Vintage", s)][-1]
    assert "› Vintage" in saved and "Retro" not in saved


def test_a_renamed_board_is_titled_by_its_new_name(db, lane, sysop):
    from netbbs.boards.boards import create_board

    create_board(db, "Pen Repair", creator=sysop)
    session = FakeSession(["m", "m", "l", "0", "1", "ENTER", "Nib Repair", "s", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    saved = _screen_with(_visible(_written_text(session)), "Updated 'Nib Repair'")
    assert "› Nib Repair" in saved and "Pen Repair" not in saved


def test_a_renamed_channel_and_door_are_titled_by_their_new_names(db, lane, sysop):
    import sys

    from netbbs.chat.channels import create_channel
    from netbbs.doors import create_door

    create_channel(db, "Lobby", creator=sysop)
    session = FakeSession(["m", "n", "l", "0", "1", "ENTER", "Lounge", "s", "b", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    saved = _screen_with(_visible(_written_text(session)), "Updated 'Lounge'")
    assert "› Lounge" in saved and "Lobby" not in saved

    create_door(db, "Lotto", sys.executable, creator=sysop)
    session = FakeSession(["m", "d", "l", "0", "1", "ENTER", "Lotto2", "s", "b", "b", "b", "b"])
    _run(session, lane, sysop)
    saved = _screen_with(_visible(_written_text(session)), "Updated 'Lotto2'")
    assert "› Lotto2" in saved and "› Lotto\r" not in saved

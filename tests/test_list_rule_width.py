"""A list's rules are exactly as wide as its rows.

A board's post list, the mailbox and a file area fit their rows to the
terminal less its last column (`netbbs.rendering.list_width`), but drew the
rules above, between and below them capped at the 78 columns a frame uses:
one column short of the rows at 80 columns, and half a screen short at 132.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.boards.posts import set_post_pinned
from netbbs.files.areas import create_file_area
from netbbs.files.entries import set_file_pinned, upload_file
from netbbs.mail import send_mail
from netbbs.moderation import BoardPermission, grant_permissions
from netbbs.net import board_flow
from netbbs.net.file_flow import _show_area
from netbbs.net.mail_flow import browse_mail
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.net.unicode_style_preference import set_unicode_style_enabled
from netbbs.rendering import list_rule, list_width
from netbbs.rendering.width import display_width
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_mail_list import FakeSession as MailSession
from tests.test_pinning import BoardSession, FileSession, _board, _posts

_SGR = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# A plain rule, a labelled one ("-- Pinned ---"), or a file area's rule of
# one segment per column.
_RULE = re.compile(r"^(?:[─-]{2} [A-Za-z]+ )?[─-]+(?: [─-]+)*$")


def _lines(text: str) -> list[str]:
    return [line for line in _SGR.sub("", text).replace("\r\n", "\n").split("\n") if line.strip()]


def _list_rules(lines: list[str]) -> list[str]:
    """The rules a list draws; the first rule on the screen is the one
    under its title, sized to the title rather than to the list."""
    return [line for line in lines if _RULE.match(line)][1:]


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.mark.parametrize("unicode_style", [True, False])
@pytest.mark.parametrize("width", [80, 132])
def test_the_board_lists_rules_are_as_wide_as_its_rows(db, monkeypatch, width, unicode_style):
    mod = create_user(db, "mod", password="hunter2", user_level=10)
    set_unicode_style_enabled(db, mod, unicode_style)
    board = _board(db, mod)
    made = _posts(db, board, mod, 5, monkeypatch)
    set_post_pinned(db, made[0], True, changed_by=mod)
    session = BoardSession(["b"], width=width)

    asyncio.run(board_flow._show_board(session, db, board, mod))

    lines = _lines("".join(session.written))
    rows = [line for line in lines if re.search(r"Subject \d", line)]
    rules = _list_rules(lines)
    assert len(rows) == 5
    # The labelled Pinned rule, the one parting the pins from the dated
    # posts, and the one under the list.
    assert len(rules) == 3
    assert rules[0].startswith(("── Pinned ", "-- Pinned "))
    assert {display_width(line) for line in rows + rules} == {width - 1}
    assert ("─" in rules[1]) is unicode_style


@pytest.mark.parametrize("unicode_style", [True, False])
def test_a_narrow_boards_rules_stay_inside_the_terminal(db, monkeypatch, unicode_style):
    """Below the table width the rows are prose; the rules keep to the
    rows' width and leave the last column free, as the rows do."""
    mod = create_user(db, "mod", password="hunter2", user_level=10)
    set_unicode_style_enabled(db, mod, unicode_style)
    board = _board(db, mod)
    _posts(db, board, mod, 3, monkeypatch)
    session = BoardSession(["b"], width=50)

    asyncio.run(board_flow._show_board(session, db, board, mod))

    lines = _lines("".join(session.written))
    rows = [line for line in lines if re.search(r"Subject \d", line)]
    rules = _list_rules(lines)
    assert len(rows) == 3 and len(rules) == 2
    assert {display_width(line) for line in rules} == {49}
    assert all(display_width(line) <= 49 for line in rows)


@pytest.fixture
def mail_node(tmp_path):
    path = tmp_path / "mail.db"
    db = Database(path)
    lane = DatabaseLane(path)
    bob = create_user(db, "bob", password="hunter2pw", user_level=10)
    set_redraw_in_place_enabled(db, bob, True)
    alice = create_user(db, "alice", password="hunter2pw", user_level=10)
    yield db, lane, bob, alice
    lane.close()
    db.close()


@pytest.mark.parametrize("unicode_style", [True, False])
@pytest.mark.parametrize("width", [80, 132, 50])
def test_the_mail_lists_rules_are_as_wide_as_its_rows(mail_node, width, unicode_style):
    db, lane, bob, alice = mail_node
    set_unicode_style_enabled(db, bob, unicode_style)
    for subject in ("Hello", "Second"):
        send_mail(db, alice, bob, subject, "body")
    session = MailSession(["b"], width=width, height=24)

    asyncio.run(browse_mail(session, lane, bob))

    lines = _lines(session.screens()[0])
    rows = [line for line in lines if "alice" in line and ("Hello" in line or "Second" in line)]
    rules = _list_rules(lines)
    assert len(rows) == 2 and len(rules) == 2
    assert {display_width(line) for line in rules} == {width - 1}
    if width >= 60:
        assert {display_width(line) for line in rows} == {width - 1}
    else:
        assert all(display_width(line) <= width - 1 for line in rows)


@pytest.mark.parametrize("unicode_style", [True, False])
@pytest.mark.parametrize("width", [80, 132])
def test_the_file_lists_rules_line_up_with_its_rows(tmp_path, width, unicode_style):
    """The column heading, each column's rule and the rows are the same
    width, and the heading's columns start where the row's cells do: the
    index column was one narrower than the "[01]" under it."""
    path = tmp_path / "files.db"
    db = Database(path)
    mod = create_user(db, "mod", password="hunter2", user_level=10)
    set_unicode_style_enabled(db, mod, unicode_style)
    area = create_file_area(db, "downloads", creator=mod)
    grant_permissions(
        db, mod, object_type="file_area", object_id=area.id, permissions=BoardPermission.EDIT, granted_by=mod
    )
    files = [upload_file(db, area, mod, f"f{i}.txt", f"payload {i}".encode()) for i in range(3)]
    set_file_pinned(db, files[0], True, changed_by=mod)
    lane = DatabaseLane(path)
    try:
        session = FileSession(["b"], width=width)
        asyncio.run(_show_area(session, lane, area, mod))
    finally:
        lane.close()
        db.close()

    lines = _lines("".join(session.written))
    heading = next(line for line in lines if "Filename" in line and "Uploader" in line)
    rows = [line for line in lines if re.search(r"\[0\d\] ", line)]
    rules = _list_rules(lines)
    assert len(rows) == 3
    # The column rule, the labelled Pinned rule, the one under the pins.
    assert len(rules) == 3
    assert {display_width(line) for line in [heading, *rows, *rules]} == {width - 1}
    assert heading.index("Filename") == rows[1].index("f1.txt")
    assert rules[0].index(" ", 1) == rows[1].index("f1.txt") - 1


@pytest.mark.parametrize("unicode_style", [True, False])
@pytest.mark.parametrize("width", [50, 59])
def test_a_narrow_file_lists_rows_fit_the_terminal(tmp_path, width, unicode_style):
    """Below the table width the files are prose rows (issue #1452): no row
    is wider than `list_width`, where the columns were 54 wide at 50 and
    wrapped. The labelled Pinned rule stands where the column heading was,
    and the rules are as wide as the rows may be, as a narrow board's are."""
    path = tmp_path / "files.db"
    db = Database(path)
    mod = create_user(db, "mod", password="hunter2", user_level=10)
    set_unicode_style_enabled(db, mod, unicode_style)
    area = create_file_area(db, "downloads", creator=mod)
    grant_permissions(
        db, mod, object_type="file_area", object_id=area.id, permissions=BoardPermission.EDIT, granted_by=mod
    )
    names = ["f0.txt", "copperplate-minuscules-and-majuscules-week1.png", "f2.txt"]
    files = [upload_file(db, area, mod, name, f"payload {i}".encode()) for i, name in enumerate(names)]
    set_file_pinned(db, files[0], True, changed_by=mod)
    lane = DatabaseLane(path)
    try:
        session = FileSession(["b"], width=width)
        asyncio.run(_show_area(session, lane, area, mod))
    finally:
        lane.close()
        db.close()

    lines = _lines("".join(session.written))
    assert not any("Filename" in line and "Uploader" in line for line in lines)
    rows = [line for line in lines if re.search(r"\[0\d\] ", line)]
    rules = _list_rules(lines)
    assert len(rows) == 3
    # The labelled Pinned rule and the one under the pins.
    assert len(rules) == 2 and "Pinned" in rules[0]
    assert {display_width(line) for line in rules} == {list_width(width)}
    assert all(display_width(line) <= list_width(width) for line in rows)
    # The filename keeps its end; the size follows it.
    assert "week1.png -- " in rows[1]
    assert rows[0].startswith(" [01] pin f0.txt -- ") and rows[0].endswith(", mod")


@pytest.mark.parametrize("width", [80, 132, 40, 1])
def test_list_rule_is_list_width_wide(width):
    assert list_width(width) == max(1, width - 1)
    for unicode_style, char in ((True, "─"), (False, "-")):
        plain = _SGR.sub("", list_rule(width, unicode_style=unicode_style))
        assert plain == char * list_width(width)
        labelled = _SGR.sub("", list_rule(width, unicode_style=unicode_style, label="Pinned"))
        if width >= 20:
            assert labelled.startswith(f"{char * 2} Pinned {char}")
            assert display_width(labelled) == list_width(width)

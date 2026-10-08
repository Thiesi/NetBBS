"""
Level names (design doc §5.7, issue #1009): optional labels shown beside a
level's number, typed in its place where the console asks for a level, and
the `python -m netbbs.admin levels` report.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from netbbs.admin.__main__ import main as admin_main
from netbbs.auth.users import SYSOP_LEVEL, create_user, get_user_by_username
from netbbs.boards.boards import create_board
from netbbs.level_names import (
    LevelNameError,
    get_level_names,
    level_label,
    parse_level,
    set_level_name,
)
from netbbs.net.admin_flow import admin_menu
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
def lane(db):
    database_lane = DatabaseLane(db.path)
    yield database_lane
    database_lane.close()


def _screen(lane, sysop, keys) -> str:
    session = ScriptedSession(keys)
    with pytest.raises(_Exhausted):
        asyncio.run(admin_menu(session, lane, sysop))
    return "\n".join(" ".join(row.split()) for row in session.on_terminal())


# -- the names themselves


def test_naming_renaming_and_clearing_a_level(db, sysop):
    assert get_level_names(db) == {SYSOP_LEVEL: "SysOp"}
    set_level_name(db, 10, "  Member ", changed_by=sysop)
    assert get_level_names(db)[10] == "Member"
    set_level_name(db, 10, "Regular", changed_by=sysop)
    assert get_level_names(db)[10] == "Regular"
    set_level_name(db, 10, "", changed_by=sysop)
    assert 10 not in get_level_names(db)
    logged = db.connection.execute("SELECT COUNT(*) FROM moderation_log WHERE action = 'name_level'").fetchone()[0]
    assert logged == 3


@pytest.mark.parametrize(
    ("level", "name", "message"),
    [
        (SYSOP_LEVEL, "Boss", "always called SysOp"),
        (300, "Ghost", "0 to 255"),
        (10, "x" * 13, "at most 12"),
        (10, "42", "needs a letter"),
        (10, "#1", "needs a letter"),
        (20, "member", "Level 10 is already called Member"),
        (20, "sysop", "Level 255 is already called SysOp"),
    ],
)
def test_a_name_that_cannot_be_used_is_refused(db, sysop, level, name, message):
    set_level_name(db, 10, "Member", changed_by=sysop)

    with pytest.raises(LevelNameError, match=message):
        set_level_name(db, level, name, changed_by=sysop)


def test_a_name_is_a_label_and_changes_no_access(db, sysop):
    from netbbs.access_map import list_gates

    create_board(db, "lounge", min_read_level=10, creator=sysop)
    before = list_gates(db)
    set_level_name(db, 10, "Member", changed_by=sysop)

    assert list_gates(db) == before


def test_labels_and_parsing(db, sysop):
    set_level_name(db, 10, "Member", changed_by=sysop)
    names = get_level_names(db)

    assert level_label(10, names) == "10 (Member)"
    assert level_label(11, names) == "11"
    assert level_label(SYSOP_LEVEL, names) == "255 (SysOp)"
    assert [parse_level(raw, names) for raw in ("10", " member ", "SYSOP", "Elder", "")] == [10, 10, 255, None, None]


def test_a_broken_stored_value_is_ignored(db, sysop):
    from netbbs.config import set_config

    set_config(db, "level_names", "{not json")
    assert get_level_names(db) == {SYSOP_LEVEL: "SysOp"}
    set_config(db, "level_names", json.dumps({"10": "Member", "x": "y", "300": "z", "20": 5}))
    assert get_level_names(db) == {10: "Member", SYSOP_LEVEL: "SysOp"}


# -- the console


def test_the_ladder_shows_names_and_names_a_level(db, lane, sysop):
    create_board(db, "lounge", min_read_level=10, creator=sysop)

    text = _screen(lane, sysop, ["u", "l", "n", "02", "Member"])

    assert get_level_names(db)[10] == "Member"
    assert "02. 10 (Member)" in text
    assert "Level 10 is now called Member." in text
    assert "255 (SysOp)" in text


def test_a_refused_name_says_why_on_the_ladder(db, lane, sysop):
    text = _screen(lane, sysop, ["u", "l", "n", "01", "123"])

    assert "A level name needs a letter" in text
    assert 0 not in get_level_names(db)


def test_the_user_level_prompt_takes_a_name(db, lane, sysop):
    create_user(db, "alice", password="hunter2")
    set_level_name(db, 10, "Member", changed_by=sysop)

    text = _screen(lane, sysop, ["u", "u", "/", "alice", "l", "member"])

    assert get_user_by_username(db, "alice").user_level == 10
    assert "Level: 10 (Member)" in text


def test_the_preview_and_the_level_screen_use_names(db, lane, sysop):
    create_board(db, "lounge", min_read_level=10, creator=sysop)
    create_user(db, "alice", password="hunter2")
    set_level_name(db, 10, "Member", changed_by=sysop)

    assert "Level 0 → 10 (Member)" in _screen(lane, sysop, ["u", "u", "/", "alice", "l", "10"])
    assert "Level 10 (Member)" in _screen(lane, sysop, ["u", "l", "g", "Member"])


# -- the CLI


def _cli(capsys, db, *args) -> str:
    admin_main(["levels", *args, "--db", str(db.path)])
    return capsys.readouterr().out


def test_the_cli_prints_the_ladder_a_level_and_an_account_change(db, sysop, capsys):
    create_board(db, "lounge", min_read_level=10, min_write_level=50, creator=sysop)
    create_user(db, "alice", password="hunter2", user_level=10)
    set_level_name(db, 10, "Member", changed_by=sysop)
    db.close()

    ladder = _cli(capsys, db)
    assert "10 (Member)" in ladder and "1 read" in ladder

    level = _cli(capsys, db, "member")
    assert "Level 10 (Member): 1 account" in level and "lounge" in level

    change = _cli(capsys, db, "--user", "alice", "--to", "50")
    assert "alice: level 10 (Member) -> 50" in change
    assert "post" in change.split("Gains:")[1].split("Loses:")[0]


def test_the_cli_prints_json(db, sysop, capsys):
    create_board(db, "lounge", min_read_level=10, creator=sysop)
    db.close()

    data = json.loads(_cli(capsys, db, "10", "--json"))

    assert data["level"] == 10
    assert {gate["name"] for gate in data["opens"]} >= {"lounge"}


@pytest.mark.parametrize(
    ("args", "message"),
    [(["300"], "not a level"), (["--user", "nobody", "--to", "10"], "No account"), (["--to", "10"], "go together")],
)
def test_the_cli_refuses_what_it_cannot_answer(db, sysop, capsys, args, message):
    db.close()

    with pytest.raises(SystemExit) as exc:
        admin_main(["levels", *args, "--db", str(db.path)])

    assert message in str(exc.value.code)


def test_the_cli_answers_to_last_and_still_to_levels(db, sysop, capsys):
    create_board(db, "lounge", min_read_level=10, creator=sysop)
    db.close()

    admin_main(["last", "--db", str(db.path)])
    as_last = capsys.readouterr().out
    admin_main(["levels", "--db", str(db.path)])

    assert "1 read" in as_last and as_last == capsys.readouterr().out

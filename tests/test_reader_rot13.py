"""The ROT-13 toggle in the message reader (issue #1473, design doc §6.8):
`[D]ecode ROT-13` on a post and on a received letter shows the body decoded.
It is display-only -- the stored body never changes -- and a post's toggle
holds for that post alone."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board
from netbbs.boards.posts import create_post, get_post
from netbbs.mail import list_inbox, send_mail
from netbbs.net import board_flow
from netbbs.net.mail_flow import _show_inbox_message
from netbbs.net.redraw_preference import set_redraw_in_place_enabled
from netbbs.rendering.rot13 import rot13
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_board_list_and_reader import FakeSession as ReaderSession
from tests.test_help_everywhere_screens import FakeSession as LineSession


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
    user = create_user(db, "alice", password="hunter2", user_level=10)
    set_redraw_in_place_enabled(db, user, True)  # one screen per redraw
    return user


@pytest.fixture
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


def test_rot13_turns_letters_only_and_undoes_itself():
    assert rot13("Uryyb, Jbeyq! 73") == "Hello, World! 73"
    # Color and pipe codes are not text: they render the same decoded.
    coded = "\x1b[1;31mFcbvyre\x1b[0m |07|UN nurnq"
    assert rot13(coded) == "\x1b[1;31mSpoiler\x1b[0m |07|UN ahead"
    assert rot13(rot13(coded)) == coded
    assert rot13("Grüße") == "Teüßr"  # only ASCII letters turn


def test_a_post_is_decoded_on_request_and_stays_stored_as_written(db, alice):
    board = create_board(db, "general", creator=alice)
    post = create_post(db, board, alice, "Spoiler", "Gur ohgyre qvq vg.")
    session = ReaderSession(["0", "1", "d", "d", "b", "b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    opened, decoded, again = session.screens()[1:4]
    assert "Gur ohgyre qvq vg." in opened and "[D]ecode ROT-13: off" in opened
    assert "The butler did it." in decoded and "[D]ecode ROT-13: on" in decoded
    assert "Gur ohgyre qvq vg." in again and "[D]ecode ROT-13: off" in again
    assert get_post(db, post.post_id).body == "Gur ohgyre qvq vg."


def test_the_toggle_holds_for_the_post_on_screen_only(db, alice):
    board = create_board(db, "general", creator=alice)
    create_post(db, board, alice, "First", "Nycun")
    create_post(db, board, alice, "Second", "Oenib")
    session = ReaderSession(["0", "1", "d", "n", "p", "b", "b"])

    asyncio.run(board_flow._show_board(session, db, board, alice))

    _opened, decoded, stepped, back = session.screens()[1:5]
    assert "Alpha" in decoded
    assert "Oenib" in stepped and "[D]ecode ROT-13: off" in stepped
    # Coming back shows the post as written: nothing was remembered.
    assert "Nycun" in back and "Alpha" not in back


def test_a_received_letter_is_decoded_on_request(db, lane, alice, bob):
    send_mail(db, alice, bob, "Riddle", "Gur nafjre vf n gbjry.")
    [letter] = list_inbox(db, bob)
    session = LineSession(["d", "d", "b"])

    asyncio.run(_show_inbox_message(session, lane, bob, letter))

    text = session.squeezed()
    assert "The answer is a towel." in text
    assert "[D]ecode ROT-13: on" in text and "[D]ecode ROT-13: off" in text
    assert session.leftover == []
    [stored] = list_inbox(db, bob)
    assert stored.body == "Gur nafjre vf n gbjry."

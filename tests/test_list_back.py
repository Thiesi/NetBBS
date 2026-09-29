"""Back from a board or file area returns to the list it was picked from.

The field test's callers took Back to mean "one level up" (issue #839, F045).
Back from a board went past its list to the menu that list was opened from,
so a caller reading three boards went round that menu three times.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.boards.boards import create_board
from netbbs.boards.categories import create_category
from netbbs.files.areas import create_file_area
from netbbs.net import board_flow, file_flow
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane
from tests.test_login_flow_board_picker_sort import FakeSession, _visible_text


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
    return create_user(db, "alice", password="hunter2", user_level=10)


def test_back_from_a_board_returns_to_the_board_list_on_it(db, alice):
    create_board(db, "Pens", creator=alice)
    create_board(db, "Inks", creator=alice)

    # Board 02, Back: the list again; Back leaves it.
    session = FakeSession(["0", "2", "b", "b"])
    asyncio.run(board_flow._browse_boards(session, db, alice))
    text = _visible_text(session)

    after_board = text.rsplit("has no posts yet", 1)[1]
    assert "Available message boards" in after_board
    assert re.search(r">\s*02\.", after_board)  # the cursor on the board just left


def test_back_from_a_category_returns_to_the_top_level_list(db, alice):
    pens = create_category(db, "Pens", created_by=alice)
    create_board(db, "Vintage", creator=alice, category_id=pens.id)
    create_board(db, "Chatter", creator=alice)

    # The category (01), Back out of it: the top-level list again.
    session = FakeSession(["0", "1", "b", "b"])
    asyncio.run(board_flow._browse_boards(session, db, alice))
    text = _visible_text(session)

    assert text.count("[Pens]") == 2


def test_back_from_a_file_area_returns_to_the_area_list(db, lane, alice):
    create_file_area(db, "Scans", creator=alice)
    create_file_area(db, "Manuals", creator=alice)

    session = FakeSession(["0", "1", "b", "b"])
    asyncio.run(file_flow.browse_file_areas(session, lane, alice))
    text = _visible_text(session)

    after_area = text.rsplit("has no files yet", 1)[1]
    assert "Available file areas" in after_area

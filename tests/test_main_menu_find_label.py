"""The main menu's Find entry names what Find searches (issue #811).

Find searches posts, files, retained chat and -- since issue #824 -- the
caller's own mail. A caller mail is closed to (issue #816) gets no mail
results, so for them the entry does not promise any.
"""

from __future__ import annotations

import pytest

from netbbs.auth.users import create_user
from netbbs.config import set_mail_min_level
from netbbs.storage.database import Database
from tests.test_first_time_caller import _menu, _MenuSession
from tests.test_new_scan import _visible_text


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.mark.parametrize("width", [80, 40])
def test_find_names_mail_posts_files_and_chat(db, width):
    lena = create_user(db, "lena_h", password="hunter2", user_level=10)
    session = _MenuSession(["l", "y"])
    session.terminal_width = width
    _menu(db, session, lena)
    text = _visible_text(session)

    # Mail first, as its results are (issue #918).
    assert "Search mail, posts, files, chat" in text


@pytest.mark.parametrize("width", [80, 40])
def test_find_promises_no_mail_to_a_caller_mail_is_closed_to(db, width):
    lena = create_user(db, "lena_h", password="hunter2", user_level=10)
    set_mail_min_level(db, 20)
    session = _MenuSession(["l", "y"])
    session.terminal_width = width
    _menu(db, session, lena)
    text = _visible_text(session)

    assert "Search posts, files, and chat" in text
    assert "and mail" not in text

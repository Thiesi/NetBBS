"""The one-time "which line looks right?" question after login (issue #929)."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.auth.users import create_user
from netbbs.net.login_flow import _confirm_charset
from netbbs.net.unicode_style_preference import charset_preference, set_charset_preference
from netbbs.rendering.charset import ASCII, CP437, UTF8
from netbbs.storage.database import Database

_SAMPLE = "┌── café ──┐"


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


class _Session:
    transport_name = "telnet"
    terminal_width = 80

    def __init__(self, keys, *, certain=False, detected=ASCII):
        self.keys = list(keys)
        self.charset_certain = certain
        self.detected_charset = detected
        self.output_charset = detected
        self.text = ""
        self.raw = b""

    async def write(self, text):
        self.text += text

    async def write_line(self, text=""):
        self.text += text + "\r\n"

    async def write_raw(self, data):
        self.raw += data

    async def read_key(self, echo=True):
        return self.keys.pop(0)


@pytest.mark.parametrize(
    ("key", "preference", "charset"),
    [("1", "unicode", UTF8), ("2", "cp437", CP437), ("3", "ascii", ASCII)],
)
def test_the_answer_becomes_the_preference_at_once(db, key, preference, charset):
    user = create_user(db, "harold", password="hunter2", user_level=10)
    session = _Session(["x", key])
    asyncio.run(_confirm_charset(session, db, user))
    assert charset_preference(db, user) == preference
    assert session.output_charset == charset
    assert "Which of these lines looks right" in session.text


def test_the_samples_go_out_in_both_encodings_byte_for_byte(db):
    user = create_user(db, "harold", password="hunter2", user_level=10)
    session = _Session(["2"])
    asyncio.run(_confirm_charset(session, db, user))
    assert _SAMPLE.encode("utf-8") in session.raw
    assert _SAMPLE.encode("cp437") in session.raw


def test_a_terminal_that_said_for_certain_is_not_asked(db):
    user = create_user(db, "harold", password="hunter2", user_level=10)
    session = _Session([], certain=True, detected=CP437)
    asyncio.run(_confirm_charset(session, db, user))
    assert session.text == "" and charset_preference(db, user) == "auto"


def test_a_caller_who_already_chose_is_not_asked(db):
    user = create_user(db, "harold", password="hunter2", user_level=10)
    set_charset_preference(db, user, "unicode")
    session = _Session([])
    asyncio.run(_confirm_charset(session, db, user))
    assert session.text == ""

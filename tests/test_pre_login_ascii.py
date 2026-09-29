"""Before sign-in, Telnet callers get plain ASCII chrome (issue #841, F073).

A CP437 terminal such as SyncTERM showed the UTF-8 rules, arrows and the
default banner's box as noise until after login, when the caller could
first ask for plain ASCII. The browser and SSH keep the Unicode look."""

from __future__ import annotations

import asyncio

import pytest

from netbbs.net.login_flow import _write_connection_notice
from netbbs.net.welcome_banner import banner_path, load_welcome_banner, pre_login_unicode_style, set_welcome_banner_enabled
from netbbs.rendering import strip_ansi
from netbbs.storage.database import Database

_UNICODE_CHROME = set("─═║╔╗╚╝›")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


class _Session:
    terminal_width = 80

    def __init__(self, transport_name: str):
        self.transport_name = transport_name
        self.written: list[str] = []

    async def write_line(self, text: str = "") -> None:
        self.written.append(text)


@pytest.mark.parametrize(("transport", "unicode"), [("telnet", False), ("web", True), ("ssh", True)])
def test_pre_login_style_follows_the_transport(transport, unicode):
    assert pre_login_unicode_style(_Session(transport)) is unicode


def test_default_banner_in_ascii_has_no_box_characters(db):
    text = load_welcome_banner(db, unicode_style=False)
    assert not _UNICODE_CHROME & set(text)
    assert "+====" in text
    assert "N E T B B S" in strip_ansi(text)


def test_default_banner_keeps_its_box_where_unicode_is_fine(db):
    assert "╔" in load_welcome_banner(db, unicode_style=True)


def test_a_sysop_banner_is_shown_as_authored_either_way(db):
    banner_path(db).write_bytes("MY ═ PEN".encode())
    set_welcome_banner_enabled(db, True)
    assert "MY ═ PEN" in load_welcome_banner(db, unicode_style=False)


@pytest.mark.parametrize(("transport", "expect_unicode"), [("telnet", False), ("web", True)])
def test_connection_notices_follow_the_transport(db, transport, expect_unicode):
    session = _Session(transport)
    asyncio.run(_write_connection_notice(session, db, "Sign-in failed", "Too many failed attempts."))
    text = "".join(session.written)
    assert bool(_UNICODE_CHROME & set(text)) is expect_unicode

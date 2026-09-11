"""
What a caller sees when their terminal cannot do Zmodem (issue #475).

The file area used to offer exactly one way to move a file, and to
discover that the caller's client could not do it by starting a
handshake nobody answered. These tests drive the real `_show_area` loop
with a session that declares it has no Zmodem -- what the web transport
is, and what a PuTTY caller effectively is -- and check that the screen
offers the browser link instead of the transfer that could not work.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.files.areas import create_file_area
from netbbs.files.entries import upload_file
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.file_transfer import TransferGrants
from netbbs.net.file_flow import _show_area
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


class FakeSession:
    supports_zmodem = True

    def __init__(self, editor_keys=None, lines=None, width=80, height=24):
        self._keys = iter(editor_keys or [])
        self._lines = iter(lines or [])
        self.written: list[str] = []
        self.terminal_width = width
        self.terminal_height = height
        self.node_display_name = "NetBBS"
        self.node_name_gradient = None
        self.peer_address = "203.0.113.5"
        self.supports_truecolor = False

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True) -> str:
        return next(self._lines, "")

    async def read_key(self, echo: bool = True) -> str:
        return next(self._lines, "b")

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
        return next(self._keys, EditorKey(EditorKeyKind.CHAR, char="b"))

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError

    async def read_byte(self):
        raise NotImplementedError

    @property
    def visible_output(self) -> str:
        return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", "".join(self.written))


class BrowserSession(FakeSession):
    """A transport that can never carry Zmodem -- `netbbs.net.web`'s
    own answer."""

    supports_zmodem = False


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def lane(tmp_path):
    database_lane = DatabaseLane(tmp_path / "node.db")
    yield database_lane
    database_lane.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


@pytest.fixture
def grants():
    return TransferGrants(base_url="https://bbs.example.org")


def _key(char: str) -> EditorKey:
    return EditorKey(EditorKeyKind.CHAR, char=char)


def _url_in(session: FakeSession) -> str | None:
    match = re.search(r"https://bbs\.example\.org/transfer/(\S+)", session.visible_output)
    return match.group(0) if match else None


# -- the browser transport ----------------------------------------------


def test_upload_from_a_browser_session_offers_a_link_instead_of_zmodem(db, lane, alice, grants):
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "game.zip", b"payload")
    session = BrowserSession(editor_keys=[_key("u")])

    asyncio.run(_show_area(session, lane, area, alice, transfers=grants))

    assert "Zmodem send" not in session.visible_output
    assert _url_in(session) is not None
    assert "works once" in session.visible_output


def test_download_from_a_browser_session_offers_a_link(db, lane, alice, grants):
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "game.zip", b"payload")
    session = BrowserSession(editor_keys=[_key("1")])

    asyncio.run(_show_area(session, lane, area, alice, transfers=grants))

    assert "Starting Zmodem send" not in session.visible_output
    assert _url_in(session) is not None


def test_a_browser_session_on_a_node_without_transfers_is_told_why(db, lane, alice):
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "game.zip", b"payload")
    session = BrowserSession(editor_keys=[_key("u")])

    asyncio.run(_show_area(session, lane, area, alice))

    assert "cannot carry a Zmodem transfer" in session.visible_output
    assert "web listener" in session.visible_output


# -- an ordinary terminal, whose emulator may still lack Zmodem ---------


def test_w_offers_an_upload_link_on_a_zmodem_capable_transport(db, lane, alice, grants):
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "game.zip", b"payload")
    session = FakeSession(editor_keys=[_key("w")], lines=["u"])

    asyncio.run(_show_area(session, lane, area, alice, transfers=grants))

    assert "Browser transfer" in session.visible_output
    assert _url_in(session) is not None
    assert "upload to [downloads]" in session.visible_output


def test_w_offers_a_download_link_for_the_selected_file(db, lane, alice, grants):
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "game.zip", b"payload")
    session = FakeSession(editor_keys=[_key("w")], lines=["d"])

    asyncio.run(_show_area(session, lane, area, alice, transfers=grants))

    assert "download of 'game.zip'" in session.visible_output
    assert _url_in(session) is not None


def test_backing_out_of_the_link_screen_mints_nothing(db, lane, alice, grants):
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "game.zip", b"payload")
    session = FakeSession(editor_keys=[_key("w")], lines=["b"])

    asyncio.run(_show_area(session, lane, area, alice, transfers=grants))

    assert _url_in(session) is None
    assert len(grants) == 0


def test_the_key_is_not_offered_when_the_node_has_no_transfers(db, lane, alice):
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "game.zip", b"payload")
    session = FakeSession()

    asyncio.run(_show_area(session, lane, area, alice))

    assert "eb transfer" not in session.visible_output


def test_a_node_with_no_public_address_says_so_rather_than_printing_one(db, lane, alice):
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "game.zip", b"payload")
    session = FakeSession(editor_keys=[_key("w")], lines=["u"])

    asyncio.run(_show_area(session, lane, area, alice, transfers=TransferGrants()))

    assert "no public web address configured" in session.visible_output


def test_a_minted_link_is_the_one_the_gateway_will_honour(db, lane, alice, grants):
    """The URL on screen has to be a token the grant table will accept
    -- printing one it would not is the failure mode this catches."""
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "game.zip", b"payload")
    session = FakeSession(editor_keys=[_key("w")], lines=["u"])

    asyncio.run(_show_area(session, lane, area, alice, transfers=grants))

    url = _url_in(session)
    assert url is not None
    token = url.rsplit("/", 1)[-1]
    redeemed = grants.redeem(token)
    assert redeemed is not None
    assert redeemed.user_id == alice.id
    assert redeemed.area_id == area.area_id


def test_the_hints_describe_what_this_transport_can_actually_do(db, lane, alice, grants):
    """Telling a browser caller to "receive via Zmodem" describes a
    transfer their client cannot start -- which is how the file area
    came to look broken to most people in the first place."""
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "game.zip", b"payload")

    terminal = FakeSession()
    asyncio.run(_show_area(terminal, lane, area, alice, transfers=grants))
    assert "receive via Zmodem" in terminal.visible_output
    assert "Send a file via Zmodem" in terminal.visible_output
    assert "eb transfer" in terminal.visible_output  # the link screen, for a terminal

    browser = BrowserSession()
    asyncio.run(_show_area(browser, lane, area, alice, transfers=grants))
    assert "Zmodem" not in browser.visible_output
    assert "browser download link" in browser.visible_output
    assert "Send a file from your browser" in browser.visible_output
    # ... and no second key for what [U] and the numbers already do.
    assert "eb transfer" not in browser.visible_output


def test_an_empty_area_still_offers_a_browser_upload_link(db, lane, alice, grants):
    """An empty area is exactly where a caller whose emulator has no
    Zmodem needs to put the first file (Codex review)."""
    area = create_file_area(db, "downloads", creator=alice)
    session = FakeSession(lines=["w", "u"])

    asyncio.run(_show_area(session, lane, area, alice, transfers=grants))

    assert "eb transfer" in session.visible_output
    assert _url_in(session) is not None


def test_minting_a_link_does_not_touch_the_database_lane(db, lane, alice, grants, monkeypatch):
    """`TransferGrants` is event-loop state that touches no database, so
    issuing must not run on the lane's worker thread while an HTTP
    request redeems on the loop (Codex review)."""
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "game.zip", b"payload")
    issued_on: list[str] = []
    real_issue = grants.issue

    def watching_issue(**kwargs):
        import threading

        issued_on.append(threading.current_thread().name)
        return real_issue(**kwargs)

    monkeypatch.setattr(grants, "issue", watching_issue)
    session = FakeSession(editor_keys=[_key("w")], lines=["u"])

    asyncio.run(_show_area(session, lane, area, alice, transfers=grants))

    assert issued_on == ["MainThread"]


class PageSession(BrowserSession):
    """A browser session whose page can act on a transfer itself."""

    def __init__(self, *args, accepts=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.offered: list[dict] = []
        self._accepts = accepts

    async def offer_transfer(self, *, direction, url, filename=None):
        self.offered.append({"direction": direction, "url": url, "filename": filename})
        return self._accepts


def test_a_browser_page_is_handed_the_transfer_rather_than_the_url(db, lane, alice, grants):
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "game.zip", b"payload")
    session = PageSession(editor_keys=[_key("u")])

    asyncio.run(_show_area(session, lane, area, alice, transfers=grants))

    assert len(session.offered) == 1
    assert session.offered[0]["direction"] == "upload"
    assert session.offered[0]["url"].startswith("https://bbs.example.org/transfer/")
    # ... and the terminal says what is happening rather than printing a
    # URL the caller would have to copy.
    assert "Pick a file in your browser" in session.visible_output
    assert _url_in(session) is None


def test_a_download_offered_to_the_page_names_the_file(db, lane, alice, grants):
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "game.zip", b"payload")
    session = PageSession(editor_keys=[_key("1")])

    asyncio.run(_show_area(session, lane, area, alice, transfers=grants))

    assert session.offered[0]["direction"] == "download"
    assert session.offered[0]["filename"] == "game.zip"
    assert "Your browser is handling" in session.visible_output


def test_a_page_that_cannot_take_it_still_gets_the_url(db, lane, alice, grants):
    """A closed socket, an older client, a page that ignores the frame:
    the caller must still end up with something they can use."""
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "game.zip", b"payload")
    session = PageSession(editor_keys=[_key("u")], accepts=False)

    asyncio.run(_show_area(session, lane, area, alice, transfers=grants))

    assert _url_in(session) is not None
    assert "works once" in session.visible_output

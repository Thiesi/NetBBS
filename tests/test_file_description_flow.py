"""
The file-area description surface (issue #463): `[E]dit description`
on a file listing, and the multi-line rendering a `FILE_ID.DIZ` needs.

Drives the real `_show_area` loop against a fake session, so the
editor, the permission gate, and the redraw are exercised together --
the description field existed in the domain layer and the Link
protocol for a long time with no way for anyone actually calling the
BBS to set or see more than one line of it, which is the bug this
screen closes.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from netbbs.auth.users import create_user
from netbbs.files import get_file, upload_file
from netbbs.files.areas import create_file_area
from netbbs.files.entries import FileEntryError
from netbbs.moderation.roles import BoardPermission, grant_permissions
from netbbs.net import file_flow
from netbbs.net.char_input import EditorKey, EditorKeyKind
from netbbs.net.file_flow import _show_area
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


class FakeSession:
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

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
        # Falling back to [B]ack keeps an exhausted script from hanging
        # the loop -- see tests/test_file_columnar_and_shortcuts.py.
        return next(self._keys, EditorKey(EditorKeyKind.CHAR, char="b"))

    async def write_raw(self, data: bytes) -> None:
        raise NotImplementedError

    async def read_byte(self):
        raise NotImplementedError

    @property
    def visible_output(self) -> str:
        return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", "".join(self.written))


class FakeLineSession(FakeSession):
    """A transport with no editor-key support at all -- `_read_file_
    choice` falls back to reading a whole command line, the path a web
    session takes."""

    async def read_editor_key(self, *, distinguish_ctrl_h: bool = False) -> EditorKey:
        raise NotImplementedError


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
def bob(db):
    return create_user(db, "bob", password="hunter2", user_level=10)


def _key(char: str) -> EditorKey:
    return EditorKey(EditorKeyKind.CHAR, char=char)


# -- rendering ----------------------------------------------------------


def test_every_description_line_is_rendered(db, lane, alice):
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(
        db, area, alice, "game.zip", b"payload",
        description="╔═╗\nCool Game v1.0\nBy Someone\n\nRequires 640K",
    )
    session = FakeSession()

    asyncio.run(_show_area(session, lane, area, alice))

    output = session.visible_output
    for line in ("╔═╗", "Cool Game v1.0", "By Someone", "Requires 640K"):
        assert line in output


# -- the [E] action -----------------------------------------------------


def test_e_describes_the_only_file_on_the_page(db, lane, alice):
    area = create_file_area(db, "downloads", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    session = FakeSession(editor_keys=[_key("e")], lines=["Cool Game v1.0", "By Someone", ""])

    asyncio.run(_show_area(session, lane, area, alice))

    assert get_file(db, entry.file_id).description == "Cool Game v1.0\nBy Someone"
    assert "Description saved" in session.visible_output
    # And the amended entry is on screen straight away, without a
    # re-query that would move the page.
    assert "By Someone" in session.visible_output


def test_e_targets_the_highlighted_file(db, lane, alice, monkeypatch):
    from netbbs.files import entries as entries_module

    timestamps = iter(f"2026-01-01T00:00:0{i}.000000Z" for i in range(3))
    monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(timestamps))
    area = create_file_area(db, "downloads", creator=alice)
    first = upload_file(db, area, alice, "first.zip", b"one")
    second = upload_file(db, area, alice, "second.zip", b"two")

    # Down twice: no highlight -> entry 1 -> entry 2 (the page is
    # oldest-first within the page), then [E].
    session = FakeSession(
        editor_keys=[
            EditorKey(EditorKeyKind.DOWN), EditorKey(EditorKeyKind.DOWN), _key("e"),
        ],
        lines=["the second one", ""],
    )

    asyncio.run(_show_area(session, lane, area, alice))

    assert get_file(db, second.file_id).description == "the second one"
    assert get_file(db, first.file_id).description is None


def test_describe_by_name_from_a_typed_command(db, lane, alice):
    area = create_file_area(db, "downloads", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    # No editor-key support at all (a transport without it): the
    # command line reaches the same place.
    session = FakeLineSession(lines=["/describe game.zip", "typed by name", "", "b"])

    asyncio.run(_show_area(session, lane, area, alice))

    assert get_file(db, entry.file_id).description == "typed by name"


def test_a_stranger_is_not_offered_the_action_and_cannot_use_it(db, lane, alice, bob):
    area = create_file_area(db, "downloads", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    session = FakeSession(editor_keys=[_key("e")], lines=["should never be saved", ""])

    asyncio.run(_show_area(session, lane, area, bob))

    assert "dit description" not in session.visible_output
    assert get_file(db, entry.file_id).description is None


def test_a_moderator_holding_edit_may_describe_someone_elses_upload(db, lane, alice, bob):
    area = create_file_area(db, "downloads", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    grant_permissions(
        db, bob, object_type="file_area", object_id=area.id,
        permissions=BoardPermission.EDIT, granted_by=alice,
    )
    session = FakeSession(editor_keys=[_key("e")], lines=["moderator's wording", ""])

    asyncio.run(_show_area(session, lane, area, bob))

    assert "dit description" in session.visible_output
    assert get_file(db, entry.file_id).description == "moderator's wording"


def test_the_editor_enforces_the_same_line_cap_the_domain_does(db, lane, alice):
    area = create_file_area(db, "downloads", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    session = FakeSession(
        editor_keys=[_key("e")],
        lines=[*[f"line {i}" for i in range(12)], ""],
    )

    asyncio.run(_show_area(session, lane, area, alice))

    assert "cannot exceed 10 logical lines" in session.visible_output
    assert len(get_file(db, entry.file_id).description.split("\n")) == 10


def test_a_rejected_save_keeps_the_draft_and_changes_nothing(db, lane, alice, monkeypatch):
    area = create_file_area(db, "downloads", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload", description="the original")

    def refuse(db_, entry_, description, *, changed_by):
        raise FileEntryError("nope")

    monkeypatch.setattr(file_flow, "set_file_description", refuse)
    session = FakeSession(
        editor_keys=[_key("e")],
        # First pass writes a description and finishes; the refusal
        # sends it back into the editor, where /cancel gives up.
        lines=["a replacement", "", "/cancel"],
    )

    asyncio.run(_show_area(session, lane, area, alice))

    output = session.visible_output
    assert "Not saved: nope" in output
    # Re-opened seeded with what was typed, not with a blank buffer.
    assert output.rindex("a replacement") > output.index("Not saved: nope")
    assert get_file(db, entry.file_id).description == "the original"


def test_cancelling_the_editor_leaves_the_description_alone(db, lane, alice):
    area = create_file_area(db, "downloads", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload", description="the original")
    session = FakeSession(editor_keys=[_key("e")], lines=["/cancel"])

    asyncio.run(_show_area(session, lane, area, alice))

    assert "Description unchanged" in session.visible_output
    assert get_file(db, entry.file_id).description == "the original"


def test_a_peers_description_cannot_decide_how_tall_the_listing_is(db, lane, alice):
    """A `files` row written before descriptions were bounded — a peer's
    catalogue entry promoted by `netbbs.link.file_transfer`, say — must
    not be able to push an unbounded block onto the screen."""
    area = create_file_area(db, "downloads", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    db.connection.execute(
        "UPDATE files SET description = ? WHERE id = ?",
        ("\n".join(f"shouty line {i}" for i in range(200)), entry.id),
    )
    db.connection.commit()
    session = FakeSession()

    asyncio.run(_show_area(session, lane, area, alice))

    output = session.visible_output
    assert "shouty line 0" in output
    assert "shouty line 9" in output
    assert "shouty line 10" not in output

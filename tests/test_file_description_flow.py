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
    """Codex review: the fullscreen editor deletes its own draft on the
    way out, believing the save will take, so a rejection has to put the
    text back on disk before anything is awaited -- and then say where
    it went rather than reopening over its own recovery prompt."""
    from netbbs.net.draft_storage import drafts_directory

    area = create_file_area(db, "downloads", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload", description="the original")

    def refuse(db_, entry_, description, *, changed_by):
        raise FileEntryError("nope")

    monkeypatch.setattr(file_flow, "set_file_description", refuse)
    session = FakeSession(editor_keys=[_key("e")], lines=["a replacement", ""])

    asyncio.run(_show_area(session, lane, area, alice))

    output = session.visible_output
    assert "Not saved: nope" in output
    assert "kept as a draft" in output
    assert get_file(db, entry.file_id).description == "the original"
    draft = drafts_directory(db) / f"filedesc_{entry.file_id[:16]}_{alice.id}.draft"
    assert draft.exists()
    assert "a replacement" in draft.read_text(encoding="utf-8")


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


# -- review follow-ups --------------------------------------------------


def test_e_with_no_cursor_and_several_files_opens_a_picker(db, lane, alice, monkeypatch):
    """Design doc §3.5 (Codex review): `[E]` on a page with nothing
    highlighted has to find out which file it means, and a picker is
    how this codebase asks — never a typed prompt in front of the
    editor."""
    from netbbs.files import entries as entries_module

    timestamps = iter(f"2026-01-01T00:00:0{i}.000000Z" for i in range(3))
    monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(timestamps))
    area = create_file_area(db, "downloads", creator=alice)
    first = upload_file(db, area, alice, "first.zip", b"one")
    upload_file(db, area, alice, "second.zip", b"two")

    # [E], then the picker's own two-digit row selection.
    session = FakeSession(
        editor_keys=[_key("e"), _key("0"), _key("1")],
        lines=["picked from the list", ""],
    )

    asyncio.run(_show_area(session, lane, area, alice))

    assert "Describe a file in downloads" in session.visible_output
    assert get_file(db, first.file_id).description == "picked from the list"


def test_backing_out_of_the_picker_changes_nothing(db, lane, alice, monkeypatch):
    from netbbs.files import entries as entries_module

    timestamps = iter(f"2026-01-01T00:00:0{i}.000000Z" for i in range(3))
    monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(timestamps))
    area = create_file_area(db, "downloads", creator=alice)
    first = upload_file(db, area, alice, "first.zip", b"one")
    second = upload_file(db, area, alice, "second.zip", b"two")

    session = FakeSession(editor_keys=[_key("e"), _key("b")])

    asyncio.run(_show_area(session, lane, area, alice))

    assert get_file(db, first.file_id).description is None
    assert get_file(db, second.file_id).description is None


def test_describe_by_name_reaches_a_file_the_page_gate_would_have_refused(db, lane, alice, bob):
    """Codex review: `_can_describe` answers "is this hotkey worth
    offering for what is on screen", which is the wrong question for a
    filename the caller typed — the real answer comes from the domain,
    and a caller who names someone else's file deserves to be told
    that rather than have the command silently rejected."""
    area = create_file_area(db, "downloads", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    session = FakeLineSession(lines=["/describe game.zip", "b"])

    asyncio.run(_show_area(session, lane, area, bob))

    assert "was uploaded by someone else" in session.visible_output
    assert get_file(db, entry.file_id).description is None


def test_leaving_the_editor_with_a_kept_draft_says_so(db, lane, alice):
    """`/exit` keeps the draft on disk (issue #149) — reporting that as
    "unchanged" would hide work the caller expects to find again
    (Codex review)."""
    area = create_file_area(db, "downloads", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    session = FakeSession(editor_keys=[_key("e")], lines=["half a description", "/exit"])

    asyncio.run(_show_area(session, lane, area, alice))

    assert "Draft kept" in session.visible_output
    assert get_file(db, entry.file_id).description is None


def test_describing_a_file_deleted_meanwhile_fails_instead_of_claiming_success(db, lane, alice):
    """Codex review: the entry on screen can be stale by the time the
    editor closes. Re-resolving by `file_id` is what turns "saved!"
    over an UPDATE that matched nothing into an honest refusal."""
    from netbbs.files.entries import delete_file, set_file_description

    area = create_file_area(db, "downloads", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    grant_permissions(
        db, alice, object_type="file_area", object_id=area.id,
        permissions=BoardPermission.DELETE, granted_by=alice,
    )
    delete_file(db, entry, deleted_by=alice)

    with pytest.raises(FileEntryError):
        set_file_description(db, entry, "into the void", changed_by=alice)


def test_a_rejected_save_is_written_back_to_disk_before_anything_else(db, lane, alice, monkeypatch, tmp_path):
    """Codex review: the fullscreen editor deletes its own draft on the
    way out, believing the save will take. If the domain then rejects
    it, the text lives only in memory until the reopened editor's next
    autosave — so it goes back to disk first, before any awaited UI."""
    from netbbs.net.draft_storage import drafts_directory

    area = create_file_area(db, "downloads", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    seen: list[str] = []

    def refuse(db_, entry_, description, *, changed_by):
        raise FileEntryError("nope")

    async def capture_write_line(text=""):
        # Whatever is on disk at the moment the failure is announced.
        draft = drafts_directory(db) / f"filedesc_{entry.file_id[:16]}_{alice.id}.draft"
        if draft.exists():
            seen.append(draft.read_text(encoding="utf-8"))

    monkeypatch.setattr(file_flow, "set_file_description", refuse)
    session = FakeSession(editor_keys=[_key("e")], lines=["a replacement", "", "/cancel"])
    original_write_line = session.write_line

    async def write_line(text: str = "") -> None:
        if "Not saved" in text:
            await capture_write_line(text)
        await original_write_line(text)

    session.write_line = write_line

    asyncio.run(_show_area(session, lane, area, alice))

    assert seen and "a replacement" in seen[0]


def test_u_starts_an_upload_without_typing_a_slash_command(db, lane, alice, monkeypatch):
    """`/upload` was a slash command only because this screen read whole
    lines before it grew editor-key support; it never took an argument.
    `[U]` now starts it, and `/upload` still works."""
    started: list[str] = []

    async def fake_upload(session, lane_, area_, user_, **kwargs):
        started.append(area_.name)

    monkeypatch.setattr(file_flow, "_handle_upload", fake_upload)
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "game.zip", b"payload")

    asyncio.run(_show_area(FakeSession(editor_keys=[_key("u")]), lane, area, alice))
    assert started == ["downloads"]

    started.clear()
    asyncio.run(_show_area(FakeLineSession(lines=["/upload"]), lane, area, alice))
    assert started == ["downloads"]


def test_u_is_refused_without_write_access(db, lane, alice, bob, monkeypatch):
    started: list[str] = []

    async def fake_upload(session, lane_, area_, user_, **kwargs):
        started.append(area_.name)

    monkeypatch.setattr(file_flow, "_handle_upload", fake_upload)
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "game.zip", b"payload")
    # Raised only after the file is in place, so alice's own upload
    # above still stands while bob is now below the write gate.
    from netbbs.files.areas import get_file_area_by_name

    db.connection.execute("UPDATE file_areas SET min_write_level = 50 WHERE id = ?", (area.id,))
    db.connection.commit()
    area = get_file_area_by_name(db, "downloads")

    session = FakeSession(editor_keys=[_key("u")])
    asyncio.run(_show_area(session, lane, area, bob))

    assert started == []
    # ("Uploader" is a column header, hence matching the offered key.)
    assert "[U]pload" not in session.visible_output


def test_e_describes_a_pending_upload_on_the_empty_screen(db, lane, alice):
    """A moderated area holding only the caller's own pending upload
    renders as the empty state — `list_files_page` shows nothing
    unapproved — so `[E]` there resolves its target from what they have
    waiting rather than from a listing that has nothing on it."""
    area = create_file_area(db, "downloads", creator=alice, moderated=True)
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    assert get_file(db, entry.file_id).status == "pending"

    session = FakeLineSession(lines=["e", "describing it while it waits", ""])

    asyncio.run(_show_area(session, lane, area, alice))

    assert "dit description" in session.visible_output
    assert get_file(db, entry.file_id).description == "describing it while it waits"


def test_the_empty_screen_offers_nothing_to_describe_when_nothing_is_waiting(db, lane, alice):
    area = create_file_area(db, "downloads", creator=alice)
    session = FakeLineSession(lines=["e"])

    asyncio.run(_show_area(session, lane, area, alice))

    assert "dit description" not in session.visible_output
    assert "Unknown command." in session.visible_output


def test_describe_by_name_still_works_on_the_empty_screen(db, lane, alice):
    area = create_file_area(db, "downloads", creator=alice, moderated=True)
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    session = FakeLineSession(lines=["/describe game.zip", "named while pending", ""])

    asyncio.run(_show_area(session, lane, area, alice))

    assert get_file(db, entry.file_id).description == "named while pending"
def test_describing_a_file_in_a_linked_area_says_the_change_stays_local(db, lane, alice):
    """Issue #464 made an approved upload's catalogue entry go out the
    moment it lands, so by the time anyone edits its description peers
    already have the original — and a `file_descriptor` is immutable.
    Say so rather than let the caller assume the edit travels."""
    from netbbs.files.areas import get_file_area_by_name
    from netbbs.link.files import link_file_area, queue_file_descriptor_if_linked
    from netbbs.link.node_identity import bootstrap_node_identity

    identity = bootstrap_node_identity("roanoke")
    area = create_file_area(db, "downloads", creator=alice)
    link_file_area(db, area, node_identity=identity)
    area = get_file_area_by_name(db, "downloads")
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    queue_file_descriptor_if_linked(db, entry, area, node_identity=identity)
    session = FakeSession(editor_keys=[_key("e")], lines=["a better description", ""])

    asyncio.run(_show_area(session, lane, area, alice))

    assert get_file(db, entry.file_id).description == "a better description"
    assert "peers keep the description they were already sent" in session.visible_output


def test_an_unlinked_area_says_nothing_about_peers(db, lane, alice):
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "game.zip", b"payload")
    session = FakeSession(editor_keys=[_key("e")], lines=["a better description", ""])

    asyncio.run(_show_area(session, lane, area, alice))

    assert "peers keep" not in session.visible_output


def test_a_file_predating_the_link_is_not_described_as_already_sent(db, lane, alice):
    """Codex review: a file approved before its area was Linked has no
    descriptor and never gets one (pre-Link history is not backfilled),
    so telling its describer that peers hold an older wording would be
    false."""
    from netbbs.link.files import link_file_area
    from netbbs.link.node_identity import bootstrap_node_identity

    area = create_file_area(db, "downloads", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    link_file_area(db, area, node_identity=bootstrap_node_identity("roanoke"))
    session = FakeSession(editor_keys=[_key("e")], lines=["a better description", ""])

    asyncio.run(_show_area(session, lane, area, alice))

    assert get_file(db, entry.file_id).description == "a better description"
    assert "peers keep" not in session.visible_output

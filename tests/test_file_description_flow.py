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
    def __init__(self, editor_keys=None, keys=None, lines=None, width=80, height=24):
        self._keys = iter(editor_keys or [])
        self._chars = iter(keys or [])
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

    async def read_line(self, echo: bool = True, **kwargs) -> str:
        return next(self._lines, "")

    async def read_key(self, echo: bool = True) -> str:
        # Raises rather than returning "" forever: an unrecognized key
        # bells and re-renders instead of leaving, so a silent ""
        # would spin this screen's action bar in an infinite loop and
        # hang the test rather than fail it. A test that needs the
        # loop to end scripts an explicit "b".
        key = next(self._chars, None)
        if key is None:
            raise AssertionError("FakeSession.read_key() called with no more scripted keys")
        return key

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
    choice` falls back to `read_key()`, the same keystrokes without a
    cursor, which is the path a web session takes. It used to fall back
    to reading a whole slash-command line; this screen no longer has
    one."""

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


def _key_down() -> EditorKey:
    return EditorKey(EditorKeyKind.DOWN)


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


def test_e_reaches_the_editor_on_a_transport_without_editor_keys(db, lane, alice):
    """A transport with no `read_editor_key` (a web session) answers to
    the same `[E]`, without the cursor -- it does not get a second
    dialect of this screen."""
    area = create_file_area(db, "downloads", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    session = FakeLineSession(keys=["e", "b"], lines=["pressed without a cursor", ""])

    asyncio.run(_show_area(session, lane, area, alice))

    assert get_file(db, entry.file_id).description == "pressed without a cursor"


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


def test_picking_someone_elses_file_out_of_the_picker_says_why_not(db, lane, alice, bob, monkeypatch):
    """Codex review: `_can_describe` answers "is this hotkey worth
    offering for what is on screen", which is the wrong question for
    the one file finally chosen — the real answer comes from the
    domain, and a caller who picks someone else's file deserves to be
    told that rather than have the action silently rejected.

    Reached through the picker now that there is no `/describe
    <filename>` to name a file the page gate would have refused: the
    page carries bob's own upload, so `[E]` is offered, and the file he
    then picks is alice's."""
    from netbbs.files import entries as entries_module

    timestamps = iter(f"2026-01-01T00:00:0{i}.000000Z" for i in range(3))
    monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(timestamps))
    area = create_file_area(db, "downloads", creator=alice)
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    upload_file(db, area, bob, "bobs.zip", b"his own")

    # [E] with no cursor and two files: the picker's row 01 is alice's,
    # the older of the two.
    session = FakeSession(editor_keys=[_key("e"), _key("0"), _key("1")])

    asyncio.run(_show_area(session, lane, area, bob))

    output = session.visible_output
    assert "Describe a file in downloads" in output
    assert "was uploaded by someone else" in output
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


def test_u_starts_an_upload_on_either_transport(db, lane, alice, monkeypatch):
    """`/upload` was a slash command only because this screen read whole
    lines before it grew editor-key support; it never took an argument.
    `[U]` is the whole of it now, on a transport with editor keys and
    on one without."""
    started: list[str] = []

    async def fake_upload(session, lane_, area_, user_, **kwargs):
        started.append(area_.name)

    monkeypatch.setattr(file_flow, "_handle_upload", fake_upload)
    area = create_file_area(db, "downloads", creator=alice)
    upload_file(db, area, alice, "game.zip", b"payload")

    asyncio.run(_show_area(FakeSession(editor_keys=[_key("u")]), lane, area, alice))
    assert started == ["downloads"]

    started.clear()
    asyncio.run(_show_area(FakeLineSession(keys=["u"]), lane, area, alice))
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
    waiting rather than from a listing that has nothing on it.

    The empty screen is an action bar read with `read_key()` now, so
    this is the `e` keystroke, not a typed line."""
    area = create_file_area(db, "downloads", creator=alice, moderated=True)
    entry = upload_file(db, area, alice, "game.zip", b"payload")
    assert get_file(db, entry.file_id).status == "pending"

    session = FakeSession(keys=["e"], lines=["describing it while it waits", ""])

    asyncio.run(_show_area(session, lane, area, alice))

    assert "dit description" in session.visible_output
    assert get_file(db, entry.file_id).description == "describing it while it waits"


def test_e_describes_a_pending_upload_from_a_listing_that_is_not_empty(db, lane, alice, bob, monkeypatch):
    """The same waiting upload, on a moderated area that also holds
    somebody else's approved file — so the listing renders, and the
    empty screen's `[E]` above never runs.

    This is the reach `/describe <filename>` used to cover: a pending
    file is invisible in the listing (`list_files_page` carries
    `'approved'` rows only), and naming it was the only way in. `[E]`
    now offers the page's rows *plus* what the caller has waiting, so
    the picker can reach it. Without that, the one screen able to
    describe this file would not have offered to."""
    from netbbs.files import entries as entries_module
    from netbbs.files.entries import approve_file

    timestamps = iter(f"2026-01-01T00:00:0{i}.000000Z" for i in range(3))
    monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(timestamps))
    area = create_file_area(db, "downloads", creator=bob, moderated=True)
    grant_permissions(
        db, bob, object_type="file_area", object_id=area.id,
        permissions=BoardPermission.APPROVE, granted_by=bob,
    )
    theirs = upload_file(db, area, bob, "bobs.zip", b"his own")
    approve_file(db, theirs, approved_by=bob)
    mine = upload_file(db, area, alice, "game.zip", b"payload")
    assert get_file(db, mine.file_id).status == "pending"

    # Two candidates and no cursor, so the picker opens: row 01 is the
    # listing's approved file, row 02 alice's own pending upload,
    # appended after it.
    session = FakeSession(
        editor_keys=[_key("e"), _key("0"), _key("2")],
        lines=["describing it while it waits", ""],
    )

    asyncio.run(_show_area(session, lane, area, alice))

    assert get_file(db, mine.file_id).description == "describing it while it waits"
    # The pending file is describable, not browsable: it must not have
    # been rendered into the approved-only listing.
    assert "game.zip" not in session.visible_output.split("Describe a file in")[0]


def test_describing_a_pending_upload_twice_in_one_visit_sees_the_first_edit(db, lane, alice, bob, monkeypatch):
    """Claude review of PR #638: `[E]`'s candidate list holds the page's
    rows *plus* the caller's pending uploads, and only the page half was
    being amended after a save. A second `[E]` in the same visit then
    offered the pre-edit row — the picker said "(no description yet)"
    and the editor reopened on the old text, inviting the caller to
    overwrite work they had just saved."""
    from netbbs.files import entries as entries_module
    from netbbs.files.entries import approve_file

    timestamps = iter(f"2026-01-01T00:00:0{i}.000000Z" for i in range(3))
    monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(timestamps))
    area = create_file_area(db, "downloads", creator=bob, moderated=True)
    grant_permissions(
        db, bob, object_type="file_area", object_id=area.id,
        permissions=BoardPermission.APPROVE, granted_by=bob,
    )
    theirs = upload_file(db, area, bob, "bobs.zip", b"his own")
    approve_file(db, theirs, approved_by=bob)
    mine = upload_file(db, area, alice, "game.zip", b"payload")

    # Describe row 02 (the pending upload), then do it again.
    session = FakeSession(
        editor_keys=[_key("e"), _key("0"), _key("2"), _key("e"), _key("0"), _key("2")],
        lines=["first wording", "", "second wording", ""],
    )

    asyncio.run(_show_area(session, lane, area, alice))

    # The second editor opened *on the saved text* and appended to it,
    # which is what proves the candidate row was refreshed. With the
    # stale row this read "second wording" alone: the editor started
    # from the pre-edit `None` and the first save was overwritten.
    assert get_file(db, mine.file_id).description == "first wording\nsecond wording"
    # And the picker row for it: "no description yet" the first time,
    # the saved wording the second, rather than claiming twice over that
    # the file has none.
    output = session.visible_output
    assert "game.zip - awaiting approval — (no description yet)" in output
    assert "game.zip - awaiting approval — first wording" in output


def test_the_empty_screen_offers_nothing_to_describe_when_nothing_is_waiting(db, lane, alice):
    """And `e` there bells without dropping the caller out of the area —
    the empty line that used to mean "back" is `[B]ack` now.

    The bell is the *whole* response: the action bar is drawn once and a
    refused key leaves the screen exactly as it was, which is the rule
    `file_flow._CHOICE_PROMPT` states. Reprinting the bar per keystroke
    scrolled the empty-state explanation away on a short terminal."""
    area = create_file_area(db, "downloads", creator=alice)
    session = FakeSession(keys=["e", "b"])

    asyncio.run(_show_area(session, lane, area, alice))

    output = session.visible_output
    assert "dit description" not in output
    assert "\a" in "".join(session.written)
    assert output.count("[B]ack") == 1
    assert output.count("Choice: ") == 1
    assert "This file area has no files yet" in output


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


def test_an_approved_file_in_a_moderated_area_offers_its_uploader_no_editor(db, lane, alice, bob):
    """Codex review: the domain refuses this save, so advertising `[E]`
    and opening an editor would take the caller's text only to throw it
    back at them."""
    area = create_file_area(db, "downloads", creator=bob, moderated=True)
    grant_permissions(
        db, bob, object_type="file_area", object_id=area.id,
        permissions=BoardPermission.APPROVE, granted_by=bob,
    )
    entry = upload_file(db, area, alice, "game.zip", b"payload", description="honest")
    from netbbs.files.entries import approve_file

    approve_file(db, entry, approved_by=bob)

    session = FakeSession(editor_keys=[_key("e")], lines=["spam", ""])
    asyncio.run(_show_area(session, lane, area, alice))

    assert "dit description" not in session.visible_output
    assert get_file(db, entry.file_id).description == "honest"


def test_the_cursor_on_an_approved_file_in_a_moderated_area_says_why_not(db, lane, alice, bob, monkeypatch):
    """`test_naming_an_approved_file_in_a_moderated_area_says_why_not`
    lived here: it typed `/describe <filename>` past the on-screen gate
    to reach this refusal. Losing the slash form did not lose the path,
    it moved it — `[E]` is offered because alice has an upload waiting,
    and the cursor is then sitting on an approved file of her own in the
    same moderated area.

    Refused before the editor opens, which is the whole point: the
    domain's own rejection talks about an EDIT permission she never had,
    and this says what actually happened."""
    from netbbs.files import entries as entries_module
    from netbbs.files.entries import approve_file

    timestamps = iter(f"2026-01-01T00:00:0{i}.000000Z" for i in range(3))
    monkeypatch.setattr(entries_module, "utc_now_iso", lambda: next(timestamps))
    area = create_file_area(db, "downloads", creator=bob, moderated=True)
    grant_permissions(
        db, bob, object_type="file_area", object_id=area.id,
        permissions=BoardPermission.APPROVE, granted_by=bob,
    )
    approved = upload_file(db, area, alice, "old.zip", b"payload", description="honest")
    approve_file(db, approved, approved_by=bob)
    upload_file(db, area, alice, "new.zip", b"the next one")

    # DOWN puts the cursor on the listing's only row -- her approved
    # file -- and `e` acts on it. The description text would be typed
    # into an editor that must never open.
    session = FakeSession(editor_keys=[_key_down(), _key("e")], lines=["a new wording", ""])

    asyncio.run(_show_area(session, lane, area, alice))

    output = session.visible_output
    assert "has already been approved in a moderated area" in output
    assert "does not hold EDIT permission" not in output
    assert "Up to 10 lines" not in output  # the editor never opened
    assert get_file(db, approved.file_id).description == "honest"

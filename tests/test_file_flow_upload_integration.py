"""
End-to-end regression test for GitHub issue #34, reopened a second
time: the real `/upload` path (`netbbs.net.file_flow._handle_upload`)
now streams received Zmodem content to a temp file
(`netbbs.files.storage.new_incoming_temp_path`) and moves it into
permanent storage (`netbbs.files.entries.upload_file_from_temp`) rather
than ever holding the complete upload as one in-memory `bytes` object.

`netbbs.net.zmodem`'s own test suite already exercises the protocol
layer directly (`receive_file` against a real sender, in isolation);
this drives the real `_show_area`/`_handle_upload` menu flow on top of
that, the same "menu text, then the session's raw byte stream, on the
same session object" combination a real Telnet/SSH connection actually
has -- proving the whole chain (menu -> real Zmodem handshake -> temp
file -> content-addressed storage -> a queryable FileEntry) works
together, not just each piece in isolation.
"""

from __future__ import annotations

import asyncio
import collections
import io
import re
import zipfile
from pathlib import Path

import pytest

from netbbs.auth.users import create_user
from netbbs.files.areas import create_file_area
from netbbs.files.entries import list_files_page
from netbbs.files.storage import storage_root
from netbbs.net import file_flow
from netbbs.net import zmodem
from netbbs.net.session import Session
from netbbs.storage.database import Database
from netbbs.storage.execution import DatabaseLane


class _BytePipe:
    """Same shape as tests/test_zmodem.py's own -- a real client task
    driving zmodem.send_file against a real server task running
    zmodem.receive_file (via _handle_upload), connected by an in-memory
    duplex byte pipe rather than a socket."""

    def __init__(self):
        self._buffer: collections.deque[int] = collections.deque()
        self._event = asyncio.Event()

    def feed(self, data: bytes) -> None:
        self._buffer.extend(data)
        self._event.set()

    async def read_byte(self) -> int:
        while not self._buffer:
            self._event.clear()
            await self._event.wait()
        return self._buffer.popleft()


class _ServerSession(Session):
    """The NetBBS-side session: `read_line`/`write`/`write_line` drive
    _show_area's ordinary menu text, `read_byte`/`write_raw` (backed by
    a real duplex byte pipe) carry the Zmodem exchange once /upload
    switches the session into raw mode -- both live on the same session
    object, matching a real transport."""

    def __init__(self, lines: list[str], read_pipe: _BytePipe, write_pipe: _BytePipe):
        self._lines = list(lines)
        self._read_pipe = read_pipe
        self._write_pipe = write_pipe
        self.written: list[str] = []
        self.terminal_width = 80
        self.node_display_name = "NetBBS"
        self.terminal_height = 24
        self.peer_address = "203.0.113.5"

    async def write(self, text: str) -> None:
        self.written.append(text)

    async def write_line(self, text: str = "") -> None:
        self.written.append(text + "\n")

    async def read_line(self, echo: bool = True, **kwargs) -> str:
        return self._lines.pop(0) if self._lines else ""

    async def read_key(self, echo: bool = True) -> str:
        raise NotImplementedError

    async def read_editor_key(self):
        raise NotImplementedError

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        return await self._read_pipe.read_byte()

    async def write_raw(self, data: bytes) -> None:
        self._write_pipe.feed(data)


class _ClientSession(Session):
    """The simulated sending terminal's side of the same byte pipes --
    only what zmodem.send_file itself needs."""

    def __init__(self, read_pipe: _BytePipe, write_pipe: _BytePipe):
        self._read_pipe = read_pipe
        self._write_pipe = write_pipe

    async def write(self, text: str) -> None:
        raise NotImplementedError

    async def write_line(self, text: str = "") -> None:
        raise NotImplementedError

    async def read_line(self, echo: bool = True, **kwargs) -> str:
        raise NotImplementedError

    async def read_key(self, echo: bool = True) -> str:
        raise NotImplementedError

    async def read_editor_key(self):
        raise NotImplementedError

    async def close(self) -> None:
        pass

    async def read_byte(self) -> int | None:
        return await self._read_pipe.read_byte()

    async def write_raw(self, data: bytes) -> None:
        self._write_pipe.feed(data)


def _written_text(session: _ServerSession) -> str:
    return "".join(session.written)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _visible_text(session: _ServerSession) -> str:
    return _ANSI_ESCAPE_RE.sub("", _written_text(session))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


@pytest.fixture
def lane(tmp_path):
    # netbbs.net.file_flow is migrated onto the two-lane database
    # execution model (issue #57) -- a second, independent connection
    # to the same file the `db` fixture
    # above opens, matching real node startup.
    database_lane = DatabaseLane(tmp_path / "node.db")
    yield database_lane
    database_lane.close()


@pytest.fixture
def alice(db):
    return create_user(db, "alice", password="hunter2", user_level=10)


def test_upload_via_show_area_streams_to_storage_with_no_leftover_temp_file(db, lane, alice):
    area = create_file_area(db, "docs", creator=alice)
    payload = b"hello from a real zmodem upload" * 100  # spans multiple subpackets

    client_to_server, server_to_client = _BytePipe(), _BytePipe()
    server_session = _ServerSession(["/upload"], read_pipe=client_to_server, write_pipe=server_to_client)
    client_session = _ClientSession(read_pipe=server_to_client, write_pipe=client_to_server)

    async def scenario():
        server_task = asyncio.create_task(file_flow._show_area(server_session, lane, area, alice))
        client_task = asyncio.create_task(zmodem.send_file(client_session, "upload.bin", payload))
        await asyncio.wait_for(server_task, timeout=5)
        try:
            await asyncio.wait_for(client_task, timeout=1)
        except Exception:
            pass  # the client side tearing down after the transfer completes isn't under test here

    asyncio.run(scenario())

    assert "Uploaded" in _written_text(server_session)
    assert "NetBBS › Files › docs › Upload" in _visible_text(server_session)
    page = list_files_page(db, area, alice)
    assert len(page.entries) == 1
    entry = page.entries[0]
    assert entry.filename == "upload.bin"
    assert entry.size_bytes == len(payload)
    assert entry.storage_path
    assert Path(entry.storage_path).read_bytes() == payload

    # GitHub issue #34's actual point: nothing left behind in staging.
    incoming_dir = storage_root(db) / ".incoming"
    assert not incoming_dir.exists() or list(incoming_dir.iterdir()) == []


def test_upload_exceeding_the_node_limit_leaves_no_temp_file_and_no_entry(db, lane, alice, monkeypatch):
    monkeypatch.setattr(file_flow, "get_max_upload_bytes", lambda db: 10)

    area = create_file_area(db, "docs", creator=alice)
    payload = b"x" * 1000

    client_to_server, server_to_client = _BytePipe(), _BytePipe()
    server_session = _ServerSession(["/upload"], read_pipe=client_to_server, write_pipe=server_to_client)
    client_session = _ClientSession(read_pipe=server_to_client, write_pipe=client_to_server)

    async def scenario():
        server_task = asyncio.create_task(file_flow._show_area(server_session, lane, area, alice))
        client_task = asyncio.create_task(zmodem.send_file(client_session, "toobig.bin", payload))
        await asyncio.wait_for(server_task, timeout=5)
        try:
            await asyncio.wait_for(client_task, timeout=1)
        except Exception:
            pass

    asyncio.run(scenario())

    assert "Upload failed" in _written_text(server_session)
    assert list_files_page(db, area, alice).entries == []
    incoming_dir = storage_root(db) / ".incoming"
    assert not incoming_dir.exists() or list(incoming_dir.iterdir()) == []


def test_uploading_a_zip_with_file_id_diz_describes_it(db, lane, alice):
    """Issue #463 end to end: the archive's own catalogue text becomes
    the file's description, over the real Zmodem path, with no
    intervention from whoever uploaded it."""
    area = create_file_area(db, "docs", creator=alice)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("GAME.EXE", b"MZ" + b"\x00" * 500)
        # CP437, the encoding a DIZ from the era is actually written in.
        archive.writestr("FILE_ID.DIZ", "\u2554\u2550\u2557\r\nCool Game v1.0\r\nBy Someone\r\n".encode("cp437"))
    payload = buffer.getvalue()

    client_to_server, server_to_client = _BytePipe(), _BytePipe()
    server_session = _ServerSession(["/upload"], read_pipe=client_to_server, write_pipe=server_to_client)
    client_session = _ClientSession(read_pipe=server_to_client, write_pipe=client_to_server)

    async def scenario():
        server_task = asyncio.create_task(file_flow._show_area(server_session, lane, area, alice))
        client_task = asyncio.create_task(zmodem.send_file(client_session, "game.zip", payload))
        await asyncio.wait_for(server_task, timeout=5)
        try:
            await asyncio.wait_for(client_task, timeout=1)
        except Exception:
            pass

    asyncio.run(scenario())

    entry = list_files_page(db, area, alice).entries[0]
    assert entry.description == "\u2554\u2550\u2557\nCool Game v1.0\nBy Someone"
    assert "Description read from FILE_ID.DIZ" in _visible_text(server_session)
    # And the bytes stored are still the archive, untouched by reading it.
    assert Path(entry.storage_path).read_bytes() == payload


def test_uploading_a_file_with_no_diz_says_so_and_points_at_the_editor(db, lane, alice):
    area = create_file_area(db, "docs", creator=alice)
    payload = b"just a text file, no archive at all"

    client_to_server, server_to_client = _BytePipe(), _BytePipe()
    server_session = _ServerSession(["/upload"], read_pipe=client_to_server, write_pipe=server_to_client)
    client_session = _ClientSession(read_pipe=server_to_client, write_pipe=client_to_server)

    async def scenario():
        server_task = asyncio.create_task(file_flow._show_area(server_session, lane, area, alice))
        client_task = asyncio.create_task(zmodem.send_file(client_session, "notes.txt", payload))
        await asyncio.wait_for(server_task, timeout=5)
        try:
            await asyncio.wait_for(client_task, timeout=1)
        except Exception:
            pass

    asyncio.run(scenario())

    assert list_files_page(db, area, alice).entries[0].description is None
    assert "No description was read from it" in _visible_text(server_session)


def _link_context():
    from netbbs.link.boards import LinkContext
    from netbbs.link.node_identity import bootstrap_node_identity
    from netbbs.link.protocol import LinkNode

    node_identity = bootstrap_node_identity("roanoke")
    return LinkContext(node_identity=node_identity, link_node=LinkNode(identity=node_identity))


def _upload(db, lane, area, user, filename, payload, *, link_context=None):
    client_to_server, server_to_client = _BytePipe(), _BytePipe()
    server_session = _ServerSession(["/upload"], read_pipe=client_to_server, write_pipe=server_to_client)
    client_session = _ClientSession(read_pipe=server_to_client, write_pipe=client_to_server)

    async def scenario():
        server_task = asyncio.create_task(
            file_flow._show_area(server_session, lane, area, user, link_context=link_context)
        )
        client_task = asyncio.create_task(zmodem.send_file(client_session, filename, payload))
        await asyncio.wait_for(server_task, timeout=5)
        try:
            await asyncio.wait_for(client_task, timeout=1)
        except Exception:
            pass

    asyncio.run(scenario())
    return server_session


def test_uploading_into_a_linked_area_queues_its_link_descriptor(db, lane, alice):
    """Issue #464: nothing in the running BBS ever called
    `queue_file_descriptor_if_linked`, so a Linked file area never
    announced its own uploads — only the test suite, calling it by hand,
    ever exercised §11.2 at all.

    Asserted against `load_own_file_area_events`, which is exactly what
    `netbbs.link.sync` pushes to peers, rather than only against the
    column it reads: the column being set is not the point, being in
    that list is."""
    from netbbs.link.files import link_file_area, load_own_file_area_events

    link_context = _link_context()
    area = create_file_area(db, "docs", creator=alice)
    link_file_area(db, area, node_identity=link_context.node_identity)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("FILE_ID.DIZ", b"Cool Game v1.0\r\nBy Someone\r\n")
    _upload(db, lane, area, alice, "game.zip", buffer.getvalue(), link_context=link_context)

    entry = list_files_page(db, area, alice).entries[0]
    descriptors = [
        event for event in load_own_file_area_events(db, link_context.node_identity.fingerprint)
        if event.payload.get("file_id") == entry.file_id
    ]
    assert len(descriptors) == 1
    # And it carries the FILE_ID.DIZ description (issue #463), which is
    # only true because extraction happens before the row is written.
    assert descriptors[0].payload["description"] == "Cool Game v1.0\nBy Someone"
    assert descriptors[0].payload["sha256"] == entry.sha256


def test_uploading_into_an_unlinked_area_queues_nothing(db, lane, alice):
    from netbbs.link.files import load_own_file_area_events

    link_context = _link_context()
    area = create_file_area(db, "docs", creator=alice)

    _upload(db, lane, area, alice, "notes.txt", b"hello", link_context=link_context)

    assert load_own_file_area_events(db, link_context.node_identity.fingerprint) == []


def test_a_pending_upload_into_a_moderated_linked_area_is_not_announced(db, lane, alice):
    """The moderation queue must never leak onto the network (design
    doc §9.2/§11.2): a pending upload is queued by the approval screen,
    not by the upload itself."""
    from netbbs.link.files import link_file_area, load_own_file_area_events

    link_context = _link_context()
    area = create_file_area(db, "docs", creator=alice, moderated=True)
    link_file_area(db, area, node_identity=link_context.node_identity)

    _upload(db, lane, area, alice, "game.zip", b"hello", link_context=link_context)

    entry = db.connection.execute("SELECT status, link_event_json FROM files").fetchone()
    assert entry["status"] == "pending"
    assert entry["link_event_json"] is None
    # The area's own genesis is there; no descriptor alongside it.
    events = load_own_file_area_events(db, link_context.node_identity.fingerprint)
    assert [event.payload.get("file_id") for event in events] == [None]


def test_uploading_into_a_carried_area_announces_nothing(db, lane, alice):
    """Codex review of issue #464: a carried area is writable locally,
    but peers verify a `file_descriptor` against the area's own genesis
    origin — signing one here would be permanently unverifiable
    everywhere it went."""
    from netbbs.link.events import build_file_area_genesis
    from netbbs.link.files import load_own_file_area_events, materialize_carried_file_area
    from netbbs.link.node_identity import bootstrap_node_identity
    from netbbs.files.areas import get_file_area_by_name

    link_context = _link_context()
    peer_identity = bootstrap_node_identity("faraway")
    genesis = build_file_area_genesis(
        signing_identity=peer_identity.signing_key,
        origin_fingerprint=peer_identity.fingerprint,
        area_id="carried-area-id",
        name="theirs",
        created_at="2026-01-01T00:00:00Z",
    )
    materialize_carried_file_area(db, genesis)
    area = get_file_area_by_name(db, "theirs")

    _upload(db, lane, area, alice, "game.zip", b"hello", link_context=link_context)

    entry = list_files_page(db, area, alice).entries[0]
    assert entry.filename == "game.zip"  # the upload itself still worked
    assert db.connection.execute(
        "SELECT link_event_json FROM files WHERE file_id = ?", (entry.file_id,)
    ).fetchone()["link_event_json"] is None
    assert load_own_file_area_events(db, link_context.node_identity.fingerprint) == []

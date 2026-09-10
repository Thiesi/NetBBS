"""
The HTTP transfer endpoints (issue #475), driven over a real loopback
listener with a real aiohttp client -- the project's "use real
boundaries" rule (AGENTS.md), and the only way to test what actually
matters here: streaming, size limits, content headers, and the status a
refused transfer returns.

A caller reaching these has a link the BBS printed (or a browser page
the BBS served). What they must *not* be able to do is use it twice,
use it after it expires, use it for a file they could not otherwise
read, or use it to put more bytes on this node than the SysOp allows.
"""

from __future__ import annotations

import asyncio
import io
import zipfile

import pytest

aiohttp = pytest.importorskip("aiohttp")

from netbbs.auth.users import create_user  # noqa: E402
from netbbs.config import set_max_upload_bytes  # noqa: E402
from netbbs.files.areas import create_file_area  # noqa: E402
from netbbs.files.entries import list_files_page, upload_file  # noqa: E402
from netbbs.net.file_transfer import (  # noqa: E402
    DOWNLOAD,
    UPLOAD,
    TransferGateway,
    TransferGrants,
)
from netbbs.net.web import WebServer  # noqa: E402
from netbbs.storage.database import Database  # noqa: E402
from netbbs.storage.execution import DatabaseLane  # noqa: E402


class _Node:
    """A node with just enough of itself to serve transfers: a database,
    a lane, the grant table, and a real listener on an ephemeral port."""

    def __init__(self, tmp_path):
        self.db = Database(tmp_path / "node.db")
        self.lane = DatabaseLane(tmp_path / "node.db")
        self.grants = TransferGrants()
        self.server = WebServer(
            host="127.0.0.1", port=0, session_handler=self._no_sessions,
            transfers=TransferGateway(self.grants, self.lane),
        )

    async def _no_sessions(self, session):  # pragma: no cover - never reached
        raise AssertionError("these tests do not open terminal sessions")

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.port}"

    async def __aenter__(self):
        await self.server.start()
        return self

    async def __aexit__(self, *exc):
        # The listener and the lane go; the plain `db` handle stays open
        # so a test can look at what the node actually stored.
        await self.server.stop()
        self.lane.close()

    def close(self):
        self.db.close()


def _run(scenario):
    return asyncio.run(scenario())


@pytest.fixture
def node(tmp_path):
    running = _Node(tmp_path)
    yield running
    running.close()


# -- download -----------------------------------------------------------


def test_a_download_link_serves_the_stored_bytes(node):
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)
    payload = b"the actual bytes" * 1000
    entry = upload_file(node.db, area, alice, "game.zip", payload)

    async def scenario():
        async with node:
            grant = node.grants.issue(
                direction=DOWNLOAD, user_id=alice.id, area_id=area.id, file_id=entry.file_id
            )
            async with aiohttp.ClientSession() as client:
                async with client.get(f"{node.base}/transfer/{grant.token}") as response:
                    assert response.status == 200
                    assert "game.zip" in response.headers["Content-Disposition"]
                    return await response.read()

    assert _run(scenario) == payload


def test_a_download_link_works_only_once(node):
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)
    entry = upload_file(node.db, area, alice, "game.zip", b"payload")

    async def scenario():
        async with node:
            grant = node.grants.issue(
                direction=DOWNLOAD, user_id=alice.id, area_id=area.id, file_id=entry.file_id
            )
            url = f"{node.base}/transfer/{grant.token}"
            async with aiohttp.ClientSession() as client:
                async with client.get(url) as first:
                    assert first.status == 200
                    await first.read()
                async with client.get(url) as second:
                    return second.status

    assert _run(scenario) == 404


def test_an_unknown_token_is_a_plain_not_found(node):
    async def scenario():
        async with node:
            async with aiohttp.ClientSession() as client:
                async with client.get(f"{node.base}/transfer/not-a-real-token") as response:
                    return response.status, await response.text()

    status, body = _run(scenario)
    assert status == 404
    # The same answer an expired or already-used token gets: nothing in
    # the wording distinguishes "never existed" from "gone".
    assert "not valid" in body


def test_a_non_ascii_filename_survives_the_content_disposition(node):
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)
    entry = upload_file(node.db, area, alice, "grüße.zip", b"payload")

    async def scenario():
        async with node:
            grant = node.grants.issue(
                direction=DOWNLOAD, user_id=alice.id, area_id=area.id, file_id=entry.file_id
            )
            async with aiohttp.ClientSession() as client:
                async with client.get(f"{node.base}/transfer/{grant.token}") as response:
                    return response.headers["Content-Disposition"]

    disposition = _run(scenario)
    assert "filename*=UTF-8''" in disposition
    assert "gr%C3%BC%C3%9Fe.zip" in disposition


def test_a_download_grant_refused_at_redemption_says_so(node):
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)
    entry = upload_file(node.db, area, alice, "game.zip", b"payload")

    async def scenario():
        async with node:
            grant = node.grants.issue(
                direction=DOWNLOAD, user_id=alice.id, area_id=area.id, file_id=entry.file_id
            )
            node.db.connection.execute(
                "UPDATE file_areas SET min_read_level = 250 WHERE id = ?", (area.id,)
            )
            node.db.connection.commit()
            async with aiohttp.ClientSession() as client:
                async with client.get(f"{node.base}/transfer/{grant.token}") as response:
                    return response.status, await response.text()

    status, body = _run(scenario)
    assert status == 403
    assert "may no longer read" in body


# -- upload -------------------------------------------------------------


def _archive_with_diz() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("GAME.EXE", b"MZ")
        archive.writestr("FILE_ID.DIZ", b"Cool Game v1.0\r\nBy Someone\r\n")
    return buffer.getvalue()


def test_an_upload_link_stores_the_file_and_reads_its_diz(node):
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)
    payload = _archive_with_diz()

    async def scenario():
        async with node:
            grant = node.grants.issue(direction=UPLOAD, user_id=alice.id, area_id=area.id)
            form = aiohttp.FormData()
            form.add_field("file", payload, filename="game.zip")
            async with aiohttp.ClientSession() as client:
                async with client.post(f"{node.base}/transfer/{grant.token}", data=form) as response:
                    return response.status, await response.json()

    status, body = _run(scenario)
    assert status == 200
    assert body["filename"] == "game.zip"
    assert body["size_bytes"] == len(payload)
    # The same intake as a Zmodem upload, so FILE_ID.DIZ (#463) applies.
    assert body["description"] == "Cool Game v1.0\nBy Someone"

    entry = list_files_page(node.db, area, alice).entries[0]
    assert entry.filename == "game.zip"
    assert entry.sha256


def test_a_raw_body_upload_names_the_file_from_the_query(node):
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)

    async def scenario():
        async with node:
            grant = node.grants.issue(direction=UPLOAD, user_id=alice.id, area_id=area.id)
            async with aiohttp.ClientSession() as client:
                url = f"{node.base}/transfer/{grant.token}?filename=notes.txt"
                async with client.post(url, data=b"plain bytes") as response:
                    return response.status, await response.json()

    status, body = _run(scenario)
    assert status == 200
    assert body["filename"] == "notes.txt"
    assert list_files_page(node.db, area, alice).entries[0].filename == "notes.txt"


def test_an_upload_over_the_node_limit_is_refused_and_stores_nothing(node):
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)
    set_max_upload_bytes(node.db, 64)

    async def scenario():
        async with node:
            grant = node.grants.issue(direction=UPLOAD, user_id=alice.id, area_id=area.id)
            form = aiohttp.FormData()
            form.add_field("file", b"x" * 4096, filename="big.bin")
            async with aiohttp.ClientSession() as client:
                async with client.post(f"{node.base}/transfer/{grant.token}", data=form) as response:
                    return response.status

    assert _run(scenario) == 413
    assert list_files_page(node.db, area, alice).entries == []


def test_an_upload_link_cannot_be_used_to_download(node):
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)

    async def scenario():
        async with node:
            grant = node.grants.issue(direction=UPLOAD, user_id=alice.id, area_id=area.id)
            async with aiohttp.ClientSession() as client:
                async with client.get(f"{node.base}/transfer/{grant.token}") as response:
                    return response.status

    assert _run(scenario) == 405


def test_a_download_link_cannot_be_used_to_upload(node):
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)
    entry = upload_file(node.db, area, alice, "game.zip", b"payload")

    async def scenario():
        async with node:
            grant = node.grants.issue(
                direction=DOWNLOAD, user_id=alice.id, area_id=area.id, file_id=entry.file_id
            )
            async with aiohttp.ClientSession() as client:
                async with client.post(f"{node.base}/transfer/{grant.token}", data=b"nope") as response:
                    return response.status

    assert _run(scenario) == 405


def test_an_upload_into_a_moderated_area_lands_pending(node):
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    sysop = create_user(node.db, "sysop", password="hunter2", user_level=255)
    area = create_file_area(node.db, "docs", creator=sysop, moderated=True)

    async def scenario():
        async with node:
            grant = node.grants.issue(direction=UPLOAD, user_id=alice.id, area_id=area.id)
            form = aiohttp.FormData()
            form.add_field("file", b"payload", filename="game.zip")
            async with aiohttp.ClientSession() as client:
                async with client.post(f"{node.base}/transfer/{grant.token}", data=form) as response:
                    return await response.json()

    assert _run(scenario)["status"] == "pending"


def test_an_empty_upload_is_refused(node):
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)

    async def scenario():
        async with node:
            grant = node.grants.issue(direction=UPLOAD, user_id=alice.id, area_id=area.id)
            async with aiohttp.ClientSession() as client:
                async with client.post(f"{node.base}/transfer/{grant.token}", data=b"") as response:
                    return response.status

    assert _run(scenario) == 400
    assert list_files_page(node.db, area, alice).entries == []


def test_a_node_without_a_gateway_serves_no_transfer_routes(tmp_path):
    """A node that has not been given a transfer service must not have
    the endpoint at all -- not an endpoint that refuses, which would
    still be a surface."""

    async def scenario():
        server = WebServer(host="127.0.0.1", port=0, session_handler=lambda session: None)
        await server.start()
        try:
            async with aiohttp.ClientSession() as client:
                async with client.get(f"http://127.0.0.1:{server.port}/transfer/anything") as response:
                    return response.status
        finally:
            await server.stop()

    assert _run(scenario) == 404

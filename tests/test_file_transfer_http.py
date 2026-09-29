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
            grant = node.grants.issue(direction=DOWNLOAD, user=alice, area=area, file_id=entry.file_id)
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
            grant = node.grants.issue(direction=DOWNLOAD, user=alice, area=area, file_id=entry.file_id)
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
            grant = node.grants.issue(direction=DOWNLOAD, user=alice, area=area, file_id=entry.file_id)
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
            grant = node.grants.issue(direction=DOWNLOAD, user=alice, area=area, file_id=entry.file_id)
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
            grant = node.grants.issue(direction=UPLOAD, user=alice, area=area)
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
            grant = node.grants.issue(direction=UPLOAD, user=alice, area=area)
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
            grant = node.grants.issue(direction=UPLOAD, user=alice, area=area)
            form = aiohttp.FormData()
            form.add_field("file", b"x" * 4096, filename="big.bin")
            async with aiohttp.ClientSession() as client:
                async with client.post(f"{node.base}/transfer/{grant.token}", data=form) as response:
                    return response.status

    assert _run(scenario) == 413
    assert list_files_page(node.db, area, alice).entries == []


def test_opening_an_upload_link_serves_a_form_without_spending_it(node):
    """The printed instruction is "open this in a browser to upload",
    and a browser opens things with GET. Redeeming there would consume
    the caller's one use before they had chosen a file -- which made the
    advertised flow impossible to follow."""
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)

    async def scenario():
        async with node:
            grant = node.grants.issue(direction=UPLOAD, user=alice, area=area)
            url = f"{node.base}/transfer/{grant.token}"
            async with aiohttp.ClientSession() as client:
                async with client.get(url) as page:
                    body = await page.text()
                    status = page.status
                # ... and the grant is still there to be used.
                form = aiohttp.FormData()
                form.add_field("file", b"payload", filename="game.zip")
                async with client.post(url, data=form) as upload:
                    return status, body, upload.status

    status, body, upload_status = _run(scenario)
    assert status == 200
    assert 'type="file"' in body
    assert upload_status == 200
    assert list_files_page(node.db, area, alice).entries[0].filename == "game.zip"


def test_a_head_probe_does_not_spend_a_grant(node):
    """A link scanner, proxy or download manager probing the URL must
    not burn the caller's one use."""
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)
    entry = upload_file(node.db, area, alice, "game.zip", b"payload")

    async def scenario():
        async with node:
            grant = node.grants.issue(direction=DOWNLOAD, user=alice, area=area, file_id=entry.file_id)
            url = f"{node.base}/transfer/{grant.token}"
            async with aiohttp.ClientSession() as client:
                async with client.head(url) as probe:
                    probed = probe.status
                async with client.get(url) as response:
                    return probed, response.status, await response.read()

    probed, status, body = _run(scenario)
    assert probed == 200
    assert status == 200
    assert body == b"payload"


# -- HEAD says what a GET would do (issue #511) ---------------------------


def _head(node, url_for):
    async def scenario():
        async with node:
            url = url_for()
            async with aiohttp.ClientSession() as client:
                async with client.head(url) as probe:
                    return probe.status, probe.headers.get("X-NetBBS-Transfer-Message"), dict(probe.headers)

    return _run(scenario)


def test_a_head_probe_of_an_unknown_link_says_so(node):
    status, message, _ = _head(node, lambda: f"{node.base}/transfer/not-a-real-token")
    assert status == 404
    assert "not valid" in message


def test_a_head_probe_reports_a_refusal_without_spending_the_link(node):
    """The browser saves a refused GET's body under the filename; the probe
    is how the page finds out first. It must also leave the link alone, so
    the answer is the same however many times it is asked."""
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)
    entry = upload_file(node.db, area, alice, "game.zip", b"payload")
    node.db.connection.execute("UPDATE file_areas SET min_read_level = 50 WHERE id = ?", (area.id,))
    node.db.connection.commit()

    async def scenario():
        async with node:
            grant = node.grants.issue(direction=DOWNLOAD, user=alice, area=area, file_id=entry.file_id)
            url = f"{node.base}/transfer/{grant.token}"
            async with aiohttp.ClientSession() as client:
                answers = []
                for _ in range(2):
                    async with client.head(url) as probe:
                        answers.append((probe.status, probe.headers.get("X-NetBBS-Transfer-Message")))
                return answers, node.grants.peek(grant.token) is not None

    answers, still_there = _run(scenario)
    assert answers[0] == answers[1]
    assert answers[0][0] == 403
    assert "no longer read" in answers[0][1]
    assert still_there


def test_a_head_probe_of_a_good_download_carries_its_headers(node):
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)
    entry = upload_file(node.db, area, alice, "game.zip", b"payload")

    def url():
        grant = node.grants.issue(direction=DOWNLOAD, user=alice, area=area, file_id=entry.file_id)
        return f"{node.base}/transfer/{grant.token}"

    status, message, headers = _head(node, url)
    assert status == 200
    assert message is None
    assert headers["Content-Length"] == str(len(b"payload"))
    assert "game.zip" in headers["Content-Disposition"]


def test_a_head_probe_reports_a_busy_node(tmp_path):
    running = _Node(tmp_path)
    running.server._transfers._max_concurrent_uploads = 0  # every slot already taken
    alice = create_user(running.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(running.db, "docs", creator=alice)
    entry = upload_file(running.db, area, alice, "game.zip", b"payload")

    def url():
        grant = running.grants.issue(direction=DOWNLOAD, user=alice, area=area, file_id=entry.file_id)
        return f"{running.base}/transfer/{grant.token}"

    try:
        status, message, _ = _head(running, url)
    finally:
        running.close()
    assert status == 429
    assert "busy" in message


def test_a_download_link_cannot_be_used_to_upload(node):
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)
    entry = upload_file(node.db, area, alice, "game.zip", b"payload")

    async def scenario():
        async with node:
            grant = node.grants.issue(direction=DOWNLOAD, user=alice, area=area, file_id=entry.file_id)
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
            grant = node.grants.issue(direction=UPLOAD, user=alice, area=area)
            form = aiohttp.FormData()
            form.add_field("file", b"payload", filename="game.zip")
            async with aiohttp.ClientSession() as client:
                async with client.post(f"{node.base}/transfer/{grant.token}", data=form) as response:
                    return await response.json()

    assert _run(scenario)["status"] == "pending"


# -- multipart request accounting (issue #511) ---------------------------
#
# `max_upload_bytes` bounds the *file*; the request as a whole gets that
# plus MULTIPART_FRAMING_ALLOWANCE. Built by hand rather than with
# `aiohttp.FormData`, because the shapes that matter here -- a preamble,
# padding parts, a part after the file -- are ones no well-behaved client
# sends.

_BOUNDARY = "netbbs-test-boundary"


def _multipart(*parts: tuple[str, str | None, bytes], preamble: bytes = b"") -> bytes:
    body = preamble
    for name, filename, content in parts:
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        body += (
            f"--{_BOUNDARY}\r\nContent-Disposition: {disposition}\r\n\r\n".encode()
            + content + b"\r\n"
        )
    return body + f"--{_BOUNDARY}--\r\n".encode()


def _post_multipart(node, alice, area, body: bytes):
    async def scenario():
        async with node:
            grant = node.grants.issue(direction=UPLOAD, user=alice, area=area)
            headers = {"Content-Type": f"multipart/form-data; boundary={_BOUNDARY}"}
            async with aiohttp.ClientSession() as client:
                async with client.post(f"{node.base}/transfer/{grant.token}", data=body, headers=headers) as response:
                    return response.status

    return _run(scenario)


def test_a_multipart_preamble_counts_against_the_request(node):
    """`reader.next()` consumes the preamble before the first boundary,
    in a loop with no limit of its own -- before this, 200 KB of it was
    read and the upload answered 200."""
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)
    set_max_upload_bytes(node.db, 64)
    preamble = (b"x" * 1000 + b"\r\n") * 200

    body = _multipart(("file", "tiny.txt", b"hello"), preamble=preamble)
    assert _post_multipart(node, alice, area, body) == 413
    assert list_files_page(node.db, area, alice).entries == []


def test_parts_skipped_before_the_file_count_against_the_request(node):
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)
    set_max_upload_bytes(node.db, 64)

    body = _multipart(("padding", None, b"y" * 200_000), ("file", "tiny.txt", b"hello"))
    assert _post_multipart(node, alice, area, body) == 413
    assert list_files_page(node.db, area, alice).entries == []


def test_a_file_at_the_limit_is_accepted_despite_its_framing(node):
    """`max_upload_bytes` means file bytes, as it does over Zmodem: a
    browser's boundaries and part headers must not push a file that fits
    over the line."""
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)
    set_max_upload_bytes(node.db, 4096)

    body = _multipart(("comment", None, b"a small field"), ("file", "exact.bin", b"z" * 4096))
    assert _post_multipart(node, alice, area, body) == 200
    assert list_files_page(node.db, area, alice).entries[0].size_bytes == 4096


def test_an_empty_file_part_is_refused_however_it_is_padded(node):
    """An earlier attempt let padding bytes into the counter the empty-file
    check reads, and stored a zero-byte file."""
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)

    body = _multipart(("padding", None, b"p" * 10_000), ("file", "empty.txt", b""))
    assert _post_multipart(node, alice, area, body) == 400
    assert list_files_page(node.db, area, alice).entries == []


def test_a_part_after_the_file_counts_against_the_request(node):
    """Left unread, a trailing part is read and discarded by aiohttp after
    the handler returns, for up to its lingering time and outside the
    transfer slot, deadline and bound -- so it is drained, counted, here."""
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)
    set_max_upload_bytes(node.db, 64)

    body = _multipart(("file", "tiny.txt", b"hello"), ("trailer", None, b"t" * 200_000))
    assert _post_multipart(node, alice, area, body) == 413
    assert list_files_page(node.db, area, alice).entries == []


def test_a_small_trailing_part_is_harmless(node):
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)
    set_max_upload_bytes(node.db, 64)

    body = _multipart(("file", "tiny.txt", b"hello"), ("note", None, b"thanks"))
    assert _post_multipart(node, alice, area, body) == 200
    assert list_files_page(node.db, area, alice).entries[0].filename == "tiny.txt"


def test_one_overlong_preamble_line_is_refused_as_too_large(node):
    """One preamble line longer than the request stream's high-water mark
    (512 KiB here) makes aiohttp raise before the counter sees the line;
    that is a too-large request, not a 400 or 500. The file limit is set
    high so that the per-line ceiling, not the request bound, is what
    trips."""
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)
    set_max_upload_bytes(node.db, 2_000_000)

    body = _multipart(("file", "tiny.txt", b"hello"), preamble=b"x" * 700_000 + b"\r\n")
    assert _post_multipart(node, alice, area, body) == 413
    assert list_files_page(node.db, area, alice).entries == []


def test_an_empty_upload_is_refused(node):
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)

    async def scenario():
        async with node:
            grant = node.grants.issue(direction=UPLOAD, user=alice, area=area)
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


def test_concurrent_uploads_are_bounded(node):
    """Codex review: redeeming frees the grant slot before a byte of the
    body arrives, so the outstanding-grant ceiling does not bound work
    in flight. Without a second limit a caller can mint, POST, mint,
    POST and hold arbitrarily many long handlers and staging files."""
    from netbbs.net.file_transfer import TransferGateway

    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)
    node.server._transfers = TransferGateway(node.grants, node.lane, max_concurrent_uploads=1)

    async def scenario():
        async with node:
            first = node.grants.issue(direction=UPLOAD, user=alice, area=area)
            second = node.grants.issue(direction=UPLOAD, user=alice, area=area)

            async with aiohttp.ClientSession() as client:
                # A body the server will wait on, so the first upload is
                # genuinely still in flight when the second arrives.
                async def slow_body():
                    yield b"x" * 16
                    await asyncio.sleep(0.4)
                    yield b"y" * 16

                held = asyncio.create_task(
                    client.post(
                        f"{node.base}/transfer/{first.token}?filename=slow.bin", data=slow_body()
                    ).__aenter__()
                )
                await asyncio.sleep(0.1)
                async with client.post(
                    f"{node.base}/transfer/{second.token}?filename=quick.bin", data=b"payload"
                ) as refused:
                    status = refused.status
                response = await held
                await response.release()
                return status

    assert _run(scenario) == 429


def test_a_download_response_is_not_cacheable(node):
    """A single-use URL a browser or shared proxy can replay from cache
    is not single-use (Codex review)."""
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)
    entry = upload_file(node.db, area, alice, "game.zip", b"payload")

    async def scenario():
        async with node:
            grant = node.grants.issue(direction=DOWNLOAD, user=alice, area=area, file_id=entry.file_id)
            async with aiohttp.ClientSession() as client:
                async with client.get(f"{node.base}/transfer/{grant.token}") as response:
                    return response.headers.get("Cache-Control", "")

    assert "no-store" in _run(scenario)
def test_an_upload_survives_a_failed_link_announcement(tmp_path, monkeypatch):
    """Codex review of #482, unaddressed at merge. `upload_file_from_temp`
    has already moved the bytes and committed the row by the time a
    descriptor is signed, so a failure there leaves a genuinely stored
    file. Reporting "could not be stored" invited the caller to upload a
    duplicate of something that already existed.

    The gateway needs a real announce identity for this to mean
    anything: without one the descriptor call is skipped entirely and
    the test would pass against the unfixed code."""
    import netbbs.net.file_transfer as transfer_module
    from netbbs.link.node_identity import bootstrap_node_identity

    running = _Node(tmp_path)
    running.server = WebServer(
        host="127.0.0.1", port=0, session_handler=running._no_sessions,
        transfers=TransferGateway(
            running.grants, running.lane,
            announce_identity=bootstrap_node_identity("thisnode"),
        ),
    )
    try:
        alice = create_user(running.db, "alice", password="hunter2", user_level=10)
        area = create_file_area(running.db, "docs", creator=alice)

        called = []

        def exploding_queue(*args, **kwargs):
            called.append(True)
            raise RuntimeError("signing is unavailable")

        monkeypatch.setattr(transfer_module, "queue_file_descriptor_if_linked", exploding_queue)

        async def scenario():
            async with running:
                grant = running.grants.issue(direction=UPLOAD, user=alice, area=area)
                form = aiohttp.FormData()
                form.add_field("file", b"contents", filename="notes.txt")
                async with aiohttp.ClientSession() as client:
                    async with client.post(
                        f"{running.base}/transfer/{grant.token}", data=form
                    ) as response:
                        return response.status

        status = _run(scenario)
        # The path under test was actually reached.
        assert called, "the descriptor call was never made; the test proves nothing"
        # The announcement failed; the upload did not.
        assert status == 200
        assert [e.filename for e in list_files_page(running.db, area, alice).entries] == ["notes.txt"]
    finally:
        running.close()


def test_a_failed_rollback_does_not_reverse_a_stored_upload(tmp_path, monkeypatch):
    """Codex review of #508: the rollback added to keep the lane's shared
    connection usable was itself unguarded, so a connection broken badly
    enough to fail the descriptor write would fail the rollback too and
    raise straight out of `_store_upload` -- turning a best-effort
    announcement back into the failed upload the catch exists to prevent.

    The connection is closed by the stub rather than mocked, so the
    rollback raises the real `sqlite3.ProgrammingError` this guards."""
    import netbbs.net.file_transfer as transfer_module
    from netbbs.link.node_identity import bootstrap_node_identity

    running = _Node(tmp_path)
    running.server = WebServer(
        host="127.0.0.1", port=0, session_handler=running._no_sessions,
        transfers=TransferGateway(
            running.grants, running.lane,
            announce_identity=bootstrap_node_identity("thisnode"),
        ),
    )
    try:
        alice = create_user(running.db, "alice", password="hunter2", user_level=10)
        area = create_file_area(running.db, "docs", creator=alice)

        rolled_back = []

        def exploding_queue(db, *args, **kwargs):
            # Break the connection the caller will try to roll back, the
            # way an I/O failure would.
            db.connection.close()
            rolled_back.append(True)
            raise RuntimeError("signing is unavailable")

        monkeypatch.setattr(transfer_module, "queue_file_descriptor_if_linked", exploding_queue)

        async def scenario():
            async with running:
                grant = running.grants.issue(direction=UPLOAD, user=alice, area=area)
                form = aiohttp.FormData()
                form.add_field("file", b"contents", filename="notes.txt")
                async with aiohttp.ClientSession() as client:
                    async with client.post(
                        f"{running.base}/transfer/{grant.token}", data=form
                    ) as response:
                        return response.status

        status = _run(scenario)
        assert rolled_back, "the descriptor call was never made; the test proves nothing"
        # The upload succeeded and stays succeeded, rollback failure and all.
        assert status == 200
        # Read back on a second connection: the lane's is deliberately shut.
        assert [e.filename for e in list_files_page(running.db, area, alice).entries] == ["notes.txt"]
    finally:
        running.close()


def test_head_probes_have_their_own_bound(node, monkeypatch):
    """A probe holds no transfer slot, so it gets a ceiling of its own on
    work queued against the database; past it, a busy answer."""
    from netbbs.net import file_transfer

    monkeypatch.setattr(file_transfer, "MAX_CONCURRENT_PROBES", 0)
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)
    entry = upload_file(node.db, area, alice, "game.zip", b"payload")

    def url():
        grant = node.grants.issue(direction=DOWNLOAD, user=alice, area=area, file_id=entry.file_id)
        return f"{node.base}/transfer/{grant.token}"

    status, message, _ = _head(node, url)
    assert status == 429
    assert "busy" in message


# -- what an upload answers, and whom it tells (issue #842) ---------------


def _post_upload(node, alice, area, *, accept: str | None = None, on_stored=None, payload=b"payload"):
    async def scenario():
        async with node:
            grant = node.grants.issue(direction=UPLOAD, user=alice, area=area, on_stored=on_stored)
            form = aiohttp.FormData()
            form.add_field("file", payload, filename="copperplate-week1.png")
            headers = {"Accept": accept} if accept is not None else {}
            async with aiohttp.ClientSession() as client:
                async with client.post(f"{node.base}/transfer/{grant.token}", data=form, headers=headers) as response:
                    return response.status, response.headers.get("Content-Type", ""), await response.text()

    return _run(scenario)


def test_a_browser_form_upload_ends_on_a_page_not_on_json(node):
    """A person who opened a printed link in a browser tab submits its
    form, which asks for HTML. They used to be left looking at
    `{"filename": ..., "status": "approved"}`."""
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "Practice pages", creator=alice)

    status, content_type, body = _post_upload(
        node, alice, area, accept="text/html,application/xhtml+xml,*/*;q=0.8",
    )

    assert status == 200
    assert content_type.startswith("text/html")
    assert "<h1>Uploaded</h1>" in body
    assert "copperplate-week1.png" in body
    assert "[Practice pages]" in body
    assert "go back to your terminal" in body
    assert '"status"' not in body


def test_a_browser_form_upload_into_a_moderated_area_says_it_waits(node):
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    sysop = create_user(node.db, "sysop", password="hunter2", user_level=255)
    area = create_file_area(node.db, "docs", creator=sysop, moderated=True)

    _, _, body = _post_upload(node, alice, area, accept="text/html")

    assert "waits for approval" in body


def test_the_terminal_page_and_scripts_still_get_json(node):
    """The browser terminal's own upload panel uses `fetch()`, which sends
    `*/*`, and parses the JSON it gets back."""
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)

    status, content_type, body = _post_upload(node, alice, area, accept="*/*")

    assert status == 200
    assert content_type.startswith("application/json")
    assert '"status": "approved"' in body


def test_an_upload_grant_tells_whoever_asked_for_it(node):
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)
    told = []

    _post_upload(node, alice, area, on_stored=told.append)

    assert [entry.filename for entry in told] == ["copperplate-week1.png"]


def test_a_failing_listener_does_not_fail_the_upload(node):
    """The file is stored before anyone is told: a terminal that has gone
    wrong must not turn that into a refused upload."""
    alice = create_user(node.db, "alice", password="hunter2", user_level=10)
    area = create_file_area(node.db, "docs", creator=alice)

    def broken(entry):
        raise RuntimeError("the terminal went away")

    status, _, _ = _post_upload(node, alice, area, on_stored=broken)

    assert status == 200
    assert list_files_page(node.db, area, alice).entries[0].filename == "copperplate-week1.png"

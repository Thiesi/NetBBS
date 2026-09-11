"""
HTTP file transfer for callers whose terminal cannot speak Zmodem
(issue #475).

Zmodem lives inside the terminal's byte stream, so the *emulator* has
to implement it. SyncTERM, NetRunner, Qodem, Tera Term, ZOC, MobaXterm
and minicom do; PuTTY, Windows Terminal and an ordinary OpenSSH client
do not, and neither does this project's own browser terminal
(`netbbs.net.web`, whose `write_raw`/`read_byte` are available only
inside door mode). For most callers `[U]pload` could never have worked,
and the file area was effectively unreachable — see the issue for the
full accounting.

This module is the other path: one bounded, single-use HTTP grant per
transfer, redeemed against the aiohttp application the web transport
already runs. A caller on any transport can be handed a URL; a caller
already in the browser gets the page's own drag-and-drop and download
buttons pointed at the same endpoints (a later slice).

**A grant is not a capability.** It names *who* asked for *what*, and
every gate the terminal path enforces — area read/write level, age and
name requirements, Community inheritance, moderation state, the node's
maximum upload size — is enforced again when the grant is redeemed,
against the live user and area rows. A leaked URL therefore buys an
attacker exactly one thing: whatever that one caller could already do
with that one file, for the few minutes before it expires. It is not a
way around anything, and nothing here trusts the browser.

Grants live in memory only, deliberately: they are worth less than a
restart, they must not accumulate in the database, and a node that
restarts mid-transfer is a node whose caller can simply ask again.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from netbbs.attestation import meets_age, meets_name_requirement
from netbbs.auth.users import User, get_user_by_id
from netbbs.communities import (
    get_effective_min_age,
    get_effective_min_read_level,
    get_effective_min_write_level,
    get_effective_name_requirement,
)
from netbbs.config import get_max_upload_bytes
from netbbs.files.areas import FileArea, get_file_area_by_area_id
from netbbs.files.diz import read_archive_description
from netbbs.files.entries import FileEntry, get_file, upload_file_from_temp
from netbbs.moderation import BoardPermission, has_permission
from netbbs.moderation.blocklist import is_blocked
from netbbs.files.storage import new_incoming_temp_path
from netbbs.link.files import queue_file_descriptor_if_linked
from netbbs.net.zmodem import safe_filename
from netbbs.permissions import meets_level
from netbbs.storage.database import Database

_logger = logging.getLogger(__name__)

DEFAULT_GRANT_TTL_SECONDS = 600
"""Ten minutes: long enough to switch to a browser, paste a URL and pick
a file; short enough that a URL shoulder-surfed off a terminal is worth
little by the time anyone acts on it."""

MAX_CONCURRENT_TRANSFERS = 4
"""How many transfers may be *in flight* across the node at once, in
each direction.

Distinct from the outstanding-grant ceiling, and needed alongside it
(Codex review): redeeming frees the grant slot before a single byte of
the body arrives, so without this a caller could mint, POST, mint, POST
and hold arbitrarily many long-running handlers, sockets and staging
files at once. A download is cheaper -- nothing staged, no long-running
handler -- but a slow client still holds a socket and a descriptor for
as long as it likes, so the same ceiling applies to both. Refused
visibly, with the caller told to retry."""

TRANSFER_TIMEOUT_SECONDS = 900
"""Wall-clock ceiling on one transfer, start to finish, in either
direction. Generous for a
large file on a slow line, and finite -- which is the point: without it
a caller can hold a request, a staging file and a handler task open for
as long as they care to."""

DEFAULT_MAX_OUTSTANDING_GRANTS = 128
"""A ceiling on unredeemed grants across the whole node. Every caller
can mint these, so the table is remotely influenced and needs a bound
like any other (AGENTS.md); expired entries are swept first, and a node
genuinely holding this many live transfers refuses visibly rather than
growing."""

DOWNLOAD = "download"
UPLOAD = "upload"


class TransferError(Exception):
    """Raised when a grant cannot be issued -- the node is at its
    outstanding-grant ceiling, or the caller could not do this transfer
    in the terminal either."""


@dataclass(frozen=True)
class TransferGrant:
    """One caller's permission to perform one transfer, once.

    `area_id`/`file_id` are the *local* identifiers rather than the
    objects themselves: the grant is redeemed later, in a different
    task, and everything it names is re-read then. Holding a
    `FileEntry` here would be holding a snapshot of a row that may have
    been approved, deleted or expired in the meantime.
    """

    token: str
    direction: str
    user_id: int
    #: Checked against the account `user_id` resolves to. Both halves
    #: are needed (Codex review, twice): a SQLite rowid is reusable, and
    #: so is a username once the account holding it is deleted -- but an
    #: account's creation timestamp is not, so the two together name one
    #: account and no successor to it.
    username: str
    user_created_at: str
    #: The *content-addressed* area id, not the row id: see
    #: `netbbs.files.areas.get_file_area_by_area_id`.
    area_id: str
    file_id: str | None
    expires_at: float

    def is_live(self, *, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) < self.expires_at


class TransferGrants:
    """
    The node's outstanding transfer grants: issue one, redeem it once,
    forget it.

    Bounded and self-sweeping (see `DEFAULT_MAX_OUTSTANDING_GRANTS`).
    Not thread-safe and not meant to be: everything here runs on the
    node's event loop, the same as every other shared session service.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_GRANT_TTL_SECONDS,
        max_outstanding: int = DEFAULT_MAX_OUTSTANDING_GRANTS,
        base_url: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_outstanding = max_outstanding
        self._base_url = base_url.rstrip("/") if base_url else None
        self._clock = clock
        self._grants: dict[str, TransferGrant] = {}

    @property
    def base_url(self) -> str | None:
        """Where a caller should be told to go, or `None` when this node
        has no idea -- a node whose web listener is loopback-only, or
        behind a proxy nobody configured, cannot honestly print a URL,
        and the interface says so rather than inventing one."""
        return self._base_url

    def issue(
        self, *, direction: str, user: User, area: FileArea, file_id: str | None = None
    ) -> TransferGrant:
        self._sweep()
        if len(self._grants) >= self._max_outstanding:
            raise TransferError(
                f"this node already has {len(self._grants)} transfers waiting to start -- "
                "try again in a few minutes"
            )
        grant = TransferGrant(
            # 32 bytes of urlsafe randomness: this is the only thing
            # standing between a URL and someone else's transfer, so it
            # is generated the way a session token is, never from a
            # counter or a hash of the file.
            token=secrets.token_urlsafe(32),
            direction=direction,
            user_id=user.id,
            username=user.username,
            user_created_at=user.created_at,
            area_id=area.area_id,
            file_id=file_id,
            expires_at=self._clock() + self._ttl_seconds,
        )
        self._grants[grant.token] = grant
        return grant

    def peek(self, token: str) -> TransferGrant | None:
        """Look a grant up *without* spending it (Codex review).

        The upload flow needs this: opening the printed URL in a browser
        is a GET, and that GET has to serve a page with a file input on
        it. Redeeming there would consume the caller's one use before
        they had chosen a file, which is precisely the flow the screen
        tells them to follow."""
        self._sweep()
        grant = self._grants.get(token)
        return grant if grant is not None and grant.is_live(now=self._clock()) else None

    def redeem(self, token: str) -> TransferGrant | None:
        """Take a grant out of the table, or `None` if it was never
        there, has already been used, or has expired. Single-use is
        enforced by removal, not by a flag -- there is no state in which
        a redeemed grant still exists to be reasoned about."""
        self._sweep()
        grant = self._grants.pop(token, None)
        if grant is None or not grant.is_live(now=self._clock()):
            return None
        return grant

    def url_for(self, grant: TransferGrant) -> str | None:
        return f"{self._base_url}/transfer/{grant.token}" if self._base_url else None

    def _sweep(self) -> None:
        now = self._clock()
        expired = [token for token, grant in self._grants.items() if not grant.is_live(now=now)]
        for token in expired:
            del self._grants[token]

    def __len__(self) -> int:  # pragma: no cover - trivial, used by tests and logging
        return len(self._grants)


@dataclass(frozen=True)
class RedeemedTransfer:
    """A grant resolved back into the live rows it names, with every
    gate re-checked. Produced by `resolve`, which is the only way the
    HTTP handlers learn what they are allowed to do."""

    grant: TransferGrant
    user: User
    area: FileArea
    entry: FileEntry | None
    max_upload_bytes: int


def resolve(db: Database, grant: TransferGrant) -> RedeemedTransfer:
    """
    Turn a redeemed grant back into live rows, re-applying every gate
    the terminal path applies.

    Raises `TransferError` for anything that would have been refused in
    the BBS itself: an account or area that has since gone, a level,
    age or name requirement the caller no longer meets, a file that was
    deleted or is still pending for someone who may not see it. The
    caller of this function turns that into an HTTP status; nothing
    here decides how to say it.
    """
    user = get_user_by_id(db, grant.user_id)
    if (
        user is None
        or user.disabled_at is not None
        or user.username != grant.username
        or user.created_at != grant.user_created_at
    ):
        # Name, id *and* creation time: a deleted account can hand its
        # rowid and even its username to a new one, but not the instant
        # it was created, so the three together are an identity no
        # recreation can inherit (Codex review).
        raise TransferError("this account can no longer transfer files")
    area = get_file_area_by_area_id(db, grant.area_id)
    if area is None:
        raise TransferError("that file area no longer exists")

    if is_blocked(db, user):
        # An administrative lockout revokes live sessions; an
        # outstanding link must not quietly outlive it (Codex review).
        raise TransferError("this account can no longer transfer files")

    if not meets_age(db, user, get_effective_min_age(db, area)):
        raise TransferError("this file area has an age requirement your account no longer meets")

    # Reading the area is required either way: a link is issued from
    # inside it, and an upload grant redeemed after the read level rose
    # would let a caller contribute to an area they can no longer enter
    # (Codex review).
    if not meets_level(user, get_effective_min_read_level(db, area)):
        raise TransferError("you may no longer read this file area")

    if grant.direction == UPLOAD:
        if not meets_level(user, get_effective_min_write_level(db, area)):
            raise TransferError("you may no longer upload to this file area")
        # The name requirement gates *contributing*, not reading -- the
        # terminal applies it to `can_write` alone, and applying it to
        # downloads here would refuse over HTTP what Zmodem allows
        # (Codex review).
        if not meets_name_requirement(db, user, get_effective_name_requirement(db, area)):
            raise TransferError("this file area has a name requirement your account no longer meets")
        return RedeemedTransfer(
            grant=grant, user=user, area=area, entry=None,
            max_upload_bytes=get_max_upload_bytes(db),
        )

    entry = _visible_file(db, grant, area, user)
    return RedeemedTransfer(
        grant=grant, user=user, area=area, entry=entry, max_upload_bytes=get_max_upload_bytes(db),
    )


def _visible_file(db: Database, grant: TransferGrant, area: FileArea, user: User) -> FileEntry:
    assert grant.file_id is not None
    try:
        entry = get_file(db, grant.file_id)
    except Exception as exc:  # netbbs.files.entries raises its own error type
        raise TransferError("that file is no longer in this area") from exc
    if entry.area_id != area.id:
        # Compared against the resolved area's row id, since that is
        # what a `FileEntry` carries, while the grant names the area by
        # its content-addressed id. Nothing should be able to produce a
        # mismatch, which is exactly why it is checked: a grant names an
        # area, and the file it hands over must still be in it.
        raise TransferError("that file is no longer in this area")
    if entry.status == "pending" and not _may_see_pending(db, entry, user):
        raise TransferError("that file has not been approved yet")
    if not Path(entry.storage_path).exists():
        raise TransferError("this node no longer has that file's content")
    return entry


def _may_see_pending(db: Database, entry: FileEntry, user: User) -> bool:
    """The same answer `netbbs.files.entries.get_file_by_name` gives a
    terminal caller: its own uploader, or a moderator holding APPROVE
    on the area (Codex review -- a moderator who could ask for the file
    by name was being handed a link that always refused)."""
    if entry.uploader_user_id == user.id:
        return True
    return has_permission(
        db, user, object_type="file_area", object_id=entry.area_id,
        permission=BoardPermission.APPROVE,
    )


def hash_and_measure(path: Path, *, chunk_size: int = 64 * 1024) -> tuple[str, int]:
    """The sha256 and size of an already-written staging file, read in
    bounded chunks -- the streaming-upload counterpart of what
    `netbbs.net.zmodem` computes incrementally while receiving."""
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


# -- the HTTP endpoints -------------------------------------------------
#
# Registered onto the aiohttp application the web transport already
# runs (`netbbs.net.web.WebServer.start`), because a node that offers
# links has an HTTP listener by definition. Kept here rather than in
# that module so the web transport stays about being a terminal:
# nothing below renders a screen, and nothing in `web.py` knows what a
# file area is.


class TransferGateway:
    """Serves `/transfer/{token}` for `TransferGrants`.

    Holds the grant table and a `DatabaseLane` -- the same lane
    discipline every other database caller follows, since these
    handlers run on the node's event loop alongside live sessions and
    must not do their SQLite work there.
    """

    def __init__(
        self, grants: "TransferGrants", lane, *, announce_identity=None,
        max_concurrent_uploads: int = MAX_CONCURRENT_TRANSFERS,
    ) -> None:
        self._grants = grants
        self._lane = lane
        self._uploads_in_flight = 0
        self._downloads_in_flight = 0
        self._max_concurrent_uploads = max_concurrent_uploads
        # A callable, not a value: a node builds its listeners before it
        # loads its Link identity, so asking at construction time would
        # be asking too early. Called once per upload, which is late
        # enough for the answer to exist and cheap enough not to care.
        # A plain identity (or `None`) is accepted too, for tests and
        # for any caller that already has one.
        self._announce_identity = (
            announce_identity if callable(announce_identity) else (lambda: announce_identity)
        )

    def add_routes(self, app) -> None:
        # `add_get` would register HEAD alongside GET, and a HEAD from a
        # link scanner, proxy or download manager would spend the
        # caller's one use without ever delivering a byte (Codex
        # review). HEAD gets its own handler, which answers without
        # touching the grant table.
        app.router.add_get("/transfer/{token}", self.handle_download, allow_head=False)
        app.router.add_route("HEAD", "/transfer/{token}", self.handle_head)
        app.router.add_post("/transfer/{token}", self.handle_upload)

    async def handle_head(self, request):
        """Answer a probe without spending anything. Deliberately says
        nothing about whether the token is real: a HEAD that 404s for
        unknown tokens and 200s for live ones is an oracle for guessing
        them."""
        from aiohttp import web

        return web.Response(status=204)

    async def _redeem(self, request):
        """Take the grant named by the URL and resolve it against live
        rows, or raise the HTTP failure the caller should see.

        Redeemed *before* anything else happens, so a token is spent
        even when what follows fails: a link is one attempt, not one
        success, which is what keeps a leaked URL from being retried
        against a moving target."""
        from aiohttp import web

        grant = self._grants.redeem(request.match_info["token"])
        if grant is None:
            # One message for "never existed", "already used" and
            # "expired" alike -- distinguishing them tells an attacker
            # which tokens were once real.
            raise web.HTTPNotFound(text="This transfer link is not valid. Ask the BBS for a new one.")
        try:
            return await self._lane.run(resolve, grant)
        except TransferError as exc:
            raise web.HTTPForbidden(text=str(exc)) from exc

    async def handle_download(self, request):
        from aiohttp import web

        # An upload grant opened in a browser is the *advertised* flow
        # ("Open this in a browser to upload"), and a GET is how a
        # browser opens anything -- so it serves the form and leaves the
        # grant alone (Codex review: redeeming here spent the caller's
        # one use before they had chosen a file, which made the printed
        # instruction impossible to follow). The POST that follows is
        # what spends it.
        peeked = self._grants.peek(request.match_info["token"])
        if peeked is not None and peeked.direction == UPLOAD:
            return web.Response(
                text=_upload_form(request.path, peeked),
                content_type="text/html",
                headers={"Cache-Control": "no-store"},
            )

        # Reserved before anything is awaited (Codex review): several
        # GETs can otherwise pass this check while each waits on the
        # database lane, and every one of them then takes a slot that
        # was never counted.
        if self._downloads_in_flight >= self._max_concurrent_uploads:
            # A download costs less than an upload -- nothing staged, no
            # long-running handler -- but a slow client still holds a
            # socket and a descriptor, and a grant stops bounding
            # anything the moment it is redeemed (Codex review).
            raise web.HTTPTooManyRequests(
                text="This node is already busy sending files. Try again in a moment."
            )
        self._downloads_in_flight += 1
        try:
            return await self._send_file(request)
        finally:
            self._downloads_in_flight -= 1

    async def _send_file(self, request):
        from aiohttp import web

        resolved = await self._redeem(request)
        entry = resolved.entry
        if entry is None:  # an upload grant that expired between peek and redeem
            raise web.HTTPMethodNotAllowed(method="GET", allowed_methods=["POST"])
        _logger.info(
            "transfer: %r downloading %r from area %r",
            resolved.user.username, entry.filename, resolved.area.name,
        )

        # Streamed here rather than handed to `FileResponse`, so the
        # in-flight count falls when the *transfer* ends rather than
        # when this handler returns -- with `FileResponse` the body is
        # written after the handler is gone, which is precisely the
        # window the ceiling exists to bound. Range requests are no loss:
        # a single-use token cannot be resumed anyway.
        response = web.StreamResponse(
            headers={
                # RFC 6266's `filename*` form, so a CP437-era name with
                # non-ASCII characters survives the trip; `filename` is
                # the ASCII fallback for anything that still cares.
                "Content-Disposition": _content_disposition(entry.filename),
                "Content-Type": "application/octet-stream",
                # A single-use URL that a browser or shared proxy can
                # replay from cache is not single-use (Codex review).
                "Cache-Control": "no-store, no-cache, must-revalidate, private",
                "Pragma": "no-cache",
            },
        )
        response.content_length = entry.size_bytes
        await response.prepare(request)
        try:
            # Bounded in time as well as in count (Codex review): four
            # clients consuming at a trickle would otherwise hold every
            # slot for as long as they cared to, which is the same
            # denial the upload deadline exists to prevent.
            await asyncio.wait_for(_stream_file(response, Path(entry.storage_path)),
                                   timeout=TRANSFER_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            _logger.warning(
                "transfer: download of %r by %r exceeded %ss; dropping it",
                entry.filename, resolved.user.username, TRANSFER_TIMEOUT_SECONDS,
            )
        return response

    async def handle_upload(self, request):
        from aiohttp import web

        if self._uploads_in_flight >= self._max_concurrent_uploads:
            raise web.HTTPTooManyRequests(
                text="This node is already busy receiving files. Try again in a moment."
            )
        self._uploads_in_flight += 1
        try:
            return await self._receive_and_store(request)
        finally:
            self._uploads_in_flight -= 1

    async def _receive_and_store(self, request):
        from aiohttp import web

        resolved = await self._redeem(request)
        if resolved.entry is not None:  # a download grant used with POST
            raise web.HTTPMethodNotAllowed(method="POST", allowed_methods=["GET"])

        temp_path = await self._lane.run(new_incoming_temp_path)
        try:
            # Bounded in time, not only in bytes (Codex review): an
            # authenticated caller could otherwise hold a POST open
            # indefinitely, keeping a staging file, a socket and a
            # handler task for as long as they liked. The deadline
            # covers the whole receive rather than each read, so a
            # trickle is refused as surely as a stall.
            filename, received = await asyncio.wait_for(
                _receive_upload(request, temp_path, max_bytes=resolved.max_upload_bytes),
                timeout=TRANSFER_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            temp_path.unlink(missing_ok=True)
            _logger.warning(
                "transfer: upload by %r timed out after %ss",
                resolved.user.username, TRANSFER_TIMEOUT_SECONDS,
            )
            raise web.HTTPRequestTimeout(text="That upload took too long. Ask the BBS for a new link.")
        except OSError as exc:
            # A staging filesystem that is full or unwritable is a
            # resource failure the caller should see as one, not a bare
            # 500 (Codex review).
            temp_path.unlink(missing_ok=True)
            _logger.error("transfer: could not stage an upload: %s", exc)
            raise web.HTTPInsufficientStorage(
                text="This node could not store that file right now. Tell the SysOp."
            ) from exc
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        if received == 0:
            temp_path.unlink(missing_ok=True)
            raise web.HTTPBadRequest(text="No file was sent.")

        try:
            sha256, size_bytes = await asyncio.to_thread(hash_and_measure, temp_path)
            description = await read_archive_description(temp_path, filename)
            entry = await self._lane.run(
                _store_upload, resolved.area, resolved.user, filename,
                temp_path=temp_path, sha256=sha256, size_bytes=size_bytes,
                description=description,
                announce_identity=self._announce_identity(),
            )
        except BaseException as exc:
            # `BaseException`, so a cancelled request takes its staging
            # file with it (Codex review): `CancelledError` is not an
            # `Exception`, and until the move into storage this handler
            # is the only thing that owns the file.
            temp_path.unlink(missing_ok=True)
            if isinstance(exc, Exception):
                _logger.warning("transfer: upload by %r failed: %s", resolved.user.username, exc)
                raise web.HTTPBadRequest(text=f"The upload could not be stored: {exc}") from exc
            raise

        _logger.info(
            "transfer: %r uploaded %r (%d bytes) to area %r",
            resolved.user.username, entry.filename, entry.size_bytes, resolved.area.name,
        )
        return web.json_response({
            "filename": entry.filename,
            "size_bytes": entry.size_bytes,
            "status": entry.status,
            "description": entry.description,
        })


async def _stream_file(response, path: Path) -> None:
    """Write one stored file to an already-prepared response, in bounded
    chunks read off the event loop."""
    with path.open("rb") as handle:
        while True:
            chunk = await asyncio.to_thread(handle.read, 64 * 1024)
            if not chunk:
                break
            await response.write(chunk)
    await response.write_eof()


def _store_upload(
    db: Database, area: FileArea, user: User, filename: str, *,
    temp_path: Path, sha256: str, size_bytes: int, description: str | None,
    announce_identity,
) -> FileEntry:
    """Store and announce in one database job -- the same pairing
    `netbbs.net.file_flow._handle_upload` makes for a Zmodem upload, and
    for the same reason (issue #464): a file that lands without its
    catalogue entry is never revisited."""
    entry = upload_file_from_temp(
        db, area, user, filename,
        temp_path=temp_path, sha256=sha256, size_bytes=size_bytes, description=description,
    )
    if announce_identity is not None:
        queue_file_descriptor_if_linked(db, entry, area, node_identity=announce_identity)
    return entry


async def _receive_upload(request, temp_path: Path, *, max_bytes: int) -> tuple[str, int]:
    """Stream one uploaded file to `temp_path`, bounded.

    Accepts either a `multipart/form-data` part (what a browser's own
    form or `FormData` sends) or a raw request body with the name in a
    query parameter (what `curl --data-binary` sends, which is what a
    caller following a printed URL from a script will reach for). The
    body is never read into memory: it arrives in chunks and is written
    straight through, and the byte count is checked as it goes rather
    than trusted from `Content-Length`, which is a claim.
    """
    from aiohttp import web

    filename = request.query.get("filename") or "unnamed"
    written = 0
    with temp_path.open("wb") as handle:
        if request.content_type == "multipart/form-data":
            reader = await request.multipart()
            part = await reader.next()
            while part is not None and part.name != "file":
                part = await reader.next()
            if part is None:
                raise web.HTTPBadRequest(text="No 'file' part in that upload.")
            filename = part.filename or filename
            while True:
                chunk = await part.read_chunk()
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise web.HTTPRequestEntityTooLarge(max_size=max_bytes, actual_size=written)
                handle.write(chunk)
        else:
            async for chunk in request.content.iter_chunked(64 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise web.HTTPRequestEntityTooLarge(max_size=max_bytes, actual_size=written)
                handle.write(chunk)
    return safe_filename(filename), written


def _content_disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", errors="replace").decode("ascii").replace('"', "_")
    quoted = quote(filename, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"

def _upload_form(action: str, grant: TransferGrant) -> str:
    """The page a caller lands on when they open an upload link.

    Deliberately one self-contained page with no scripts, no styling
    beyond a few lines, and no requests anywhere but back to this node:
    it is served to someone who followed a URL off a terminal, and the
    less it does the less there is to go wrong or to trust. A browser
    caller who wants drag-and-drop gets it in the terminal page itself,
    which is a different surface with a different job.
    """
    minutes = max(1, int((grant.expires_at - time.monotonic()) // 60))
    return (
        "<!doctype html>"
        "<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>Upload to NetBBS</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:2rem auto;max-width:32rem;"
        "line-height:1.5}p{color:#555}button{font:inherit;padding:.4rem 1rem}</style>"
        "</head><body>"
        "<h1>Upload a file</h1>"
        f"<form method=\"post\" action=\"{html.escape(action)}\" enctype=\"multipart/form-data\">"
        "<p><input type=\"file\" name=\"file\" required></p>"
        "<p><button type=\"submit\">Upload</button></p>"
        "</form>"
        f"<p>This link works once, and expires in about {minutes} minute"
        f"{'' if minutes == 1 else 's'}.</p>"
        "</body></html>"
    )

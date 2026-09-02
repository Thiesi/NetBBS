"""
aiohttp.web API for the managed-DNS service (design doc §16, issue
#201).

Route/response-shape conventions mirror `netbbs.link.transport.
LinkServer` exactly (same project, same reasoning): `web.Application()`
+ `app.router.add_post(...)`, `web.AppRunner`/`web.TCPSite` for real
start/stop, a `port` property once started, and every handler returning
`web.json_response({"error": ...}, status=...)` on failure rather than
letting an unhandled exception produce a bare 500.

**Phase 2 scope**: `POST /register` only -- name validation (syntax +
Decision 3's reserved-word blocklist) and the one-name-per-node cap.
Deliberately does *not* simulate or stub the service-wide rate limiter
or the cumulative active-registration cap (both a later phase; Decision
3's originally-locked review queue was dropped entirely during
implementation planning -- see the issue's own plan), or heartbeat/
maturation/release/reclaim (also later phases). This endpoint's current
behavior is exactly what it claims to be, not a partial placeholder for
unbuilt behavior.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

from aiohttp import web

from services.managed_dns.blocklist import is_reserved
from services.managed_dns.names import InvalidNameError, normalize_name
from services.managed_dns.store import (
    Database,
    count_registrations_for_node,
    hash_credential,
    insert_registration,
)

# Design doc §16 Decision 3: "a one-name-per-node cap" -- counted against
# only the active statuses (pending/matured), see
# services.managed_dns.store.count_registrations_for_node's own
# docstring for why released/abandoned don't count against it.
_ACTIVE_STATUSES = ("pending", "matured")
_MAX_REGISTRATIONS_PER_NODE = 1

_CREDENTIAL_BYTES = 32


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


class ManagedDnsServer:
    """One running instance of the managed-DNS service. `clock` is
    injectable (defaults to real UTC now) -- the same "plain callable
    parameter, real value by default" shape `netbbs.net.throttle`'s
    token buckets already use -- so later phases (age-gate maturation,
    cooldown expiry) can drive it deterministically in tests without
    real sleeps."""

    def __init__(self, host: str, port: int, db: Database, *, clock: Callable[[], datetime] = _default_clock) -> None:
        self._host = host
        self._port = port
        self._db = db
        self._clock = clock
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def port(self) -> int:
        if self._site is None:
            raise RuntimeError("server has not been started yet")
        return self._site.port

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/register", self._handle_register)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _handle_register(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except ValueError as exc:
            return web.json_response({"error": f"malformed JSON body: {exc}"}, status=400)

        raw_name = body.get("name")
        node_fingerprint = body.get("node_fingerprint")
        dynamic = body.get("dynamic", False)
        if not isinstance(raw_name, str) or not isinstance(node_fingerprint, str) or not isinstance(dynamic, bool):
            return web.json_response(
                {"error": "request must contain a string name, a string node_fingerprint, and a boolean dynamic"},
                status=400,
            )

        try:
            name = normalize_name(raw_name)
        except InvalidNameError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        if is_reserved(name):
            return web.json_response({"error": f"{name!r} is a reserved name"}, status=403)

        active_for_node = count_registrations_for_node(self._db, node_fingerprint, statuses=_ACTIVE_STATUSES)
        if active_for_node >= _MAX_REGISTRATIONS_PER_NODE:
            return web.json_response(
                {"error": "this node already has an active managed-DNS registration"}, status=403
            )

        secret = secrets.token_urlsafe(_CREDENTIAL_BYTES)
        created_at = self._clock().isoformat()
        try:
            registration = insert_registration(
                self._db, name=name, credential_hash=hash_credential(secret),
                node_fingerprint=node_fingerprint, dynamic=dynamic, created_at=created_at,
            )
        except sqlite3.IntegrityError:
            # First-come-first-served (design doc §16 Decision 3): the
            # primary key is the actual enforcement, not a check-then-
            # insert race this catch is papering over -- see store.
            # insert_registration's own docstring.
            return web.json_response({"error": f"{name!r} is already registered"}, status=409)

        return web.json_response(
            {
                "name": registration.name,
                "credential": secret,
                "status": registration.status,
                "created_at": registration.created_at,
            },
            status=201,
        )

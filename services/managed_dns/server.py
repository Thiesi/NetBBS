"""
aiohttp.web API for the managed-DNS service (design doc §16, issue
#201).

Route/response-shape conventions mirror `netbbs.link.transport.
LinkServer` exactly (same project, same reasoning): `web.Application()`
+ `app.router.add_post(...)`, `web.AppRunner`/`web.TCPSite` for real
start/stop, a `port` property once started, and every handler returning
`web.json_response({"error": ...}, status=...)` on failure rather than
letting an unhandled exception produce a bare 500.

**Phase 2 scope**: `POST /register` -- name validation (syntax +
Decision 3's reserved-word blocklist) and the one-name-per-node cap.
**Phase 3 adds**: `POST /heartbeat` -- Decision 3's age-gate maturation
(a `pending` registration only actually publishes once the node has
stayed in contact for `min_age_seconds`) and, for a `dynamic`
registration, keeping the published record pointed at the caller's own
observed address. Deliberately does *not* simulate or stub the
service-wide rate limiter or the cumulative active-registration cap
(a later phase; Decision 3's originally-locked review queue was dropped
entirely during implementation planning -- see the issue's own plan),
or release/reclaim (also a later phase). Each endpoint's current
behavior is exactly what it claims to be, not a partial placeholder for
unbuilt behavior.
"""

from __future__ import annotations

import ipaddress
import secrets
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from aiohttp import web

from services.managed_dns.blocklist import is_reserved
from services.managed_dns.dns_provider import DnsProvider, DnsProviderError, LoggingDnsProvider, RecordKind
from services.managed_dns.names import InvalidNameError, normalize_name
from services.managed_dns.store import (
    Database,
    count_registrations_for_node,
    get_registration_by_credential_hash,
    hash_credential,
    insert_registration,
    mark_matured,
    set_last_contact_at,
    set_last_known_address,
)

# Design doc §16 Decision 3: "a one-name-per-node cap" -- counted against
# only the active statuses (pending/matured), see
# services.managed_dns.store.count_registrations_for_node's own
# docstring for why released/abandoned don't count against it.
_ACTIVE_STATUSES = ("pending", "matured")
_MAX_REGISTRATIONS_PER_NODE = 1

_CREDENTIAL_BYTES = 32

# The zone every registered name is a label under -- design doc §16's
# own "myboard.netbbs.org" example.
_ZONE = "netbbs.org"

# Design doc §16 Decision 3: "a minimum age of successful contact with
# the registration service" before a reservation actually publishes --
# exact period explicitly left as an implementation-time parameter, not
# fixed by the design doc itself. 24 hours is a reasoned default (long
# enough that a disposable Sybil identity has to stay genuinely running,
# short enough a legitimate SysOp isn't kept waiting more than a day for
# their first-ever registration to go live) -- constructor-injectable so
# a real deployment can retune it without a code change.
_DEFAULT_MIN_AGE_SECONDS = 24 * 60 * 60


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _address_kind(address: str) -> RecordKind:
    return "AAAA" if ipaddress.ip_address(address).version == 6 else "A"


class ManagedDnsServer:
    """One running instance of the managed-DNS service. `clock` is
    injectable (defaults to real UTC now) -- the same "plain callable
    parameter, real value by default" shape `netbbs.net.throttle`'s
    token buckets already use -- so age-gate maturation and (a later
    phase) cooldown expiry can be driven deterministically in tests
    without real sleeps.

    `trust_x_forwarded_for`: whether to read the caller's real address
    from `X-Forwarded-For` (set when this service sits behind its own
    TLS-terminating reverse proxy, the same requirement design doc §16
    Decision 6 already places on a *node's* web listener) rather than
    `aiohttp`'s own `request.remote`, which would otherwise report the
    proxy's address for every caller. Off by default -- a deployment
    not yet behind a proxy must not trust a header any client can freely
    forge. Trusts only the first (leftmost, original-client) entry,
    correct for exactly one trusted proxy prepending it -- a multi-hop
    trusted-proxy chain is not a scenario this single-instance service
    is deployed into.
    """

    def __init__(
        self, host: str, port: int, db: Database, *, dns_provider: DnsProvider | None = None,
        clock: Callable[[], datetime] = _default_clock, min_age_seconds: float = _DEFAULT_MIN_AGE_SECONDS,
        trust_x_forwarded_for: bool = False,
    ) -> None:
        self._host = host
        self._port = port
        self._db = db
        # Defaults to LoggingDnsProvider (see its own docstring: "the
        # default until BIND is actually configured, and the only
        # provider any automated test ever exercises") rather than
        # requiring every caller, including every test, to construct
        # one explicitly.
        self._dns_provider = dns_provider if dns_provider is not None else LoggingDnsProvider()
        self._clock = clock
        self._min_age_seconds = min_age_seconds
        self._trust_x_forwarded_for = trust_x_forwarded_for
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
        app.router.add_post("/heartbeat", self._handle_heartbeat)
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

    def _observed_address(self, request: web.Request) -> str | None:
        if self._trust_x_forwarded_for:
            forwarded = request.headers.get("X-Forwarded-For")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.remote

    async def _handle_heartbeat(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except ValueError as exc:
            return web.json_response({"error": f"malformed JSON body: {exc}"}, status=400)

        credential = body.get("credential")
        if not isinstance(credential, str) or not credential:
            return web.json_response({"error": "request must contain a string credential"}, status=400)

        registration = get_registration_by_credential_hash(self._db, hash_credential(credential))
        if registration is None or registration.status not in _ACTIVE_STATUSES:
            # Deliberately the same message/status for "no such
            # credential" and "credential belongs to a released/
            # abandoned registration" -- neither is anything a caller
            # presenting an invalid/stale credential needs told apart
            # (design doc §16 Decision 3's own "none of these reasons
            # are anything a rejected peer needs to be told apart"
            # reasoning, reused here for the same kind of ambiguity).
            return web.json_response({"error": "unknown or inactive registration"}, status=401)

        now = self._clock()
        now_iso = now.isoformat()
        set_last_contact_at(self._db, registration.name, now_iso)

        newly_matured = False
        status = registration.status
        if status == "pending":
            created_at = datetime.fromisoformat(registration.created_at)
            if now - created_at >= timedelta(seconds=self._min_age_seconds):
                mark_matured(self._db, registration.name, matured_at=now_iso)
                status = "matured"
                newly_matured = True

        last_known_address = registration.last_known_address
        observed_address = self._observed_address(request)
        # Publish on the transition to matured (the record must exist
        # at all once a registration goes live, regardless of whether
        # this is a "dynamic" registration) or, for a dynamic
        # registration only, whenever the observed address has actually
        # changed -- a static-IP registration that already has a
        # published record never calls the DNS provider again after its
        # initial publish, matching design doc §16's "a board could
        # plausibly want [the subdomain] without [dynamic tracking]."
        should_publish = status == "matured" and (newly_matured or registration.dynamic)
        if should_publish and observed_address and observed_address != last_known_address:
            fqdn = f"{registration.name}.{_ZONE}."
            try:
                self._dns_provider.upsert_record(fqdn, _address_kind(observed_address), observed_address)
            except DnsProviderError:
                # Best-effort: the next heartbeat retries. A transient
                # DNS-provider failure must not fail the heartbeat call
                # itself -- last_contact_at above is already recorded,
                # so this registration doesn't spuriously look abandoned
                # over one failed publish attempt.
                pass
            else:
                set_last_known_address(self._db, registration.name, observed_address)
                last_known_address = observed_address

        return web.json_response(
            {"name": registration.name, "status": status, "last_known_address": last_known_address}
        )

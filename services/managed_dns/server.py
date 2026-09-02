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
observed address. **Phase 4 adds**: `POST /release` and reclaim (folded
into `/register` itself, not a separate endpoint -- see `_handle_
register`'s own docstring for why), plus a periodic sweep that abandons
a registration gone silent past `abandonment_seconds` and, independently,
purges any `released`/`abandoned` row whose Decision 5 cooldown has
fully elapsed. **Phase 5 adds**: Decision 3's remaining abuse controls
-- a service-wide rate limit on new registrations (`services.managed_
dns.rate_limit.GlobalRateLimiter`, not per-node/identity -- the one
thing a Sybil attacker cannot multiply by minting more identities) and a
separate cumulative cap on total active registrations. Both hard-reject
once exceeded; Decision 3's originally-locked review queue was dropped
entirely during implementation planning (see the issue's own plan) in
favor of this simpler, symmetric shape. Reclaim bypasses the admission
rate but still obeys the cumulative and per-node active caps because it
reactivates a real capacity-consuming row. Each endpoint's current
behavior is exactly what it claims to be, not a partial placeholder for
unbuilt behavior.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import secrets
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from aiohttp import web

from services.managed_dns.blocklist import is_reserved
from services.managed_dns.dns_provider import DnsProvider, DnsProviderError, LoggingDnsProvider, RecordKind
from services.managed_dns.names import InvalidNameError, normalize_name
from services.managed_dns.rate_limit import GlobalRateLimiter
from services.managed_dns.store import (
    Database,
    Registration,
    count_registrations,
    count_registrations_for_node,
    clear_last_known_address,
    delete_expired_registrations,
    delete_registration,
    get_registration_by_credential_hash,
    get_registration_by_name,
    hash_credential,
    insert_registration,
    list_stale_active_registrations,
    load_rate_limit_state,
    mark_abandoned,
    mark_matured,
    mark_released,
    reclaim,
    save_rate_limit_state,
    set_contact_window,
    set_last_contact_at,
    set_last_known_address,
)

_logger = logging.getLogger(__name__)

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

# Design doc §16 Decision 5: "on the order of 90 days, well past a
# typical registrar's ~30-45-day redemption window" -- deliberately more
# generous than commercial practice needs to be, since this project has
# no commercial pressure to recycle a name quickly. Shared by both exit
# paths (voluntary release and abandonment), one parameter, not two.
_DEFAULT_COOLDOWN_SECONDS = 90 * 24 * 60 * 60

# How long without any heartbeat contact before an active registration
# is swept into "abandoned" -- long enough to tolerate a legitimately
# offline node (an extended outage, a SysOp on vacation) without
# prematurely reclaiming their name out from under them; short enough
# that squatted or genuinely dead capacity doesn't sit unavailable
# forever. A reasoned default, not fixed by the design doc itself (same
# implementation-time latitude as every other bound here).
_DEFAULT_ABANDONMENT_SECONDS = 7 * 24 * 60 * 60

# How often the sweep runs at all -- frequent enough that abandonment/
# expiry are noticed within a reasonable window of the thresholds above
# actually elapsing, infrequent enough not to be wasteful against a
# table this small.
_DEFAULT_SWEEP_INTERVAL_SECONDS = 60 * 60

# Design doc §16 Decision 3: the actual bound on registration *volume*
# (the age-gate above only costs an attacker wall-clock time, not effort
# per identity -- see the design doc's own account of why a bare age-gate
# was found insufficient). A handful an hour comfortably serves genuine
# demand for a project at this scale while bounding how fast a burst can
# consume DNS-provider record slots -- a reasoned default, not fixed by
# the design doc itself.
_DEFAULT_RATE_LIMIT_CAPACITY = 5.0
_DEFAULT_RATE_LIMIT_REFILL_PER_MINUTE = 5.0 / 60.0

# Design doc §16 Decision 3: the separate ceiling a rate limit alone
# can't provide -- a patient attacker submitting exactly at (never over)
# the rate limit, keeping every registered identity's contact alive
# indefinitely so nothing qualifies as abandoned, could otherwise
# accumulate an unbounded number of active records over a long enough
# time. Counted against pending+matured only (count_registrations'
# own convention throughout this module) -- a released/abandoned
# registration already stopped occupying real capacity. A reasoned
# default, not fixed by the design doc itself.
_DEFAULT_CUMULATIVE_CAP = 1000


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
        trust_x_forwarded_for: bool = False, cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
        abandonment_seconds: float = _DEFAULT_ABANDONMENT_SECONDS,
        sweep_interval_seconds: float = _DEFAULT_SWEEP_INTERVAL_SECONDS,
        sweep_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rate_limit_capacity: float = _DEFAULT_RATE_LIMIT_CAPACITY,
        rate_limit_refill_per_minute: float = _DEFAULT_RATE_LIMIT_REFILL_PER_MINUTE,
        cumulative_cap: int = _DEFAULT_CUMULATIVE_CAP,
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
        self._cooldown_seconds = cooldown_seconds
        self._abandonment_seconds = abandonment_seconds
        self._sweep_interval_seconds = sweep_interval_seconds
        self._sweep_sleep = sweep_sleep
        self._cumulative_cap = cumulative_cap
        # Driven off this same injectable `clock` (converted to a plain
        # float via `datetime.timestamp()`) rather than a second,
        # independent clock -- one thing for a test to control
        # deterministically, not two.
        persisted_rate_state = load_rate_limit_state(db)
        self._rate_limiter = GlobalRateLimiter(
            capacity=rate_limit_capacity, refill_per_minute=rate_limit_refill_per_minute,
            clock=lambda: self._clock().timestamp(),
            tokens=None if persisted_rate_state is None else persisted_rate_state[0],
            last_refill=None if persisted_rate_state is None else persisted_rate_state[1],
        )
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._sweep_task: asyncio.Task | None = None
        # One bounded transition lane protects the SQLite snapshot around
        # each blocking DNS operation. Handlers reject visibly when it is
        # occupied instead of accumulating work in asyncio's executor queue.
        self._dns_transition_lock = asyncio.Lock()

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        if self._site is None:
            raise RuntimeError("server has not been started yet")
        return self._site.port

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/register", self._handle_register)
        app.router.add_post("/heartbeat", self._handle_heartbeat)
        app.router.add_post("/release", self._handle_release)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        self._sweep_task = asyncio.create_task(self._run_sweep_loop())

    async def stop(self) -> None:
        try:
            if self._sweep_task is not None:
                self._sweep_task.cancel()
                try:
                    await self._sweep_task
                except asyncio.CancelledError:
                    pass
        finally:
            if self._runner is not None:
                await self._runner.cleanup()

    async def _run_sweep_loop(self) -> None:
        """Runs for the service's lifetime: one sweep pass immediately
        on entry, then every `sweep_interval_seconds` after -- same
        "immediate first pass, then interval" shape every periodic task
        in this project already uses (e.g. `netbbs.link.seedlist.
        run_scheduled_seed_refresh`)."""
        while True:
            try:
                await self._sweep_once()
            except Exception:
                _logger.exception("managed-DNS sweep pass failed; it will retry on the next interval")
            await self._sweep_sleep(self._sweep_interval_seconds)

    async def _sweep_once(self) -> None:
        now = self._clock()
        abandonment_cutoff = now - timedelta(seconds=self._abandonment_seconds)
        candidates = list_stale_active_registrations(
            self._db, older_than=abandonment_cutoff.isoformat()
        )
        for candidate in candidates:
            async with self._dns_transition_lock:
                # The candidate list is intentionally outside the lane so one
                # slow provider cannot monopolize it for the whole pass. Re-read
                # after admission because a heartbeat may have refreshed the row.
                registration = get_registration_by_name(self._db, candidate.name)
                if registration is None or registration.status not in _ACTIVE_STATUSES:
                    continue
                latest_contact = registration.last_contact_at or registration.created_at
                if datetime.fromisoformat(latest_contact) >= abandonment_cutoff:
                    continue
                if registration.status == "matured":
                    if not await self._delete_record(registration.name):
                        continue
                mark_abandoned(self._db, registration.name, released_at=now.isoformat())
                _logger.info(
                    "Managed-DNS registration %r abandoned (no contact since %s)",
                    registration.name, registration.last_contact_at,
                )
            # Give a waiting heartbeat/release/reclaim a chance to acquire the
            # now-free lane before the sweep considers its next stale row.
            await asyncio.sleep(0)
        cooldown_cutoff = (now - timedelta(seconds=self._cooldown_seconds)).isoformat()
        async with self._dns_transition_lock:
            removed = delete_expired_registrations(self._db, older_than=cooldown_cutoff)
        if removed:
            _logger.info("Purged %d managed-DNS registration(s) past their cooldown", removed)

    async def _delete_record(self, name: str) -> bool:
        try:
            await asyncio.to_thread(self._dns_provider.delete_record, f"{name}.{_ZONE}.")
        except DnsProviderError:
            return False
        return True

    async def _handle_register(self, request: web.Request) -> web.Response:
        """`credential` is optional (design doc §16 Decision 5) --
        reclaim is folded into this same endpoint rather than a
        separate one: a caller presenting the *same* credential a
        cooldown-window `released`/`abandoned` row already has on file
        is reclaiming that exact row, not asking for a fresh one, and a
        request otherwise looks identical either way (same `name`
        field). No `node_fingerprint`/`dynamic` re-validation on
        reclaim -- those stay whatever the row already has; a reclaim
        doesn't get to silently redirect an existing registration to a
        different node or flip its dynamic-tracking preference.
        """
        try:
            body = await request.json()
        except ValueError as exc:
            return web.json_response({"error": f"malformed JSON body: {exc}"}, status=400)

        if not isinstance(body, dict):
            return web.json_response({"error": "request body must be a JSON object"}, status=400)
        raw_name = body.get("name")
        node_fingerprint = body.get("node_fingerprint")
        dynamic = body.get("dynamic", False)
        credential = body.get("credential")
        if (
            not isinstance(raw_name, str) or not isinstance(node_fingerprint, str)
            or not isinstance(dynamic, bool) or (credential is not None and not isinstance(credential, str))
        ):
            return web.json_response(
                {
                    "error": "request must contain a string name, a string node_fingerprint, a boolean "
                    "dynamic, and an optional string credential"
                },
                status=400,
            )

        try:
            name = normalize_name(raw_name)
        except InvalidNameError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        if is_reserved(name):
            return web.json_response({"error": f"{name!r} is a reserved name"}, status=403)

        existing = get_registration_by_name(self._db, name)
        if existing is not None and existing.status in _ACTIVE_STATUSES:
            return web.json_response({"error": f"{name!r} is already registered"}, status=409)

        now = self._clock()
        if existing is not None:
            # existing.status is 'released' or 'abandoned' here -- decide
            # whether this is a reclaim (same credential, still within
            # the cooldown), a rejection (a *different* credential, or
            # no credential, still within the cooldown -- Decision 5's
            # whole point), or the cooldown has simply elapsed and the
            # name is genuinely available to anyone now.
            cooldown_elapsed = now - datetime.fromisoformat(existing.released_at)
            if cooldown_elapsed < timedelta(seconds=self._cooldown_seconds):
                if credential and hash_credential(credential) == existing.credential_hash:
                    active_for_node = count_registrations_for_node(
                        self._db, existing.node_fingerprint, statuses=_ACTIVE_STATUSES
                    )
                    if active_for_node >= _MAX_REGISTRATIONS_PER_NODE:
                        return web.json_response(
                            {"error": "this node already has an active managed-DNS registration"}, status=403
                        )
                    if count_registrations(self._db, statuses=_ACTIVE_STATUSES) >= self._cumulative_cap:
                        return web.json_response(
                            {"error": "the managed-DNS service is at capacity -- try again later"}, status=503
                        )
                    if self._dns_transition_lock.locked():
                        return web.json_response(
                            {"error": "a managed-DNS transition is already in progress; retry shortly"},
                            status=503,
                        )
                    async with self._dns_transition_lock:
                        current = get_registration_by_name(self._db, name)
                        if (
                            current is None or current.status in _ACTIVE_STATUSES
                            or hash_credential(credential) != current.credential_hash
                        ):
                            return web.json_response(
                                {"error": f"{name!r} is already registered or no longer reclaimable"},
                                status=409,
                            )
                        return await self._reclaim(current, request, credential, dynamic=dynamic)
                return web.json_response(
                    {"error": f"{name!r} is in a cooldown period and not currently available"}, status=409
                )
            delete_registration(self._db, name)

        active_for_node = count_registrations_for_node(self._db, node_fingerprint, statuses=_ACTIVE_STATUSES)
        if active_for_node >= _MAX_REGISTRATIONS_PER_NODE:
            return web.json_response(
                {"error": "this node already has an active managed-DNS registration"}, status=403
            )

        # Design doc §16 Decision 3's remaining abuse controls -- both
        # hard-reject and neither queues. Reclaim bypasses only the rate
        # limiter; its cumulative-cap check happened above. For a fresh
        # registration, check the cumulative cap first (a request
        # that's already going to be refused for being over capacity
        # shouldn't also spend a scarce rate-limit token), then the rate
        # limit.
        if count_registrations(self._db, statuses=_ACTIVE_STATUSES) >= self._cumulative_cap:
            return web.json_response(
                {"error": "the managed-DNS service is at capacity -- try again later"}, status=503
            )
        if not self._rate_limiter.allow():
            return web.json_response(
                {"error": "too many registrations right now -- try again shortly"}, status=429
            )
        tokens, last_refill = self._rate_limiter.snapshot()
        save_rate_limit_state(self._db, tokens=tokens, last_refill=last_refill)

        secret = secrets.token_urlsafe(_CREDENTIAL_BYTES)
        created_at = now.isoformat()
        try:
            registration = insert_registration(
                self._db, name=name, credential_hash=hash_credential(secret),
                node_fingerprint=node_fingerprint, dynamic=dynamic, created_at=created_at,
            )
        except sqlite3.IntegrityError:
            # First-come-first-served (design doc §16 Decision 3): the
            # primary key is the actual enforcement, not a check-then-
            # insert race this catch is papering over -- see store.
            # insert_registration's own docstring. Reachable here despite
            # the `existing` check above only under real concurrent
            # requests for the same name racing each other, not a logic
            # gap in the sequential path.
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

    async def _reclaim(
        self, existing: Registration, request: web.Request, credential: str, *, dynamic: bool,
    ) -> web.Response:
        matured = existing.matured_at is not None
        now = self._clock()
        now_iso = now.isoformat()
        contact_started_at = existing.contact_started_at
        if (
            existing.status == "abandoned" or existing.last_contact_at is None
            or now - datetime.fromisoformat(existing.last_contact_at)
            > timedelta(seconds=self._abandonment_seconds)
        ):
            contact_started_at = now_iso
        reclaim(
            self._db, existing.name, matured=matured, dynamic=dynamic,
            last_contact_at=now_iso, contact_started_at=contact_started_at,
        )
        status: str = "matured" if matured else "pending"

        last_known_address = existing.last_known_address
        if matured:
            observed_address = self._observed_address(request)
            if observed_address and await self._best_effort_publish(existing.name, observed_address):
                last_known_address = observed_address
            else:
                clear_last_known_address(self._db, existing.name)
                last_known_address = None

        return web.json_response(
            {"name": existing.name, "credential": credential, "status": status, "created_at": existing.created_at},
            status=201,
        )

    async def _best_effort_publish(self, name: str, address: str) -> bool:
        """Publishes `address` for `name`, returning whether it
        succeeded -- the shared "call the DNS provider, tolerate
        failure, and let the caller decide what to persist" step both
        `_handle_heartbeat` and `_reclaim` need at the moment a
        registration (re)establishes a live published record."""
        fqdn = f"{name}.{_ZONE}."
        try:
            kind = _address_kind(address)
            await asyncio.to_thread(self._dns_provider.upsert_record, fqdn, kind, address)
        except (DnsProviderError, ValueError):
            return False
        set_last_known_address(self._db, name, address)
        return True

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

        if not isinstance(body, dict):
            return web.json_response({"error": "request body must be a JSON object"}, status=400)
        credential = body.get("credential")
        if not isinstance(credential, str) or not credential:
            return web.json_response({"error": "request must contain a string credential"}, status=400)

        if self._dns_transition_lock.locked():
            return web.json_response(
                {"error": "a managed-DNS transition is already in progress; retry shortly"}, status=503
            )
        async with self._dns_transition_lock:
            return await self._process_heartbeat(request, credential)

    async def _process_heartbeat(self, request: web.Request, credential: str) -> web.Response:

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
        contact_started_at = registration.contact_started_at
        if (
            contact_started_at is None or registration.last_contact_at is None
            or now - datetime.fromisoformat(registration.last_contact_at)
            > timedelta(seconds=self._abandonment_seconds)
        ):
            contact_started_at = now_iso
        set_contact_window(
            self._db, registration.name, last_contact_at=now_iso, contact_started_at=contact_started_at
        )

        newly_matured = False
        status = registration.status
        if status == "pending":
            if now - datetime.fromisoformat(contact_started_at) >= timedelta(seconds=self._min_age_seconds):
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
        should_publish = status == "matured" and (
            newly_matured or last_known_address is None or registration.dynamic
        )
        if should_publish and observed_address and observed_address != last_known_address:
            # Best-effort: the next heartbeat retries. A transient
            # DNS-provider failure must not fail the heartbeat call
            # itself -- last_contact_at above is already recorded, so
            # this registration doesn't spuriously look abandoned over
            # one failed publish attempt.
            if await self._best_effort_publish(registration.name, observed_address):
                last_known_address = observed_address

        return web.json_response(
            {"name": registration.name, "status": status, "last_known_address": last_known_address}
        )

    async def _handle_release(self, request: web.Request) -> web.Response:
        """Voluntary release (design doc §16 Decision 5). The DNS record
        is deleted immediately -- released is released, not "still
        published until the cooldown elapses" -- but the *name* itself
        stays reserved, non-claimable by a different registrant, until
        that cooldown elapses (see `_handle_register`'s own reclaim
        handling for the other half of this)."""
        try:
            body = await request.json()
        except ValueError as exc:
            return web.json_response({"error": f"malformed JSON body: {exc}"}, status=400)

        if not isinstance(body, dict):
            return web.json_response({"error": "request body must be a JSON object"}, status=400)
        credential = body.get("credential")
        if not isinstance(credential, str) or not credential:
            return web.json_response({"error": "request must contain a string credential"}, status=400)

        if self._dns_transition_lock.locked():
            return web.json_response(
                {"error": "a managed-DNS transition is already in progress; retry shortly"}, status=503
            )
        async with self._dns_transition_lock:
            return await self._process_release(credential)

    async def _process_release(self, credential: str) -> web.Response:

        registration = get_registration_by_credential_hash(self._db, hash_credential(credential))
        if registration is None or registration.status not in _ACTIVE_STATUSES:
            return web.json_response({"error": "unknown or inactive registration"}, status=401)

        if registration.status == "matured":
            if not await self._delete_record(registration.name):
                return web.json_response(
                    {"error": "DNS deletion failed; the registration remains active and may be retried"},
                    status=503,
                )

        released_at = self._clock().isoformat()
        mark_released(self._db, registration.name, released_at=released_at)
        return web.json_response({"name": registration.name, "status": "released"})

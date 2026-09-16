"""
Periodic managed-DNS heartbeat task (design doc §16, issue #201 Phase
3) -- the node-side half of `services.managed_dns.server`'s `/heartbeat`
endpoint: keeps a registration's age-gate maturing, and (for a `dynamic`
registration) its published record current, by calling in on an
interval for as long as the node stays up.

Needs `aiohttp` (via `netbbs.managed_dns.client`), an optional extra --
this module must therefore be imported *lazily*, right at its own
task-creation call site in `netbbs.__main__.run`, never at that module's
own top level, the same convention already established for `netbbs.
link.sync.run_link_sync` (also aiohttp-dependent) and, from the other
direction, issue #245's fix for `netbbs.net.chat_flow`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import asyncio
from aiohttp import ClientSession

from netbbs.managed_dns.client import (
    CancelRenameResult, HeartbeatResult, ManagedDnsError, cancel_rename, heartbeat, reclaim,
)
from netbbs.managed_dns.credential import (
    credential_path_for, delete_credential, load_credential, previous_credential_path_for,
    managed_dns_transition_lock, recover_credential_transition, stage_credential_cancellation,
)
from netbbs.managed_dns.state import (
    OptIn,
    RecoveryNote,
    RegistrationStatus,
    get_dynamic,
    get_last_contact_at,
    get_opt_in,
    get_previous_name,
    get_recovery_note,
    get_previous_published,
    get_previous_status,
    get_published,
    get_registered_name,
    get_registration_status,
    get_service_url,
    foreign_credential_service_url,
    set_heartbeat_reconciliation_state,
    set_recovery_note,
    set_registration_result_state,
    set_revoked_state,
)
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

_logger = logging.getLogger(__name__)

# Frequent enough that a dynamic-IP board's record doesn't stay stale
# for long, infrequent enough not to hammer the managed service --
# a reasoned default, not fixed by the design doc itself (same
# "implementation-time parameter" latitude as the service's own
# age-gate/cooldown constants).
_DEFAULT_INTERVAL_SECONDS = 15 * 60


async def run_scheduled_managed_dns_updater(
    db: Database, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
) -> None:
    """
    Runs for the node's lifetime: sends one heartbeat immediately on
    entry (if this node currently has something to heartbeat), then
    every `interval_seconds` (default 15 minutes) after -- mirrors
    `netbbs.link.reliable_nodes.run_scheduled_reliable_nodes_refresh`'s exact shape:
    plain `db`, not a `DatabaseLane` (the same accepted brief-blocking-
    cost precedent for a periodic task touching only small, fast local
    config reads/writes, the one network call aside), and a fresh
    per-pass check of whether there's anything to do rather than a
    static enable/disable decision made once at task-creation time --
    this is what lets a SysOp who registers *after* this node already
    started (e.g. via the admin screen in a later phase, having
    initially declined the opt-in prompt) start getting heartbeats on
    the very next pass, with no restart required.

    A no-op pass whenever the opt-in decision isn't `ACCEPTED`, no name
    is registered, the service URL isn't configured, or the credential
    file is missing -- every one of these just means "nothing to
    heartbeat yet," not a failure. A failed heartbeat call
    (`ManagedDnsError`) logs and leaves this node's cached status/
    last-contact state untouched -- the same "a stale reachability claim
    only ever costs a failed connection attempt" tolerance
    `run_scheduled_reliable_nodes_refresh` already established for its own fetch
    failures.
    """
    while True:
        async with managed_dns_transition_lock(db.path):
            await _run_managed_dns_update_pass(db)
        await sleep(interval_seconds)


# Which foreign issuer each node database has already been warned about
# -- the pass runs every 15 minutes and the condition is an operator
# setting that will not change on its own, so one line per actual change
# is the whole of what is worth saying.
_reported_foreign_credentials: dict[Path, tuple[str, str]] = {}


def _report_foreign_credential(db_path: Path, issuer: str, base_url: str) -> None:
    if _reported_foreign_credentials.get(db_path) == (issuer, base_url):
        return
    _reported_foreign_credentials[db_path] = (issuer, base_url)
    _logger.warning(
        "managed-DNS updates are paused: this node's registration credential was issued by %s "
        "but its configured service is %s. Register with the new service, or point the node back "
        "at %s, from the SysOp console's DNS screen.",
        issuer, base_url, issuer,
    )


async def _run_managed_dns_update_pass(db: Database) -> None:
    """Heartbeat and reconcile one credential generation under its lock."""
    if get_opt_in(db) is not OptIn.ACCEPTED:
        return
    recover_credential_transition(db.path)
    name = get_registered_name(db)
    base_url = get_service_url(db)
    status = get_registration_status(db)
    previous_credential = load_credential(previous_credential_path_for(db.path))
    # An abandoned replacement can coexist with a still-live previous
    # name after the old heartbeat failed transiently. Keep servicing
    # that outstanding rename so the next successful old heartbeat
    # can restore the usable registration instead of stranding it.
    has_outstanding_rename = previous_credential is not None and get_previous_name(db) is not None
    if name is None or base_url is None or status in (RegistrationStatus.RELEASED, RegistrationStatus.REVOKED):
        # Released is the SysOp's own decision to stop (design doc §16
        # Decision 5) and revoked is the operator's (Decision 4): nothing
        # here overrides either. The SysOp's next `[R]egister` is where
        # both end.
        return
    credential = load_credential(credential_path_for(db.path))
    if credential is None:
        return
    issuer = foreign_credential_service_url(db, base_url)
    if issuer is not None:
        # This node's credential was issued by a different managed-DNS
        # service (its address is an operator setting since issue #583).
        # Heartbeating it here would hand that service's bearer secret to
        # this one, so the pass stops instead -- the registration at the
        # issuer is left to lapse on its own, and the SysOp is told what
        # to do about it the moment they touch the DNS screen.
        _report_foreign_credential(db.path, issuer, base_url)
        return
    if status is RegistrationStatus.ABANDONED and not has_outstanding_rename:
        note = get_recovery_note(db)
        if note is not None and note.final:
            # A 409 already said this credential can never reclaim the
            # name; asking again every pass changes nothing. The SysOp's
            # `[R]egister` is the way forward, and the screen says so.
            return
        # Design doc §16 Decision 10 (issue #600): abandonment is the
        # service noticing this node was away, not the SysOp choosing to
        # leave, so the node gets its name back by itself when it
        # returns -- the same reclaim `[R]egister` would perform, made
        # every pass until it works or the SysOp acts. Before this a
        # node back from a fortnight's outage stayed dark until somebody
        # happened to open the DNS screen, and lost the name for good
        # once the cooldown purged it.
        if not await _reclaim_abandoned_name(db, base_url, name, credential):
            return
        status = get_registration_status(db)
    previous_result = None
    previous_inactive = False
    if previous_credential is not None:
        previous_result, previous_inactive = await _send_heartbeat(
            base_url, previous_credential
        )
    result, primary_inactive = await _send_heartbeat(base_url, credential)
    if isinstance(primary_inactive, ManagedDnsError):
        # Design doc §16 Decision 4: the operator took the name. The
        # service takes both halves of a rename, so this is the whole
        # answer for the previous name too, and nothing is retried --
        # the state is terminal until the SysOp registers something else.
        _apply_revocation(db, name=name, contact=primary_inactive.contact)
        return
    if result is not None:
        _apply_heartbeat_result(
            db, result, previous_result=previous_result,
            has_previous_credential=previous_credential is not None,
            previous_inactive=previous_inactive,
        )
    elif primary_inactive and previous_result is not None and previous_credential is not None:
        cancellation, rename_absent = await _cancel_remote_rename(
            base_url, previous_credential
        )
        if cancellation is not None:
            _apply_cancelled_rename_result(db, cancellation)
        elif rename_absent:
            # A 401 from cancellation means the relationship is already gone
            # (for example, cancellation completed before a node crash), so
            # the working previous heartbeat can safely repair local state.
            _apply_heartbeat_result(
                db, previous_result, previous_result=None,
                has_previous_credential=False,
            )
        else:
            # The service may still retain the abandoned replacement. Keep the
            # transition visible and both credentials recoverable so a later
            # pass or the SysOp can retry cancellation.
            _preserve_outstanding_rename(
                db, name=name, previous_result=previous_result,
            )
        if cancellation is not None or rename_absent:
            # Commit the recovered service truth while both files still exist,
            # then journal the reverse swap. A crash at any point leaves either
            # the fallback credential or a replayable journal.
            stage_credential_cancellation(db.path, previous_credential)
            recover_credential_transition(db.path)
    elif previous_result is not None and previous_credential is not None:
        # The previous name can mature or be republished even when the
        # replacement heartbeat times out. Preserve the unknown primary state,
        # but do not discard authoritative progress from the successful old
        # credential.
        _apply_previous_heartbeat_result(db, name=name, result=previous_result)
    elif primary_inactive or previous_inactive:
        # Each 401 is authoritative independently of the other
        # request's outcome. Reconcile both cached identities in
        # one transaction, even if the other heartbeat merely
        # failed transiently.
        _apply_inactive_heartbeat_results(
            db,
            name=name,
            primary_inactive=primary_inactive,
            previous_inactive=(
                previous_inactive and previous_credential is not None
            ),
        )


# The last automatic-reclaim failure each node database was warned about
# -- the pass runs every 15 minutes and the answer rarely changes between
# passes, so one log line per actual change.
_reported_reclaim_failures: dict[Path, tuple[str, str]] = {}


async def _reclaim_abandoned_name(db: Database, base_url: str, name: str, credential: str) -> bool:
    """One automatic reclaim attempt for an abandoned name. Returns
    whether the registration is active again locally.

    `/reclaim` is the whole safety of this: the service performs the
    reclaim this credential entitles the node to, or refuses. It never
    registers afresh on the node's behalf -- a fresh registration mints
    a credential, spends a rate-limit token and, once the cooldown has
    purged the row, is for a name that may no longer be this node's; all
    of that stays a SysOp's keystroke -- and a service too old to have
    the route answers 404, which fails closed here like any refusal
    (Codex review of PR #608). A refusal is recorded as a recovery note
    so the DNS screen can say what was tried and why it did not work,
    and is retried next pass: capacity frees, services come back, and
    the SysOp may act in between. One refusal is not retried: a row the
    service says is *released* means the local `abandoned` was stale (a
    backup restored from before the SysOp's own release), and the node
    adopts the service's word rather than undoing a decision."""
    try:
        # Direct connection, as for the heartbeat: a matured reclaim
        # republishes the record at the address this request arrives
        # from, and through a forward proxy that would be the proxy.
        async with ClientSession(trust_env=False) as session:
            result = await reclaim(
                session, base_url, name=name, credential=credential, dynamic=get_dynamic(db),
            )
    except ManagedDnsError as exc:
        if exc.revoked:
            # Not a failed reclaim to retry: the operator took the name
            # (design doc §16 Decision 4), and the automatic path ends
            # exactly where the manual one does.
            _apply_revocation(db, name=name, contact=exc.contact)
            return False
        if exc.service_status == "released":
            set_heartbeat_reconciliation_state(
                db, name=name, status=RegistrationStatus.RELEASED, published=False,
                last_contact_at=None, previous_name=None, previous_status=None, previous_published=False,
            )
            _reported_reclaim_failures.pop(db.path, None)
            _logger.info(
                "Managed-DNS registration %r is released at the service; the node's abandoned view was "
                "stale and has been corrected", name,
            )
            return False
        _report_reclaim_failure(db, name, str(exc), final=exc.status_code == 409)
        return False
    if result.credential != credential:
        # `/reclaim` never mints, so this is a service that is not this
        # project's answering something else. Fail closed: adopting an
        # unknown credential would make a background task the author of
        # a registration nobody asked for (Codex review of PR #608).
        _report_reclaim_failure(db, name, "the service answered with a different credential; not adopted")
        return False
    set_registration_result_state(
        db, name=result.name, status=RegistrationStatus(result.status),
        dynamic=get_dynamic(db), service_url=base_url,
    )
    _reported_reclaim_failures.pop(db.path, None)
    _logger.info("Managed-DNS registration %r reclaimed automatically after abandonment", name)
    return True


def _report_reclaim_failure(db: Database, name: str, detail: str, *, final: bool = False) -> None:
    set_recovery_note(db, RecoveryNote(at=utc_now_iso(), text=detail, final=final))
    if _reported_reclaim_failures.get(db.path) == (name, detail):
        return
    _reported_reclaim_failures[db.path] = (name, detail)
    _logger.warning(
        "Managed-DNS registration %r is abandoned and could not be reclaimed automatically: %s "
        "(%s; [R]egister on the SysOp console's DNS screen registers afresh)",
        name, detail, "not retrying -- the refusal is final" if final else "retrying every pass",
    )


def _apply_previous_heartbeat_result(
    db: Database, *, name: str, result: HeartbeatResult,
) -> None:
    """Apply a successful old-name heartbeat without guessing primary state."""
    previous_name = get_previous_name(db)
    if previous_name is None or result.name != previous_name:
        return
    set_heartbeat_reconciliation_state(
        db,
        name=name,
        status=get_registration_status(db),
        published=get_published(db),
        last_contact_at=utc_now_iso(),
        previous_name=previous_name,
        previous_status=RegistrationStatus(result.status),
        previous_published=result.last_known_address is not None,
    )


async def _send_heartbeat(
    base_url: str, credential: str,
) -> tuple[HeartbeatResult | None, bool | ManagedDnsError]:
    """`(result, inactive)`. `inactive` is truthy for the authoritative
    401 every caller already acts on; it is the `ManagedDnsError` itself
    when that 401's body says the credential's registration was
    *revoked* (design doc §16 Decision 4), so the contact channel it
    names travels with it -- read from `ManagedDnsError.service_status`,
    never from the message text. Tests substitute this function with
    fakes returning plain bools, which stay valid."""
    try:
        # trust_env=True: honor HTTP_PROXY/HTTPS_PROXY/NO_PROXY, same as
        # every other outbound call this project makes to project-
        # operated infrastructure (see netbbs.managed_dns.client's own
        # docstring for the full worklog citation).
        # The service derives the node address from the TCP peer. A
        # forward proxy would make that peer the proxy, so this request
        # must always use a direct connection.
        async with ClientSession(trust_env=False) as session:
            result = await heartbeat(session, base_url, credential=credential)
    except ManagedDnsError as exc:
        _logger.warning("Managed-DNS heartbeat failed: %s", exc)
        inactive = exc.status_code == 401
        return None, (exc if inactive and exc.revoked else inactive)
    return result, False


def _apply_revocation(db: Database, *, name: str, contact: str | None) -> None:
    set_revoked_state(db, name=name, contact=contact)
    _reported_reclaim_failures.pop(db.path, None)
    _logger.warning(
        "Managed-DNS registration %r was revoked by the service operator%s",
        name, f" -- to dispute it, contact {contact}" if contact else "",
    )


async def _cancel_remote_rename(
    base_url: str, previous_credential: str,
) -> tuple[CancelRenameResult | None, bool]:
    """Cancel a retained remote rename; report whether it was already absent."""
    try:
        async with ClientSession(trust_env=False) as session:
            result = await cancel_rename(
                session, base_url, credential=previous_credential,
            )
    except ManagedDnsError as exc:
        _logger.warning("Managed-DNS automatic rename cancellation failed: %s", exc)
        return None, exc.status_code == 401
    return result, False


def _apply_cancelled_rename_result(
    db: Database, result: CancelRenameResult,
) -> None:
    """Apply the service's authoritative state after automatic cancellation."""
    set_heartbeat_reconciliation_state(
        db,
        name=result.previous_name,
        status=RegistrationStatus(result.previous_status),
        published=result.previous_last_known_address is not None,
        last_contact_at=utc_now_iso(),
        previous_name=None,
        previous_status=None,
        previous_published=False,
    )


def _preserve_outstanding_rename(
    db: Database, *, name: str, previous_result: HeartbeatResult,
) -> None:
    """Record both heartbeat truths without hiding an uncancelled rename."""
    previous_name = get_previous_name(db)
    if previous_name is None or previous_result.name != previous_name:
        return
    set_heartbeat_reconciliation_state(
        db,
        name=name,
        status=RegistrationStatus.ABANDONED,
        published=False,
        last_contact_at=utc_now_iso(),
        previous_name=previous_name,
        previous_status=RegistrationStatus(previous_result.status),
        previous_published=previous_result.last_known_address is not None,
    )


def _apply_inactive_heartbeat_results(
    db: Database, *, name: str, primary_inactive: bool, previous_inactive: bool,
) -> None:
    """Atomically apply independently authoritative inactive responses."""
    previous_name = get_previous_name(db)
    previous_was_inactive = previous_inactive and previous_name is not None
    set_heartbeat_reconciliation_state(
        db,
        name=name,
        status=(
            RegistrationStatus.ABANDONED
            if primary_inactive else get_registration_status(db)
        ),
        published=False if primary_inactive else get_published(db),
        # No heartbeat succeeded, so preserve the last successful-contact
        # timestamp while atomically withdrawing whichever registrations the
        # service authoritatively rejected.
        last_contact_at=get_last_contact_at(db),
        previous_name=previous_name,
        previous_status=(
            RegistrationStatus.ABANDONED
            if previous_was_inactive else get_previous_status(db)
        ),
        previous_published=(
            False if previous_was_inactive else get_previous_published(db)
        ),
    )


def _apply_heartbeat_result(
    db: Database, result: HeartbeatResult, *, previous_result: HeartbeatResult | None,
    has_previous_credential: bool, previous_inactive: bool = False,
) -> None:
    """Apply authoritative service state and repair an interrupted local rename."""
    previous_name = result.previous_name
    if previous_name is None and previous_result is not None and previous_result.name != result.name:
        previous_name = previous_result.name

    previous_status = get_previous_status(db)
    previous_published = get_previous_published(db)
    delete_previous_credential = False
    if result.status == RegistrationStatus.MATURED.value:
        previous_name = None
        previous_status = None
        previous_published = False
        delete_previous_credential = has_previous_credential
    elif previous_name is not None:
        if previous_inactive:
            previous_status = RegistrationStatus.ABANDONED
            previous_published = False
        elif previous_result is not None:
            previous_status = RegistrationStatus(previous_result.status)
            previous_published = previous_result.last_known_address is not None
    elif has_previous_credential:
        # The file can be left behind if a crash happens after copying the old
        # credential but before installing a replacement. Both heartbeats then
        # authenticate the same registration, so the extra copy is redundant.
        previous_name = None
        previous_status = None
        previous_published = False
        delete_previous_credential = True

    set_heartbeat_reconciliation_state(
        db,
        name=result.name,
        status=RegistrationStatus(result.status),
        # A reported address is the service's confirmation that a record is
        # actually published; `matured` alone is not.
        published=result.last_known_address is not None,
        last_contact_at=utc_now_iso(),
        previous_name=previous_name,
        previous_status=previous_status,
        previous_published=previous_published,
    )
    if delete_previous_credential:
        delete_credential(previous_credential_path_for(db.path))

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
    ContactProblem,
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
    set_contact_problem,
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
    file is missing -- on a node that has not registered, every one of
    these just means "nothing to heartbeat yet," not a failure. Once a
    name *is* registered each of them is one (issue #640): the name
    never goes live and is swept a week later while the DNS screen shows
    a healthy registration, so the pass records a `ContactProblem` for
    that screen instead of returning silently. A failed heartbeat call
    (`ManagedDnsError`) is recorded the same way and otherwise leaves
    this node's cached status/last-contact state untouched -- the same
    "a stale reachability claim only ever costs a failed connection
    attempt" tolerance `run_scheduled_reliable_nodes_refresh` already
    established for its own fetch failures.

    A pass that *raises* does not end the task. It used to, for the rest
    of the node's uptime, and what raises here is ordinary: the
    credential is a 0600 file, so one written by `netbbs.admin` run as
    another account (root, on the documented deployment) cannot be read
    by the node's, and the standalone console is a second writer that
    can hold the database past `busy_timeout`. Both are repaired without
    a restart, so the next pass has to be there to notice.
    """
    while True:
        try:
            async with managed_dns_transition_lock(db.path):
                await _run_managed_dns_update_pass(db)
        except Exception as exc:
            _report_failed_pass(db, exc)
        await sleep(interval_seconds)


# The last failed-pass error each node database was warned about -- one
# traceback per actual change, as for the other per-pass reports here.
_reported_failed_passes: dict[Path, str] = {}


def _report_failed_pass(db: Database, exc: Exception) -> None:
    detail = f"{type(exc).__name__}: {exc}"
    if _reported_failed_passes.get(db.path) != detail:
        _reported_failed_passes[db.path] = detail
        _logger.error("managed-DNS updater pass failed; retrying next pass", exc_info=exc)
    try:
        _note_contact_problem(db, f"this node's check-in failed before it was sent ({detail})", log=False)
    except Exception:
        # The database itself may be what failed; the log line above is
        # then the whole of what can be said, and the task still lives.
        _logger.debug("could not record the failed managed-DNS pass", exc_info=True)


_CONTACT_EXPECTED_STATUSES = (RegistrationStatus.PENDING, RegistrationStatus.MATURED)

# The last contact problem each node database was warned about.
_reported_contact_problems: dict[Path, tuple[str, str]] = {}


def _note_contact_problem(db: Database, text: str, *, log: bool = True) -> None:
    """Record why this pass sent no heartbeat, or sent one that never
    arrived, for a name the service is waiting to hear from -- shown on
    the SysOp console's DNS screen. A no-op on a node with nothing the
    service expects contact for: never registered, released, revoked, or
    abandoned (whose own reclaim path keeps a `RecoveryNote`), where the
    same condition is not a fault."""
    name = get_registered_name(db)
    if name is None or get_registration_status(db) not in _CONTACT_EXPECTED_STATUSES:
        return
    set_contact_problem(db, ContactProblem(at=utc_now_iso(), text=text))
    if not log or _reported_contact_problems.get(db.path) == (name, text):
        return
    _reported_contact_problems[db.path] = (name, text)
    _logger.warning(
        "Managed-DNS registration %r is not being kept alive: %s. Until a check-in succeeds the name "
        "stays out of DNS, or is taken out of it after about a week.", name, text,
    )


def _unreadable_credential_text(db_path: Path, exc: OSError) -> str:
    return (
        f"the registration's credential file beside the database ({credential_path_for(db_path).name}) "
        f"could not be read: {exc.strerror or exc}. It is an owner-only file, so it must belong to the "
        "account the node runs as -- one written by running netbbs.admin as a different account does not"
    )


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
    global _last_heartbeat_failure
    _last_heartbeat_failure = None
    if get_opt_in(db) is not OptIn.ACCEPTED:
        # Registering records the decision in the same transaction as
        # the name, so this pairing is a restored or hand-edited
        # database -- but it is what issue #640 was filed as, and saying
        # so costs one line.
        _note_contact_problem(
            db, "managed DNS is not opted in on this node although a name is registered; [R]egister on "
            "the SysOp console's DNS screen records the decision again",
        )
        return
    try:
        recover_credential_transition(db.path)
        previous_credential = load_credential(previous_credential_path_for(db.path))
        credential = load_credential(credential_path_for(db.path))
    except OSError as exc:
        _note_contact_problem(db, _unreadable_credential_text(db.path, exc))
        return
    name = get_registered_name(db)
    base_url = get_service_url(db)
    status = get_registration_status(db)
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
        if base_url is None:
            _note_contact_problem(db, "no managed-DNS service address is configured on this node")
        return
    if credential is None:
        _note_contact_problem(
            db, f"the registration's credential file beside the database "
            f"({credential_path_for(db.path).name}) is missing; restore it from a backup, or [R]egister "
            "again once the service has freed the name",
        )
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
        _note_contact_problem(
            db, f"this node's credential was issued by {issuer} but the node is configured to use "
            f"{base_url}, so check-ins are paused rather than present it there", log=False,
        )
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
    if isinstance(previous_inactive, ManagedDnsError) and not isinstance(primary_inactive, ManagedDnsError):
        # A revocation takes both halves of a rename, so hearing it on
        # the old credential is the whole answer even while the new
        # one's heartbeat merely failed transiently (Codex review of PR
        # #609); without this the generic path below would record the
        # old name as abandoned and keep retrying.
        primary_inactive = previous_inactive
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
        if isinstance(rename_absent, ManagedDnsError):
            # The cancellation was answered "revoked" (Codex review of PR
            # #609): not a rename gone, a name taken. Treating it as
            # absent would restore the old name from its last good
            # heartbeat and advertise a name the operator removed.
            _apply_revocation(db, name=name, contact=rename_absent.contact)
            return
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
    else:
        # No usable answer. `_send_heartbeat` has logged it; the DNS
        # screen is where a SysOp who was told to wait will look.
        _note_contact_problem(db, failed_check_in_text(_last_heartbeat_failure), log=False)


# Why the most recent `_send_heartbeat` failed, kept for the one pass
# that reads it straight afterwards. A module global rather than a third
# return value because tests substitute `_send_heartbeat` with two-tuple
# fakes, and one updater runs per process.
_last_heartbeat_failure: ManagedDnsError | None = None


def failed_check_in_text(exc: ManagedDnsError | None) -> str:
    """The sentence for a check-in that got no usable answer. For one
    that never reached the service at all, the second half is the part a
    SysOp cannot guess: `/register` honours `HTTPS_PROXY`, the heartbeat
    deliberately does not (the service publishes the address it arrives
    from, which through a forward proxy is the proxy's), so a node whose
    only way out is a proxy registers without trouble and can then never
    check in (issue #640)."""
    if exc is None:
        return "the service could not be reached"
    if exc.status_code is not None:
        return str(exc)
    return (
        f"{exc}. Check-ins connect to the service directly, never through an HTTP proxy, because the "
        "service publishes the address they arrive from"
    )


async def check_in_now(base_url: str, credential: str) -> tuple[HeartbeatResult | None, str | None]:
    """One heartbeat outside the schedule, for the register flow to send
    the moment a registration succeeds: `(result, None)`, or `(None,
    why)`. Network only; `record_check_in` is the database half.

    Registering used to end on "it will go live once this node has
    stayed in contact", with the first contact up to 15 minutes away and
    its failure visible only in the log -- so a SysOp whose node could
    never check in was told to wait for something that was not going to
    happen (issue #640). Sent from wherever `[R]egister` was pressed,
    which for the standalone console is not the node's own process; it is
    the same host, which is all the service reads from it."""
    try:
        async with ClientSession(trust_env=False) as session:
            return await heartbeat(session, base_url, credential=credential), None
    except ManagedDnsError as exc:
        _logger.warning("Managed-DNS check-in after registering failed: %s", exc)
        return None, failed_check_in_text(exc)


def record_check_in(db: Database, result: HeartbeatResult | None, problem: str | None) -> None:
    """Apply `check_in_now`'s outcome. A failure is only ever recorded,
    never acted on: what a 401 means for a rename or a revocation is the
    scheduled pass's business, with both credentials in hand."""
    if result is not None:
        _apply_heartbeat_result(db, result, previous_result=None, has_previous_credential=False)
    elif problem is not None:
        _note_contact_problem(db, problem, log=False)


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
    global _last_heartbeat_failure
    _last_heartbeat_failure = None
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
        _last_heartbeat_failure = exc
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
) -> tuple[CancelRenameResult | None, bool | ManagedDnsError]:
    """Cancel a retained remote rename; report whether it was already
    absent. The same contract as `_send_heartbeat`: the second element
    is the error itself when the 401 says the credential's registration
    was revoked, so the caller can tell a rename gone from a name taken.
    Tests substitute this with fakes returning plain bools."""
    try:
        async with ClientSession(trust_env=False) as session:
            result = await cancel_rename(
                session, base_url, credential=previous_credential,
            )
    except ManagedDnsError as exc:
        _logger.warning("Managed-DNS automatic rename cancellation failed: %s", exc)
        absent = exc.status_code == 401
        return None, (exc if absent and exc.revoked else absent)
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

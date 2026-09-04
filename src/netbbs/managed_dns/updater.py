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

import asyncio
from aiohttp import ClientSession

from netbbs.managed_dns.client import HeartbeatResult, ManagedDnsError, heartbeat
from netbbs.managed_dns.credential import (
    credential_path_for, delete_credential, load_credential, previous_credential_path_for,
    recover_credential_transition, save_credential,
)
from netbbs.managed_dns.state import (
    OptIn,
    RegistrationStatus,
    get_opt_in,
    get_registered_name,
    get_registration_status,
    get_service_url,
    set_last_contact_at,
    set_previous_name,
    set_previous_published,
    set_previous_status,
    set_published,
    set_registered_name,
    set_registration_status,
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
        if get_opt_in(db) is OptIn.ACCEPTED:
            recover_credential_transition(db.path)
            name = get_registered_name(db)
            base_url = get_service_url(db)
            status = get_registration_status(db)
            if name is not None and base_url is not None and status not in (
                RegistrationStatus.RELEASED, RegistrationStatus.ABANDONED,
            ):
                credential = load_credential(credential_path_for(db.path))
                if credential is not None:
                    previous_credential = load_credential(previous_credential_path_for(db.path))
                    previous_result = None
                    previous_inactive = False
                    if previous_credential is not None:
                        previous_result, previous_inactive = await _send_heartbeat(
                            base_url, previous_credential
                        )
                    result, primary_inactive = await _send_heartbeat(base_url, credential)
                    if result is not None:
                        _apply_heartbeat_result(
                            db, result, previous_result=previous_result,
                            has_previous_credential=previous_credential is not None,
                            previous_inactive=previous_inactive,
                        )
                    elif primary_inactive and previous_result is not None and previous_credential is not None:
                        save_credential(credential_path_for(db.path), previous_credential)
                        delete_credential(previous_credential_path_for(db.path))
                        set_previous_name(db, None)
                        set_previous_status(db, None)
                        _apply_heartbeat_result(
                            db, previous_result, previous_result=None,
                            has_previous_credential=False,
                        )
                    elif primary_inactive:
                        # A 401 is authoritative service state, unlike a
                        # transient transport/provider failure. Preserve the
                        # bearer secrets for reclaim/cancellation, but stop
                        # advertising registrations the service withdrew.
                        set_registration_status(db, RegistrationStatus.ABANDONED)
                        set_published(db, False)
                        if previous_inactive and previous_credential is not None:
                            set_previous_status(db, RegistrationStatus.ABANDONED)
                            set_previous_published(db, False)
        await sleep(interval_seconds)


async def _send_heartbeat(
    base_url: str, credential: str,
) -> tuple[HeartbeatResult | None, bool]:
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
        return None, exc.status_code == 401
    return result, False


def _apply_heartbeat_result(
    db: Database, result: HeartbeatResult, *, previous_result: HeartbeatResult | None,
    has_previous_credential: bool, previous_inactive: bool = False,
) -> None:
    """Apply authoritative service state and repair an interrupted local rename."""
    set_registered_name(db, result.name)
    set_registration_status(db, RegistrationStatus(result.status))
    # A reported address is the service's confirmation that a record is
    # actually published; `matured` alone is not (see state.get_published).
    set_published(db, result.last_known_address is not None)
    set_last_contact_at(db, utc_now_iso())

    previous_name = result.previous_name
    if previous_name is None and previous_result is not None and previous_result.name != result.name:
        previous_name = previous_result.name

    if result.status == RegistrationStatus.MATURED.value:
        set_previous_name(db, None)
        set_previous_status(db, None)
        set_previous_published(db, False)
        if has_previous_credential:
            delete_credential(previous_credential_path_for(db.path))
    elif previous_name is not None:
        set_previous_name(db, previous_name)
        if previous_inactive:
            set_previous_status(db, RegistrationStatus.ABANDONED)
            set_previous_published(db, False)
        elif previous_result is not None:
            set_previous_status(db, RegistrationStatus(previous_result.status))
            set_previous_published(db, previous_result.last_known_address is not None)
    elif has_previous_credential:
        # The file can be left behind if a crash happens after copying the old
        # credential but before installing a replacement. Both heartbeats then
        # authenticate the same registration, so the extra copy is redundant.
        set_previous_name(db, None)
        set_previous_status(db, None)
        set_previous_published(db, False)
        delete_credential(previous_credential_path_for(db.path))

"""
Node-wide managed-DNS registration state (design doc §16, issue #201).

A handful of plain scalars -- opt-in decision, chosen subdomain label,
last-known registration status, last successful contact time -- so this
follows `netbbs.config`'s own `get_config`/`set_config` key-value
convention directly (same shape as `RegistrationMode`/
`registration_mode`) rather than a dedicated table: nothing here needs
`node_config`'s own row-per-key generality beyond what a handful of
typed wrapper functions already provide. The one piece of durable state
that does *not* belong here is the credential itself -- see
`netbbs.managed_dns.credential`'s own docstring for why.
"""

from __future__ import annotations

from enum import Enum

from netbbs.config import get_config, set_config
from netbbs.storage.database import Database


class OptIn(str, Enum):
    """A node's decision on the managed-DNS opt-in prompt (design doc
    §16 Decision 1) -- a tri-state, not a boolean, so "never asked yet"
    is distinguishable from "asked and declined." The prompt shown at
    first-SysOp bootstrap and, as a fallback, first SysOp login, checks
    only whether this is still `UNDECIDED` -- the decision is node-wide,
    not per-user, so that single check is sufficient to guarantee the
    prompt fires exactly once regardless of which of the two call sites
    gets there first."""

    UNDECIDED = "undecided"
    ACCEPTED = "accepted"
    DECLINED = "declined"


class RegistrationStatus(str, Enum):
    """This node's own last-known view of its registration, as reported
    by the managed service's responses -- mirrors the status values the
    backend's own `registrations.status` column can hold (`services.
    managed_dns.store`), plus `NONE` for "never registered." Advisory
    only: the managed service's own record is authoritative; this is
    what the SysOp-facing status screen shows without necessarily making
    a live call on every view."""

    NONE = "none"
    PENDING = "pending"
    MATURED = "matured"
    RELEASED = "released"
    ABANDONED = "abandoned"


OPT_IN_CONFIG_KEY = "managed_dns_opt_in"
NAME_CONFIG_KEY = "managed_dns_name"
STATUS_CONFIG_KEY = "managed_dns_status"
LAST_CONTACT_AT_CONFIG_KEY = "managed_dns_last_contact_at"
DYNAMIC_CONFIG_KEY = "managed_dns_dynamic"
NODE_FINGERPRINT_CONFIG_KEY = "managed_dns_node_fingerprint"
SERVICE_URL_CONFIG_KEY = "managed_dns_service_url"
PREVIOUS_NAME_CONFIG_KEY = "managed_dns_previous_name"
PREVIOUS_STATUS_CONFIG_KEY = "managed_dns_previous_status"


def get_opt_in(db: Database) -> OptIn:
    value = get_config(db, OPT_IN_CONFIG_KEY)
    return OptIn(value) if value is not None else OptIn.UNDECIDED


def set_opt_in(db: Database, decision: OptIn) -> None:
    set_config(db, OPT_IN_CONFIG_KEY, decision.value)


def get_node_fingerprint(db: Database) -> str | None:
    """This node's own `NodeIdentity.fingerprint` (design doc §16
    Decision 3's `node_fingerprint`, the value the managed service's
    one-name-per-node cap keys on) -- a cached copy, not the source of
    truth. `NodeIdentity` itself is always loaded/bootstrapped at node
    startup regardless of whether Link is enabled (see `netbbs.
    __main__.run`'s own comment on that), but its fingerprint isn't
    otherwise reachable from `netbbs.net.login_flow`/`netbbs.admin`
    without threading a new parameter through `handle_session`/
    `handle_ssh_session`/`run_authenticated_session` purely for this
    one Link-independent feature -- `netbbs.__main__.run` writes this
    cache once, synchronously, right where it already loads the real
    `NodeIdentity`, the same place `load_link_node`'s own direct-`db`
    write already happens at that point in startup. Safe to cache:
    the root key this is derived from doesn't rotate (only the
    *operational* signing/transport keys do), so this can't silently go
    stale across a node's lifetime the way a mutable setting could.
    `None` only ever means "this node hasn't started up since this
    feature was added yet" -- self-healing on the very next startup."""
    return get_config(db, NODE_FINGERPRINT_CONFIG_KEY)


def set_node_fingerprint(db: Database, fingerprint: str) -> None:
    set_config(db, NODE_FINGERPRINT_CONFIG_KEY, fingerprint)


def get_service_url(db: Database) -> str | None:
    """The managed-DNS service's own base URL (e.g.
    `"https://managed.netbbs.org"`) -- `None` until an operator
    configures it. Deliberately not a hardcoded default: this project
    runs one instance of `services.managed_dns`, but its real production
    address is an operational decision independent of this client code,
    the same "which DNS provider... is implementation-time detail, not
    blocking" reasoning design doc §16 already applies to the DNS
    provider choice itself. `set_service_url` stores `None` as `""`
    (same "empty string means None" convention as `set_registered_
    name`), so this translates it back rather than ever returning an
    empty string a caller never actually set."""
    value = get_config(db, SERVICE_URL_CONFIG_KEY)
    return value or None


def set_service_url(db: Database, url: str | None) -> None:
    set_config(db, SERVICE_URL_CONFIG_KEY, url or "")


def get_registered_name(db: Database) -> str | None:
    """The subdomain label this node has registered (e.g. `"myboard"`
    for `myboard.netbbs.org`), or `None` if it never has (or has been
    explicitly cleared -- `set_registered_name` stores `None` as `""`,
    same "empty string means None" convention `get_invitation_expiry_
    days` already uses, so this must translate it back rather than
    return the empty string a caller never actually set)."""
    value = get_config(db, NAME_CONFIG_KEY)
    return value or None


def set_registered_name(db: Database, name: str | None) -> None:
    set_config(db, NAME_CONFIG_KEY, name or "")


def get_previous_name(db: Database) -> str | None:
    return get_config(db, PREVIOUS_NAME_CONFIG_KEY) or None


def set_previous_name(db: Database, name: str | None) -> None:
    set_config(db, PREVIOUS_NAME_CONFIG_KEY, name or "")


def get_previous_status(db: Database) -> RegistrationStatus | None:
    value = get_config(db, PREVIOUS_STATUS_CONFIG_KEY)
    return RegistrationStatus(value) if value else None


def set_previous_status(db: Database, status: RegistrationStatus | None) -> None:
    set_config(db, PREVIOUS_STATUS_CONFIG_KEY, status.value if status else "")


def get_registration_status(db: Database) -> RegistrationStatus:
    value = get_config(db, STATUS_CONFIG_KEY)
    return RegistrationStatus(value) if value else RegistrationStatus.NONE


def set_registration_status(db: Database, status: RegistrationStatus) -> None:
    set_config(db, STATUS_CONFIG_KEY, status.value)


def get_last_contact_at(db: Database) -> str | None:
    """ISO 8601 timestamp of this node's last successful contact with
    the managed service, or `None` if it has never successfully
    contacted it."""
    return get_config(db, LAST_CONTACT_AT_CONFIG_KEY)


def set_last_contact_at(db: Database, timestamp: str) -> None:
    set_config(db, LAST_CONTACT_AT_CONFIG_KEY, timestamp)


def get_dynamic(db: Database) -> bool:
    """Whether this registration should track this node's current
    public address (the "dynamic DNS" half) as opposed to a static
    board that only wanted the friendly subdomain name (design doc §16's
    own "a board could plausibly want the first without the second")."""
    return get_config(db, DYNAMIC_CONFIG_KEY) == "1"


def set_dynamic(db: Database, dynamic: bool) -> None:
    set_config(db, DYNAMIC_CONFIG_KEY, "1" if dynamic else "0")

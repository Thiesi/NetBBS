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

import ipaddress
import json
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

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
    #: The service operator took the name away (design doc §16 Decision
    #: 4). Terminal for this node: the updater stops, `[R]egister` with
    #: the same name is refused for the cooldown, and the screen says
    #: so and names the contact channel the service gave.
    REVOKED = "revoked"


OPT_IN_CONFIG_KEY = "managed_dns_opt_in"
NAME_CONFIG_KEY = "managed_dns_name"
STATUS_CONFIG_KEY = "managed_dns_status"
LAST_CONTACT_AT_CONFIG_KEY = "managed_dns_last_contact_at"
DYNAMIC_CONFIG_KEY = "managed_dns_dynamic"
NODE_FINGERPRINT_CONFIG_KEY = "managed_dns_node_fingerprint"
SERVICE_URL_CONFIG_KEY = "managed_dns_service_url"
PREVIOUS_NAME_CONFIG_KEY = "managed_dns_previous_name"
PREVIOUS_STATUS_CONFIG_KEY = "managed_dns_previous_status"
PUBLISHED_CONFIG_KEY = "managed_dns_published"
PREVIOUS_PUBLISHED_CONFIG_KEY = "managed_dns_previous_published"
CREDENTIAL_SERVICE_URL_CONFIG_KEY = "managed_dns_credential_service_url"
LISTENERS_CONFIG_KEY = "managed_dns_listeners"
RECOVERY_NOTE_CONFIG_KEY = "managed_dns_recovery_note"
SERVICE_CONTACT_CONFIG_KEY = "managed_dns_service_contact"
ADMIN_TOKEN_CONFIG_KEY = "managed_dns_admin_token"

# The address of the project's own `services.managed_dns` instance, as
# shipped -- what a node reaches when its operator configures nothing,
# which is every ordinary node (design doc §16 Decision 8). `None` while
# that backend is not standing anywhere: a node then simply has no
# service to talk to, which `netbbs.net.managed_dns_flow` now says
# plainly instead of telling the SysOp to go ask an operator who is
# themselves (issue #583).
#
# Deploying the backend is an operational step
# (`services/managed_dns/README.md`); the code change that follows it is
# this one line. Same shape, and the same reason, as `netbbs.link.
# reliable_nodes.RELIABLE_NODES_URL`: a project-run service a node must
# not need to be told about to use.
DEFAULT_SERVICE_URL: str | None = None


def _set_config_values(db: Database, values: tuple[tuple[str, str], ...]) -> None:
    """Commit a related set of node-config values as one transaction."""
    with db.connection:
        db.connection.executemany(
            """
            INSERT INTO node_config (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            values,
        )


def set_pending_rename_state(
    db: Database, *, name: str, previous_name: str,
    previous_status: RegistrationStatus, previous_published: bool,
) -> None:
    """Atomically switch the local presentation to a pending replacement."""
    _set_config_values(db, (
        (PREVIOUS_NAME_CONFIG_KEY, previous_name),
        (PREVIOUS_STATUS_CONFIG_KEY, previous_status.value),
        (PREVIOUS_PUBLISHED_CONFIG_KEY, "1" if previous_published else "0"),
        (NAME_CONFIG_KEY, name),
        (STATUS_CONFIG_KEY, RegistrationStatus.PENDING.value),
        (PUBLISHED_CONFIG_KEY, "0"),
    ))


def set_registration_result_state(
    db: Database, *, name: str, status: RegistrationStatus, dynamic: bool, service_url: str,
) -> None:
    """Commit an interactive registration result as one conservative view.

    `service_url` is the address that issued the credential this result
    came with, recorded in the same transaction as the registration it
    belongs to so the two can never disagree -- see
    `foreign_credential_service_url`."""
    _set_config_values(db, (
        (CREDENTIAL_SERVICE_URL_CONFIG_KEY, service_url),
        (NAME_CONFIG_KEY, name),
        (STATUS_CONFIG_KEY, status.value),
        # Registration/reclaim never proves provider publication. A later
        # heartbeat supplies that authoritative fact.
        (PUBLISHED_CONFIG_KEY, "0"),
        (DYNAMIC_CONFIG_KEY, "1" if dynamic else "0"),
        (OPT_IN_CONFIG_KEY, OptIn.ACCEPTED.value),
        (PREVIOUS_NAME_CONFIG_KEY, ""),
        (PREVIOUS_STATUS_CONFIG_KEY, ""),
        (PREVIOUS_PUBLISHED_CONFIG_KEY, "0"),
        (RECOVERY_NOTE_CONFIG_KEY, ""),
    ))


def set_cancelled_rename_state(
    db: Database, *, name: str, status: RegistrationStatus, published: bool,
) -> None:
    """Atomically restore the previous registration after cancellation."""
    _set_config_values(db, (
        (NAME_CONFIG_KEY, name),
        (STATUS_CONFIG_KEY, status.value),
        (PUBLISHED_CONFIG_KEY, "1" if published else "0"),
        (PREVIOUS_NAME_CONFIG_KEY, ""),
        (PREVIOUS_STATUS_CONFIG_KEY, ""),
        (PREVIOUS_PUBLISHED_CONFIG_KEY, "0"),
    ))


def set_heartbeat_reconciliation_state(
    db: Database, *, name: str, status: RegistrationStatus, published: bool,
    last_contact_at: str | None, previous_name: str | None,
    previous_status: RegistrationStatus | None, previous_published: bool,
) -> None:
    """Commit one heartbeat's complete authoritative local view atomically."""
    values = (
        (NAME_CONFIG_KEY, name),
        (STATUS_CONFIG_KEY, status.value),
        (PUBLISHED_CONFIG_KEY, "1" if published else "0"),
        (PREVIOUS_NAME_CONFIG_KEY, previous_name or ""),
        (PREVIOUS_STATUS_CONFIG_KEY, previous_status.value if previous_status else ""),
        (PREVIOUS_PUBLISHED_CONFIG_KEY, "1" if previous_published else "0"),
        # A recovery note describes an automatic reclaim attempt made
        # *after* the current abandonment; any authoritative answer from
        # the service supersedes it (`get_recovery_note`).
        (RECOVERY_NOTE_CONFIG_KEY, ""),
    )
    # None means preserve an absent/existing contact timestamp; inactive-only
    # reconciliation has no successful contact to record.
    if last_contact_at is not None:
        values += ((LAST_CONTACT_AT_CONFIG_KEY, last_contact_at),)
    _set_config_values(db, values)


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
    `"https://managed.netbbs.org"`) -- this node's `[managed_dns]
    service_url` if its operator set one, otherwise the shipped
    `DEFAULT_SERVICE_URL`, and `None` only when neither exists.

    This used to be database-only and documented as deliberately having
    no default, on the reasoning that the production address is an
    operational decision independent of this client code. That reasoning
    held; what was missing is that nothing ever carried the operational
    decision *in*. `set_service_url` had no caller outside the tests, so
    every node that accepted the opt-in (the pre-set answer, design doc
    §16 Decision 7) dead-ended at registration with no way forward from
    any surface a SysOp or operator has -- issue #583. Both halves are
    answered now: a shipped default for the project's own instance, and
    `netbbs.net.nodeconfig`'s `[managed_dns] service_url` (mirrored here
    at startup by `netbbs.__main__.run`) for anyone pointing a node at a
    different one.

    `set_service_url` stores `None` as `""` (same "empty string means
    None" convention as `set_registered_name`), so this translates it
    back rather than ever returning an empty string a caller never
    actually set -- and, because a cleared config key is indistinguishable
    from an absent one here, removing `service_url` from a node's
    configuration correctly falls back to the shipped default on the
    next startup rather than stranding the node on a stale override."""
    value = get_config(db, SERVICE_URL_CONFIG_KEY)
    return value or DEFAULT_SERVICE_URL


def set_service_url(db: Database, url: str | None) -> None:
    set_config(db, SERVICE_URL_CONFIG_KEY, url or "")


def get_credential_service_url(db: Database) -> str | None:
    """The managed-DNS service that issued this node's stored
    credential, recorded by `set_registration_result_state`. `None` on a
    node that has never registered."""
    return get_config(db, CREDENTIAL_SERVICE_URL_CONFIG_KEY) or None


def canonical_service_url(url: str) -> str:
    """`url` reduced to the form two spellings of the same address
    share, for comparison only -- never for display or for what is
    actually dialed.

    Codex review of PR #587: the issuer comparison below is the
    difference between a node heartbeating and a node paused, so
    `https://DNS.EXAMPLE`, `https://dns.example:443` and
    `https://dns.example/` must not read as three different services.
    Scheme and host are lowercased, a default port for the scheme is
    dropped, and a trailing slash goes; userinfo is dropped because it
    is refused at config load and is not part of which service this is.
    An IP literal is normalised through `ipaddress` so two spellings of
    one address match, and an IPv6 one is re-bracketed afterwards, since
    the split hands back `hostname` without its brackets.

    `urlsplit`, not `urlparse`: `urlparse` peels a final-segment path
    parameter off into `params`, so a canonical form built from `path`
    alone would read `https://dns.example/api;tenant=a` and
    `.../api;tenant=b` as one service and hand the first one's
    credential to the second (Codex review of PR #587). Path parameters
    are deliberately accepted by `netbbs.net.nodeconfig`'s own
    validation -- that is how a node behind a reverse-proxy subpath is
    reached -- so they are part of which service this is. `urlsplit`
    never separates them.

    A string this cannot parse is returned trimmed rather than raised
    over: this function only ever decides whether two addresses match,
    and a validated address is the only kind that reaches it."""
    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return url.strip().rstrip("/")
    try:
        # `::1` and `0:0:0:0:0:0:0:1` are the same address written two
        # ways, and this comparison is the difference between a node
        # heartbeating and a node paused (Codex review of PR #587).
        # Raises for an ordinary hostname, which is left alone.
        host = ipaddress.ip_address(host).compressed
    except ValueError:
        pass
    if ":" in host:
        host = f"[{host}]"
    if port is not None and port != {"http": 80, "https": 443}.get(scheme):
        host = f"{host}:{port}"
    return f"{scheme}://{host}{parsed.path.rstrip('/')}"


def foreign_credential_service_url(db: Database, base_url: str) -> str | None:
    """The issuing service's address when this node's stored credential
    belongs to a *different* managed-DNS service than `base_url` --
    otherwise `None`.

    A node's managed-DNS credential is a bearer secret: whoever holds it
    controls that registration, including releasing it (design doc §16
    Decision 2). Since issue #583 the service address is an operator
    setting, so it can change under a node that already holds one, and
    an unguarded heartbeat, rename, release or reclaim would then hand
    the secret issued by one service straight to another operator's
    (Codex review of PR #587). Every caller that is about to present the
    credential asks this first and declines rather than sending it.

    `None` when no credential has ever been issued, so a node that has
    never registered is never held back by a comparison there is nothing
    to make.
    """
    issuer = get_credential_service_url(db)
    if issuer is None or canonical_service_url(issuer) == canonical_service_url(base_url):
        return None
    return issuer


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


def get_published(db: Database) -> bool:
    """Whether the service last confirmed a published DNS record for the
    registered name (a heartbeat reporting a `last_known_address`).
    Distinct from `matured`: the service matures a registration before
    its first provider upsert, so a matured name can still have no
    record -- and must not be advertised as this node's canonical DNS
    name until it does (`netbbs.link.node_profiles.own_canonical_dns_name`)."""
    return get_config(db, PUBLISHED_CONFIG_KEY) == "1"


def set_published(db: Database, published: bool) -> None:
    set_config(db, PUBLISHED_CONFIG_KEY, "1" if published else "0")


def get_previous_published(db: Database) -> bool:
    """`get_published` for the previous name while a rename is pending."""
    return get_config(db, PREVIOUS_PUBLISHED_CONFIG_KEY) == "1"


def set_previous_published(db: Database, published: bool) -> None:
    set_config(db, PREVIOUS_PUBLISHED_CONFIG_KEY, "1" if published else "0")


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


@dataclass(frozen=True)
class ListenerFacts:
    """This node's own caller-facing listeners, as configured at its last
    startup -- what design doc §16 Decision 6's standard-ports convention
    is measured against (issue #603). A port of `None` means the
    transport is not enabled. `web_public_url` is `[web] public_url`
    when set: the one statement a SysOp has already made about an HTTPS
    front for the web listener, which is what decides whether the web
    address counts as part of the managed name's promise."""

    telnet_port: int | None
    ssh_port: int | None
    web_port: int | None
    web_public_url: str | None


def set_local_listeners(db: Database, facts: ListenerFacts) -> None:
    """Written by `netbbs.__main__.run` once per startup, the same way
    the node fingerprint and the service address reach this module: the
    SysOp console's DNS screen cannot reach `NodeConfig`, and the ports
    are the one thing the node knows with certainty about how a caller
    dialling its managed name will fare."""
    set_config(db, LISTENERS_CONFIG_KEY, json.dumps({
        "telnet": facts.telnet_port, "ssh": facts.ssh_port, "web": facts.web_port,
        "web_public_url": facts.web_public_url,
    }))


def get_local_listeners(db: Database) -> ListenerFacts | None:
    """`None` on a node that has not started since this was recorded;
    the screens say so rather than guessing."""
    raw = get_config(db, LISTENERS_CONFIG_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    def port(key: str) -> int | None:
        value = data.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

    public_url = data.get("web_public_url")
    return ListenerFacts(
        telnet_port=port("telnet"), ssh_port=port("ssh"), web_port=port("web"),
        web_public_url=public_url if isinstance(public_url, str) and public_url else None,
    )


@dataclass(frozen=True)
class RecoveryNote:
    """The most recent automatic reclaim attempt the updater made for an
    abandoned name and how it went (design doc §16 Decision 10, issue
    #600) -- so the SysOp console can say what is being retried every
    pass and why it has not worked yet, instead of showing a bare
    ABANDONED badge over a name the node is quietly trying to get back."""

    at: str
    text: str
    #: The service refused with a 409: the row is purged, held by another
    #: credential, or revoked -- nothing a later pass can change, so the
    #: updater stops retrying and leaves it to the SysOp's `[R]egister`
    #: (Codex review of PR #608). A refusal of any other kind (capacity,
    #: unreachable, a service without the route) is retried every pass.
    final: bool = False


# A note is one refusal sentence from the service; this is the most of
# it that is ever persisted, whatever arrived (Codex review of PR #608).
_MAX_RECOVERY_NOTE_CHARS = 500


def set_recovery_note(db: Database, note: RecoveryNote | None) -> None:
    set_config(
        db, RECOVERY_NOTE_CONFIG_KEY,
        json.dumps({"at": note.at, "text": note.text[:_MAX_RECOVERY_NOTE_CHARS], "final": note.final})
        if note else "",
    )


def get_recovery_note(db: Database) -> RecoveryNote | None:
    raw = get_config(db, RECOVERY_NOTE_CONFIG_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("at"), str) or not isinstance(data.get("text"), str):
        return None
    return RecoveryNote(at=data["at"], text=data["text"], final=data.get("final") is True)


def set_service_contact(db: Database, contact: str | None) -> None:
    """The operator's contact channel as the service last named it in a
    refusal (design doc §16 Decision 3/4) -- kept so the DNS screen can
    repeat it beside a REVOKED badge after the refusal itself has
    scrolled away. Free text from the service, so it is sanitised and
    bounded where it is shown, never here."""
    set_config(db, SERVICE_CONTACT_CONFIG_KEY, (contact or "")[:512])


def get_service_contact(db: Database) -> str | None:
    return get_config(db, SERVICE_CONTACT_CONFIG_KEY) or None


def set_revoked_state(db: Database, *, name: str, contact: str | None) -> None:
    """The node's view once the service says its credential was revoked
    (design doc §16 Decision 4): the name and, if a rename was in flight,
    the previous name are both revoked -- the service takes both halves
    -- nothing is published, and the contact channel is kept. One
    transaction, like every other reconciliation here."""
    previous_name = get_previous_name(db)
    _set_config_values(db, (
        (NAME_CONFIG_KEY, name),
        (STATUS_CONFIG_KEY, RegistrationStatus.REVOKED.value),
        (PUBLISHED_CONFIG_KEY, "0"),
        (PREVIOUS_NAME_CONFIG_KEY, previous_name or ""),
        (PREVIOUS_STATUS_CONFIG_KEY, RegistrationStatus.REVOKED.value if previous_name else ""),
        (PREVIOUS_PUBLISHED_CONFIG_KEY, "0"),
        (RECOVERY_NOTE_CONFIG_KEY, ""),
        (SERVICE_CONTACT_CONFIG_KEY, (contact or "")[:512]),
    ))


def get_admin_token(db: Database) -> str | None:
    """The bearer token for the managed service's `/admin/` routes, when
    this node's operator is also the service's operator (design doc §16
    Decision 4): `[managed_dns] admin_token` in `netbbs.toml`, mirrored
    here by `netbbs.__main__.run` once per startup, absence included --
    exactly as `service_url` travels. Its presence is what makes the
    SysOp console offer the service-administration screen at all."""
    return get_config(db, ADMIN_TOKEN_CONFIG_KEY) or None


def set_admin_token(db: Database, token: str | None) -> None:
    set_config(db, ADMIN_TOKEN_CONFIG_KEY, token or "")

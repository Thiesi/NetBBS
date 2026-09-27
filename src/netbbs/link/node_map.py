"""
The node map (design doc §8.12, issue #777): the other NetBBS boards this
node knows, how it knows each one, and when it last heard of it.

One list, two audiences. `build_node_map` answers for a caller or for the
SysOp; the screens in `netbbs.net.node_map_flow` and `netbbs.net.admin_flow`
only draw what it returns.

Where a node comes from, best-verified first -- a node is listed once, from
the best source this node has for it:

1. **met** -- a completed hello (`link_peers`);
2. **introduced** -- a verified bundle a carrier served
   (`link_introduced_identities`, naming the carrier in `introduced_by`);
3. **candidate** -- a descriptor a peer list named, unverified
   (`link_peer_candidates`); the SysOp's alone (§16, issue #767 Decision 3);
4. **origin** -- the origin of a board, file area or linked channel this node
   carries, with nothing else on file, e.g. after its introduced identity was
   displaced from the bounded store. Every field not on file reads unknown.

Callers never see a candidate, nor a node quarantined or blocked in any of the
identity, resource or content dimensions (§12.2). Operational reachability is
not a trust state and hides nothing. A node that is both a candidate and an
origin is listed for callers as an origin: what a peer list said about it is
unverified, and what this node carries from it is not.

Synchronous and `db`-first, like every domain function; the screens dispatch
it through a `DatabaseLane`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

from netbbs.link.node_profiles import (
    UNKNOWN_NODE_NAME,
    UNNAMED_NODE_NAME,
    identity_for_fingerprint,
    normalize_dns_name,
    normalize_friendly_name,
)
from netbbs.link.reliability import reliability_score
from netbbs.link.trust import TrustDimension, TrustState, TrustSubject, get_effective_trust_state
from netbbs.storage.database import Database
from netbbs.timeutil import _parse_stored_timestamp

MET = "met"
INTRODUCED = "introduced"
CANDIDATE = "candidate"
ORIGIN = "origin"
_SOURCE_RANK = {MET: 0, INTRODUCED: 1, CANDIDATE: 2, ORIGIN: 3}

# A node not heard of for this long is marked, not removed (§8.12).
STALE_AFTER = timedelta(days=30)

# The three dimensions whose quarantine or block leaves a node off a caller's
# list. Operational reachability is deliberately not one of them.
HIDING_DIMENSIONS = (
    TrustDimension.IDENTITY_INTEGRITY,
    TrustDimension.RESOURCE_BEHAVIOR,
    TrustDimension.CONTENT_CONDUCT,
)
_HIDING_STATES = frozenset({TrustState.QUARANTINED, TrustState.BLOCKED})

ANOTHER_NODE = "another node"


@dataclass(frozen=True)
class NodeMapEntry:
    """One row of the node map.

    `friendly_name`/`dns_name`/the descriptor fields come from the node's own
    signed descriptor where there is one, and are remote text: sanitize before
    display. `relationship` is already worded for the viewer the map was built
    for ("direct", "via <carrier>", "via another node", "unverified",
    "unknown")."""

    fingerprint: str
    friendly_name: str
    dns_name: str | None
    # The node's permanent number on this board's map (`node_numbers`).
    number: int
    source: str
    relationship: str
    last_heard: datetime | None
    stale: bool
    carrier_fingerprint: str | None = None
    first_named: datetime | None = None
    is_origin: bool = False
    trust: dict[str, str] = field(default_factory=dict)
    # The signed descriptor's payload as stored, unvalidated -- empty for an
    # origin-only node. Kept for the detail view's `dial_in` addresses (§8.2),
    # which are read from it by that field's own validating reader.
    descriptor_payload: dict = field(default_factory=dict)
    # SysOp-only facts. Filled for the SysOp's map alone; a caller's map
    # leaves them empty so no screen can show them by accident.
    addresses: tuple[str, ...] = ()
    outgoing_only: bool | None = None
    published_relays: int = 0
    live_relays: int = 0
    we_relay_for_it: bool = False
    it_relays_for_us: bool = False
    reliability: float | None = None

    @property
    def hidden_from_callers(self) -> bool:
        return self.source == CANDIDATE or any(
            state in _HIDING_STATES for state in self.trust.values()
        )


@dataclass
class _Known:
    fingerprint: str
    source: str
    descriptor_json: str | None = None
    first_stored_at: str | None = None
    last_direct_contact_at: str | None = None
    introduced_by: str | None = None
    first_named_at: str | None = None


def unknown_node_label(fingerprint: str) -> str:
    """What a node with no name on file is called on the map: two such nodes
    must not read the same. A fingerprint is public, so its start is fine."""
    return f"Unknown node {fingerprint[:6]}"


def _display_name(db: Database, fingerprint: str) -> tuple[str, str | None]:
    """Whatever name this node still has for `fingerprint`, or the unknown
    label -- with its DNS name, if any."""
    identity = identity_for_fingerprint(db, fingerprint)
    if identity.friendly_name == UNKNOWN_NODE_NAME:
        return unknown_node_label(fingerprint), None
    return identity.friendly_name, identity.dns_name


def node_numbers(db: Database, fingerprints: list[str]) -> dict[str, int]:
    """Each fingerprint's permanent map number, assigning the next free one to
    any the map has not met before, in the order given.

    Numbers are never reused or renumbered: nothing deletes a row, and a new
    one takes one past the highest ever given, so a node that leaves the map
    and comes back keeps its number."""
    numbers = {
        row["fingerprint"]: row["number"]
        for row in db.connection.execute("SELECT fingerprint, number FROM link_node_numbers")
    }
    missing = [fp for fp in dict.fromkeys(fingerprints) if fp not in numbers]
    if missing:
        next_number = max(numbers.values(), default=0) + 1
        for fingerprint in missing:
            db.connection.execute(
                "INSERT INTO link_node_numbers (fingerprint, number) VALUES (?, ?)", (fingerprint, next_number)
            )
            numbers[fingerprint] = next_number
            next_number += 1
        db.connection.commit()
    return {fp: numbers[fp] for fp in fingerprints}


def _parse(value: object) -> datetime | None:
    """An aware UTC datetime, or `None` for anything missing or unparsable --
    a malformed time reads as unknown rather than failing the map."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return _parse_stored_timestamp(value)
    except (ValueError, TypeError, OverflowError):
        return None


def _payload(descriptor_json: str | None) -> dict:
    if descriptor_json is None:
        return {}
    try:
        payload = json.loads(descriptor_json).get("envelope", {}).get("payload", {})
    except (ValueError, AttributeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def last_heard(
    *, last_direct_contact_at: str | None, descriptor_created_at: object, descriptor_first_stored_at: str | None,
) -> datetime | None:
    """The later of this node's own last direct contact and the descriptor's
    signed `created_at`, the latter never later than when this node first
    stored that descriptor (§8.12). `None` when neither is known.

    Without a parsable first-stored time the descriptor's own date cannot be
    capped, so it is not used: an uncapped date is exactly what could keep a
    node fresh for years."""
    contact = _parse(last_direct_contact_at)
    created = _parse(descriptor_created_at)
    first_stored = _parse(descriptor_first_stored_at)
    signed = min(created, first_stored) if created is not None and first_stored is not None else None
    candidates = [t for t in (contact, signed) if t is not None]
    return max(candidates) if candidates else None


def _current_origin(genesis_json: str | None, override: str | None = None) -> str | None:
    if override:
        return override
    if genesis_json is None:
        return None
    try:
        origin = json.loads(genesis_json)["envelope"]["payload"]["origin_fingerprint"]
    except (ValueError, KeyError, TypeError):
        return None
    return origin if isinstance(origin, str) and origin else None


def carried_origins(db: Database) -> dict[str, dict[str, list[str]]]:
    """origin fingerprint -> {"boards"|"file_areas"|"channels": [resource ids]}
    for every Linked resource this node holds and has not hidden. A board's
    origin follows a completed transfer (`link_origin_fingerprint`), as
    `netbbs.link.boards.board_origin_fingerprint` resolves it; file areas and
    channels have no succession and keep their genesis's origin."""
    origins: dict[str, dict[str, list[str]]] = {}

    def _add(origin: str | None, kind: str, resource_id: str) -> None:
        if origin:
            origins.setdefault(origin, {"boards": [], "file_areas": [], "channels": []})[kind].append(resource_id)

    for row in db.connection.execute(
        "SELECT board_id, link_genesis_json, link_origin_fingerprint FROM boards "
        "WHERE link_genesis_json IS NOT NULL AND link_hidden_at IS NULL"
    ):
        _add(_current_origin(row["link_genesis_json"], row["link_origin_fingerprint"]), "boards", row["board_id"])
    for row in db.connection.execute(
        "SELECT area_id, link_genesis_json FROM file_areas "
        "WHERE link_genesis_json IS NOT NULL AND link_hidden_at IS NULL"
    ):
        _add(_current_origin(row["link_genesis_json"]), "file_areas", row["area_id"])
    for row in db.connection.execute(
        "SELECT channel_id, link_genesis_json FROM channels "
        "WHERE link_genesis_json IS NOT NULL AND link_hidden_at IS NULL"
    ):
        _add(_current_origin(row["link_genesis_json"]), "channels", row["channel_id"])
    return origins


def _node_trust(db: Database, fingerprint: str) -> dict[str, str]:
    """Each hiding dimension's effective state; a node with no projection yet
    is probationary, as `netbbs.link.enforcement` reads it."""
    subject = TrustSubject.node(fingerprint)
    states: dict[str, str] = {}
    for dimension in HIDING_DIMENSIONS:
        try:
            states[dimension.value] = get_effective_trust_state(db, subject, dimension).state.value
        except ValueError:
            states[dimension.value] = TrustState.PROBATIONARY.value
    return states


def _gather(db: Database, own_fingerprint: str, *, include_candidates: bool) -> dict[str, _Known]:
    """Every node this one knows, from its best source."""
    known: dict[str, _Known] = {}

    def _offer(item: _Known) -> None:
        if item.fingerprint == own_fingerprint:
            return
        current = known.get(item.fingerprint)
        if current is None or _SOURCE_RANK[item.source] < _SOURCE_RANK[current.source]:
            known[item.fingerprint] = item

    for row in db.connection.execute(
        "SELECT fingerprint, descriptor_json, descriptor_first_stored_at, last_direct_contact_at FROM link_peers"
    ):
        _offer(_Known(
            row["fingerprint"], MET, row["descriptor_json"], row["descriptor_first_stored_at"],
            last_direct_contact_at=row["last_direct_contact_at"],
        ))
    for row in db.connection.execute(
        "SELECT fingerprint, descriptor_json, descriptor_first_stored_at, introduced_by "
        "FROM link_introduced_identities"
    ):
        _offer(_Known(
            row["fingerprint"], INTRODUCED, row["descriptor_json"], row["descriptor_first_stored_at"],
            introduced_by=row["introduced_by"],
        ))
    if include_candidates:
        for row in db.connection.execute(
            "SELECT fingerprint, descriptor_json, descriptor_first_stored_at, first_named_at "
            "FROM link_peer_candidates"
        ):
            _offer(_Known(
                row["fingerprint"], CANDIDATE, row["descriptor_json"], row["descriptor_first_stored_at"],
                first_named_at=row["first_named_at"],
            ))
    return known


def build_node_map(
    db: Database, *, own_fingerprint: str, sysop: bool, now: datetime | None = None,
) -> list[NodeMapEntry]:
    """The node map as `sysop` or a caller sees it, sorted by name (§8.12).

    For a caller: met, introduced and origin-only nodes, less every node
    quarantined or blocked in any of the identity, resource or content
    dimensions, and a carrier that is itself left off named as "another node".
    For the SysOp: everything, candidates and hidden nodes included, with the
    Link addresses, relay roles and reliability a caller never sees."""
    now = now or datetime.now(timezone.utc)
    origins = carried_origins(db)
    known = _gather(db, own_fingerprint, include_candidates=sysop)
    for origin in origins:
        if origin != own_fingerprint and origin not in known:
            known[origin] = _Known(origin, ORIGIN)

    trust = {fingerprint: _node_trust(db, fingerprint) for fingerprint in known}
    consents: dict[str, set[str]] = {}
    if sysop:
        for row in db.connection.execute("SELECT fingerprint, role FROM link_relay_consents"):
            consents.setdefault(row["fingerprint"], set()).add(row["role"])

    def _hidden(fingerprint: str) -> bool:
        return any(state in _HIDING_STATES for state in trust.get(fingerprint, {}).values())

    if not sysop:
        known = {fp: item for fp, item in known.items() if not _hidden(fp)}

    entries = []
    for fingerprint, item in known.items():
        payload = _payload(item.descriptor_json)
        if item.source == ORIGIN:
            # Whatever name this node still has for it, or the unknown label.
            friendly, dns_name = _display_name(db, fingerprint)
        else:
            friendly = normalize_friendly_name(payload.get("friendly_name")) or UNNAMED_NODE_NAME
            dns_name = normalize_dns_name(payload.get("canonical_dns_name"))

        if item.source == MET:
            relationship = "direct"
        elif item.source == INTRODUCED:
            carrier = item.introduced_by
            if not carrier or (not sysop and carrier not in known):
                # The carrier is itself left off this caller's list.
                relationship = f"via {ANOTHER_NODE}"
            else:
                # The carrier's friendly name alone: its DNS name is on its
                # own row, and would only crowd this column.
                relationship = f"via {_display_name(db, carrier)[0]}"
        elif item.source == CANDIDATE:
            relationship = "unverified"
        else:
            relationship = "unknown"

        if item.source in (MET, INTRODUCED):
            heard = last_heard(
                last_direct_contact_at=item.last_direct_contact_at,
                descriptor_created_at=payload.get("created_at"),
                descriptor_first_stored_at=item.first_stored_at,
            )
        else:
            # A candidate's descriptor is unverified, so it has no last-heard
            # time; an origin-only node has nothing on file.
            heard = None

        extra: dict = {}
        if sysop:
            roles = consents.get(fingerprint, set())
            addresses = payload.get("addresses")
            relays = payload.get("relays")
            live = payload.get("live_relays")
            extra = dict(
                addresses=tuple(
                    f"{a.get('protocol')}://{a.get('address')}:{a.get('port')}"
                    for a in (addresses if isinstance(addresses, list) else []) if isinstance(a, dict)
                ),
                outgoing_only=bool(payload["outgoing_only"]) if "outgoing_only" in payload else None,
                published_relays=len(relays) if isinstance(relays, list) else 0,
                live_relays=len(live) if isinstance(live, list) else 0,
                we_relay_for_it="i_relay_for" in roles,
                it_relays_for_us="relay_for_me" in roles,
                reliability=reliability_score(db, fingerprint),
            )

        entries.append(NodeMapEntry(
            fingerprint=fingerprint,
            friendly_name=friendly,
            dns_name=dns_name,
            number=0,
            source=item.source,
            relationship=relationship,
            last_heard=heard,
            stale=heard is not None and now - heard > STALE_AFTER,
            carrier_fingerprint=item.introduced_by,
            first_named=_parse(item.first_named_at) if item.source == CANDIDATE else None,
            is_origin=fingerprint in origins,
            trust=trust[fingerprint],
            descriptor_payload=payload,
            **extra,
        ))
    entries.sort(key=lambda e: (e.friendly_name.casefold(), e.dns_name or "", e.fingerprint))
    # Numbered after sorting, so nodes met for the first time together are
    # numbered in the order the list shows them.
    numbers = node_numbers(db, [e.fingerprint for e in entries])
    return [replace(e, number=numbers[e.fingerprint]) for e in entries]


def has_known_nodes(db: Database, *, own_fingerprint: str) -> bool:
    """Whether the SysOp's map would list anything -- without building it."""
    for table in ("link_peers", "link_introduced_identities", "link_peer_candidates"):
        if db.connection.execute(
            f"SELECT 1 FROM {table} WHERE fingerprint != ? LIMIT 1", (own_fingerprint,)
        ).fetchone() is not None:
            return True
    return any(origin != own_fingerprint for origin in carried_origins(db))


def relative_time(when: datetime | None, *, now: datetime | None = None) -> str:
    """"3 days ago", or "unknown" when nothing is on file."""
    if when is None:
        return "unknown"
    now = now or datetime.now(timezone.utc)
    seconds = (now - when).total_seconds()
    if seconds < 90:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 90:
        return f"{minutes} minutes ago"
    hours = int(minutes // 60)
    if hours < 48:
        return f"{hours} hours ago"
    days = int(hours // 24)
    return f"{days} days ago"


def carried_from(db: Database, fingerprint: str) -> dict[str, list[str]]:
    """The resource ids this node carries from origin `fingerprint`, by kind --
    before any caller's read gates, which the screen applies with the same
    filters as ordinary browsing."""
    return carried_origins(db).get(fingerprint, {"boards": [], "file_areas": [], "channels": []})

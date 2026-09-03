"""Human-facing names for cryptographically identified Link nodes.

Fingerprints remain protocol and persistence keys. This module resolves and
presents authenticated claims from a peer's signed endpoint descriptor; DNS
and friendly names never become trust authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
import json
import re
import unicodedata

from netbbs.managed_dns.state import (
    RegistrationStatus, get_node_fingerprint, get_previous_name, get_previous_published, get_previous_status,
    get_published, get_registered_name, get_registration_status,
)
from netbbs.config import get_config, get_node_display_name, set_config
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


MAX_NODE_FRIENDLY_NAME_LENGTH = 64
MAX_CANONICAL_DNS_NAME_LENGTH = 253
MAX_IDENTITY_OBSERVATIONS_PER_PEER = 20
MAX_IDENTITY_OBSERVATIONS_TOTAL = 5000
_NODE_FINGERPRINT_RE = re.compile(r"^[a-z2-7]{32}$")
_OWN_CANONICAL_DNS_CONFIG_KEY = "link_own_canonical_dns_claim"
UNKNOWN_NODE_NAME = "Unknown linked node"
UNNAMED_NODE_NAME = "Unnamed linked node"


@dataclass(frozen=True)
class NodeDisplayIdentity:
    fingerprint: str
    friendly_name: str
    dns_name: str | None

    @property
    def label(self) -> str:
        """The caller-facing presentation. A node with no authenticated
        profile at all (an administratively configured fingerprint this
        node has never admitted) has only its technical identity, so the
        fingerprint is shown rather than a placeholder that would make
        every such node look the same."""
        if self.friendly_name == UNKNOWN_NODE_NAME and self.fingerprint:
            return self.fingerprint
        return f"{self.friendly_name} · {self.dns_name}" if self.dns_name else self.friendly_name


@dataclass(frozen=True)
class NodeIdentityObservation:
    id: int
    node_fingerprint: str
    previous_fingerprint: str | None
    friendly_name: str | None
    previous_friendly_name: str | None
    canonical_dns_name: str | None
    previous_dns_name: str | None
    severity: str
    kind: str
    observed_at: str
    dismissed_at: str | None


def name_key(value: str) -> str:
    """Comparison key for a friendly or DNS name: one Unicode form (NFC)
    and one case, so a precomposed and a combining-accent spelling of the
    same name -- identical on every terminal -- can never be two
    distinct claims."""
    return unicodedata.normalize("NFC", value).lower()


def normalize_friendly_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    # NFC is the canonical form: a peer's claim must arrive already in
    # it (`profile_claims_are_canonical`) and the local setter stores
    # it (`netbbs.config.canonical_node_display_name`), so canonically
    # equivalent spellings compare equal everywhere below.
    value = unicodedata.normalize("NFC", value.strip())
    if (
        not value or len(value) > MAX_NODE_FRIENDLY_NAME_LENGTH
        or name_key(value) == name_key(UNNAMED_NODE_NAME)
    ):
        return None
    if "·" in value or '"' in value or any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        return None
    return value


def normalize_dns_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().rstrip(".").lower()
    if not value or len(value) > MAX_CANONICAL_DNS_NAME_LENGTH:
        return None
    try:
        ip_address(value)
        return None
    except ValueError:
        pass
    labels = value.split(".")
    if len(labels) < 2 or any(
        not label or len(label) > 63 or label[0] == "-" or label[-1] == "-"
        or any(not (char.isascii() and (char.isalnum() or char == "-")) for char in label)
        for label in labels
    ):
        return None
    return value


def profile_claims_are_canonical(payload: dict) -> bool:
    friendly_name = payload.get("friendly_name")
    canonical_dns_name = payload.get("canonical_dns_name")
    return (
        (friendly_name is None or normalize_friendly_name(friendly_name) == friendly_name)
        and (canonical_dns_name is None or normalize_dns_name(canonical_dns_name) == canonical_dns_name)
    )


def is_node_fingerprint(value: str) -> bool:
    return bool(_NODE_FINGERPRINT_RE.fullmatch(value.strip().lower()))


def identity_for_peer(peer) -> NodeDisplayIdentity:
    if peer is None:
        return NodeDisplayIdentity("", UNKNOWN_NODE_NAME, None)
    if peer.descriptor is None:
        return NodeDisplayIdentity(peer.fingerprint, UNNAMED_NODE_NAME, None)
    payload = peer.descriptor.payload
    friendly = normalize_friendly_name(payload.get("friendly_name"))
    dns_name = normalize_dns_name(payload.get("canonical_dns_name"))
    if dns_name is None:
        for address in payload.get("addresses") or ():
            dns_name = normalize_dns_name(address.get("address")) if isinstance(address, dict) else None
            if dns_name:
                break
    return NodeDisplayIdentity(peer.fingerprint, friendly or UNNAMED_NODE_NAME, dns_name)


def own_canonical_dns_name(db: Database, advertised_host: str | None) -> str | None:
    """The DNS name this node advertises as its own. A managed name is
    claimed only once the service has confirmed a published record for
    it (`matured` alone is not enough: the service matures a
    registration *before* its first provider upsert, and a failed
    upsert leaves it matured with no record) -- until then the
    configured host stays advertised rather than being replaced by a
    name nobody can resolve yet."""
    managed_name = get_registered_name(db)
    status = get_registration_status(db)
    previous_name = get_previous_name(db)
    if (
        previous_name and status is RegistrationStatus.PENDING
        and get_previous_status(db) is RegistrationStatus.MATURED and get_previous_published(db)
    ):
        return f"{previous_name}.netbbs.org"
    if managed_name and status is RegistrationStatus.MATURED and get_published(db):
        return f"{managed_name}.netbbs.org"
    return normalize_dns_name(advertised_host)


def remember_own_identity_claims(db: Database, *, canonical_dns_name: str | None) -> None:
    """Persist the local DNS presentation used for collision detection."""
    set_config(db, _OWN_CANONICAL_DNS_CONFIG_KEY, canonical_dns_name or "")


def resolve_peer_reference(peers, reference: str):
    """Resolve DNS, a unique friendly name, or a fingerprint prefix."""
    name_needle = name_key(reference.strip())
    if not name_needle:
        return []
    dns_needle = name_needle.rstrip(".")
    values = [peer for peer in peers if peer is not None]
    exact_fingerprint = [peer for peer in values if peer.fingerprint.lower() == name_needle]
    if exact_fingerprint:
        return exact_fingerprint[0]
    exact_dns = [peer for peer in values if identity_for_peer(peer).dns_name == dns_needle]
    if exact_dns:
        return exact_dns[0] if len(exact_dns) == 1 else exact_dns
    exact_name = [peer for peer in values if name_key(identity_for_peer(peer).friendly_name) == name_needle]
    if exact_name:
        return exact_name[0] if len(exact_name) == 1 else exact_name
    fingerprints = [peer for peer in values if peer.fingerprint.lower().startswith(name_needle)]
    return fingerprints[0] if len(fingerprints) == 1 else fingerprints


def _identity_from_descriptor_json(fingerprint: str, raw: str) -> NodeDisplayIdentity:
    data = json.loads(raw)
    payload = data.get("envelope", {}).get("payload", {})
    friendly = normalize_friendly_name(payload.get("friendly_name")) or UNNAMED_NODE_NAME
    dns_name = normalize_dns_name(payload.get("canonical_dns_name"))
    if dns_name is None:
        for address in payload.get("addresses") or ():
            dns_name = normalize_dns_name(address.get("address")) if isinstance(address, dict) else None
            if dns_name:
                break
    return NodeDisplayIdentity(fingerprint, friendly, dns_name)


def identity_for_fingerprint(db: Database, fingerprint: str) -> NodeDisplayIdentity:
    row = db.connection.execute(
        "SELECT descriptor_json FROM link_peers WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    if row is None:
        return NodeDisplayIdentity(fingerprint, UNKNOWN_NODE_NAME, None)
    return _identity_from_descriptor_json(fingerprint, row["descriptor_json"])


def present_link_author_label(db: Database, label: str) -> str:
    """Render a persisted `user@<home-node-fingerprint>` label by its home
    node's *current* friendly identity. Persistence keeps the technical
    identity (design doc §4.4) -- a carried board post or fetched Link
    file must still read correctly after its home node renames -- so the
    friendly presentation is resolved at render time, never stored. Any
    other label (a local account, or a suffix that isn't a complete node
    fingerprint) is returned unchanged."""
    user_id, separator, node = label.rpartition("@")
    if not separator or not is_node_fingerprint(node):
        return label
    return f"{user_id}@{identity_for_fingerprint(db, node).label}"


def resolve_stored_peer_reference(db: Database, reference: str) -> str | list[str]:
    """Resolve a UI-entered DNS/friendly/technical reference from persisted peers."""
    name_needle = name_key(reference.strip())
    if not name_needle:
        return []
    dns_needle = name_needle.rstrip(".")
    identities = [
        _identity_from_descriptor_json(row["fingerprint"], row["descriptor_json"])
        for row in db.connection.execute("SELECT fingerprint, descriptor_json FROM link_peers")
    ]
    exact_fingerprint = [item.fingerprint for item in identities if item.fingerprint.lower() == name_needle]
    if exact_fingerprint:
        return exact_fingerprint[0]
    dns_matches = [item.fingerprint for item in identities if item.dns_name == dns_needle]
    if dns_matches:
        return dns_matches[0] if len(dns_matches) == 1 else dns_matches
    name_matches = [item.fingerprint for item in identities if name_key(item.friendly_name) == name_needle]
    if name_matches:
        return name_matches[0] if len(name_matches) == 1 else name_matches
    fingerprint_matches = [
        item.fingerprint for item in identities
        if item.fingerprint.lower().startswith(name_needle)
    ]
    return fingerprint_matches[0] if len(fingerprint_matches) == 1 else fingerprint_matches


def record_peer_identity_observation(db: Database, peer) -> None:
    """Record authenticated presentation changes before ``save_peer`` overwrites them."""
    current = identity_for_peer(peer)
    existing_row = db.connection.execute(
        "SELECT descriptor_json FROM link_peers WHERE fingerprint = ?", (peer.fingerprint,)
    ).fetchone()
    previous = (
        _identity_from_descriptor_json(peer.fingerprint, existing_row["descriptor_json"])
        if existing_row is not None else None
    )
    if (
        previous is not None
        and previous.friendly_name == current.friendly_name
        and previous.dns_name == current.dns_name
    ):
        return

    kind = "first_seen"
    severity = "info"
    previous_fingerprint = None
    previous_name = previous.friendly_name if previous else None
    previous_dns = previous.dns_name if previous else None
    collision = None
    current_claims = {
        name_key(value) for value in (current.friendly_name, current.dns_name)
        if value and value != UNNAMED_NODE_NAME
    }
    local_claims = {
        name_key(value)
        for value in (get_node_display_name(db), get_config(db, _OWN_CANONICAL_DNS_CONFIG_KEY))
        if value
    }
    if current_claims & local_claims:
        collision = NodeDisplayIdentity(
            get_node_fingerprint(db) or "local-node", get_node_display_name(db),
            normalize_dns_name(get_config(db, _OWN_CANONICAL_DNS_CONFIG_KEY)),
        )
    for row in db.connection.execute(
        "SELECT fingerprint, descriptor_json FROM link_peers WHERE fingerprint <> ?",
        (peer.fingerprint,),
    ):
        known = _identity_from_descriptor_json(row["fingerprint"], row["descriptor_json"])
        known_claims = {
            name_key(value) for value in (known.friendly_name, known.dns_name)
            if value and value != UNNAMED_NODE_NAME
        }
        if collision is None and current_claims & known_claims:
            collision = known
            break

    # A name remains familiar after its owner renames. Check the bounded
    # authenticated observation history as well as current descriptors so a
    # different key cannot quietly take over a recently-used presentation.
    if collision is None:
        for row in db.connection.execute(
            """
            SELECT node_fingerprint, friendly_name, previous_friendly_name,
                   canonical_dns_name, previous_dns_name
            FROM link_node_identity_observations
            WHERE node_fingerprint <> ?
            ORDER BY id DESC
            """,
            (peer.fingerprint,),
        ):
            historical_claims = {
                name_key(value) for value in (
                    row["friendly_name"], row["previous_friendly_name"],
                    row["canonical_dns_name"], row["previous_dns_name"],
                ) if value and value != UNNAMED_NODE_NAME
            }
            matched_claims = current_claims & historical_claims
            if matched_claims:
                matched_claim = next(iter(matched_claims))
                collision = NodeDisplayIdentity(
                    row["node_fingerprint"],
                    current.friendly_name
                    if name_key(current.friendly_name) == matched_claim
                    else row["friendly_name"] or row["previous_friendly_name"] or UNKNOWN_NODE_NAME,
                    current.dns_name
                    if current.dns_name and name_key(current.dns_name) == matched_claim
                    else row["canonical_dns_name"] or row["previous_dns_name"],
                )
                break

    if collision is not None:
        kind = "cryptographic_identity_changed"
        severity = "security"
        previous_fingerprint = collision.fingerprint
        previous_name = collision.friendly_name
        previous_dns = collision.dns_name
    elif previous is not None:
        name_changed = previous.friendly_name != current.friendly_name
        dns_changed = previous.dns_name != current.dns_name
        kind = "dns_name_changed" if dns_changed else "friendly_name_changed"
        severity = "warning" if dns_changed else "info"
    db.connection.execute(
        """
        INSERT INTO link_node_identity_observations
            (node_fingerprint, previous_fingerprint, friendly_name, previous_friendly_name,
             canonical_dns_name, previous_dns_name, severity, kind, observed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            current.fingerprint, previous_fingerprint, current.friendly_name, previous_name,
            current.dns_name, previous_dns, severity, kind, utc_now_iso(),
        ),
    )
    db.connection.execute(
        """
        DELETE FROM link_node_identity_observations
        WHERE node_fingerprint = ? AND id NOT IN (
            SELECT id FROM link_node_identity_observations
            WHERE node_fingerprint = ? ORDER BY id DESC LIMIT ?
        )
        """,
        (current.fingerprint, current.fingerprint, MAX_IDENTITY_OBSERVATIONS_PER_PEER),
    )
    db.connection.execute(
        """
        DELETE FROM link_node_identity_observations WHERE id IN (
            SELECT id FROM link_node_identity_observations
            ORDER BY id DESC LIMIT -1 OFFSET ?
        )
        """,
        (MAX_IDENTITY_OBSERVATIONS_TOTAL,),
    )


def list_identity_observations(
    db: Database, *, include_dismissed: bool = False, include_first_seen: bool = False,
) -> list[NodeIdentityObservation]:
    clauses = []
    if not include_dismissed:
        clauses.append("dismissed_at IS NULL")
    if not include_first_seen:
        clauses.append("kind <> 'first_seen'")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = db.connection.execute(
        "SELECT * FROM link_node_identity_observations" + where + " ORDER BY id DESC"
    ).fetchall()
    return [NodeIdentityObservation(**dict(row)) for row in rows]


def dismiss_identity_observation(db: Database, observation_id: int) -> None:
    db.connection.execute(
        "UPDATE link_node_identity_observations SET dismissed_at = ? WHERE id = ?",
        (utc_now_iso(), observation_id),
    )
    db.connection.commit()


def latest_identity_observation(
    db: Database, fingerprint: str,
) -> NodeIdentityObservation | None:
    """Latest undismissed presentation/identity change for one node."""
    row = db.connection.execute(
        """
        SELECT * FROM link_node_identity_observations
        WHERE node_fingerprint = ? AND dismissed_at IS NULL AND kind <> 'first_seen'
        ORDER BY CASE WHEN severity = 'security' THEN 0 ELSE 1 END, id DESC LIMIT 1
        """,
        (fingerprint,),
    ).fetchone()
    return NodeIdentityObservation(**dict(row)) if row is not None else None

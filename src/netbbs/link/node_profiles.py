"""Human-facing names for cryptographically identified Link nodes.

Fingerprints remain protocol and persistence keys. This module resolves and
presents authenticated claims from a peer's signed endpoint descriptor; DNS
and friendly names never become trust authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
import json

from netbbs.managed_dns.state import (
    RegistrationStatus, get_previous_name, get_registered_name, get_registration_status,
)
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


MAX_NODE_FRIENDLY_NAME_LENGTH = 64
MAX_CANONICAL_DNS_NAME_LENGTH = 253


@dataclass(frozen=True)
class NodeDisplayIdentity:
    fingerprint: str
    friendly_name: str
    dns_name: str | None

    @property
    def label(self) -> str:
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


def normalize_friendly_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_NODE_FRIENDLY_NAME_LENGTH:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
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


def identity_for_peer(peer) -> NodeDisplayIdentity:
    if peer is None:
        return NodeDisplayIdentity("", "Unknown linked node", None)
    if peer.descriptor is None:
        return NodeDisplayIdentity(peer.fingerprint, "Unnamed linked node", None)
    payload = peer.descriptor.payload
    friendly = normalize_friendly_name(payload.get("friendly_name"))
    dns_name = normalize_dns_name(payload.get("canonical_dns_name"))
    if dns_name is None:
        for address in payload.get("addresses") or ():
            dns_name = normalize_dns_name(address.get("address")) if isinstance(address, dict) else None
            if dns_name:
                break
    return NodeDisplayIdentity(peer.fingerprint, friendly or "Unnamed linked node", dns_name)


def own_canonical_dns_name(db: Database, advertised_host: str | None) -> str | None:
    managed_name = get_registered_name(db)
    status = get_registration_status(db)
    previous_name = get_previous_name(db)
    if previous_name and status is RegistrationStatus.PENDING:
        return f"{previous_name}.netbbs.org"
    if managed_name and status in (RegistrationStatus.PENDING, RegistrationStatus.MATURED):
        return f"{managed_name}.netbbs.org"
    return normalize_dns_name(advertised_host)


def resolve_peer_reference(peers, reference: str):
    """Resolve DNS, a unique friendly name, or a fingerprint prefix."""
    needle = reference.strip().lower().rstrip(".")
    values = [peer for peer in peers if peer is not None]
    exact_dns = [peer for peer in values if identity_for_peer(peer).dns_name == needle]
    if exact_dns:
        return exact_dns[0] if len(exact_dns) == 1 else exact_dns
    exact_name = [peer for peer in values if identity_for_peer(peer).friendly_name.lower() == needle]
    if exact_name:
        return exact_name[0] if len(exact_name) == 1 else exact_name
    fingerprints = [peer for peer in values if peer.fingerprint.lower().startswith(needle)]
    return fingerprints[0] if len(fingerprints) == 1 else fingerprints


def _identity_from_descriptor_json(fingerprint: str, raw: str) -> NodeDisplayIdentity:
    data = json.loads(raw)
    payload = data.get("envelope", {}).get("payload", {})
    friendly = normalize_friendly_name(payload.get("friendly_name")) or "Unnamed linked node"
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
        return NodeDisplayIdentity(fingerprint, "Unknown linked node", None)
    return _identity_from_descriptor_json(fingerprint, row["descriptor_json"])


def resolve_stored_peer_reference(db: Database, reference: str) -> str | list[str]:
    """Resolve a UI-entered DNS/friendly/technical reference from persisted peers."""
    needle = reference.strip().lower().rstrip(".")
    identities = [
        _identity_from_descriptor_json(row["fingerprint"], row["descriptor_json"])
        for row in db.connection.execute("SELECT fingerprint, descriptor_json FROM link_peers")
    ]
    dns_matches = [item.fingerprint for item in identities if item.dns_name == needle]
    if dns_matches:
        return dns_matches[0] if len(dns_matches) == 1 else dns_matches
    name_matches = [item.fingerprint for item in identities if item.friendly_name.lower() == needle]
    if name_matches:
        return name_matches[0] if len(name_matches) == 1 else name_matches
    fingerprint_matches = [item.fingerprint for item in identities if item.fingerprint.lower().startswith(needle)]
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

    kind = "first_seen"
    severity = "info"
    previous_fingerprint = None
    previous_name = previous.friendly_name if previous else None
    previous_dns = previous.dns_name if previous else None
    if previous is not None:
        name_changed = previous.friendly_name != current.friendly_name
        dns_changed = previous.dns_name != current.dns_name
        if not name_changed and not dns_changed:
            return
        kind = "dns_name_changed" if dns_changed else "friendly_name_changed"
        severity = "warning" if dns_changed else "info"
    else:
        for row in db.connection.execute(
            "SELECT fingerprint, descriptor_json FROM link_peers WHERE fingerprint <> ?",
            (peer.fingerprint,),
        ):
            known = _identity_from_descriptor_json(row["fingerprint"], row["descriptor_json"])
            same_dns = bool(current.dns_name and known.dns_name == current.dns_name)
            same_name = (
                current.friendly_name != "Unnamed linked node"
                and known.friendly_name.lower() == current.friendly_name.lower()
            )
            if same_dns or same_name:
                kind = "cryptographic_identity_changed"
                severity = "security"
                previous_fingerprint = known.fingerprint
                previous_name = known.friendly_name
                previous_dns = known.dns_name
                break

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
        ORDER BY id DESC LIMIT 1
        """,
        (fingerprint,),
    ).fetchone()
    return NodeIdentityObservation(**dict(row)) if row is not None else None

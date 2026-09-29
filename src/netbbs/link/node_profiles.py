"""Human-facing names for cryptographically identified Link nodes.

Fingerprints remain protocol and persistence keys. This module resolves and
presents authenticated claims from a peer's signed endpoint descriptor; DNS
and friendly names never become trust authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
import json
import unicodedata
import weakref

from netbbs.managed_dns.state import (
    RegistrationStatus, get_node_fingerprint, get_previous_name, get_previous_published, get_previous_status,
    get_published, get_registered_name, get_registration_status,
)
from netbbs.auth.users import presentation_skeleton
from netbbs.config import get_config, get_node_display_name, is_node_fingerprint_shape, set_config
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


MAX_NODE_FRIENDLY_NAME_LENGTH = 64
MAX_CANONICAL_DNS_NAME_LENGTH = 253
MAX_IDENTITY_OBSERVATIONS_PER_PEER = 20
MAX_IDENTITY_OBSERVATIONS_TOTAL = 5000
_MAX_OWN_IDENTITY_CLAIM_HISTORY = 40
_OWN_FRIENDLY_NAME_CONFIG_KEY = "link_own_friendly_name_claim"
_OWN_CANONICAL_DNS_CONFIG_KEY = "link_own_canonical_dns_claim"
_OWN_IDENTITY_HISTORY_CONFIG_KEY = "link_own_identity_claim_history"
UNKNOWN_NODE_NAME = "Unknown linked node"
UNNAMED_NODE_NAME = "Unnamed linked node"
#: Between a friendly name and what qualifies it. `·` is reserved in
#: friendly names, so a qualified name never equals another node's name.
NAME_QUALIFIER_SEPARATOR = " · "


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
        return (
            f"{self.friendly_name}{NAME_QUALIFIER_SEPARATOR}{self.dns_name}"
            if self.dns_name else self.friendly_name
        )


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


def look_alike_key(value: str) -> str:
    """Comparison key for whether a reader could take two friendly names
    for one (issue #900): `presentation_skeleton`, the key local aliases
    are checked with (issue #843), so "OutBound", "0utBound", "Out Bound"
    and "OutBоund" with a Cyrillic о share one. A name with no letter or
    digit in it has an empty skeleton and keeps its `name_key`.

    For "could these be confused" -- identity warnings and telling nodes
    apart on screen. "Is this the same name" (a rename, a typed reference)
    stays `name_key`: a fuzzy match there could pick a different node."""
    return presentation_skeleton(value) or name_key(value)


def _friendly_claim_keys(value: str | None) -> set[tuple[str, str]]:
    if not value or value == UNNAMED_NODE_NAME:
        return set()
    return {("exact", name_key(value)), ("look", look_alike_key(value))}


def _dns_claim_keys(value: str | None) -> set[tuple[str, str]]:
    # A DNS name is unique by registration, and folding would equate
    # different real hostnames (`presentation_skeleton` drops `-` and `.`).
    return {("exact", name_key(value))} if value else set()


def _identity_claim_keys(identity: NodeDisplayIdentity) -> set[tuple[str, str]]:
    """What `identity` claims, for the collision checks: exact friendly and
    DNS names, plus the friendly name's look-alike key."""
    return _friendly_claim_keys(identity.friendly_name) | _dns_claim_keys(identity.dns_name)


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
        or name_key(value) in {
            name_key(UNNAMED_NODE_NAME), name_key(UNKNOWN_NODE_NAME),
        }
        or is_node_fingerprint_shape(value)
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
    return is_node_fingerprint_shape(value)


def identity_for_peer(peer) -> NodeDisplayIdentity:
    if peer is None:
        return NodeDisplayIdentity("", UNKNOWN_NODE_NAME, None)
    if peer.descriptor is None:
        return NodeDisplayIdentity(peer.fingerprint, UNNAMED_NODE_NAME, None)
    payload = peer.descriptor.payload
    friendly = normalize_friendly_name(payload.get("friendly_name"))
    dns_name = normalize_dns_name(payload.get("canonical_dns_name"))
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
        previous_name and status in (RegistrationStatus.PENDING, RegistrationStatus.ABANDONED)
        and get_previous_status(db) is RegistrationStatus.MATURED and get_previous_published(db)
    ):
        return f"{previous_name}.netbbs.org"
    if managed_name and status is RegistrationStatus.MATURED and get_published(db):
        return f"{managed_name}.netbbs.org"
    return normalize_dns_name(advertised_host)


def remember_own_identity_claims(db: Database, *, canonical_dns_name: str | None) -> None:
    """Persist current and recently replaced local presentation claims."""
    friendly_name = get_node_display_name(db)
    previous = (
        get_config(db, _OWN_FRIENDLY_NAME_CONFIG_KEY),
        get_config(db, _OWN_CANONICAL_DNS_CONFIG_KEY),
    )
    current = (friendly_name, canonical_dns_name)
    try:
        history = json.loads(get_config(db, _OWN_IDENTITY_HISTORY_CONFIG_KEY) or "[]")
    except (TypeError, ValueError):
        history = []
    if not isinstance(history, list):
        history = []
    if any(previous) and previous != current:
        for value in previous:
            if value:
                # A claim may be reused and retired more than once. Move its
                # existing occurrence to the newest end so bounded pruning
                # reflects when it was last advertised, not first retired.
                history = [
                    item for item in history
                    if not isinstance(item, str) or name_key(item) != name_key(value)
                ]
                history.append(value)
    history = [
        value for value in history if isinstance(value, str)
    ][-_MAX_OWN_IDENTITY_CLAIM_HISTORY:]
    set_config(db, _OWN_IDENTITY_HISTORY_CONFIG_KEY, json.dumps(history))
    set_config(db, _OWN_FRIENDLY_NAME_CONFIG_KEY, friendly_name)
    set_config(db, _OWN_CANONICAL_DNS_CONFIG_KEY, canonical_dns_name or "")
    if previous != current:
        _recheck_stored_peers_against_local_claims(db)


def _recheck_stored_peers_against_local_claims(db: Database) -> None:
    """A peer which claimed a name *before* this node adopted it never sends
    another hello just because we renamed, so the collision check that runs
    on peer persistence would not see it. Re-evaluate every stored
    descriptor once per local-claim change; unchanged, non-colliding peers
    are a no-op and an identical already-recorded collision is deduplicated."""
    rows = db.connection.execute(
        """SELECT fingerprint, descriptor_json,
                  fingerprint IN (SELECT fingerprint FROM link_peers) AS met
           FROM link_known_identities"""
    ).fetchall()
    for row in rows:
        _record_identity_observation(
            db, _identity_from_descriptor_json(row["fingerprint"], row["descriptor_json"]),
            met=bool(row["met"]),
        )


#: Shortest typed reference read as the start of a node's technical
#: identity (issue #807). Below it a one- or two-letter friendly name would
#: collide with every peer whose fingerprint happens to start with the same
#: letters -- one in 32 per peer for a single letter -- and could never be
#: used. Six is what the node map shows after an unnamed node.
MIN_FINGERPRINT_PREFIX = 6


def unquote_reference(reference: str) -> str:
    """A typed node reference without one pair of enclosing double quotes:
    `link_address_label` quotes a node name that contains `@`, and what a
    caller reads must be what they can type back. No friendly name, DNS name
    or fingerprint can contain a double quote, so the quotes are never part
    of a name. An unmatched quote is kept, so that a message about the
    reference shows what was actually looked up."""
    value = reference.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1].strip()
    return value


def reference_needle(reference: str) -> str:
    """The comparison key for a typed node reference."""
    return name_key(unquote_reference(reference))


def fingerprint_prefix_matches(fingerprint: str, needle: str) -> bool:
    """Whether a typed `needle` names `fingerprint` by its first characters."""
    return len(needle) >= MIN_FINGERPRINT_PREFIX and fingerprint.lower().startswith(needle)


def _identity_matches(identity: NodeDisplayIdentity, needle: str) -> bool:
    """A reference names a node by its DNS name, its friendly name, the full
    label a screen shows for it (`Name · dns.example`), or the start of its
    technical identity. The `·` in a label is reserved (a friendly name may
    not contain it), so a label can never be mistaken for another node's
    name. `Name · abc123`, the form `qualified_node_name` gives a shared
    name with no DNS name, is read as the friendly name plus the start of
    the technical identity (issue #899)."""
    name, separator, prefix = needle.rpartition(NAME_QUALIFIER_SEPARATOR)
    return (
        identity.dns_name == needle.rstrip(".")
        or name_key(identity.friendly_name) == needle
        or name_key(identity.label) == needle
        or fingerprint_prefix_matches(identity.fingerprint, needle)
        or (
            bool(separator) and name_key(identity.friendly_name) == name
            and fingerprint_prefix_matches(identity.fingerprint, prefix)
        )
    )


def link_address_label(user: str, node_label: str) -> str:
    """`user@node` as a caller reads it (issue #807). A node name that itself
    contains `@` is quoted -- `bob@"Cats @ Night"` -- so a reader can tell
    where the user name ends; the To prompt accepts the quoted form back.

    The user half comes from a peer's signed payload and nothing on the way
    in holds it to the username grammar, so a peer could otherwise name its
    user `alice@"Trusted Node"` and have its own posts read as another
    node's. `@` and `"` in it are shown as `?`: no real user name contains
    either, and the address a reader sees then has one `@`, the real one."""
    user, node = link_address_parts(user, node_label)
    return f"{user}@{node}"


def link_address_parts(user: str, node_label: str) -> tuple[str, str]:
    """The two halves `link_address_label` joins with `@`, for a caller
    that styles them apart (the chat line, issue #899)."""
    user = user.replace("@", "?").replace('"', "?")
    return user, (f'"{node_label}"' if "@" in node_label else node_label)


def qualified_node_name(identity: NodeDisplayIdentity) -> str:
    """How a node reads where its friendly name alone is not enough to
    tell it apart: `Name · dns.example`, or `Name · abc123` -- the start
    of its technical identity -- without a DNS name. Both forms resolve
    back to the node when typed (`_identity_matches`)."""
    if identity.friendly_name == UNKNOWN_NODE_NAME or identity.dns_name:
        return identity.label
    prefix = identity.fingerprint[:MIN_FINGERPRINT_PREFIX]
    return f"{identity.friendly_name}{NAME_QUALIFIER_SEPARATOR}{prefix}"


#: Stands for this BBS among the owners of a claimed name.
_OWN_NODE = ""
#: Per database: which nodes claim each name, and the state it was read at.
_NAME_CLAIM_INDEX: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _own_claimed_names(db: Database) -> list[str]:
    """This BBS's current friendly and DNS claims, and the bounded history
    of the ones it retired -- what another node must not wear unnoticed."""
    try:
        history = json.loads(get_config(db, _OWN_IDENTITY_HISTORY_CONFIG_KEY) or "[]")
    except (TypeError, ValueError):
        history = []
    if not isinstance(history, list):
        history = []
    return [
        value for value in (
            get_node_display_name(db),
            get_config(db, _OWN_FRIENDLY_NAME_CONFIG_KEY),
            get_config(db, _OWN_CANONICAL_DNS_CONFIG_KEY),
            *history,
        ) if isinstance(value, str) and value
    ]


def _shown_friendly_keys(value: str | None) -> set[tuple[str, str]]:
    """`_friendly_claim_keys`, keeping the unnamed placeholder: two unnamed
    nodes are no identity collision, but a screen must still tell them
    apart."""
    if not value:
        return set()
    return {("exact", name_key(value)), ("look", look_alike_key(value))}


def _name_claim_owners(db: Database) -> dict[tuple[str, str], set[str]]:
    """Every friendly and DNS name a known node claims, and this BBS's own
    claims, keyed as the collision check keys them (`_identity_claim_keys`)
    to the fingerprints claiming it.

    Friendly and DNS names are one namespace here, as in the identity
    collision check: a node whose friendly name is another node's DNS name
    shares it. A chat line asks this once per line, so the index is kept
    until the database changes -- this connection's own writes
    (`total_changes`) or another connection's commits (`data_version`)."""
    connection = db.connection
    stamp = (connection.total_changes, connection.execute("PRAGMA data_version").fetchone()[0])
    cached = _NAME_CLAIM_INDEX.get(db)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    owners: dict[tuple[str, str], set[str]] = {}
    for value in _own_claimed_names(db):
        # Retired claims do not say which were friendly names (the
        # collision check reads them the same way).
        for key in _shown_friendly_keys(value) | _dns_claim_keys(value):
            owners.setdefault(key, set()).add(_OWN_NODE)
    for row in connection.execute("SELECT fingerprint, descriptor_json FROM link_known_identities"):
        known = _identity_from_descriptor_json(row["fingerprint"], row["descriptor_json"])
        for key in _shown_friendly_keys(known.friendly_name) | _dns_claim_keys(known.dns_name):
            owners.setdefault(key, set()).add(known.fingerprint)
    _NAME_CLAIM_INDEX[db] = (stamp, owners)
    return owners


def friendly_name_is_shared(db: Database, identity: NodeDisplayIdentity) -> bool:
    """Whether another node this BBS knows of, or this BBS itself, claims
    `identity`'s friendly name, as a friendly name or as a DNS name, or a
    friendly name a reader could take for it (`look_alike_key`, issue
    #900)."""
    index = _name_claim_owners(db)
    owners: set[str] = set()
    for key in _shown_friendly_keys(identity.friendly_name):
        owners |= index.get(key, set())
    return bool(owners - {identity.fingerprint})


def known_nodes_named_like(
    db: Database, name: str, *, exclude: str | None = None
) -> list[NodeDisplayIdentity]:
    """Every node this BBS knows of whose friendly name reads as `name`
    (`look_alike_key`), leaving out the node `exclude` names."""
    key = look_alike_key(name)
    found = []
    for row in db.connection.execute(
        "SELECT fingerprint, descriptor_json FROM link_known_identities WHERE fingerprint IS NOT ?",
        (exclude,),
    ):
        other = _identity_from_descriptor_json(row["fingerprint"], row["descriptor_json"])
        if look_alike_key(other.friendly_name) == key:
            found.append(other)
    return found


def short_node_name(db: Database, fingerprint: str) -> str:
    """A node's name on a chat line (issue #899): the friendly name alone,
    since the DNS name repeated on every line of a conversation cost more
    width than it told anyone. A name another known node, or this BBS,
    also goes by is qualified (`qualified_node_name`) so the two stay
    distinguishable. A node never admitted reads as its fingerprint, as
    `NodeDisplayIdentity.label` does."""
    identity = identity_for_fingerprint(db, fingerprint)
    if identity.friendly_name == UNKNOWN_NODE_NAME:
        return identity.label
    if friendly_name_is_shared(db, identity):
        return qualified_node_name(identity)
    return identity.friendly_name


def unknown_node_guidance(reference: str) -> str:
    """What to do when `reference` names no node this BBS is linked with.
    The caller sanitizes `reference`."""
    return (
        f"No BBS linked with this one goes by \"{reference}\". Check the name after the @: "
        "it is the one shown after the @ on their mail, posts and in Who's online."
    )


def ambiguous_node_guidance(reference: str, user: str, candidates: list[tuple[str, str]]) -> str:
    """What to type when `reference` names more than one node: each
    candidate's address by technical identity, with the name it goes by.
    `candidates` is `(fingerprint, label)` pairs; the caller sanitizes."""
    shown = "; ".join(f"{user}@{fingerprint} for {label}" for fingerprint, label in candidates)
    return f"More than one linked node goes by \"{reference}\". Type one of these instead: {shown}."


def resolve_peer_reference(peers, reference: str):
    """Resolve DNS, a unique friendly name, or a fingerprint prefix."""
    name_needle = reference_needle(reference)
    if not name_needle:
        return []
    values = [peer for peer in peers if peer is not None]
    exact_fingerprint = [peer for peer in values if peer.fingerprint.lower() == name_needle]
    if exact_fingerprint:
        return exact_fingerprint[0]
    matches = [peer for peer in values if _identity_matches(identity_for_peer(peer), name_needle)]
    return matches[0] if len(matches) == 1 else matches


def _identity_from_descriptor_json(fingerprint: str, raw: str) -> NodeDisplayIdentity:
    data = json.loads(raw)
    payload = data.get("envelope", {}).get("payload", {})
    friendly = normalize_friendly_name(payload.get("friendly_name")) or UNNAMED_NODE_NAME
    dns_name = normalize_dns_name(payload.get("canonical_dns_name"))
    return NodeDisplayIdentity(fingerprint, friendly, dns_name)


def identity_for_fingerprint(db: Database, fingerprint: str) -> NodeDisplayIdentity:
    row = db.connection.execute(
        "SELECT descriptor_json FROM link_known_identities WHERE fingerprint = ?", (fingerprint,)
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
    rendered = link_address_label(user_id, identity_for_fingerprint(db, node).label)
    observation = latest_identity_observation(db, node)
    if observation is not None and observation.severity == "security":
        return (
            f"{rendered} [Caution: familiar node name has a different "
            f"cryptographic identity; technical identity: {node}]"
        )
    return rendered


def resolve_stored_peer_reference(
    db: Database, reference: str, *, met_only: bool = False
) -> str | list[str]:
    """Resolve a UI-entered DNS/friendly/technical reference from persisted peers.

    `met_only` leaves out nodes this one has only been introduced to (issue
    #630). Addressing mail asks for it: mail goes to completed peers alone, and
    a node nobody here has met must not be able to make a real peer's name
    ambiguous by wearing it. Trust administration does not, since a node
    learned from a carrier is exactly what a SysOp goes there to look up.
    """
    name_needle = reference_needle(reference)
    if not name_needle:
        return []
    source = "link_peers" if met_only else "link_known_identities"
    identities = [
        _identity_from_descriptor_json(row["fingerprint"], row["descriptor_json"])
        for row in db.connection.execute(f"SELECT fingerprint, descriptor_json FROM {source}")
    ]
    exact_fingerprint = [item.fingerprint for item in identities if item.fingerprint.lower() == name_needle]
    if exact_fingerprint:
        return exact_fingerprint[0]
    matches = [item.fingerprint for item in identities if _identity_matches(item, name_needle)]
    return matches[0] if len(matches) == 1 else matches


def record_peer_identity_observation(db: Database, peer, *, met: bool = True) -> None:
    """Record authenticated presentation changes before ``save_peer`` overwrites them.

    `met=False` for an identity a carrier introduced (issue #630)."""
    _record_identity_observation(db, identity_for_peer(peer), met=met)


def recheck_introduced_identities_against(db: Database, peer) -> None:
    """Re-judge nodes known only by introduction once `peer` has been met (issue #630).

    A met node is never compared with an introduced one, so that a stranger
    cannot get a real peer flagged. That leaves the other arrival order: the
    stranger's name on file first, the real node met later, and nobody flagged.
    Called after the peer's row is written, so that the introduced node's own
    comparison finds it.
    """
    met = identity_for_peer(peer)
    claims = _identity_claim_keys(met)
    if not claims:
        return
    rows = db.connection.execute(
        """SELECT fingerprint, descriptor_json FROM link_introduced_identities
           WHERE fingerprint NOT IN (SELECT fingerprint FROM link_peers)"""
    ).fetchall()
    for row in rows:
        introduced = _identity_from_descriptor_json(row["fingerprint"], row["descriptor_json"])
        if claims & _identity_claim_keys(introduced):
            _record_identity_observation(db, introduced, met=False)
    db.connection.commit()


def _record_identity_observation(db: Database, current: NodeDisplayIdentity, *, met: bool = True) -> None:
    """`met` is whether this node has completed a hello with `current`.

    A node it has met is compared only with other nodes it has met. One it was
    merely introduced to is compared with everyone. Otherwise anybody could
    have a real peer flagged as an impostor on every node that carries a shared
    board, by naming a node after it and posting once: the introduction would
    put the name on file first, and the real peer's next hello would be the
    one that collides. This way round it is the newcomer that gets the warning.
    """
    existing_row = db.connection.execute(
        "SELECT descriptor_json FROM link_known_identities WHERE fingerprint = ?", (current.fingerprint,)
    ).fetchone()
    previous = (
        _identity_from_descriptor_json(current.fingerprint, existing_row["descriptor_json"])
        if existing_row is not None else None
    )
    presentation_unchanged = (
        previous is not None
        and previous.friendly_name == current.friendly_name
        and previous.dns_name == current.dns_name
    )

    kind = "first_seen"
    severity = "info"
    previous_fingerprint = None
    previous_name = previous.friendly_name if previous else None
    previous_dns = previous.dns_name if previous else None
    collision = None
    current_claims = _identity_claim_keys(current)
    try:
        local_history = json.loads(get_config(db, _OWN_IDENTITY_HISTORY_CONFIG_KEY) or "[]")
    except (TypeError, ValueError):
        local_history = []
    if not isinstance(local_history, list):
        local_history = []
    # The history mixes retired friendly and DNS names without saying which
    # is which, so each gets both kinds of key. A DNS name's look-alike key
    # could only meet a friendly name spelled out like a hostname.
    local_claims = (
        _friendly_claim_keys(get_node_display_name(db))
        | _friendly_claim_keys(get_config(db, _OWN_FRIENDLY_NAME_CONFIG_KEY))
        | _dns_claim_keys(get_config(db, _OWN_CANONICAL_DNS_CONFIG_KEY))
    )
    for value in local_history:
        if isinstance(value, str) and value:
            local_claims |= _friendly_claim_keys(value) | _dns_claim_keys(value)
    if current_claims & local_claims:
        collision = NodeDisplayIdentity(
            get_node_fingerprint(db) or "local-node", get_node_display_name(db),
            normalize_dns_name(get_config(db, _OWN_CANONICAL_DNS_CONFIG_KEY)),
        )
    compared_with = "link_peers" if met else "link_known_identities"
    for row in db.connection.execute(
        f"SELECT fingerprint, descriptor_json FROM {compared_with} WHERE fingerprint <> ?",
        (current.fingerprint,),
    ):
        known = _identity_from_descriptor_json(row["fingerprint"], row["descriptor_json"])
        if collision is None and current_claims & _identity_claim_keys(known):
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
              AND (? = 0 OR node_fingerprint NOT IN (
                    SELECT fingerprint FROM link_introduced_identities
                    WHERE fingerprint NOT IN (SELECT fingerprint FROM link_peers)))
            ORDER BY id DESC
            """,
            (current.fingerprint, 1 if met else 0),
        ):
            historical_claims = (
                _friendly_claim_keys(row["friendly_name"])
                | _friendly_claim_keys(row["previous_friendly_name"])
                | _dns_claim_keys(row["canonical_dns_name"])
                | _dns_claim_keys(row["previous_dns_name"])
            )
            matched_claims = current_claims & historical_claims
            if matched_claims:
                matched_claim = next(iter(matched_claims))
                collision = NodeDisplayIdentity(
                    row["node_fingerprint"],
                    current.friendly_name
                    if ("exact", name_key(current.friendly_name)) == matched_claim
                    else row["friendly_name"] or row["previous_friendly_name"] or UNKNOWN_NODE_NAME,
                    current.dns_name
                    if current.dns_name and ("exact", name_key(current.dns_name)) == matched_claim
                    else row["canonical_dns_name"] or row["previous_dns_name"],
                )
                break

    if collision is not None:
        if presentation_unchanged and db.connection.execute(
            """
            SELECT 1 FROM link_node_identity_observations
            WHERE node_fingerprint = ? AND previous_fingerprint = ?
              AND friendly_name = ? AND canonical_dns_name IS ?
              AND kind = 'cryptographic_identity_changed'
            LIMIT 1
            """,
            (current.fingerprint, collision.fingerprint, current.friendly_name, current.dns_name),
        ).fetchone() is not None:
            return
        kind = "cryptographic_identity_changed"
        severity = "security"
        previous_fingerprint = collision.fingerprint
        previous_name = collision.friendly_name
        previous_dns = collision.dns_name
    elif presentation_unchanged:
        return
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
            WHERE node_fingerprint = ?
            ORDER BY CASE
                WHEN severity = 'security' AND dismissed_at IS NULL THEN 0
                WHEN dismissed_at IS NULL THEN 1 ELSE 2 END,
                id DESC LIMIT ?
        )
        """,
        (current.fingerprint, current.fingerprint, MAX_IDENTITY_OBSERVATIONS_PER_PEER),
    )
    db.connection.execute(
        """
        DELETE FROM link_node_identity_observations WHERE id IN (
            SELECT id FROM link_node_identity_observations
            ORDER BY CASE
                WHEN severity = 'security' AND dismissed_at IS NULL THEN 0
                WHEN dismissed_at IS NULL THEN 1 ELSE 2 END,
                id DESC LIMIT -1 OFFSET ?
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
        "SELECT * FROM link_node_identity_observations" + where
        + " ORDER BY CASE WHEN severity = 'security' AND dismissed_at IS NULL "
        "THEN 0 ELSE 1 END, id DESC"
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

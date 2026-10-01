"""Issuing this node's own signed trust objects (issue #589, slice 1: vouches).

Until this module, `netbbs.link.trust_wire` was receive-only. A node could
verify, store, re-serve and enforce on a peer's signed trust objects, and could
never issue one, so `link_trust_wire_objects` was empty on every node and design
doc §12.7's pull protocol correctly served nothing.

Slice 1 issues the one trust object whose trigger is plainly an explicit human
act: a vouch (§12.4). A SysOp records an *intent* to vouch for a subject; the
sync pass, which holds the node's operational key, brings the node's signed
objects in line with those intents. Nothing here decides when a node *accuses*
anyone: trust signals, their evidence classes and the automatic case stay open
in #589.

The shape is `remote_attestation.reconcile_issued_attestations`' on purpose,
for the reasons the worklog records about that one:

- the signature is made by the node's current operational key, which the
  offline `python -m netbbs.admin` console does not have at all, so the screen
  records an intent and a reconcile signs: the sync pass every time, and the
  SysOp console at once when it is running inside a Link node;
- one function decides when a signed object should exist. The operator
  listing derives its status from the same predicate, so a screen cannot tell
  a SysOp one thing while the next pass does another.

An issued vouch is stored in `link_trust_wire_objects` under this node's own
fingerprint, which is exactly what `load_trust_object_page` already serves to
a subscriber that names this node as the issuer: no new endpoint and no new
wire type. It is deliberately *not* recorded in `link_trust_vouches`. That
table is what local policy counts, and it counts a vouch only from a
configured reporter; a node is not its own reporter, and a SysOp who wants a
subject established *here* uses an override, which is the local act.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from netbbs.config import get_config, set_config
from netbbs.identity.keys import Identity, verify_signature
from netbbs.link.equivocation import SIGNED_EQUIVOCATION
from netbbs.link.events import canonical_bytes, event_content_id
from netbbs.link.trust import (
    EvidenceClass,
    TrustDimension,
    TrustSubject,
    record_local_observation,
    register_subject,
)
from netbbs.link.trust_wire import (
    TRUST_SIGNAL_OBJECT_TYPE,
    TRUST_VOUCH_OBJECT_TYPE,
    TRUST_VOUCH_REVOCATION_OBJECT_TYPE,
    SignedTrustObject,
    build_trust_revocation,
    build_trust_signal,
    build_trust_vouch,
    store_issued_trust_object,
)
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

# Inside the 180-day ceiling §12.6 has receivers clamp a vouch to. The ceiling
# is what a receiver must tolerate; this is what this node chooses to assert,
# for the reason the attestation lifetime records: a node that goes dark cannot
# withdraw what it has published, so the issued lifetime is the window in which
# a withdrawal that never reached a subscriber still leaves support standing.
ISSUED_VOUCH_LIFETIME = timedelta(days=90)

# Renewed with a third of the lifetime left, and the old object left to expire
# rather than revoked. Receivers count trust *domains* with an active vouch,
# never vouches, so the overlap cannot add weight, and a revocation would tell
# a subscriber to stop relying on support this node is re-asserting in the same
# breath.
ISSUED_VOUCH_RENEW_WITHIN = timedelta(days=30)

# The explanation is published: it is inside the signed payload every
# subscriber stores. Bounded here because nothing on the receiving side bounds
# it short of the whole response's byte limit.
MAX_VOUCH_EXPLANATION_CHARS = 280


class VouchIntentError(ValueError):
    """A vouch intent that cannot be recorded, or withdrawn, as asked."""


@dataclass(frozen=True)
class IssuedVouchChange:
    """One signing action `reconcile_issued_vouches` took."""

    action: str  # "issued" | "renewed" | "revoked"
    content_id: str
    subject: TrustSubject | None  # None: a revocation re-signed after a key rotation
    reason: str


@dataclass(frozen=True)
class VouchIntent:
    """One subject this node's SysOp has chosen to vouch for, and where that stands.

    `status` is what a subscriber would find if it pulled now, not what the
    SysOp asked for:

    - `pending`: the intent stands and no live signed vouch exists yet; the
      next sync pass signs one;
    - `published`: a live signed vouch exists;
    - `suspended`: the intent stands, but the subject is quarantined or blocked
      here, so this node is not vouching for it. A live vouch is revoked on the
      next pass, and one is signed again if the restriction is lifted;
    - `refused`: the subject is this node or one of its own users, recorded
      where this node's fingerprint was not known. It is never signed.
    """

    subject: TrustSubject
    explanation: str
    created_at: str
    status: str
    expires_at: str | None


def _stamp(value: datetime) -> str:
    """`value` in `utc_now_iso()`'s fixed, string-sortable format.

    Every liveness test below is a string comparison against a stored
    timestamp, and `datetime.isoformat()` writes `+00:00`, which sorts before
    `Z` at the same instant.
    """
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now(now_iso: str | None) -> tuple[str, datetime]:
    parsed = datetime.fromisoformat((now_iso or utc_now_iso()).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("now must include a UTC offset")
    return _stamp(parsed), parsed.astimezone(timezone.utc)


def _subject_from_row(row) -> TrustSubject:
    return TrustSubject(row["subject_kind"], row["node_fingerprint"], row["opaque_user_id"])


def _restricted_here(db: Database, subject_id: str) -> bool:
    """Whether this node has the subject quarantined or blocked in any dimension.

    A SysOp cannot coherently tell the network "I vouch for this identity"
    about one their own node refuses to deal with, whether the refusal came
    from an override or from automatic policy.
    """
    return db.connection.execute(
        """SELECT 1 FROM link_trust_effective_states
           WHERE subject_id = ? AND state IN ('quarantined', 'blocked') LIMIT 1""",
        (subject_id,),
    ).fetchone() is not None


def _active_intents(db: Database):
    return db.connection.execute(
        """SELECT i.subject_id, i.explanation, i.created_at,
                  s.subject_kind, s.node_fingerprint, s.opaque_user_id
           FROM link_trust_vouch_intents AS i
           JOIN link_trust_subjects AS s ON s.subject_id = i.subject_id
           WHERE i.withdrawn_at IS NULL
           ORDER BY i.created_at, i.intent_id"""
    ).fetchall()


def _live_own_vouches(db: Database, home_node_fingerprint: str, now_value: str):
    """This node's own unrevoked, unexpired vouches, newest first per subject."""
    return db.connection.execute(
        """SELECT rowid, content_id, subject_id, envelope_json, signature_b64, expires_at
           FROM link_trust_wire_objects
           WHERE issuer_fingerprint = ? AND object_type = ?
             AND revoked_at IS NULL AND expires_at > ?
           ORDER BY rowid DESC""",
        (home_node_fingerprint, TRUST_VOUCH_OBJECT_TYPE, now_value),
    ).fetchall()


def record_vouch_intent(
    db: Database,
    subject: TrustSubject,
    *,
    explanation: str,
    actor_user_id: int | None = None,
    own_node_fingerprint: str | None = None,
    now_iso: str | None = None,
) -> None:
    """Record that this node's SysOp vouches for `subject`. Signs nothing.

    The sync pass signs, on its next run. Recording again for a subject that
    already has a standing intent replaces the explanation, which retires the
    published vouch and signs a fresh one carrying the new text.

    `own_node_fingerprint` is this node's own identity where the caller knows
    it. A node vouching for itself says nothing, and a node vouching for its
    own users is a different statement from the one §12.4 defines -- the home
    node is the one party that cannot be independent of them -- so neither is
    accepted here.
    """
    explanation = explanation.strip()
    if not explanation:
        raise VouchIntentError("a vouch needs a reason; it is published with the vouch")
    if len(explanation) > MAX_VOUCH_EXPLANATION_CHARS:
        raise VouchIntentError(
            f"the reason is published with the vouch and may be at most "
            f"{MAX_VOUCH_EXPLANATION_CHARS} characters"
        )
    if own_node_fingerprint is not None and subject.node_fingerprint == own_node_fingerprint:
        raise VouchIntentError("a node cannot vouch for itself or for its own users")
    now_value, _ = _now(now_iso)
    with db.connection:
        if db.connection.execute(
            "SELECT 1 FROM link_trust_subjects WHERE subject_id = ?", (subject.subject_id,)
        ).fetchone() is None:
            raise VouchIntentError("that identity is not a trust subject this node has met")
        if _restricted_here(db, subject.subject_id):
            raise VouchIntentError(
                "this node has that identity quarantined or blocked; lift that first, "
                "or the vouch would say something this node does not act on"
            )
        db.connection.execute(
            """UPDATE link_trust_vouch_intents
               SET withdrawn_at = ?, withdrawn_by_user_id = ?
               WHERE subject_id = ? AND withdrawn_at IS NULL""",
            (now_value, actor_user_id, subject.subject_id),
        )
        db.connection.execute(
            """INSERT INTO link_trust_vouch_intents
               (subject_id, explanation, actor_user_id, created_at)
               VALUES (?, ?, ?, ?)""",
            (subject.subject_id, explanation, actor_user_id, now_value),
        )


def withdraw_vouch_intent(
    db: Database,
    subject: TrustSubject,
    *,
    actor_user_id: int | None = None,
    now_iso: str | None = None,
) -> bool:
    """Stop vouching for `subject`. Returns whether an intent was standing.

    Signs nothing: the next sync pass sees a live vouch with no intent behind
    it and signs the revocation. §12.4: a revocation removes current support
    and neither erases history nor accuses the subject of anything.
    """
    now_value, _ = _now(now_iso)
    with db.connection:
        cursor = db.connection.execute(
            """UPDATE link_trust_vouch_intents
               SET withdrawn_at = ?, withdrawn_by_user_id = ?
               WHERE subject_id = ? AND withdrawn_at IS NULL""",
            (now_value, actor_user_id, subject.subject_id),
        )
        return bool(cursor.rowcount)


def _signed_by(row, identity: Identity) -> bool:
    """Whether a stored object verifies under `identity`'s current key.

    A subscriber resolves only the issuer's *current* operational key, so an
    object the previous key signed stops verifying at the moment of rotation.
    Checked by verifying rather than by remembering which key signed, which
    needs no column and cannot drift from the truth.
    """
    return verify_signature(
        identity.verify_key,
        canonical_bytes(json.loads(row["envelope_json"])),
        base64.b64decode(row["signature_b64"]),
    )


def _revoke(
    db: Database, identity: Identity, row, *, home_node_fingerprint: str, now_value: str
) -> SignedTrustObject:
    revocation = build_trust_revocation(
        signing_identity=identity,
        issuer_fingerprint=home_node_fingerprint,
        revocation_id=secrets.token_hex(16),
        revoked_content_id=row["content_id"],
        issued_at=now_value,
        vouch=True,
    )
    store_issued_trust_object(db, revocation, issued_at=now_value)
    db.connection.execute(
        """UPDATE link_trust_wire_objects
           SET revoked_by_content_id = ?, revoked_at = ?
           WHERE content_id = ? AND revoked_at IS NULL""",
        (revocation.content_id, now_value, row["content_id"]),
    )
    return revocation


def _resign_orphaned_revocations(
    db: Database, identity: Identity, *, home_node_fingerprint: str, now_value: str
) -> list[str]:
    """Re-sign, under the current key, revocations a previous key signed for vouches still running.

    A subscriber that learns this node's new key can no longer verify what the
    old one signed. For a vouch that is harmless: the reconcile re-issues it.
    A *revocation* is not re-issued by anything, so one signed shortly before a
    rotation, and not yet pulled, would be skipped by that subscriber as an
    old-key object, and the vouch it retires would stay live there until it
    ran out. Signed again, it reaches that subscriber; one that already holds
    the first revocation skips the second as a repeat.

    Only while the target has not expired, since after that nobody can be
    relying on it, and only when no revocation of that target verifies under
    the current key, so this signs once per target per rotation.
    """
    rows = db.connection.execute(
        """SELECT r.envelope_json, r.signature_b64, t.content_id AS target_id
           FROM link_trust_wire_objects AS r
           JOIN link_trust_wire_objects AS t
             ON t.content_id = json_extract(r.envelope_json, '$.payload.revoked_content_id')
           WHERE r.issuer_fingerprint = ? AND r.object_type = ?
             AND t.issuer_fingerprint = ? AND t.expires_at > ?
           ORDER BY r.rowid""",
        (home_node_fingerprint, TRUST_VOUCH_REVOCATION_OBJECT_TYPE, home_node_fingerprint, now_value),
    ).fetchall()
    covered: set[str] = set()
    targets: list[str] = []
    for row in rows:
        if row["target_id"] not in targets:
            targets.append(row["target_id"])
        if _signed_by(row, identity):
            covered.add(row["target_id"])
    resigned: list[str] = []
    for target_id in targets:
        if target_id in covered:
            continue
        revocation = build_trust_revocation(
            signing_identity=identity, issuer_fingerprint=home_node_fingerprint,
            revocation_id=secrets.token_hex(16), revoked_content_id=target_id,
            issued_at=now_value, vouch=True,
        )
        store_issued_trust_object(db, revocation, issued_at=now_value)
        resigned.append(revocation.content_id)
    return resigned


def reconcile_issued_vouches(
    db: Database,
    signing_identity: Identity,
    *,
    home_node_fingerprint: str,
    now_iso: str | None = None,
) -> list[IssuedVouchChange]:
    """Bring this node's signed vouches in line with its SysOp's standing intents.

    Called once per sync pass. Signs a vouch for every standing intent that has
    no live one, renews one approaching expiry or signed by a key this node has
    since rotated away from, and signs a revocation for every live vouch whose
    intent has gone -- withdrawn, its explanation replaced, or its subject
    quarantined or blocked here. A no-op when nothing has changed.
    """
    now_value, now = _now(now_iso)
    changes: list[IssuedVouchChange] = []
    with db.connection:
        intents = {
            row["subject_id"]: (row["explanation"], _subject_from_row(row))
            for row in _active_intents(db)
        }
        # `record_vouch_intent` refuses a vouch for this node or its own users
        # only where its caller knew this node's fingerprint, and the offline
        # console on a node that has not started since the fingerprint cache
        # existed does not. Here it is always known, and here is where an
        # intent becomes a published object, so the rule is enforced again.
        restricted = {
            subject_id
            for subject_id, (_explanation, subject) in intents.items()
            if _restricted_here(db, subject_id) or subject.node_fingerprint == home_node_fingerprint
        }
        current: dict[str, object] = {}
        for row in _live_own_vouches(db, home_node_fingerprint, now_value):
            subject_id = row["subject_id"]
            payload = json.loads(row["envelope_json"])["payload"]
            if subject_id not in intents:
                reason = "intent_withdrawn"
            elif subject_id in restricted:
                # Also where an own-identity intent lands, which in practice
                # never has a live vouch to revoke: it was never signed.
                reason = "subject_restricted_here"
            elif payload["explanation"] != intents[subject_id][0]:
                reason = "explanation_replaced"
            else:
                # Newest first, so the first one kept per subject is the one a
                # renewal decision is about; an older overlap is left to expire.
                current.setdefault(subject_id, row)
                continue
            revocation = _revoke(
                db, signing_identity, row,
                home_node_fingerprint=home_node_fingerprint, now_value=now_value,
            )
            subject = TrustSubject(
                payload["subject"]["kind"], payload["subject"]["node_fingerprint"],
                payload["subject"].get("opaque_user_id"),
            )
            changes.append(IssuedVouchChange("revoked", revocation.content_id, subject, reason))

        expires_at = _stamp(now + ISSUED_VOUCH_LIFETIME)
        renew_before = _stamp(now + ISSUED_VOUCH_RENEW_WITHIN)
        for subject_id, (explanation, subject) in intents.items():
            if subject_id in restricted:
                continue
            active = current.get(subject_id)
            if active is None:
                action, reason = "issued", "intent_recorded"
            elif not _signed_by(active, signing_identity):
                action, reason = "renewed", "signing_key_rotated"
            elif active["expires_at"] <= renew_before:
                action, reason = "renewed", "approaching_expiry"
            else:
                continue
            vouch = build_trust_vouch(
                signing_identity=signing_identity,
                issuer_fingerprint=home_node_fingerprint,
                vouch_id=secrets.token_hex(16),
                subject=subject,
                issued_at=now_value,
                expires_at=expires_at,
                explanation=explanation,
            )
            store_issued_trust_object(db, vouch, issued_at=now_value)
            changes.append(IssuedVouchChange(action, vouch.content_id, subject, reason))
        for content_id in _resign_orphaned_revocations(
            db, signing_identity, home_node_fingerprint=home_node_fingerprint, now_value=now_value
        ):
            changes.append(IssuedVouchChange("revoked", content_id, None, "signing_key_rotated"))
    return changes


def list_vouch_intents(
    db: Database, *, home_node_fingerprint: str | None, now_iso: str | None = None
) -> list[VouchIntent]:
    """Every subject this node's SysOp currently vouches for, and where each stands.

    `home_node_fingerprint` is `None` where the caller cannot know it (a node
    that has not started since the fingerprint cache existed); every intent
    then reads `pending` or `suspended`, which is the honest answer when the
    published side cannot be looked up.
    """
    now_value, _ = _now(now_iso)
    # Keyed by what the reconcile would *keep*: a live vouch carrying the
    # intent's current explanation. One still carrying a replaced explanation
    # is about to be revoked, so the intent reads `pending`, not `published`.
    live: dict[tuple[str, str], str] = {}
    if home_node_fingerprint is not None:
        for row in _live_own_vouches(db, home_node_fingerprint, now_value):
            explanation = json.loads(row["envelope_json"])["payload"]["explanation"]
            live.setdefault((row["subject_id"], explanation), row["expires_at"])
    result: list[VouchIntent] = []
    for row in _active_intents(db):
        key = (row["subject_id"], row["explanation"])
        if home_node_fingerprint is not None and row["node_fingerprint"] == home_node_fingerprint:
            status = "refused"
        elif _restricted_here(db, row["subject_id"]):
            status = "suspended"
        elif key in live:
            status = "published"
        else:
            status = "pending"
        result.append(VouchIntent(
            _subject_from_row(row), row["explanation"], row["created_at"], status,
            live.get(key) if status == "published" else None,
        ))
    return result


@dataclass(frozen=True)
class VouchIntentHistoryEntry:
    """One recorded intent, standing or withdrawn: this table is its own audit trail."""

    subject: TrustSubject
    explanation: str
    actor_user_id: int | None
    created_at: str
    withdrawn_at: str | None
    withdrawn_by_user_id: int | None


def list_vouch_intent_history(db: Database, *, limit: int = 100) -> list[VouchIntentHistoryEntry]:
    """Every vouch intent this node's SysOps have recorded, newest first.

    `link_trust_config_audit` cannot hold these (its `object_kind` is
    constrained to anchor, domain and reporter), so a withdrawn intent keeps
    its row and this reads them back for the trust history screen.
    """
    rows = db.connection.execute(
        """SELECT i.explanation, i.actor_user_id, i.created_at, i.withdrawn_at,
                  i.withdrawn_by_user_id, s.subject_kind, s.node_fingerprint, s.opaque_user_id
           FROM link_trust_vouch_intents AS i
           JOIN link_trust_subjects AS s ON s.subject_id = i.subject_id
           ORDER BY i.intent_id DESC LIMIT ?""",
        (max(1, limit),),
    ).fetchall()
    return [
        VouchIntentHistoryEntry(
            _subject_from_row(row), row["explanation"], row["actor_user_id"], row["created_at"],
            row["withdrawn_at"], row["withdrawn_by_user_id"],
        )
        for row in rows
    ]


def get_vouch_intent(
    db: Database, subject: TrustSubject, *, home_node_fingerprint: str | None,
    now_iso: str | None = None,
) -> VouchIntent | None:
    """The standing intent for one subject, or `None`."""
    for intent in list_vouch_intents(
        db, home_node_fingerprint=home_node_fingerprint, now_iso=now_iso
    ):
        if intent.subject == subject:
            return intent
    return None


# -- automatic equivocation signals (issue #589) -------------------------------

#: The node-wide switch for automatic signals: on unless the SysOp turned it off.
AUTOMATIC_SIGNALS_CONFIG_KEY = "trust_automatic_equivocation_signals"
#: At most this many automatic signals are signed in any 24 hours: a node that
#: suddenly sees many forks is more likely broken than surrounded by liars.
MAX_AUTOMATIC_SIGNALS_PER_DAY = 5
AUTOMATIC_SIGNAL_EXPLANATION = (
    "Automatic: this node saw the subject sign two different objects into the same "
    "slot of one chain. Both are attached."
)


@dataclass(frozen=True)
class IssuedSignalChange:
    """One signing action `reconcile_issued_signals` took."""

    action: str  # "issued" | "renewed" | "revoked" | "deferred"
    content_id: str | None
    subject_fingerprint: str
    reason: str


@dataclass(frozen=True)
class IssuedSignal:
    """A signal this node signed about another node, and where it stands."""

    content_id: str
    observation_id: str
    subject_fingerprint: str
    issued_at: str
    expires_at: str
    revoked_at: str | None


def automatic_signals_enabled(db: Database) -> bool:
    return get_config(db, AUTOMATIC_SIGNALS_CONFIG_KEY, "1") != "0"


def set_automatic_signals_enabled(db: Database, enabled: bool) -> None:
    set_config(db, AUTOMATIC_SIGNALS_CONFIG_KEY, "1" if enabled else "0")


def equivocation_observation_id(evidence: dict) -> str:
    """One observation per pair of conflicting objects, whichever was seen first."""
    ids = sorted(event_content_id(raw["envelope"]) for raw in evidence["data"]["objects"])
    return "equivocation:" + hashlib.sha256("\n".join(ids).encode("ascii")).hexdigest()[:32]


def record_observed_equivocation(
    db: Database, subject_fingerprint: str, evidence: dict, *, now_iso: str | None = None,
) -> bool:
    """Record equivocation this node observed itself (issue #589).

    A self-verifying identity observation, which quarantines the subject's
    identity integrity here (design doc §12.8) and, unless the SysOp turned
    automatic signals off, is published by the next `reconcile_issued_signals`.
    Returns False if the same pair was already recorded."""
    now_value, _ = _now(now_iso)
    subject = TrustSubject.node(subject_fingerprint)
    register_subject(db, subject, first_accepted_at=now_value, now_iso=now_value)
    return record_local_observation(
        db,
        observation_id=equivocation_observation_id(evidence),
        subject=subject,
        dimension=TrustDimension.IDENTITY_INTEGRITY,
        category=SIGNED_EQUIVOCATION,
        evidence_class=EvidenceClass.SELF_VERIFYING,
        observed_at=now_value,
        evidence=evidence,
        explanation="Signed two different objects into the same slot of one chain (seen here).",
        now_iso=now_value,
    )


def _live_own_signals(db: Database, home_node_fingerprint: str, now_value: str):
    return db.connection.execute(
        """SELECT content_id, envelope_json, signature_b64, subject_id, expires_at, received_at
           FROM link_trust_wire_objects
           WHERE issuer_fingerprint = ? AND object_type = ? AND revoked_at IS NULL AND expires_at > ?
           ORDER BY received_at DESC, content_id""",
        (home_node_fingerprint, TRUST_SIGNAL_OBJECT_TYPE, now_value),
    ).fetchall()


def _publishable_observations(db: Database, home_node_fingerprint: str, now_value: str):
    """Active equivocation this node observed about another node, oldest first."""
    return db.connection.execute(
        """SELECT o.observation_id, o.subject_id, o.observed_at, o.expires_at, o.evidence_json,
                  o.publication_withdrawn_at, s.node_fingerprint
           FROM link_trust_local_observations AS o
           JOIN link_trust_subjects AS s ON s.subject_id = o.subject_id
           WHERE o.category = ? AND o.evidence_class = 'self_verifying' AND o.cleared_at IS NULL
             AND o.expires_at > ? AND s.subject_kind = 'node' AND s.node_fingerprint != ?
             AND o.evidence_json IS NOT NULL
           ORDER BY o.observed_at, o.observation_id""",
        (SIGNED_EQUIVOCATION, now_value, home_node_fingerprint),
    ).fetchall()


def _revoke_signal(
    db: Database, identity: Identity, content_id: str, *, home_node_fingerprint: str, now_value: str,
) -> SignedTrustObject:
    revocation = build_trust_revocation(
        signing_identity=identity,
        issuer_fingerprint=home_node_fingerprint,
        revocation_id=secrets.token_hex(16),
        revoked_content_id=content_id,
        issued_at=now_value,
    )
    store_issued_trust_object(db, revocation, issued_at=now_value)
    db.connection.execute(
        """UPDATE link_trust_wire_objects SET revoked_by_content_id = ?, revoked_at = ?
           WHERE content_id = ? AND revoked_at IS NULL""",
        (revocation.content_id, now_value, content_id),
    )
    return revocation


def reconcile_issued_signals(
    db: Database,
    signing_identity: Identity,
    *,
    home_node_fingerprint: str,
    now_iso: str | None = None,
) -> list[IssuedSignalChange]:
    """Publish, re-sign and revoke this node's automatic equivocation signals
    (issue #589). Called once per sync pass.

    A signal is signed for each active equivocation observation about another
    node -- at most one live signal per subject, at most
    `MAX_AUTOMATIC_SIGNALS_PER_DAY` signed in 24 hours -- while automatic
    signals are on and the SysOp has not withdrawn that one. A live signal is
    revoked when its observation is cleared or has expired, when the SysOp
    withdraws it, or when automatic signals are turned off; one signed by a
    key this node has since rotated away from is re-signed. Its evidence is
    the two conflicting objects themselves, which every receiver reproduces
    before it counts the signal (issue #1036).
    """
    now_value, now = _now(now_iso)
    enabled = automatic_signals_enabled(db)
    changes: list[IssuedSignalChange] = []
    with db.connection:
        observations = {row["observation_id"]: row for row in _publishable_observations(
            db, home_node_fingerprint, now_value)}
        live_subjects: set[str] = set()
        for row in _live_own_signals(db, home_node_fingerprint, now_value):
            payload = json.loads(row["envelope_json"])["payload"]
            observation = observations.get(payload["signal_id"])
            subject_fingerprint = payload["subject"]["node_fingerprint"]
            if observation is None:
                reason = "observation_cleared"
            elif observation["publication_withdrawn_at"] is not None:
                reason = "withdrawn_by_sysop"
            elif not enabled:
                reason = "automatic_signals_off"
            elif not _signed_by(row, signing_identity):
                reason = "signing_key_rotated"
            else:
                live_subjects.add(row["subject_id"])
                continue
            revocation = _revoke_signal(
                db, signing_identity, row["content_id"],
                home_node_fingerprint=home_node_fingerprint, now_value=now_value,
            )
            changes.append(IssuedSignalChange("revoked", revocation.content_id, subject_fingerprint, reason))
        if not enabled:
            return changes
        signed_today = db.connection.execute(
            """SELECT COUNT(*) FROM link_trust_wire_objects
               WHERE issuer_fingerprint = ? AND object_type = ? AND received_at > ?""",
            (home_node_fingerprint, TRUST_SIGNAL_OBJECT_TYPE, _stamp(now - timedelta(days=1))),
        ).fetchone()[0]
        for observation_id, observation in observations.items():
            if observation["publication_withdrawn_at"] is not None or observation["subject_id"] in live_subjects:
                continue
            subject_fingerprint = observation["node_fingerprint"]
            if signed_today >= MAX_AUTOMATIC_SIGNALS_PER_DAY:
                changes.append(IssuedSignalChange("deferred", None, subject_fingerprint, "daily_limit"))
                continue
            evidence = json.loads(observation["evidence_json"])
            if not isinstance(evidence, dict) or evidence.get("mode") != "embedded":
                continue
            signal = build_trust_signal(
                signing_identity=signing_identity,
                issuer_fingerprint=home_node_fingerprint,
                signal_id=observation_id,
                subject=TrustSubject.node(subject_fingerprint),
                dimension=TrustDimension.IDENTITY_INTEGRITY,
                category=SIGNED_EQUIVOCATION,
                evidence_class=EvidenceClass.SELF_VERIFYING,
                evidence=evidence,
                observed_at=observation["observed_at"],
                issued_at=now_value,
                expires_at=observation["expires_at"],
                explanation=AUTOMATIC_SIGNAL_EXPLANATION,
            )
            store_issued_trust_object(db, signal, issued_at=now_value)
            live_subjects.add(observation["subject_id"])
            signed_today += 1
            changes.append(IssuedSignalChange("issued", signal.content_id, subject_fingerprint, "observed_here"))
    return changes


def list_issued_signals(db: Database, *, home_node_fingerprint: str, limit: int = 100) -> list[IssuedSignal]:
    """Signals this node signed about other nodes, newest first."""
    rows = db.connection.execute(
        """SELECT content_id, envelope_json, received_at, expires_at, revoked_at
           FROM link_trust_wire_objects
           WHERE issuer_fingerprint = ? AND object_type = ?
           ORDER BY received_at DESC, content_id LIMIT ?""",
        (home_node_fingerprint, TRUST_SIGNAL_OBJECT_TYPE, limit),
    ).fetchall()
    result = []
    for row in rows:
        payload = json.loads(row["envelope_json"])["payload"]
        result.append(IssuedSignal(
            content_id=row["content_id"], observation_id=payload["signal_id"],
            subject_fingerprint=payload["subject"]["node_fingerprint"],
            issued_at=row["received_at"], expires_at=row["expires_at"], revoked_at=row["revoked_at"],
        ))
    return result

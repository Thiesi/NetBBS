"""Signed remote identity attestations and local acceptance policy (issue #130).

Remote users remain :class:`TrustSubject` values.  This module never creates a
local account and never treats general trust-report authority as permission to
verify age or name.  Wire verification is completed before persistence; local
authority configuration, issuer trust state, expiry/revocation, and explicit
SysOp overrides determine the restart-safe acceptance projection.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

import nacl.exceptions
import nacl.signing

from netbbs.attestation import compute_age, get_attestation
from netbbs.auth.users import User, get_user_by_id
from netbbs.identity.keys import Identity
from netbbs.link.events import canonical_bytes
from netbbs.link.trust import (
    TrustDimension,
    TrustState,
    TrustSubject,
    get_effective_trust_state,
)
from netbbs.rendering import VERIFIED_COLOR, colored, sanitize_text
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class UnknownAttestationSubject(ValueError):
    """The subject is not (yet) a Link identity this node has accepted.

    A `ValueError` like every other ingest rejection, so existing handlers are
    unchanged -- but nameable, because it is the one rejection that a later
    event can turn into an acceptance. A puller that advanced its cursor past
    one of these would never see the object again.
    """


class UnknownAttestationPullCursor(ValueError):
    """The requester's cursor names an object this node does not hold (issue #621).

    A `ValueError` so that every existing caller that treats an unknown cursor
    as a malformed request still does; its own type so the pull endpoint can
    say which, and a subscriber can recover.
    """


class NotAnAttestationRecipient(Exception):
    """The requester is not on this node's attestation recipient list.

    Deliberately not a `ValueError`: the pull handler answers a malformed
    request with HTTP 400, and this is a well-formed request from a node the
    SysOp has not chosen to tell (design doc §16, issue #596, Decision 3).
    """


REMOTE_ATTESTATION_OBJECT_TYPE = "remote_identity_attestation"
REMOTE_ATTESTATION_REVOCATION_OBJECT_TYPE = "remote_identity_attestation_revocation"
_ATTRIBUTES = frozenset({"age", "name"})
_MAX_NAME_BYTES = 128
_MAX_WIRE_BYTES = 16 * 1024
_MAX_LIFETIME = timedelta(days=365)
_FUTURE_TOLERANCE = timedelta(minutes=5)


@dataclass(frozen=True)
class AttestationAuthority:
    fingerprint: str
    attributes: tuple[str, ...]
    reason: str
    created_at: str


@dataclass(frozen=True)
class AttestationRecipient:
    """One node this node serves its own signed attestations to."""

    fingerprint: str
    reason: str
    created_at: str


@dataclass(frozen=True)
class RemoteAttestation:
    content_id: str
    issuer_fingerprint: str
    subject: TrustSubject
    attribute: str
    attested_value: str
    issued_at: str
    expires_at: str
    revoked_at: str | None


@dataclass(frozen=True)
class RemoteAttestationState:
    subject: TrustSubject
    attribute: str
    accepted: bool
    reason_code: str
    attestation: RemoteAttestation | None
    explanation: dict[str, object]
    evaluated_at: str


@dataclass(frozen=True)
class RemoteAttestationOverride:
    override_id: int
    subject_id: str
    attribute: str
    accepted: bool
    reason: str
    created_at: str


@dataclass(frozen=True)
class RemoteAttestationAudit:
    audit_id: int
    subject_id: str | None
    object_kind: str
    object_id: str
    action: str
    details: dict[str, object]
    actor_user_id: int | None
    created_at: str


def _parse_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a UTC offset")
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, OSError) as exc:
        # A syntactically valid timestamp can still overflow when normalized
        # -- `0001-01-01T00:00:00+23:59` is the reachable case. `OverflowError`
        # is not a `ValueError`, so without this one signed object from one
        # peer would escape every per-object handler and end the whole
        # background sync task (Codex review of #590).
        raise ValueError(f"{field} is outside the representable range") from exc


def _stamp(value: datetime) -> str:
    """`value` in the project's sortable storage format.

    `utc_now_iso()`'s fixed six decimals and `Z` suffix exist so stored
    timestamps compare correctly as strings, which is how every liveness query
    here asks whether an object has expired.  `datetime.isoformat()` produces
    `+00:00` and a variable number of decimals instead, and `"...+00:00" <
    "...Z"` at the same instant -- so a derived timestamp written that way
    sorts wrongly against a stored `now`.  Every timestamp this module derives
    goes through here.
    """
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now(now_iso: str | None) -> tuple[str, datetime]:
    parsed = _parse_time(now_iso or utc_now_iso(), "now")
    return _stamp(parsed), parsed


def _validate_attribute(attribute: str) -> str:
    if attribute not in _ATTRIBUTES:
        raise ValueError(f"unknown attestation attribute: {attribute!r}")
    return attribute


def _validate_value(attribute: str, value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("attested value must not be blank")
    if attribute == "age":
        try:
            birthdate = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("age attestation must contain an ISO birthdate") from exc
        if birthdate > datetime.now(timezone.utc).date():
            raise ValueError("attested birthdate cannot be in the future")
    elif len(value.encode("utf-8")) > _MAX_NAME_BYTES:
        raise ValueError(f"attested name cannot exceed {_MAX_NAME_BYTES} bytes")
    return value


def _subject_payload(subject: TrustSubject) -> dict[str, str]:
    if subject.kind != "user" or subject.opaque_user_id is None:
        raise ValueError("remote attestations require a stable user subject")
    return {
        "kind": "user",
        "node_fingerprint": subject.node_fingerprint,
        "opaque_user_id": subject.opaque_user_id,
    }


def _subject_from_payload(payload: object) -> TrustSubject:
    if not isinstance(payload, dict) or set(payload) != {
        "kind", "node_fingerprint", "opaque_user_id"
    }:
        raise ValueError("remote attestation subject has an invalid shape")
    return TrustSubject(
        str(payload["kind"]), str(payload["node_fingerprint"]),
        str(payload["opaque_user_id"]),
    )


def _signed_wire(envelope: dict[str, object], signing_key: nacl.signing.SigningKey) -> dict[str, object]:
    signature = signing_key.sign(canonical_bytes(envelope)).signature
    return {"envelope": envelope, "signature": base64.b64encode(signature).decode("ascii")}


def build_remote_attestation(
    signing_key: nacl.signing.SigningKey,
    *,
    issuer_fingerprint: str,
    subject: TrustSubject,
    attribute: str,
    attested_value: str,
    subject_opt_in: bool,
    issued_at: str,
    expires_at: str,
) -> dict[str, object]:
    attribute = _validate_attribute(attribute)
    attested_value = _validate_value(attribute, attested_value)
    issued = _parse_time(issued_at, "issued_at")
    expires = _parse_time(expires_at, "expires_at")
    if expires <= issued or expires > issued + _MAX_LIFETIME:
        raise ValueError("remote attestation lifetime must be positive and at most 365 days")
    envelope: dict[str, object] = {
        "netbbs_protocol": 1,
        "object_type": REMOTE_ATTESTATION_OBJECT_TYPE,
        "payload": {
            "issuer_fingerprint": issuer_fingerprint,
            "subject": _subject_payload(subject),
            "attribute": attribute,
            "attested_value": attested_value,
            "subject_opt_in": bool(subject_opt_in),
            "issued_at": issued_at,
            "expires_at": expires_at,
        },
    }
    return _signed_wire(envelope, signing_key)


def build_link_visible_remote_attestation(
    db: Database,
    user: User,
    signing_key: nacl.signing.SigningKey,
    *,
    home_node_fingerprint: str,
    attribute: str,
    issued_at: str,
    expires_at: str,
) -> dict[str, object]:
    """Export one local attestation only after the subject opted in.

    The password-only Link identity is the same stable username-based opaque
    identifier used by current ``node_vouched_user`` events. Re-verification
    clears the local opt-in, so callers cannot accidentally export a replaced
    value under stale consent.
    """
    attestation = get_attestation(db, user, _validate_attribute(attribute))
    if attestation is None or not attestation.link_visible:
        raise ValueError("local attestation is missing or not explicitly Link-visible")
    return build_remote_attestation(
        signing_key,
        issuer_fingerprint=home_node_fingerprint,
        subject=TrustSubject.user(home_node_fingerprint, user.username),
        attribute=attribute,
        attested_value=attestation.attested_value,
        subject_opt_in=True,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def build_remote_attestation_revocation(
    signing_key: nacl.signing.SigningKey,
    *,
    issuer_fingerprint: str,
    revoked_content_id: str,
    issued_at: str,
) -> dict[str, object]:
    _parse_time(issued_at, "issued_at")
    envelope: dict[str, object] = {
        "netbbs_protocol": 1,
        "object_type": REMOTE_ATTESTATION_REVOCATION_OBJECT_TYPE,
        "payload": {
            "issuer_fingerprint": issuer_fingerprint,
            "revoked_content_id": revoked_content_id,
            "issued_at": issued_at,
        },
    }
    return _signed_wire(envelope, signing_key)


def _verify_wire(
    wire: dict[str, object], verify_key: nacl.signing.VerifyKey
) -> tuple[dict[str, object], dict[str, object], str, str]:
    if set(wire) != {"envelope", "signature"} or not isinstance(wire["envelope"], dict):
        raise ValueError("signed remote attestation has an invalid shape")
    envelope = wire["envelope"]
    encoded = canonical_bytes(envelope)
    if len(encoded) > _MAX_WIRE_BYTES:
        raise ValueError("signed remote attestation exceeds the wire-size limit")
    try:
        signature = base64.b64decode(str(wire["signature"]), validate=True)
        verify_key.verify(encoded, signature)
    except (ValueError, nacl.exceptions.BadSignatureError) as exc:
        raise ValueError("remote attestation signature is invalid") from exc
    if set(envelope) != {"netbbs_protocol", "object_type", "payload"}:
        raise ValueError("remote attestation envelope has unknown fields")
    if envelope["netbbs_protocol"] != 1 or not isinstance(envelope["payload"], dict):
        raise ValueError("unsupported remote attestation protocol or payload")
    payload = envelope["payload"]
    content_id = hashlib.sha256(encoded).hexdigest()
    return envelope, payload, str(envelope["object_type"]), content_id


def configure_attestation_authority(
    db: Database,
    fingerprint: str,
    *,
    attributes: Iterable[str],
    reason: str,
    actor_user_id: int | None = None,
    now_iso: str | None = None,
) -> None:
    normalized = tuple(sorted({_validate_attribute(value) for value in attributes}))
    if not fingerprint or not normalized or not reason.strip():
        raise ValueError("authority fingerprint, scope, and reason are required")
    now_value, _ = _now(now_iso)
    with db.connection:
        exists = db.connection.execute(
            "SELECT 1 FROM link_attestation_authorities WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        db.connection.execute(
            """INSERT INTO link_attestation_authorities
               (fingerprint, reason, actor_user_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(fingerprint) DO UPDATE SET reason = excluded.reason,
                 actor_user_id = excluded.actor_user_id, updated_at = excluded.updated_at""",
            (fingerprint, reason, actor_user_id, now_value, now_value),
        )
        db.connection.execute(
            "DELETE FROM link_attestation_authority_scopes WHERE authority_fingerprint = ?",
            (fingerprint,),
        )
        db.connection.executemany(
            """INSERT INTO link_attestation_authority_scopes
               (authority_fingerprint, attribute) VALUES (?, ?)""",
            [(fingerprint, attribute) for attribute in normalized],
        )
        _audit(
            db, None, "authority", fingerprint, "updated" if exists else "created",
            {"attributes": normalized, "reason": reason}, actor_user_id, now_value,
        )
        _recompute_all(db, now_value)


def remove_attestation_authority(
    db: Database,
    fingerprint: str,
    *,
    actor_user_id: int | None = None,
    now_iso: str | None = None,
) -> None:
    now_value, _ = _now(now_iso)
    with db.connection:
        row = db.connection.execute(
            "SELECT reason FROM link_attestation_authorities WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if row is None:
            raise ValueError("attestation authority is missing or already removed")
        db.connection.execute(
            "DELETE FROM link_attestation_authorities WHERE fingerprint = ?", (fingerprint,)
        )
        _audit(db, None, "authority", fingerprint, "removed", {"reason": row["reason"]}, actor_user_id, now_value)
        _recompute_all(db, now_value)


def list_attestation_authorities(db: Database) -> list[AttestationAuthority]:
    rows = db.connection.execute(
        "SELECT fingerprint, reason, created_at FROM link_attestation_authorities ORDER BY fingerprint"
    ).fetchall()
    result: list[AttestationAuthority] = []
    for row in rows:
        scopes = db.connection.execute(
            """SELECT attribute FROM link_attestation_authority_scopes
               WHERE authority_fingerprint = ? ORDER BY attribute""",
            (row["fingerprint"],),
        ).fetchall()
        result.append(AttestationAuthority(row["fingerprint"], tuple(x["attribute"] for x in scopes), row["reason"], row["created_at"]))
    return result


def ingest_remote_attestation(
    db: Database,
    wire: dict[str, object],
    *,
    issuer_verify_key: nacl.signing.VerifyKey,
    now_iso: str | None = None,
) -> str:
    envelope, payload, object_type, content_id = _verify_wire(wire, issuer_verify_key)
    now_value, now = _now(now_iso)
    issuer = str(payload.get("issuer_fingerprint", ""))
    if not issuer:
        raise ValueError("remote attestation issuer is required")
    issued = _parse_time(str(payload.get("issued_at", "")), "issued_at")
    # Stored in this module's sortable format, never as the issuer wrote it:
    # these columns are compared as strings against `now_value`, and a peer's
    # `+00:00` spelling sorts before a `Z` one at the same instant. The signed
    # bytes are untouched -- they live in `envelope_json`.
    issued_at = _stamp(issued)
    if issued > now + _FUTURE_TOLERANCE:
        raise ValueError("remote attestation is too far in the future")
    signature_b64 = str(wire["signature"])
    if object_type == REMOTE_ATTESTATION_REVOCATION_OBJECT_TYPE:
        expected = {"issuer_fingerprint", "revoked_content_id", "issued_at"}
        if set(payload) != expected:
            raise ValueError("remote attestation revocation has unknown fields")
        revoked_id = str(payload["revoked_content_id"])
        with db.connection:
            target = db.connection.execute(
                """SELECT subject_id, issuer_fingerprint FROM link_remote_attestations
                   WHERE content_id = ?""", (revoked_id,)
            ).fetchone()
            if target is None or target["issuer_fingerprint"] != issuer:
                raise ValueError("revocation target is unknown or belongs to another issuer")
            db.connection.execute(
                """INSERT OR IGNORE INTO link_remote_attestation_revocations
                   (content_id, issuer_fingerprint, revoked_content_id, envelope_json,
                    signature_b64, issued_at, received_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (content_id, issuer, revoked_id, json.dumps(envelope, sort_keys=True), signature_b64, issued_at, now_value),
            )
            db.connection.execute(
                """UPDATE link_remote_attestations SET revoked_by_content_id = ?, revoked_at = ?
                   WHERE content_id = ? AND revoked_at IS NULL""",
                (content_id, now_value, revoked_id),
            )
            # Issue #596, Decision 6: told that the subject withdrew it, this
            # node forgets the value rather than merely stops relying on it.
            _forget_received_values(db, now_value, content_id=revoked_id)
            _audit(db, target["subject_id"], "attestation", revoked_id, "revoked", {"revocation_content_id": content_id}, None, now_value)
            _recompute_subject_id(db, target["subject_id"], now_value)
        return content_id
    if object_type != REMOTE_ATTESTATION_OBJECT_TYPE:
        raise ValueError(f"unsupported remote attestation object type: {object_type!r}")
    expected = {
        "issuer_fingerprint", "subject", "attribute", "attested_value",
        "subject_opt_in", "issued_at", "expires_at",
    }
    if set(payload) != expected:
        raise ValueError("remote attestation payload has unknown fields")
    if payload["subject_opt_in"] is not True:
        raise ValueError("remote attestation lacks the subject's explicit Link opt-in")
    subject = _subject_from_payload(payload["subject"])
    attribute = _validate_attribute(str(payload["attribute"]))
    value = _validate_value(attribute, str(payload["attested_value"]))
    expires = _parse_time(str(payload["expires_at"]), "expires_at")
    expires_at = _stamp(expires)
    if expires <= issued or expires > issued + _MAX_LIFETIME:
        raise ValueError("remote attestation lifetime must be positive and at most 365 days")
    with db.connection:
        # Rate was bounded per page and per pass; nothing bounded what a
        # configured-but-compromised authority could accumulate on disk by
        # minting fresh objects for a subject it had already attested
        # (design doc §12.7's "1,000 active signals per issuer", which the
        # attestation side had no counterpart for -- Codex review of #590).
        active = db.connection.execute(
            """SELECT COUNT(*) FROM link_remote_attestations
               WHERE issuer_fingerprint = ? AND revoked_at IS NULL AND expires_at > ?""",
            (issuer, now_value),
        ).fetchone()[0]
        if active >= MAX_ACTIVE_ATTESTATIONS_PER_ISSUER:
            raise ValueError(
                f"issuer {issuer} is at its active-attestation quota "
                f"({MAX_ACTIVE_ATTESTATIONS_PER_ISSUER})"
            )
        if not db.connection.execute(
            "SELECT 1 FROM link_trust_subjects WHERE subject_id = ?", (subject.subject_id,)
        ).fetchone():
            raise UnknownAttestationSubject(
                "remote attestation subject must already be a verified Link identity"
            )
        db.connection.execute(
            """INSERT OR IGNORE INTO link_remote_attestations
               (content_id, issuer_fingerprint, subject_id, attribute, attested_value,
                subject_opt_in, issued_at, expires_at, envelope_json, signature_b64, received_at)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)""",
            (content_id, issuer, subject.subject_id, attribute, value, issued_at, expires_at,
             json.dumps(envelope, sort_keys=True), signature_b64, now_value),
        )
        _audit(db, subject.subject_id, "attestation", content_id, "received", {"issuer": issuer, "attribute": attribute}, None, now_value)
        _recompute_subject_id(db, subject.subject_id, now_value)
    return content_id


def _forget_received_values(
    db: Database, now_value: str, *, content_id: str | None = None
) -> int:
    """Blank the value-bearing bytes of received attestations that are retired.

    The row stays: `link_remote_attestation_revocations`, the effective
    projection and the audit trail all reference it, and its content ID is what
    makes a re-offered copy of the same object an `INSERT OR IGNORE` no-op
    rather than a way to get the value back. `_recompute` selects only
    unrevoked, unexpired candidates, so nothing that reads a value can reach a
    row this has touched.
    """
    where = "redacted_at IS NULL AND (revoked_at IS NOT NULL OR expires_at <= ?)"
    parameters: list[object] = [now_value, now_value]
    if content_id is not None:
        where += " AND content_id = ?"
        parameters.append(content_id)
    return db.connection.execute(
        f"""UPDATE link_remote_attestations
            SET attested_value = '', envelope_json = '', signature_b64 = '', redacted_at = ?
            WHERE {where}""",
        parameters,
    ).rowcount


def forget_retired_remote_attestations(db: Database, *, now_iso: str | None = None) -> int:
    """Forget every received value whose attestation has been revoked or has expired.

    A revocation is forgotten as it is ingested; expiry has no event to hang
    that on, so the sync pass calls this. Returns how many rows it blanked.
    """
    now_value, _ = _now(now_iso)
    with db.connection:
        return _forget_received_values(db, now_value)


def _issuer_is_locally_usable(db: Database, fingerprint: str) -> tuple[bool, str]:
    row = db.connection.execute(
        """SELECT subject_kind, node_fingerprint, opaque_user_id
           FROM link_trust_subjects WHERE subject_kind = 'node' AND node_fingerprint = ?""",
        (fingerprint,),
    ).fetchone()
    if row is None:
        return True, "explicit_attestation_authority"
    subject = TrustSubject(row["subject_kind"], row["node_fingerprint"], row["opaque_user_id"])
    state = get_effective_trust_state(db, subject, TrustDimension.IDENTITY_INTEGRITY)
    return state.state == TrustState.ESTABLISHED, f"authority_{state.state.value}"


def _subject_from_id(db: Database, subject_id: str) -> TrustSubject:
    row = db.connection.execute(
        """SELECT subject_kind, node_fingerprint, opaque_user_id
           FROM link_trust_subjects WHERE subject_id = ?""", (subject_id,)
    ).fetchone()
    if row is None:
        raise ValueError("unknown remote attestation subject")
    return TrustSubject(row["subject_kind"], row["node_fingerprint"], row["opaque_user_id"])


def _recompute_subject_id(db: Database, subject_id: str, now_value: str) -> None:
    subject = _subject_from_id(db, subject_id)
    for attribute in sorted(_ATTRIBUTES):
        _recompute(db, subject, attribute, now_value)


def _recompute_all(db: Database, now_value: str) -> None:
    rows = db.connection.execute("SELECT DISTINCT subject_id FROM link_remote_attestations").fetchall()
    for row in rows:
        _recompute_subject_id(db, row["subject_id"], now_value)


def _recompute(db: Database, subject: TrustSubject, attribute: str, now_value: str) -> None:
    override = db.connection.execute(
        """SELECT override_id, accepted, reason FROM link_remote_attestation_overrides
           WHERE subject_id = ? AND attribute = ? AND cleared_at IS NULL
           ORDER BY override_id DESC LIMIT 1""",
        (subject.subject_id, attribute),
    ).fetchone()
    candidates = db.connection.execute(
        """SELECT a.* FROM link_remote_attestations AS a
           WHERE a.subject_id = ? AND a.attribute = ? AND a.revoked_at IS NULL
             AND a.expires_at > ? ORDER BY a.issued_at DESC, a.content_id DESC""",
        (subject.subject_id, attribute, now_value),
    ).fetchall()
    usable = []
    rejected_issuers: list[dict[str, str]] = []
    for candidate in candidates:
        configured = db.connection.execute(
            """SELECT 1 FROM link_attestation_authority_scopes
               WHERE authority_fingerprint = ? AND attribute = ?""",
            (candidate["issuer_fingerprint"], attribute),
        ).fetchone()
        if not configured:
            rejected_issuers.append(
                {"issuer": candidate["issuer_fingerprint"], "reason": "authority_not_configured_for_attribute"}
            )
            continue
        allowed, reason = _issuer_is_locally_usable(db, candidate["issuer_fingerprint"])
        if allowed:
            usable.append(candidate)
        else:
            rejected_issuers.append({"issuer": candidate["issuer_fingerprint"], "reason": reason})
    selected = usable[0] if usable else None
    if override is not None:
        accepted = bool(override["accepted"])
        reason_code = "sysop_accept" if accepted else "sysop_reject"
        if accepted and candidates:
            selected = candidates[0]
        elif accepted:
            accepted = False
            reason_code = "sysop_accept_without_current_attestation"
        explanation: dict[str, object] = {
            "override_id": int(override["override_id"]), "override_reason": override["reason"],
            "rejected_issuers": rejected_issuers,
        }
    elif selected is not None:
        accepted = True
        reason_code = "trusted_attestation_authority"
        explanation = {"issuer": selected["issuer_fingerprint"], "expires_at": selected["expires_at"]}
    else:
        accepted = False
        reason_code = "no_current_trusted_attestation"
        explanation = {"rejected_issuers": rejected_issuers}
    content_id = selected["content_id"] if selected is not None else None
    db.connection.execute(
        """INSERT INTO link_remote_attestation_effective
           (subject_id, attribute, accepted, reason_code, attestation_content_id,
            explanation_json, evaluated_at) VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(subject_id, attribute) DO UPDATE SET
             accepted = excluded.accepted, reason_code = excluded.reason_code,
             attestation_content_id = excluded.attestation_content_id,
             explanation_json = excluded.explanation_json, evaluated_at = excluded.evaluated_at""",
        (subject.subject_id, attribute, int(accepted), reason_code, content_id,
         json.dumps(explanation, sort_keys=True, separators=(",", ":")), now_value),
    )


def get_remote_attestation_state(
    db: Database,
    subject: TrustSubject,
    attribute: str,
    *,
    now_iso: str | None = None,
) -> RemoteAttestationState:
    attribute = _validate_attribute(attribute)
    now_value, _ = _now(now_iso)
    if db.connection.execute(
        "SELECT 1 FROM link_trust_subjects WHERE subject_id = ?", (subject.subject_id,)
    ).fetchone() is None:
        # An identity this node has never accepted anything from.  Answered
        # rather than recomputed: `link_remote_attestation_effective` has a
        # foreign key to `link_trust_subjects`, so persisting a projection for
        # an unregistered subject fails outright -- and an unknown identity has
        # no accepted attestation anyway, which is the same answer without the
        # write.  This is reachable from every gate (`remote_meets_age`,
        # `remote_meets_name_requirement`, `format_remote_name_for_resource`),
        # which before issue #584 had no production caller to reach it.
        return RemoteAttestationState(
            subject, attribute, False, "unknown_link_identity", None,
            {"rejected_issuers": []}, now_value,
        )
    with db.connection:
        _recompute(db, subject, attribute, now_value)
    row = db.connection.execute(
        """SELECT * FROM link_remote_attestation_effective
           WHERE subject_id = ? AND attribute = ?""",
        (subject.subject_id, attribute),
    ).fetchone()
    attestation = None
    if row["attestation_content_id"] is not None:
        source = db.connection.execute(
            "SELECT * FROM link_remote_attestations WHERE content_id = ?",
            (row["attestation_content_id"],),
        ).fetchone()
        attestation = RemoteAttestation(
            source["content_id"], source["issuer_fingerprint"], subject,
            source["attribute"], source["attested_value"], source["issued_at"],
            source["expires_at"], source["revoked_at"],
        )
    return RemoteAttestationState(
        subject, attribute, bool(row["accepted"]), row["reason_code"], attestation,
        json.loads(row["explanation_json"]), row["evaluated_at"],
    )


def set_remote_attestation_override(
    db: Database,
    subject: TrustSubject,
    attribute: str,
    *,
    accepted: bool,
    reason: str,
    actor_user_id: int | None = None,
    now_iso: str | None = None,
) -> int:
    attribute = _validate_attribute(attribute)
    if not reason.strip():
        raise ValueError("remote attestation override reason is required")
    now_value, _ = _now(now_iso)
    with db.connection:
        db.connection.execute(
            """UPDATE link_remote_attestation_overrides SET cleared_at = ?
               WHERE subject_id = ? AND attribute = ? AND cleared_at IS NULL""",
            (now_value, subject.subject_id, attribute),
        )
        cursor = db.connection.execute(
            """INSERT INTO link_remote_attestation_overrides
               (subject_id, attribute, accepted, reason, actor_user_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (subject.subject_id, attribute, int(accepted), reason, actor_user_id, now_value),
        )
        _audit(db, subject.subject_id, "override", str(cursor.lastrowid), "created", {"attribute": attribute, "accepted": accepted, "reason": reason}, actor_user_id, now_value)
        _recompute(db, subject, attribute, now_value)
        return int(cursor.lastrowid)


def clear_remote_attestation_override(
    db: Database,
    override_id: int,
    *,
    actor_user_id: int | None = None,
    now_iso: str | None = None,
) -> None:
    now_value, _ = _now(now_iso)
    with db.connection:
        row = db.connection.execute(
            """SELECT subject_id, attribute FROM link_remote_attestation_overrides
               WHERE override_id = ? AND cleared_at IS NULL""", (override_id,)
        ).fetchone()
        if row is None:
            raise ValueError("remote attestation override is missing or already cleared")
        db.connection.execute(
            "UPDATE link_remote_attestation_overrides SET cleared_at = ? WHERE override_id = ?",
            (now_value, override_id),
        )
        _audit(db, row["subject_id"], "override", str(override_id), "cleared", {"attribute": row["attribute"]}, actor_user_id, now_value)
        _recompute(db, _subject_from_id(db, row["subject_id"]), row["attribute"], now_value)


def list_remote_attestation_overrides(
    db: Database, subject: TrustSubject
) -> list[RemoteAttestationOverride]:
    rows = db.connection.execute(
        """SELECT override_id, subject_id, attribute, accepted, reason, created_at
           FROM link_remote_attestation_overrides
           WHERE subject_id = ? AND cleared_at IS NULL ORDER BY override_id DESC""",
        (subject.subject_id,),
    ).fetchall()
    return [
        RemoteAttestationOverride(
            int(row["override_id"]), row["subject_id"], row["attribute"],
            bool(row["accepted"]), row["reason"], row["created_at"],
        )
        for row in rows
    ]


def list_remote_attestation_audit(
    db: Database, subject: TrustSubject | None = None, *, limit: int = 100
) -> list[RemoteAttestationAudit]:
    if limit < 1:
        raise ValueError("audit limit must be positive")
    if subject is None:
        rows = db.connection.execute(
            """SELECT * FROM link_remote_attestation_audit
               ORDER BY audit_id DESC LIMIT ?""", (limit,)
        ).fetchall()
    else:
        rows = db.connection.execute(
            """SELECT * FROM link_remote_attestation_audit
               WHERE subject_id = ? ORDER BY audit_id DESC LIMIT ?""",
            (subject.subject_id, limit),
        ).fetchall()
    return [
        RemoteAttestationAudit(
            int(row["audit_id"]), row["subject_id"], row["object_kind"],
            row["object_id"], row["action"], json.loads(row["details_json"]),
            row["actor_user_id"], row["created_at"],
        )
        for row in rows
    ]


def remote_meets_age(
    db: Database, subject: TrustSubject, min_age: int | None, *, now_iso: str | None = None
) -> bool:
    if not min_age:
        return True
    state = get_remote_attestation_state(db, subject, "age", now_iso=now_iso)
    if not state.accepted or state.attestation is None:
        return False
    today = _parse_time(now_iso, "now").date() if now_iso is not None else None
    return compute_age(date.fromisoformat(state.attestation.attested_value), today=today) >= min_age


def remote_meets_name_requirement(
    db: Database, subject: TrustSubject, requirement: str | None, *, now_iso: str | None = None
) -> bool:
    if requirement is None:
        return True
    return get_remote_attestation_state(db, subject, "name", now_iso=now_iso).accepted


def format_remote_name_for_resource(
    db: Database,
    subject: TrustSubject,
    display_label: str,
    *,
    name_requirement: str | None,
    now_iso: str | None = None,
) -> str:
    """Render an accepted remote real name only inside a requiring resource."""
    primary = sanitize_text(display_label)
    if name_requirement != "verified_and_displayed":
        return primary
    state = get_remote_attestation_state(db, subject, "name", now_iso=now_iso)
    if not state.accepted or state.attestation is None:
        return primary
    unit = colored(
        f"(={sanitize_text(state.attestation.attested_value)}=)",
        fg_color=VERIFIED_COLOR,
    )
    return f"{primary} {unit}"


def _audit(
    db: Database,
    subject_id: str | None,
    object_kind: str,
    object_id: str,
    action: str,
    details: dict[str, object],
    actor_user_id: int | None,
    now_value: str,
) -> None:
    db.connection.execute(
        """INSERT INTO link_remote_attestation_audit
           (subject_id, object_kind, object_id, action, details_json, actor_user_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (subject_id, object_kind, object_id, action,
         json.dumps(details, sort_keys=True, separators=(",", ":")), actor_user_id, now_value),
    )


# -- the issuing half (issue #584) -------------------------------------------
#
# Everything above this line is the *receiving* half: verify a peer's signed
# object, store it, and project a local acceptance decision.  Until issue #584
# that was all there was, so `link_remote_attestations` was empty on every node
# in production and the Profile screen's "Share verified age/name over Link"
# toggle recorded a consent nothing ever acted on.
#
# The issuing half below closes that loop.  A node signs its own users'
# Link-visible attestations, retires them with signed revocations when consent
# or the attested value changes, and serves both to the subscribers that have
# configured it as an attestation authority.

ATTESTATION_PULL_REQUEST_OBJECT_TYPE = "remote_attestation_pull_request"

# What the pull endpoint answers a node that is not on the recipient list, in
# the `reason_code` field a policy rejection already carries. Wire-visible: a
# subscriber matches on it to tell its SysOp what to ask for.
NOT_AN_ATTESTATION_RECIPIENT_REASON_CODE = "not_an_attestation_recipient"

MAX_ATTESTATION_OBJECTS_PER_RESPONSE = 100
MAX_ATTESTATION_RESPONSE_BYTES = 1024 * 1024

# Mirrors §12.7's per-issuer active-signal bound for the attestation family.
# Deliberately generous next to any real node's user count: this is a stop
# against unbounded growth, not a working limit anyone should reach.
MAX_ACTIVE_ATTESTATIONS_PER_ISSUER = 1000

# Well inside the 365-day protocol ceiling `build_remote_attestation` enforces.
# The ceiling is what a receiver must tolerate; this is what this node chooses
# to assert.  A node that goes dark cannot withdraw consent it has already
# published, so the shorter the issued lifetime, the shorter the window in
# which an opt-out that never reached a subscriber still leaves a live
# assertion standing there.  Ninety days keeps that window to a quarter while
# renewing rarely enough that an ordinary sync interval never notices.
ISSUED_ATTESTATION_LIFETIME = timedelta(days=90)

# Re-issue once a third of the lifetime remains, so an intermittently connected
# subscriber has many sync passes in which to see the replacement before the
# one it holds expires.  The old object is left to expire rather than revoked:
# `_recompute` selects the newest unexpired candidate, so an overlap is a
# seamless handover, and a revocation would only tell a subscriber to stop
# trusting a value this node is simultaneously re-asserting.
ISSUED_ATTESTATION_RENEW_WITHIN = timedelta(days=30)


@dataclass(frozen=True)
class IssuedAttestationChange:
    """One signing action `reconcile_issued_attestations` took."""

    action: str  # "issued" | "renewed" | "revoked"
    content_id: str
    user_id: int | None
    attribute: str
    reason: str


def configure_attestation_recipient(
    db: Database,
    fingerprint: str,
    *,
    reason: str,
    actor_user_id: int | None = None,
    now_iso: str | None = None,
) -> None:
    """Name `fingerprint` as a node this one serves its signed attestations to.

    The issuing mirror of `configure_attestation_authority`, and deliberately
    narrower: a recipient is a node, with no attribute scope (design doc §16,
    issue #596, Decision 2). The caller already scopes per attribute with two
    toggles, and a per-attribute grant would make a requester's stream depend
    on its grant history, which a subscriber-owned position cursor cannot
    express.
    """
    if not fingerprint or not reason.strip():
        raise ValueError("recipient fingerprint and reason are required")
    now_value, _ = _now(now_iso)
    with db.connection:
        exists = db.connection.execute(
            "SELECT 1 FROM link_attestation_recipients WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        db.connection.execute(
            """INSERT INTO link_attestation_recipients
               (fingerprint, reason, actor_user_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(fingerprint) DO UPDATE SET reason = excluded.reason,
                 actor_user_id = excluded.actor_user_id, updated_at = excluded.updated_at""",
            (fingerprint, reason, actor_user_id, now_value, now_value),
        )
        _audit(
            db, None, "recipient", fingerprint, "updated" if exists else "created",
            {"reason": reason}, actor_user_id, now_value,
        )


def remove_attestation_recipient(
    db: Database,
    fingerprint: str,
    *,
    actor_user_id: int | None = None,
    now_iso: str | None = None,
) -> None:
    """Stop serving `fingerprint`. A statement about the future only.

    What the node already pulled stays with it until each object's own expiry;
    a removed recipient is refused outright, so it receives no further
    revocations either (Decision 3 records why that cost is accepted).
    """
    now_value, _ = _now(now_iso)
    with db.connection:
        row = db.connection.execute(
            "SELECT reason FROM link_attestation_recipients WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if row is None:
            raise ValueError("attestation recipient is missing or already removed")
        db.connection.execute(
            "DELETE FROM link_attestation_recipients WHERE fingerprint = ?", (fingerprint,)
        )
        _audit(db, None, "recipient", fingerprint, "removed", {"reason": row["reason"]}, actor_user_id, now_value)


def list_attestation_recipients(db: Database) -> list[AttestationRecipient]:
    return [
        AttestationRecipient(row["fingerprint"], row["reason"], row["created_at"])
        for row in db.connection.execute(
            "SELECT fingerprint, reason, created_at FROM link_attestation_recipients ORDER BY fingerprint"
        ).fetchall()
    ]


def count_attestation_recipients(db: Database) -> int:
    """How many nodes a caller's shared attestation can currently reach.

    What the Profile toggle shows (Decision 4): the caller is told how many,
    the SysOp sees which.
    """
    return int(
        db.connection.execute("SELECT COUNT(*) FROM link_attestation_recipients").fetchone()[0]
    )


def is_attestation_recipient(db: Database, fingerprint: str) -> bool:
    return db.connection.execute(
        "SELECT 1 FROM link_attestation_recipients WHERE fingerprint = ?", (fingerprint,)
    ).fetchone() is not None


def list_attestation_authority_fingerprints(db: Database) -> list[str]:
    """This node's explicit attestation subscription set, in stable order.

    The attestation analogue of `trust_wire.list_trusted_reporter_fingerprints`,
    and deliberately a separate set: design doc §5.5 and §12.3 both state that
    reporter or vouch configuration grants no attestation authority.
    """
    return [
        row[0]
        for row in db.connection.execute(
            "SELECT fingerprint FROM link_attestation_authorities ORDER BY fingerprint"
        ).fetchall()
    ]


def _issued_active_row(db: Database, user_id: int, attribute: str, now_value: str):
    return db.connection.execute(
        """SELECT content_id, attested_value, expires_at, signing_key_fingerprint
           FROM link_issued_remote_attestations
           WHERE object_type = ? AND user_id = ? AND attribute = ?
             AND revoked_at IS NULL AND expires_at > ?
           ORDER BY issued_at DESC, content_id DESC LIMIT 1""",
        (REMOTE_ATTESTATION_OBJECT_TYPE, user_id, attribute, now_value),
    ).fetchone()


def _store_issued(
    db: Database,
    wire: dict[str, object],
    *,
    object_type: str,
    user_id: int | None,
    attribute: str | None,
    attested_value: str | None,
    issued_at: str,
    expires_at: str | None,
    now_value: str,
    signing_key_fingerprint: str,
) -> str:
    envelope = wire["envelope"]
    content_id = hashlib.sha256(canonical_bytes(envelope)).hexdigest()
    db.connection.execute(
        """INSERT OR IGNORE INTO link_issued_remote_attestations
           (content_id, object_type, user_id, attribute, attested_value, envelope_json,
            signature_b64, issued_at, expires_at, signing_key_fingerprint, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (content_id, object_type, user_id, attribute, attested_value,
         json.dumps(envelope, sort_keys=True), str(wire["signature"]),
         issued_at, expires_at, signing_key_fingerprint, now_value),
    )
    return content_id


def _redact_retired_issued(db: Database, now_value: str) -> int:
    """Blank the value-bearing columns of issued attestations that are retired.

    Issue #596, Decision 5. Revocation used to stamp the row and leave the
    envelope, with `attested_value` inside it, to be served to whoever pulled
    from the start next year. The row stays as a tombstone: a subscriber whose
    cursor names it must still resolve to a position, and the SysOp's history
    listing still has something to show. Revocation objects carry no value and
    are never touched.
    """
    return db.connection.execute(
        """UPDATE link_issued_remote_attestations
           SET attested_value = NULL, envelope_json = '', signature_b64 = '', redacted_at = ?
           WHERE object_type = ? AND redacted_at IS NULL
             AND (revoked_at IS NOT NULL OR expires_at <= ?)""",
        (now_value, REMOTE_ATTESTATION_OBJECT_TYPE, now_value),
    ).rowcount


def _revoke_issued(
    db: Database,
    signing_identity: Identity,
    row,
    *,
    home_node_fingerprint: str,
    now_value: str,
) -> str:
    wire = build_remote_attestation_revocation(
        signing_identity.signing_key,
        issuer_fingerprint=home_node_fingerprint,
        revoked_content_id=row["content_id"],
        issued_at=now_value,
    )
    content_id = _store_issued(
        db, wire,
        object_type=REMOTE_ATTESTATION_REVOCATION_OBJECT_TYPE,
        user_id=None, attribute=None, attested_value=None,
        issued_at=now_value, expires_at=None, now_value=now_value,
        signing_key_fingerprint=signing_identity.fingerprint,
    )
    db.connection.execute(
        """UPDATE link_issued_remote_attestations
           SET revoked_by_content_id = ?, revoked_at = ?
           WHERE content_id = ? AND revoked_at IS NULL""",
        (content_id, now_value, row["content_id"]),
    )
    return content_id


def reconcile_issued_attestations(
    db: Database,
    signing_identity: Identity,
    *,
    home_node_fingerprint: str,
    now_iso: str | None = None,
) -> list[IssuedAttestationChange]:
    """Bring this node's signed attestation objects in line with local consent.

    Called once per sync pass rather than from the Profile screen, because
    signing needs the node's current operational key and a caller session has
    no business holding it.  The lag is one sync interval in either direction,
    which is already the granularity at which anything reaches a subscriber.

    Mints an object for every Link-visible attestation that has none, renews
    one approaching expiry, and signs a revocation whenever the consent behind
    a live object has gone: the toggle switched off, the attestation removed,
    the value re-verified (which clears `link_visible` anyway), or the account
    deleted.  A deleted account is why the `user_id` column is `SET NULL`
    rather than `CASCADE` -- the signed row has to outlive the account long
    enough to be revoked, or subscribers would hold a live assertion about a
    user who no longer exists until it expired on its own.
    """
    now_value, now = _now(now_iso)
    signing_fingerprint = signing_identity.fingerprint
    changes: list[IssuedAttestationChange] = []
    with db.connection:
        consented = {
            (int(row["subject_user_id"]), str(row["attribute"])): str(row["attested_value"])
            for row in db.connection.execute(
                """SELECT subject_user_id, attribute, attested_value FROM user_attestations
                   WHERE link_visible = 1"""
            ).fetchall()
        }
        live = db.connection.execute(
            """SELECT content_id, user_id, attribute, attested_value, expires_at
               FROM link_issued_remote_attestations
               WHERE object_type = ? AND revoked_at IS NULL AND expires_at > ?""",
            (REMOTE_ATTESTATION_OBJECT_TYPE, now_value),
        ).fetchall()
        for row in live:
            user_id = row["user_id"]
            key = (int(user_id), str(row["attribute"])) if user_id is not None else None
            if user_id is None:
                reason = "account_removed"
            elif key not in consented:
                reason = "consent_withdrawn"
            elif consented[key] != row["attested_value"]:
                reason = "attested_value_replaced"
            else:
                continue
            content_id = _revoke_issued(
                db, signing_identity, row,
                home_node_fingerprint=home_node_fingerprint, now_value=now_value,
            )
            changes.append(IssuedAttestationChange(
                "revoked", content_id, user_id, str(row["attribute"]), reason,
            ))

        expires_at = _stamp(now + ISSUED_ATTESTATION_LIFETIME)
        renew_before = _stamp(now + ISSUED_ATTESTATION_RENEW_WITHIN)
        for (user_id, attribute), attested_value in sorted(consented.items()):
            active = _issued_active_row(db, user_id, attribute, now_value)
            if active is not None and active["attested_value"] == attested_value:
                if active["signing_key_fingerprint"] != signing_fingerprint:
                    # This node rotated its operational key. A subscriber
                    # resolves only the *current* key, so it cannot verify
                    # what the old one signed -- leaving the object in place
                    # would silently stop the attestation working until its
                    # ordinary renewal, months away (Codex review of #590).
                    action, reason = "renewed", "signing_key_rotated"
                elif active["expires_at"] > renew_before:
                    continue
                else:
                    action, reason = "renewed", "approaching_expiry"
            else:
                action, reason = "issued", "consent_granted"
            user = get_user_by_id(db, user_id)
            if user is None:
                # The account vanished between the two queries above; the
                # revocation sweep on the next pass owns it.
                continue
            try:
                wire = build_link_visible_remote_attestation(
                    db, user, signing_identity.signing_key,
                    home_node_fingerprint=home_node_fingerprint,
                    attribute=attribute,
                    issued_at=now_value,
                    expires_at=expires_at,
                )
            except ValueError as exc:
                current = get_attestation(db, user, attribute)
                if (
                    current is not None
                    and current.link_visible
                    and current.attested_value == attested_value
                ):
                    # Not the race the next pass resolves: consent and the
                    # value are both unchanged, so this one can never be
                    # exported (a real name past the wire's 128-byte limit is
                    # the reachable case). Reported rather than swallowed, or
                    # the caller's toggle stays on with nothing behind it and
                    # nothing says why (Codex review of #590).
                    changes.append(IssuedAttestationChange(
                        "refused", "", user_id, attribute, f"not_exportable: {exc}",
                    ))
                continue
            content_id = _store_issued(
                db, wire,
                object_type=REMOTE_ATTESTATION_OBJECT_TYPE,
                user_id=user_id, attribute=attribute, attested_value=attested_value,
                issued_at=now_value, expires_at=expires_at, now_value=now_value,
                signing_key_fingerprint=signing_fingerprint,
            )
            changes.append(IssuedAttestationChange(action, content_id, user_id, attribute, reason))
        # After the revocations above, so an object retired this pass loses its
        # value in the transaction that retires it; and unconditionally, so an
        # object that merely ran out -- which nothing revokes -- loses it too.
        _redact_retired_issued(db, now_value)
    return changes


@dataclass(frozen=True)
class IssuedAttestation:
    """One object this node has signed about one of its own users.

    Deliberately carries no `attested_value`. The SysOp's question here is
    *what is my node asserting about whom, and until when* -- the value itself
    is a verified birthdate or real name they already hold, and design doc
    §5.5 keeps a verified name off screens that do not require it. A listing
    of everything the node publishes is exactly such a screen.
    """

    content_id: str
    user_id: int | None
    username: str | None
    attribute: str
    issued_at: str
    expires_at: str
    revoked_at: str | None
    status: str  # "published" | "withdrawing" | "expired" | "revoked"

    @property
    def is_live(self) -> bool:
        """Whether a subscriber that pulled today would act on this."""
        return self.status in {"published", "withdrawing"}


def list_issued_attestations(
    db: Database,
    *,
    include_inactive: bool = False,
    limit: int = 500,
    now_iso: str | None = None,
) -> list[IssuedAttestation]:
    """What this node currently publishes about its own users.

    The operator view of the issuing half. Without it a SysOp can see every
    attestation the node has *accepted* (`list_remote_attestation_overrides`,
    `list_remote_attestation_audit`) and nothing it *asserts*, which is the
    half that leaves their node.

    `status` is derived from the same predicate `reconcile_issued_attestations`
    revokes on, so the screen cannot claim a different answer from the pass
    that acts on it: a live object whose consent has gone -- toggle off,
    attestation removed, value re-verified, account deleted -- reads as
    `withdrawing`, and the next pass signs its revocation.
    """
    now_value, _ = _now(now_iso)
    rows = db.connection.execute(
        """SELECT i.content_id, i.user_id, u.username, i.attribute, i.attested_value,
                  i.issued_at, i.expires_at, i.revoked_at,
                  a.link_visible AS consented, a.attested_value AS consented_value
           FROM link_issued_remote_attestations AS i
           LEFT JOIN users AS u ON u.id = i.user_id
           LEFT JOIN user_attestations AS a
                  ON a.subject_user_id = i.user_id AND a.attribute = i.attribute
           WHERE i.object_type = ?
             {live_only}
           ORDER BY i.created_at DESC, i.content_id DESC
           LIMIT ?""".format(
            # The limit bounds history, never the current picture. Applying it
            # before the live/inactive split meant a node with enough retired
            # objects could push its own live ones off the end of the SysOp's
            # listing -- and off the withdrawal picker with them, so they
            # could be neither seen nor stopped (Codex review of #590).
            live_only="" if include_inactive else "AND i.revoked_at IS NULL AND i.expires_at > ?"
        ),
        (REMOTE_ATTESTATION_OBJECT_TYPE, max(1, limit))
        if include_inactive
        else (REMOTE_ATTESTATION_OBJECT_TYPE, now_value, max(1, limit)),
    ).fetchall()
    result: list[IssuedAttestation] = []
    for row in rows:
        if row["revoked_at"] is not None:
            status = "revoked"
        elif row["expires_at"] <= now_value:
            status = "expired"
        elif not row["consented"] or row["consented_value"] != row["attested_value"]:
            status = "withdrawing"
        else:
            status = "published"
        record = IssuedAttestation(
            row["content_id"], row["user_id"], row["username"], row["attribute"],
            row["issued_at"], row["expires_at"], row["revoked_at"], status,
        )
        if include_inactive or record.is_live:
            result.append(record)
    return result


def load_issued_attestation_page(
    db: Database,
    *,
    requester_fingerprint: str,
    after_content_id: str | None = None,
    limit: int = MAX_ATTESTATION_OBJECTS_PER_RESPONSE,
    now_iso: str | None = None,
) -> tuple[list[dict[str, object]], bool]:
    """Return one byte-bounded page of what `requester_fingerprint` may read.

    `requester_fingerprint` is required, not defaulted: issue #596 was this
    function taking no requester at all, so every peer the ordinary trust
    policy admitted read every value this node had ever signed. A requester
    that is not a recipient raises `NotAnAttestationRecipient` -- refused
    outright rather than served the value-free part of the stream, because
    any page advances the requester's cursor, and a cursor that has moved past
    attestations it was not shown would never deliver them after a later grant.
    Checked before the cursor is looked up, so a node that is not a recipient
    cannot use "unknown cursor" to ask which content IDs exist here.

    Ordered by insertion. Serves every revocation, and only those attestations
    that are live as of this read: a retired one has had its value blanked
    (`_redact_retired_issued`), and filtering on liveness here as well means
    what is served never depends on when that sweep last ran. The stream stays
    resumable because a cursor names a position and a redacted row keeps its
    position: a returning subscriber needs the revocation of anything it
    holds, and that revocation is always later in the stream than the object
    it retires.
    """
    if not is_attestation_recipient(db, requester_fingerprint):
        raise NotAnAttestationRecipient(
            "this node has not named the requester as an attestation recipient"
        )
    now_value, _ = _now(now_iso)
    limit = max(1, min(limit, MAX_ATTESTATION_OBJECTS_PER_RESPONSE))
    after_rowid = 0
    if after_content_id:
        row = db.connection.execute(
            "SELECT rowid FROM link_issued_remote_attestations WHERE content_id = ?",
            (after_content_id,),
        ).fetchone()
        if row is None:
            raise UnknownAttestationPullCursor("unknown attestation pull cursor")
        after_rowid = row[0]
    rows = db.connection.execute(
        """SELECT content_id, envelope_json, signature_b64
           FROM link_issued_remote_attestations
           WHERE rowid > ?
             AND (object_type = ?
                  OR (redacted_at IS NULL AND revoked_at IS NULL AND expires_at > ?))
           ORDER BY rowid LIMIT ?""",
        (after_rowid, REMOTE_ATTESTATION_REVOCATION_OBJECT_TYPE, now_value, limit + 1),
    ).fetchall()
    more = len(rows) > limit
    result: list[dict[str, object]] = []
    total = 2
    for _, envelope_json, signature_b64 in rows[:limit]:
        item = {"envelope": json.loads(envelope_json), "signature": signature_b64}
        item_size = len(json.dumps(item, separators=(",", ":")).encode("utf-8")) + 1
        if result and total + item_size > MAX_ATTESTATION_RESPONSE_BYTES:
            more = True
            break
        if item_size > MAX_ATTESTATION_RESPONSE_BYTES:
            raise ValueError("stored attestation object exceeds the response byte limit")
        result.append(item)
        total += item_size
    return result, more


def load_attestation_pull_cursor(
    db: Database, responder_fingerprint: str, issuer_fingerprint: str
) -> str | None:
    row = db.connection.execute(
        """SELECT after_content_id FROM link_attestation_pull_cursors
           WHERE responder_fingerprint = ? AND issuer_fingerprint = ?""",
        (responder_fingerprint, issuer_fingerprint),
    ).fetchone()
    return row[0] if row is not None else None


def clear_attestation_pull_cursor(
    db: Database, responder_fingerprint: str, issuer_fingerprint: str
) -> None:
    """Forget where this node was in one authority's stream (issue #621)."""
    with db.connection:
        db.connection.execute(
            """DELETE FROM link_attestation_pull_cursors
               WHERE responder_fingerprint = ? AND issuer_fingerprint = ?""",
            (responder_fingerprint, issuer_fingerprint),
        )


def save_attestation_pull_cursor(
    db: Database,
    responder_fingerprint: str,
    issuer_fingerprint: str,
    after_content_id: str,
    *,
    now_iso: str | None = None,
) -> None:
    now_value, _ = _now(now_iso)
    with db.connection:
        db.connection.execute(
            """INSERT INTO link_attestation_pull_cursors
               (responder_fingerprint, issuer_fingerprint, after_content_id, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(responder_fingerprint, issuer_fingerprint) DO UPDATE SET
                 after_content_id = excluded.after_content_id,
                 updated_at = excluded.updated_at""",
            (responder_fingerprint, issuer_fingerprint, after_content_id, now_value),
        )


@dataclass(frozen=True)
class AttestationPullRequest:
    """One authenticated request for a page of an issuer's own attestations.

    Deliberately its own signed object type rather than a reuse of
    `trust_wire.TrustPullRequest`.  The two carry the same fields, but the
    object type is inside the signature, so a request a peer signed for one
    subscription cannot be re-aimed at the other: without that, a carrier
    holding a signed trust pull could spend its one-shot nonce against the
    attestation endpoint and make the trust pull fail as a replay.

    `issuer_fingerprint` must be the responder itself.  A node serves only
    objects it signed: unlike a trust signal, which any carrier may re-serve
    unchanged (design doc §12.7), an attestation is a statement about the
    issuer's *own* users, so there is no third party whose copy is worth
    asking for.
    """

    requester_fingerprint: str
    responder_fingerprint: str
    issuer_fingerprint: str
    after_content_id: str | None
    limit: int
    created_at: str
    nonce: str
    signature: bytes

    @property
    def payload(self) -> dict[str, object]:
        return {
            "requester_fingerprint": self.requester_fingerprint,
            "responder_fingerprint": self.responder_fingerprint,
            "issuer_fingerprint": self.issuer_fingerprint,
            "after_content_id": self.after_content_id,
            "limit": self.limit,
            "created_at": self.created_at,
            "nonce": self.nonce,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.payload, "signature": base64.b64encode(self.signature).decode("ascii")}

    @classmethod
    def from_dict(cls, data: object) -> "AttestationPullRequest":
        keys = {
            "requester_fingerprint", "responder_fingerprint", "issuer_fingerprint",
            "after_content_id", "limit", "created_at", "nonce", "signature",
        }
        if not isinstance(data, dict) or set(data) != keys:
            raise ValueError("invalid attestation pull request fields")
        try:
            signature = base64.b64decode(str(data["signature"]), validate=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid attestation pull signature encoding") from exc
        request = cls(signature=signature, **{key: data[key] for key in keys - {"signature"}})
        if any(not isinstance(value, str) or not value for value in (
            request.requester_fingerprint, request.responder_fingerprint,
            request.issuer_fingerprint, request.created_at, request.nonce,
        )):
            raise ValueError(
                "attestation pull fingerprints, timestamp, and nonce must be non-empty strings"
            )
        if request.after_content_id is not None and (
            not isinstance(request.after_content_id, str) or len(request.after_content_id) != 64
        ):
            raise ValueError("invalid attestation pull cursor")
        if (
            not isinstance(request.limit, int)
            or isinstance(request.limit, bool)
            or not 1 <= request.limit <= MAX_ATTESTATION_OBJECTS_PER_RESPONSE
        ):
            raise ValueError("attestation pull limit must be between 1 and 100")
        _parse_time(request.created_at, "created_at")
        if len(request.nonce) != 32:
            raise ValueError("attestation pull nonce must contain 128 bits of hexadecimal data")
        try:
            bytes.fromhex(request.nonce)
        except ValueError as exc:
            raise ValueError("attestation pull nonce is not hexadecimal") from exc
        return request

    def verifies(self, verify_key: nacl.signing.VerifyKey) -> bool:
        envelope = {
            "netbbs_protocol": 1,
            "object_type": ATTESTATION_PULL_REQUEST_OBJECT_TYPE,
            "payload": self.payload,
        }
        try:
            verify_key.verify(canonical_bytes(envelope), self.signature)
        except (nacl.exceptions.BadSignatureError, ValueError):
            return False
        return True


def build_attestation_pull_request(
    *,
    signing_identity: Identity,
    requester_fingerprint: str,
    responder_fingerprint: str,
    issuer_fingerprint: str,
    after_content_id: str | None = None,
    limit: int = MAX_ATTESTATION_OBJECTS_PER_RESPONSE,
    created_at: str | None = None,
    nonce: str | None = None,
) -> AttestationPullRequest:
    unsigned = AttestationPullRequest(
        requester_fingerprint=requester_fingerprint,
        responder_fingerprint=responder_fingerprint,
        issuer_fingerprint=issuer_fingerprint,
        after_content_id=after_content_id,
        limit=limit,
        created_at=created_at or utc_now_iso(),
        nonce=nonce or secrets.token_hex(16),
        signature=b"",
    )
    # Run the receiving side's own structural validation before signing, so a
    # malformed request is this node's error rather than the responder's.
    AttestationPullRequest.from_dict(
        {**unsigned.to_dict(), "signature": base64.b64encode(b"x").decode()}
    )
    envelope = {
        "netbbs_protocol": 1,
        "object_type": ATTESTATION_PULL_REQUEST_OBJECT_TYPE,
        "payload": unsigned.payload,
    }
    return AttestationPullRequest(
        **unsigned.payload, signature=signing_identity.sign(canonical_bytes(envelope))
    )

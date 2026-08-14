"""Signed Phase-4 trust objects and bounded local ingestion (issue #127).

Trust objects use Link's canonical envelope/signature rules, but deliberately
do not enter the ordinary ``link_events`` flood.  This module owns the wire
shape, immutable carrier store, configured-reporter admission, and pull-page
bounds; :mod:`netbbs.link.trust` remains the synchronous policy engine.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import nacl.signing

from netbbs.identity.keys import Identity, verify_signature
from netbbs.link.events import (
    NETBBS_PROTOCOL_VERSION,
    build_envelope,
    canonical_bytes,
    event_content_id,
)
from netbbs.link.trust import (
    EvidenceClass,
    TrustDimension,
    TrustSubject,
    record_reproduced_signal_observation,
    record_trust_signal,
    record_vouch,
    register_subject,
    revoke_trust_signal,
    revoke_vouch,
    validate_category_evidence,
)
from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

TRUST_SIGNAL_OBJECT_TYPE = "trust_signal"
TRUST_REVOCATION_OBJECT_TYPE = "trust_revocation"
TRUST_VOUCH_OBJECT_TYPE = "trust_vouch"
TRUST_VOUCH_REVOCATION_OBJECT_TYPE = "trust_vouch_revocation"
TRUST_PULL_REQUEST_OBJECT_TYPE = "trust_pull_request"
TRUST_OBJECT_TYPES = frozenset(
    {
        TRUST_SIGNAL_OBJECT_TYPE,
        TRUST_REVOCATION_OBJECT_TYPE,
        TRUST_VOUCH_OBJECT_TYPE,
        TRUST_VOUCH_REVOCATION_OBJECT_TYPE,
    }
)

MAX_TRUST_OBJECTS_PER_RESPONSE = 100
MAX_TRUST_RESPONSE_BYTES = 1024 * 1024
MAX_ACTIVE_SIGNALS_PER_ISSUER = 1000
MAX_ACTIVE_VOUCHES_PER_ISSUER = 1000
MAX_ACTIVE_SIGNALS_PER_ISSUER_SUBJECT_CATEGORY = 10
MAX_EMBEDDED_EVIDENCE_BYTES = 256 * 1024

_SIGNAL_KEYS = frozenset(
    {
        "object_version", "issuer_fingerprint", "signal_id", "subject",
        "dimension", "category", "evidence_class", "evidence", "observed_at",
        "issued_at", "expires_at", "explanation",
    }
)
_VOUCH_KEYS = frozenset(
    {
        "object_version", "issuer_fingerprint", "vouch_id", "subject",
        "issued_at", "expires_at", "explanation",
    }
)
_REVOCATION_KEYS = frozenset(
    {
        "object_version", "issuer_fingerprint", "revocation_id",
        "revoked_content_id", "issued_at", "explanation",
    }
)


class TrustWireError(ValueError):
    """A signed trust object is malformed, unauthorized, or over quota."""


@dataclass(frozen=True)
class TrustPullRequest:
    requester_fingerprint: str
    responder_fingerprint: str
    issuer_fingerprint: str
    after_content_id: str | None
    limit: int
    created_at: str
    nonce: str
    revocations_only: bool
    signature: bytes

    @property
    def payload(self) -> dict[str, Any]:
        return {
            "requester_fingerprint": self.requester_fingerprint,
            "responder_fingerprint": self.responder_fingerprint,
            "issuer_fingerprint": self.issuer_fingerprint,
            "after_content_id": self.after_content_id,
            "limit": self.limit,
            "created_at": self.created_at,
            "nonce": self.nonce,
            "revocations_only": self.revocations_only,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload, "signature": base64.b64encode(self.signature).decode("ascii")}

    @classmethod
    def from_dict(cls, data: Any) -> "TrustPullRequest":
        keys = {
            "requester_fingerprint", "responder_fingerprint", "issuer_fingerprint",
            "after_content_id", "limit", "created_at", "nonce", "signature",
            "revocations_only",
        }
        if not isinstance(data, dict) or set(data) != keys:
            raise TrustWireError("invalid trust pull request fields")
        try:
            signature = base64.b64decode(data["signature"], validate=True)
        except (TypeError, ValueError) as exc:
            raise TrustWireError("invalid trust pull signature encoding") from exc
        request = cls(signature=signature, **{key: data[key] for key in keys - {"signature"}})
        if any(not isinstance(value, str) or not value for value in (
            request.requester_fingerprint, request.responder_fingerprint,
            request.issuer_fingerprint, request.created_at, request.nonce,
        )):
            raise TrustWireError("trust pull fingerprints, timestamp, and nonce must be non-empty strings")
        if request.after_content_id is not None and (
            not isinstance(request.after_content_id, str) or len(request.after_content_id) != 64
        ):
            raise TrustWireError("invalid trust pull cursor")
        if not isinstance(request.limit, int) or isinstance(request.limit, bool) or not 1 <= request.limit <= 100:
            raise TrustWireError("trust pull limit must be between 1 and 100")
        if not isinstance(request.revocations_only, bool):
            raise TrustWireError("revocations_only must be boolean")
        _timestamp(request.created_at, "created_at")
        if len(request.nonce) != 32:
            raise TrustWireError("trust pull nonce must contain 128 bits of hexadecimal data")
        try:
            bytes.fromhex(request.nonce)
        except ValueError as exc:
            raise TrustWireError("trust pull nonce is not hexadecimal") from exc
        return request

    def verifies(self, verify_key: nacl.signing.VerifyKey) -> bool:
        envelope = build_envelope(TRUST_PULL_REQUEST_OBJECT_TYPE, self.payload)
        return verify_signature(verify_key, canonical_bytes(envelope), self.signature)


def build_trust_pull_request(
    *, signing_identity: Identity, requester_fingerprint: str,
    responder_fingerprint: str, issuer_fingerprint: str,
    after_content_id: str | None = None, limit: int = MAX_TRUST_OBJECTS_PER_RESPONSE,
    created_at: str | None = None, nonce: str | None = None,
    revocations_only: bool = False,
) -> TrustPullRequest:
    unsigned = TrustPullRequest(
        requester_fingerprint=requester_fingerprint,
        responder_fingerprint=responder_fingerprint,
        issuer_fingerprint=issuer_fingerprint,
        after_content_id=after_content_id,
        limit=limit,
        created_at=created_at or utc_now_iso(),
        nonce=nonce or secrets.token_hex(16),
        revocations_only=revocations_only,
        signature=b"",
    )
    # Reuse from_dict's complete structural validation before signing.
    TrustPullRequest.from_dict({**unsigned.to_dict(), "signature": base64.b64encode(b"x").decode()})
    envelope = build_envelope(TRUST_PULL_REQUEST_OBJECT_TYPE, unsigned.payload)
    return TrustPullRequest(**unsigned.payload, signature=signing_identity.sign(canonical_bytes(envelope)))


def _timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise TrustWireError(f"{name} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TrustWireError(f"{name} is not a valid ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise TrustWireError(f"{name} must include a timezone")
    return parsed


def _subject_to_dict(subject: TrustSubject) -> dict[str, str]:
    result = {"kind": subject.kind, "node_fingerprint": subject.node_fingerprint}
    if subject.opaque_user_id is not None:
        result["opaque_user_id"] = subject.opaque_user_id
    return result


def _subject_from_dict(value: Any) -> TrustSubject:
    if not isinstance(value, dict):
        raise TrustWireError("subject must be an object")
    allowed = {"kind", "node_fingerprint", "opaque_user_id"}
    if set(value) - allowed:
        raise TrustWireError("subject contains unknown fields")
    try:
        return TrustSubject(
            kind=value["kind"],
            node_fingerprint=value["node_fingerprint"],
            opaque_user_id=value.get("opaque_user_id"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TrustWireError(f"invalid trust subject: {exc}") from exc


def _validate_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("mode") not in {"embedded", "digest"}:
        raise TrustWireError("evidence mode must be 'embedded' or 'digest'")
    if value["mode"] == "embedded":
        if set(value) != {"mode", "data"}:
            raise TrustWireError("embedded evidence must contain only mode and data")
        size = len(json.dumps(value["data"], ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if size > MAX_EMBEDDED_EVIDENCE_BYTES:
            raise TrustWireError("embedded evidence exceeds 256 KiB")
    else:
        if set(value) != {"mode", "sha256", "size", "locator"}:
            raise TrustWireError("digest evidence requires sha256, size, and locator")
        digest = value["sha256"]
        if not isinstance(digest, str) or len(digest) != 64:
            raise TrustWireError("evidence sha256 must be 64 hexadecimal characters")
        try:
            bytes.fromhex(digest)
        except ValueError as exc:
            raise TrustWireError("evidence sha256 is not hexadecimal") from exc
        if not isinstance(value["size"], int) or isinstance(value["size"], bool) or value["size"] < 0:
            raise TrustWireError("evidence size must be a non-negative integer")
        if value["size"] > MAX_EMBEDDED_EVIDENCE_BYTES:
            raise TrustWireError("referenced evidence exceeds 256 KiB")
        if not isinstance(value["locator"], str) or not value["locator"]:
            raise TrustWireError("evidence locator must be a non-empty string")
    return value


@dataclass(frozen=True)
class SignedTrustObject:
    envelope: dict[str, Any]
    signature: bytes

    @property
    def object_type(self) -> str:
        return self.envelope["object_type"]

    @property
    def payload(self) -> dict[str, Any]:
        return self.envelope["payload"]

    @property
    def content_id(self) -> str:
        return event_content_id(self.envelope)

    @property
    def issuer_fingerprint(self) -> str:
        return self.payload["issuer_fingerprint"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "envelope": self.envelope,
            "signature": base64.b64encode(self.signature).decode("ascii"),
        }

    @classmethod
    def from_dict(
        cls, data: Any, *, issuer_verify_key: nacl.signing.VerifyKey
    ) -> "SignedTrustObject":
        if not isinstance(data, dict) or set(data) != {"envelope", "signature"}:
            raise TrustWireError("trust object must contain only envelope and signature")
        envelope = data["envelope"]
        if not isinstance(envelope, dict) or set(envelope) != {"netbbs_protocol", "object_type", "payload"}:
            raise TrustWireError("invalid trust envelope shape")
        if envelope["netbbs_protocol"] != NETBBS_PROTOCOL_VERSION:
            raise TrustWireError("unsupported trust protocol version")
        if envelope["object_type"] not in TRUST_OBJECT_TYPES:
            raise TrustWireError("unsupported trust object type")
        if not isinstance(envelope["payload"], dict):
            raise TrustWireError("trust payload must be an object")
        try:
            signature = base64.b64decode(data["signature"], validate=True)
        except (TypeError, ValueError) as exc:
            raise TrustWireError("invalid trust signature encoding") from exc
        if not verify_signature(issuer_verify_key, canonical_bytes(envelope), signature):
            raise TrustWireError("trust signature does not verify")
        obj = cls(envelope=envelope, signature=signature)
        _validate_payload(obj.object_type, obj.payload)
        return obj


def _validate_payload(object_type: str, payload: dict[str, Any]) -> None:
    expected = _SIGNAL_KEYS if object_type == TRUST_SIGNAL_OBJECT_TYPE else (
        _VOUCH_KEYS if object_type == TRUST_VOUCH_OBJECT_TYPE else _REVOCATION_KEYS
    )
    if set(payload) != expected:
        missing = sorted(expected - set(payload))
        extra = sorted(set(payload) - expected)
        raise TrustWireError(f"invalid {object_type} fields; missing={missing}, extra={extra}")
    if payload["object_version"] != 1:
        raise TrustWireError("unsupported trust object version")
    if not isinstance(payload["issuer_fingerprint"], str) or not payload["issuer_fingerprint"]:
        raise TrustWireError("issuer_fingerprint must be non-empty")
    issued = _timestamp(payload["issued_at"], "issued_at")
    explanation = payload["explanation"]
    if explanation is not None and not isinstance(explanation, str):
        raise TrustWireError("explanation must be a string or null")
    if object_type == TRUST_SIGNAL_OBJECT_TYPE:
        if not isinstance(payload["signal_id"], str) or not payload["signal_id"]:
            raise TrustWireError("signal_id must be non-empty")
        _subject_from_dict(payload["subject"])
        try:
            dimension = TrustDimension(payload["dimension"])
            evidence_class = EvidenceClass(payload["evidence_class"])
            validate_category_evidence(dimension, payload["category"], evidence_class)
        except (TypeError, ValueError) as exc:
            raise TrustWireError("invalid trust dimension/category/evidence combination") from exc
        if not isinstance(payload["category"], str) or not payload["category"]:
            raise TrustWireError("category must be non-empty")
        _validate_evidence(payload["evidence"])
        observed = _timestamp(payload["observed_at"], "observed_at")
        expires = _timestamp(payload["expires_at"], "expires_at")
        if expires <= issued:
            raise TrustWireError("expires_at must be after issued_at")
        if observed > issued + timedelta(minutes=5):
            raise TrustWireError("observed_at cannot be after issued_at")
    elif object_type == TRUST_VOUCH_OBJECT_TYPE:
        if not isinstance(payload["vouch_id"], str) or not payload["vouch_id"]:
            raise TrustWireError("vouch_id must be non-empty")
        _subject_from_dict(payload["subject"])
        if _timestamp(payload["expires_at"], "expires_at") <= issued:
            raise TrustWireError("expires_at must be after issued_at")
    else:
        if not isinstance(payload["revocation_id"], str) or not payload["revocation_id"]:
            raise TrustWireError("revocation_id must be non-empty")
        content_id = payload["revoked_content_id"]
        if not isinstance(content_id, str) or len(content_id) != 64:
            raise TrustWireError("revoked_content_id must be a SHA-256 content ID")
        try:
            bytes.fromhex(content_id)
        except ValueError as exc:
            raise TrustWireError("revoked_content_id is not hexadecimal") from exc


def _build_signed(identity: Identity, object_type: str, payload: dict[str, Any]) -> SignedTrustObject:
    _validate_payload(object_type, payload)
    envelope = build_envelope(object_type, payload)
    return SignedTrustObject(envelope, identity.sign(canonical_bytes(envelope)))


def build_trust_signal(
    *, signing_identity: Identity, issuer_fingerprint: str, signal_id: str, subject: TrustSubject,
    dimension: TrustDimension | str, category: str,
    evidence_class: EvidenceClass | str, evidence: dict[str, Any],
    observed_at: str, issued_at: str, expires_at: str,
    explanation: str | None = None,
) -> SignedTrustObject:
    return _build_signed(signing_identity, TRUST_SIGNAL_OBJECT_TYPE, {
        "object_version": 1, "issuer_fingerprint": issuer_fingerprint,
        "signal_id": signal_id, "subject": _subject_to_dict(subject),
        "dimension": TrustDimension(dimension).value, "category": category,
        "evidence_class": EvidenceClass(evidence_class).value, "evidence": evidence,
        "observed_at": observed_at, "issued_at": issued_at, "expires_at": expires_at,
        "explanation": explanation,
    })


def build_trust_vouch(
    *, signing_identity: Identity, issuer_fingerprint: str, vouch_id: str, subject: TrustSubject,
    issued_at: str, expires_at: str, explanation: str | None = None,
) -> SignedTrustObject:
    return _build_signed(signing_identity, TRUST_VOUCH_OBJECT_TYPE, {
        "object_version": 1, "issuer_fingerprint": issuer_fingerprint,
        "vouch_id": vouch_id, "subject": _subject_to_dict(subject),
        "issued_at": issued_at, "expires_at": expires_at, "explanation": explanation,
    })


def build_trust_revocation(
    *, signing_identity: Identity, issuer_fingerprint: str, revocation_id: str, revoked_content_id: str,
    issued_at: str, explanation: str | None = None, vouch: bool = False,
) -> SignedTrustObject:
    return _build_signed(
        signing_identity,
        TRUST_VOUCH_REVOCATION_OBJECT_TYPE if vouch else TRUST_REVOCATION_OBJECT_TYPE,
        {"object_version": 1, "issuer_fingerprint": issuer_fingerprint,
         "revocation_id": revocation_id, "revoked_content_id": revoked_content_id,
         "issued_at": issued_at, "explanation": explanation},
    )


def _reporter_row(db: Database, issuer: str):
    return db.connection.execute(
        "SELECT can_vouch_nodes, can_vouch_users FROM link_trust_reporters WHERE fingerprint = ?",
        (issuer,),
    ).fetchone()


def _reporter_scope_allows(db: Database, issuer: str, dimension: str, category: str) -> bool:
    return db.connection.execute(
        """SELECT 1 FROM link_trust_reporter_scopes
           WHERE reporter_fingerprint = ? AND dimension = ? AND category = ?""",
        (issuer, dimension, category),
    ).fetchone() is not None


def _store_wire_object(db: Database, obj: SignedTrustObject, received_at: str) -> None:
    payload = obj.payload
    subject = _subject_from_dict(payload["subject"]) if "subject" in payload else None
    db.connection.execute(
        """INSERT INTO link_trust_wire_objects
           (content_id, issuer_fingerprint, object_type, envelope_json, signature_b64,
            subject_id, dimension, category, expires_at, received_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(content_id) DO NOTHING""",
        (obj.content_id, obj.issuer_fingerprint, obj.object_type,
         json.dumps(obj.envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
         base64.b64encode(obj.signature).decode("ascii"),
         subject.subject_id if subject else None, payload.get("dimension"), payload.get("category"),
         payload.get("expires_at"), received_at),
    )


def ingest_trust_objects(
    db: Database, objects: Iterable[SignedTrustObject], *, now_iso: str | None = None
) -> tuple[list[str], list[str]]:
    """Admit verified objects from configured issuers; return accepted/replayed IDs."""
    now = now_iso or utc_now_iso()
    accepted: list[str] = []
    replayed: list[str] = []
    batch = list(objects)
    if len(batch) > MAX_TRUST_OBJECTS_PER_RESPONSE:
        raise TrustWireError("trust response exceeds 100 objects")
    encoded_size = len(json.dumps([obj.to_dict() for obj in batch], separators=(",", ":")).encode())
    if encoded_size > MAX_TRUST_RESPONSE_BYTES:
        raise TrustWireError("trust response exceeds 1 MiB")
    with db.connection:
        for obj in batch:
            issuer = obj.issuer_fingerprint
            issued = datetime.fromisoformat(obj.payload["issued_at"].replace("Z", "+00:00"))
            received = datetime.fromisoformat(now.replace("Z", "+00:00"))
            if issued > received + timedelta(minutes=5):
                raise TrustWireError("issued_at is more than five minutes in the future")
            reporter = _reporter_row(db, issuer)
            if reporter is None:
                raise TrustWireError(f"issuer {issuer} is not a configured reporter")
            if db.connection.execute(
                "SELECT 1 FROM link_trust_wire_objects WHERE content_id = ?", (obj.content_id,)
            ).fetchone():
                replayed.append(obj.content_id)
                continue
            active_signals = db.connection.execute(
                """SELECT COUNT(*) FROM link_trust_wire_objects
                   WHERE issuer_fingerprint = ? AND object_type = 'trust_signal'
                     AND revoked_at IS NULL AND expires_at > ?""",
                (issuer, now),
            ).fetchone()[0]
            active_vouches = db.connection.execute(
                """SELECT COUNT(*) FROM link_trust_wire_objects
                   WHERE issuer_fingerprint = ? AND object_type = 'trust_vouch'
                     AND revoked_at IS NULL AND expires_at > ?""",
                (issuer, now),
            ).fetchone()[0]
            if obj.object_type == TRUST_SIGNAL_OBJECT_TYPE and active_signals >= MAX_ACTIVE_SIGNALS_PER_ISSUER:
                raise TrustWireError("issuer active-signal quota exceeded")
            if obj.object_type == TRUST_VOUCH_OBJECT_TYPE and active_vouches >= MAX_ACTIVE_VOUCHES_PER_ISSUER:
                raise TrustWireError("issuer active-vouch quota exceeded")
            payload = obj.payload
            if obj.object_type == TRUST_SIGNAL_OBJECT_TYPE:
                subject = _subject_from_dict(payload["subject"])
                if not _reporter_scope_allows(db, issuer, payload["dimension"], payload["category"]):
                    raise TrustWireError("reporter is not configured for this dimension/category")
                per_subject = db.connection.execute(
                    """SELECT COUNT(*) FROM link_trust_wire_objects
                       WHERE issuer_fingerprint = ? AND object_type = 'trust_signal'
                         AND subject_id = ? AND category = ? AND revoked_at IS NULL
                         AND expires_at > ?""",
                    (issuer, subject.subject_id, payload["category"], now),
                ).fetchone()[0]
                if per_subject >= MAX_ACTIVE_SIGNALS_PER_ISSUER_SUBJECT_CATEGORY:
                    raise TrustWireError("issuer/subject/category active-signal quota exceeded")
                evidence = payload["evidence"]
                if evidence["mode"] == "embedded":
                    register_subject(db, subject, first_accepted_at=now, now_iso=now)
                    record_trust_signal(
                        db, content_id=obj.content_id, issuer_fingerprint=issuer, subject=subject,
                        dimension=payload["dimension"], category=payload["category"],
                        evidence_class=payload["evidence_class"], observed_at=payload["observed_at"],
                        issued_at=payload["issued_at"], expires_at=payload["expires_at"],
                        evidence=evidence, explanation=payload["explanation"], now_iso=now,
                    )
            elif obj.object_type == TRUST_VOUCH_OBJECT_TYPE:
                subject = _subject_from_dict(payload["subject"])
                allowed = bool(reporter[0] if subject.kind == "node" else reporter[1])
                if not allowed:
                    raise TrustWireError(f"reporter may not vouch for {subject.kind} subjects")
                register_subject(db, subject, first_accepted_at=now, now_iso=now)
                record_vouch(
                    db, content_id=obj.content_id, issuer_fingerprint=issuer, subject=subject,
                    issued_at=payload["issued_at"], expires_at=payload["expires_at"],
                    explanation=payload["explanation"], now_iso=now,
                )
            else:
                target = payload["revoked_content_id"]
                row = db.connection.execute(
                    """SELECT issuer_fingerprint, object_type, revoked_by_content_id
                       FROM link_trust_wire_objects WHERE content_id = ?""",
                    (target,),
                ).fetchone()
                if row is None or row[0] != issuer:
                    raise TrustWireError("revocation target is unknown or belongs to another issuer")
                expected_type = (
                    TRUST_VOUCH_OBJECT_TYPE
                    if obj.object_type == TRUST_VOUCH_REVOCATION_OBJECT_TYPE
                    else TRUST_SIGNAL_OBJECT_TYPE
                )
                if row[1] != expected_type:
                    raise TrustWireError("revocation target belongs to another object family")
                if row[2] is not None:
                    raise TrustWireError("revocation target is already revoked")
                if obj.object_type == TRUST_VOUCH_REVOCATION_OBJECT_TYPE:
                    revoke_vouch(db, target, revocation_content_id=obj.content_id, now_iso=now)
                elif db.connection.execute(
                    "SELECT 1 FROM link_trust_signals WHERE content_id = ?", (target,)
                ).fetchone():
                    revoke_trust_signal(db, target, revocation_content_id=obj.content_id, now_iso=now)
                db.connection.execute(
                    """UPDATE link_trust_wire_objects
                       SET revoked_by_content_id = ?, revoked_at = ? WHERE content_id = ?""",
                    (obj.content_id, now, target),
                )
            _store_wire_object(db, obj, now)
            accepted.append(obj.content_id)
    return accepted, replayed


def load_trust_object_page(
    db: Database, *, issuer_fingerprint: str, after_content_id: str | None = None,
    limit: int = MAX_TRUST_OBJECTS_PER_RESPONSE,
    revocations_only: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    """Return one stable, byte-bounded issuer page suitable for direct or carrier pull."""
    limit = max(1, min(limit, MAX_TRUST_OBJECTS_PER_RESPONSE))
    after_received = ""
    if after_content_id:
        row = db.connection.execute(
            "SELECT received_at FROM link_trust_wire_objects WHERE content_id = ? AND issuer_fingerprint = ?",
            (after_content_id, issuer_fingerprint),
        ).fetchone()
        if row is None:
            raise TrustWireError("unknown trust pull cursor")
        after_received = row[0]
    type_filter = (
        "AND object_type IN ('trust_revocation', 'trust_vouch_revocation')"
        if revocations_only else ""
    )
    rows = db.connection.execute(
        """SELECT content_id, envelope_json, signature_b64 FROM link_trust_wire_objects
           WHERE issuer_fingerprint = ?
             {type_filter}
             AND (? IS NULL OR received_at > ? OR (received_at = ? AND content_id > ?))
           ORDER BY received_at, content_id LIMIT ?""".format(type_filter=type_filter),
        (issuer_fingerprint, after_content_id, after_received, after_received, after_content_id, limit + 1),
    ).fetchall()
    more = len(rows) > limit
    result: list[dict[str, Any]] = []
    total = 2
    for _, envelope_json, signature_b64 in rows[:limit]:
        item = {"envelope": json.loads(envelope_json), "signature": signature_b64}
        item_size = len(json.dumps(item, separators=(",", ":")).encode("utf-8")) + 1
        if result and total + item_size > MAX_TRUST_RESPONSE_BYTES:
            more = True
            break
        if item_size > MAX_TRUST_RESPONSE_BYTES:
            raise TrustWireError("stored trust object exceeds response byte limit")
        result.append(item)
        total += item_size
    return result, more


def list_trusted_reporter_fingerprints(db: Database) -> list[str]:
    """Return the explicit local subscription set in deterministic order."""
    return [
        row[0]
        for row in db.connection.execute(
            "SELECT fingerprint FROM link_trust_reporters ORDER BY fingerprint"
        ).fetchall()
    ]


def load_trust_pull_cursor(
    db: Database, responder_fingerprint: str, issuer_fingerprint: str
) -> str | None:
    row = db.connection.execute(
        """SELECT after_content_id FROM link_trust_pull_cursors
           WHERE responder_fingerprint = ? AND issuer_fingerprint = ?""",
        (responder_fingerprint, issuer_fingerprint),
    ).fetchone()
    return row[0] if row else None


def save_trust_pull_cursor(
    db: Database, responder_fingerprint: str, issuer_fingerprint: str,
    after_content_id: str, *, now_iso: str | None = None,
) -> None:
    with db.connection:
        db.connection.execute(
            """INSERT INTO link_trust_pull_cursors
               (responder_fingerprint, issuer_fingerprint, after_content_id, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(responder_fingerprint, issuer_fingerprint) DO UPDATE SET
                   after_content_id = excluded.after_content_id,
                   updated_at = excluded.updated_at""",
            (responder_fingerprint, issuer_fingerprint, after_content_id, now_iso or utc_now_iso()),
        )


def verify_evidence_bytes(evidence: dict[str, Any], content: bytes) -> Any:
    """Size/hash/parse a digest-only evidence body; this does not reproduce its claim."""
    checked = _validate_evidence(evidence)
    if checked["mode"] != "digest":
        raise TrustWireError("evidence is not digest-referenced")
    if len(content) != checked["size"] or len(content) > MAX_EMBEDDED_EVIDENCE_BYTES:
        raise TrustWireError("evidence body size does not match the signed claim")
    if hashlib.sha256(content).hexdigest() != checked["sha256"]:
        raise TrustWireError("evidence body hash does not match the signed claim")
    try:
        return json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrustWireError("evidence body is not valid UTF-8 JSON") from exc


def activate_reproduced_digest_signal(
    db: Database,
    content_id: str,
    evidence_content: bytes,
    *,
    observation_id: str,
    reproduce: Callable[[Any], bool],
    now_iso: str | None = None,
) -> bool:
    """Activate digest evidence only after bounded verification and reproduction.

    ``reproduce`` owns the category-specific semantic check.  Merely fetching,
    hashing, and parsing reporter-provided JSON can never make it local policy
    evidence.  A successful callback records both the remote signal and an
    independent local observation.
    """
    row = db.connection.execute(
        """SELECT envelope_json, revoked_at FROM link_trust_wire_objects
           WHERE content_id = ? AND object_type = 'trust_signal'""",
        (content_id,),
    ).fetchone()
    if row is None:
        raise TrustWireError("unknown digest trust signal")
    if row[1] is not None:
        raise TrustWireError("digest trust signal has been revoked")
    payload = json.loads(row[0])["payload"]
    evidence = payload["evidence"]
    if payload["evidence_class"] != EvidenceClass.SELF_VERIFYING.value:
        raise TrustWireError("only self-verifying digest evidence can be reproduced locally")
    parsed = verify_evidence_bytes(evidence, evidence_content)
    if not reproduce(parsed):
        raise TrustWireError("evidence claim could not be independently reproduced")
    now = now_iso or utc_now_iso()
    subject = _subject_from_dict(payload["subject"])
    with db.connection:
        register_subject(db, subject, first_accepted_at=now, now_iso=now)
        inserted = record_trust_signal(
            db, content_id=content_id, issuer_fingerprint=payload["issuer_fingerprint"],
            subject=subject, dimension=payload["dimension"], category=payload["category"],
            evidence_class=payload["evidence_class"], observed_at=payload["observed_at"],
            issued_at=payload["issued_at"], expires_at=payload["expires_at"],
            evidence=evidence, explanation=payload["explanation"], now_iso=now,
        )
        if inserted:
            record_reproduced_signal_observation(
                db, content_id, observation_id=observation_id, now_iso=now
            )
        return inserted

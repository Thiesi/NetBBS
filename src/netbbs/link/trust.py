"""Local Phase-4 trust inputs and effective-state projection.

This module stops at the local domain boundary (design doc §12, issue
#126). It accepts already-authenticated inputs, keeps node and user subjects
separate, evaluates each trust dimension independently, and persists the
effective projection in the same transaction as every input change. Issue #127
owns signed wire objects; #128 and #129 own enforcement and presentation.

Functions are synchronous and ``db``-first so async callers can dispatch them
through ``DatabaseLane``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Iterable

from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso


class TrustDimension(StrEnum):
    IDENTITY_INTEGRITY = "identity_integrity"
    RESOURCE_BEHAVIOR = "resource_behavior"
    CONTENT_CONDUCT = "content_conduct"


class TrustState(StrEnum):
    PROBATIONARY = "probationary"
    ESTABLISHED = "established"
    QUARANTINED = "quarantined"
    BLOCKED = "blocked"


class EvidenceClass(StrEnum):
    SELF_VERIFYING = "self_verifying"
    OBSERVER_ATTESTED = "observer_attested"
    SUBJECTIVE = "subjective"


IDENTITY_CATEGORIES = frozenset(
    {"signed_equivocation", "revoked_key_use", "invalid_authority", "invalid_signature_delivery"}
)
RESOURCE_CATEGORIES = frozenset(
    {"malformed_traffic", "request_flood", "quota_evasion", "inventory_nondelivery", "relay_abuse"}
)
CONTENT_CATEGORIES = frozenset(
    {"spam", "harassment", "illegal_content", "off_topic", "other"}
)

_CATEGORIES_BY_DIMENSION = {
    TrustDimension.IDENTITY_INTEGRITY: IDENTITY_CATEGORIES,
    TrustDimension.RESOURCE_BEHAVIOR: RESOURCE_CATEGORIES,
    TrustDimension.CONTENT_CONDUCT: CONTENT_CATEGORIES,
}
_MAX_LIFETIME = {
    EvidenceClass.SELF_VERIFYING: timedelta(days=90),
    EvidenceClass.OBSERVER_ATTESTED: timedelta(days=7),
    EvidenceClass.SUBJECTIVE: timedelta(days=30),
}
_VOUCH_MAX_LIFETIME = timedelta(days=180)
_FUTURE_TOLERANCE = timedelta(minutes=5)
_RECOVERY_HOLD = timedelta(hours=24)


@dataclass(frozen=True)
class TrustSubject:
    kind: str
    node_fingerprint: str
    opaque_user_id: str | None = None

    @classmethod
    def node(cls, fingerprint: str) -> "TrustSubject":
        return cls("node", fingerprint)

    @classmethod
    def user(cls, home_node_fingerprint: str, opaque_user_id: str) -> "TrustSubject":
        return cls("user", home_node_fingerprint, opaque_user_id)

    def __post_init__(self) -> None:
        if self.kind not in {"node", "user"}:
            raise ValueError("trust subject kind must be 'node' or 'user'")
        if not self.node_fingerprint:
            raise ValueError("trust subject node fingerprint must not be empty")
        if (self.kind == "node") != (self.opaque_user_id is None):
            raise ValueError("node subjects omit opaque_user_id; user subjects require it")

    @property
    def subject_id(self) -> str:
        canonical = json.dumps(
            [self.kind, self.node_fingerprint, self.opaque_user_id],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class EffectiveTrustState:
    subject: TrustSubject
    dimension: TrustDimension
    state: TrustState
    reason_code: str
    recovery_started_at: str | None
    explanation: dict[str, object]
    evaluated_at: str


def _parse_timestamp(value: str, *, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _now(now_iso: str | None) -> tuple[str, datetime]:
    value = now_iso or utc_now_iso()
    return value, _parse_timestamp(value, field_name="now")


def _dimension(value: TrustDimension | str) -> TrustDimension:
    try:
        return TrustDimension(value)
    except ValueError as exc:
        raise ValueError(f"unknown trust dimension: {value!r}") from exc


def _evidence_class(value: EvidenceClass | str) -> EvidenceClass:
    try:
        return EvidenceClass(value)
    except ValueError as exc:
        raise ValueError(f"unknown evidence class: {value!r}") from exc


def validate_category_evidence(
    dimension: TrustDimension, category: str, evidence_class: EvidenceClass
) -> bool:
    """Validate known combinations and return whether the category is known."""
    known_dimension = next(
        (candidate for candidate, categories in _CATEGORIES_BY_DIMENSION.items() if category in categories),
        None,
    )
    if known_dimension is not None and known_dimension != dimension:
        raise ValueError(f"category {category!r} does not belong to {dimension.value}")
    if known_dimension is None:
        return False
    if dimension == TrustDimension.CONTENT_CONDUCT:
        if evidence_class != EvidenceClass.SUBJECTIVE:
            raise ValueError("content-conduct categories require subjective evidence")
    elif evidence_class == EvidenceClass.SUBJECTIVE:
        raise ValueError("subjective evidence is only valid for content conduct")
    if evidence_class == EvidenceClass.SELF_VERIFYING and dimension != TrustDimension.IDENTITY_INTEGRITY:
        raise ValueError("self-verifying evidence is only valid for identity integrity")
    return True


def register_subject(
    db: Database, subject: TrustSubject, *, first_accepted_at: str, now_iso: str | None = None
) -> None:
    """Register a verified node hello or accepted stable remote-user identity."""
    first = _parse_timestamp(first_accepted_at, field_name="first_accepted_at")
    now_value, now = _now(now_iso)
    if first > now + _FUTURE_TOLERANCE:
        raise ValueError("first_accepted_at is more than five minutes in the future")
    with db.connection:
        db.connection.execute(
            """
            INSERT INTO link_trust_subjects (
                subject_id, subject_kind, node_fingerprint, opaque_user_id,
                first_accepted_at, first_verified_hello_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(subject_id) DO NOTHING
            """,
            (
                subject.subject_id,
                subject.kind,
                subject.node_fingerprint,
                subject.opaque_user_id,
                first_accepted_at,
                first_accepted_at if subject.kind == "node" else None,
            ),
        )
        _recompute_subject(db, subject, now_value, now, actor_user_id=None)


def record_activity(
    db: Database,
    subject: TrustSubject,
    *,
    activity_date: str,
    direct: bool,
    now_iso: str | None = None,
) -> None:
    activity_day = datetime.strptime(activity_date, "%Y-%m-%d").date()
    now_value, now = _now(now_iso)
    if activity_day > now.date():
        raise ValueError("trust activity date cannot be in the future")
    with db.connection:
        _require_subject(db, subject)
        db.connection.execute(
            """
            INSERT INTO link_trust_activity_days (subject_id, activity_date, direct)
            VALUES (?, ?, ?)
            ON CONFLICT(subject_id, activity_date) DO UPDATE SET
                direct = MAX(direct, excluded.direct)
            """,
            (subject.subject_id, activity_date, 1 if direct else 0),
        )
        _recompute_subject(db, subject, now_value, now, actor_user_id=None)


def configure_trust_domain(
    db: Database,
    domain_id: str,
    *,
    display_name: str,
    weight: float = 1.0,
    actor_user_id: int | None = None,
    now_iso: str | None = None,
) -> None:
    if not domain_id or not display_name:
        raise ValueError("trust domain ID and display name must not be empty")
    if not 0.0 <= weight <= 1.0:
        raise ValueError("trust domain weight must be between 0.0 and 1.0")
    now_value, now = _now(now_iso)
    details = {"display_name": display_name, "weight": weight}
    with db.connection:
        exists = db.connection.execute(
            "SELECT 1 FROM link_trust_domains WHERE domain_id = ?", (domain_id,)
        ).fetchone()
        db.connection.execute(
            """
            INSERT INTO link_trust_domains (domain_id, display_name, weight, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(domain_id) DO UPDATE SET
                display_name = excluded.display_name,
                weight = excluded.weight,
                updated_at = excluded.updated_at
            """,
            (domain_id, display_name, weight, now_value, now_value),
        )
        _audit_config(
            db, "domain", domain_id, "updated" if exists else "created",
            details, actor_user_id, now_value,
        )
        _recompute_all(db, now_value, now, actor_user_id)


def configure_trust_anchor(
    db: Database,
    fingerprint: str,
    *,
    reason: str,
    actor_user_id: int | None = None,
    now_iso: str | None = None,
) -> None:
    """Configure an explicit anchor without granting reporter authority."""
    if not fingerprint or not reason.strip():
        raise ValueError("trust anchor fingerprint and reason must not be empty")
    now_value, _ = _now(now_iso)
    with db.connection:
        exists = db.connection.execute(
            "SELECT 1 FROM link_trust_anchors WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        db.connection.execute(
            """INSERT INTO link_trust_anchors
               (fingerprint, reason, actor_user_id, created_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(fingerprint) DO UPDATE SET
                 reason = excluded.reason, actor_user_id = excluded.actor_user_id""",
            (fingerprint, reason, actor_user_id, now_value),
        )
        _audit_config(
            db, "anchor", fingerprint, "updated" if exists else "created",
            {"reason": reason}, actor_user_id, now_value,
        )


def remove_trust_anchor(
    db: Database,
    fingerprint: str,
    *,
    actor_user_id: int | None = None,
    now_iso: str | None = None,
) -> None:
    now_value, _ = _now(now_iso)
    with db.connection:
        row = db.connection.execute(
            "SELECT reason FROM link_trust_anchors WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown trust anchor: {fingerprint!r}")
        db.connection.execute(
            "DELETE FROM link_trust_anchors WHERE fingerprint = ?", (fingerprint,)
        )
        _audit_config(
            db, "anchor", fingerprint, "removed", {"reason": row["reason"]},
            actor_user_id, now_value,
        )


def _recompute_all(
    db: Database, now_value: str, now: datetime, actor_user_id: int | None
) -> None:
    rows = db.connection.execute(
        "SELECT subject_kind, node_fingerprint, opaque_user_id FROM link_trust_subjects"
    ).fetchall()
    for row in rows:
        subject = TrustSubject(
            row["subject_kind"], row["node_fingerprint"], row["opaque_user_id"]
        )
        _recompute_subject(db, subject, now_value, now, actor_user_id)


def _recompute_subject(
    db: Database,
    subject: TrustSubject,
    now_value: str,
    now: datetime,
    actor_user_id: int | None,
) -> None:
    for dimension in TrustDimension:
        _recompute_dimension(db, subject, dimension, now_value, now, actor_user_id)


def _active_override(
    db: Database, subject_id: str, dimension: TrustDimension, now_value: str
):
    return db.connection.execute(
        """
        SELECT override_id, state, reason, created_at, expires_at
        FROM link_trust_overrides
        WHERE subject_id = ? AND dimension = ? AND cleared_at IS NULL
          AND (expires_at IS NULL OR expires_at > ?)
        ORDER BY CASE WHEN state = 'blocked' THEN 0 ELSE 1 END, override_id DESC
        LIMIT 1
        """,
        (subject_id, dimension.value, now_value),
    ).fetchone()


def _active_evidence(
    db: Database, subject_id: str, dimension: TrustDimension, now_value: str
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    local = [
        {
            "observation_id": row["observation_id"],
            "category": row["category"],
            "evidence_class": row["evidence_class"],
        }
        for row in db.connection.execute(
            """
            SELECT observation_id, category, evidence_class
            FROM link_trust_local_observations
            WHERE subject_id = ? AND dimension = ?
              AND cleared_at IS NULL AND expires_at > ?
            """,
            (subject_id, dimension.value, now_value),
        ).fetchall()
    ]
    remote = [
        {
            "content_id": row["content_id"],
            "category": row["category"],
            "evidence_class": row["evidence_class"],
            "domain_id": row["domain_id"],
            "domain_weight": row["weight"],
        }
        for row in db.connection.execute(
            """
            SELECT s.content_id, s.category, s.evidence_class, r.domain_id, d.weight
            FROM link_trust_signals AS s
            JOIN link_trust_reporters AS r ON r.fingerprint = s.issuer_fingerprint
            JOIN link_trust_reporter_scopes AS rs
              ON rs.reporter_fingerprint = r.fingerprint
             AND rs.dimension = s.dimension AND rs.category = s.category
            JOIN link_trust_domains AS d ON d.domain_id = r.domain_id
            LEFT JOIN link_trust_subjects AS reporter_subject
              ON reporter_subject.subject_kind = 'node'
             AND reporter_subject.node_fingerprint = r.fingerprint
            WHERE s.subject_id = ? AND s.dimension = ?
              AND s.revoked_at IS NULL AND s.effective_expires_at > ?
              AND (
                reporter_subject.subject_id IS NULL OR NOT EXISTS (
                  SELECT 1 FROM link_trust_effective_states AS reporter_state
                  WHERE reporter_state.subject_id = reporter_subject.subject_id
                    AND reporter_state.dimension IN ('identity_integrity', 'resource_behavior')
                    AND reporter_state.state != 'established'
                )
              )
            """,
            (subject_id, dimension.value, now_value),
        ).fetchall()
    ]
    return local, remote


def _vouch_domains(
    db: Database, subject: TrustSubject, now_value: str
) -> dict[str, float]:
    permission = "can_vouch_nodes" if subject.kind == "node" else "can_vouch_users"
    rows = db.connection.execute(
        f"""
        SELECT r.domain_id, d.weight
        FROM link_trust_vouches AS v
        JOIN link_trust_reporters AS r ON r.fingerprint = v.issuer_fingerprint
        JOIN link_trust_domains AS d ON d.domain_id = r.domain_id
        LEFT JOIN link_trust_subjects AS reporter_subject
          ON reporter_subject.subject_kind = 'node'
         AND reporter_subject.node_fingerprint = r.fingerprint
        WHERE v.subject_id = ? AND v.revoked_at IS NULL
          AND v.effective_expires_at > ? AND r.{permission} = 1
          AND (
            reporter_subject.subject_id IS NULL OR NOT EXISTS (
              SELECT 1 FROM link_trust_effective_states AS reporter_state
              WHERE reporter_state.subject_id = reporter_subject.subject_id
                AND reporter_state.dimension IN ('identity_integrity', 'resource_behavior')
                AND reporter_state.state != 'established'
            )
          )
        """,
        (subject.subject_id, now_value),
    ).fetchall()
    return {row["domain_id"]: row["weight"] for row in rows}


def _ordinary_state(
    db: Database, subject: TrustSubject, now_value: str, now: datetime
) -> tuple[TrustState, str, dict[str, object]]:
    subject_row = db.connection.execute(
        """SELECT first_accepted_at, first_verified_hello_at
           FROM link_trust_subjects WHERE subject_id = ?""",
        (subject.subject_id,),
    ).fetchone()
    activity_condition = "direct = 1" if subject.kind == "node" else "1 = 1"
    activity_days = db.connection.execute(
        f"SELECT COUNT(*) FROM link_trust_activity_days WHERE subject_id = ? AND {activity_condition}",
        (subject.subject_id,),
    ).fetchone()[0]
    first_value = (
        subject_row["first_verified_hello_at"]
        if subject.kind == "node"
        else subject_row["first_accepted_at"]
    )
    required_age = timedelta(days=30 if subject.kind == "node" else 14)
    age_ok = now >= _parse_timestamp(first_value, field_name="first accepted") + required_age
    local_rows = db.connection.execute(
        """SELECT dimension, category FROM link_trust_local_observations
           WHERE subject_id = ? AND cleared_at IS NULL AND expires_at > ?""",
        (subject.subject_id, now_value),
    ).fetchall()
    remote_rows = db.connection.execute(
        """
        SELECT s.dimension, s.category FROM link_trust_signals AS s
        JOIN link_trust_reporters AS r ON r.fingerprint = s.issuer_fingerprint
        JOIN link_trust_reporter_scopes AS rs
          ON rs.reporter_fingerprint = r.fingerprint
         AND rs.dimension = s.dimension AND rs.category = s.category
        LEFT JOIN link_trust_subjects AS reporter_subject
          ON reporter_subject.subject_kind = 'node'
         AND reporter_subject.node_fingerprint = r.fingerprint
        WHERE s.subject_id = ? AND s.revoked_at IS NULL
          AND s.effective_expires_at > ?
          AND (
            reporter_subject.subject_id IS NULL OR NOT EXISTS (
              SELECT 1 FROM link_trust_effective_states AS reporter_state
              WHERE reporter_state.subject_id = reporter_subject.subject_id
                AND reporter_state.dimension IN ('identity_integrity', 'resource_behavior')
                AND reporter_state.state != 'established'
            )
          )
        """,
        (subject.subject_id, now_value),
    ).fetchall()
    applicable_dimensions = (
        set(TrustDimension)
        if subject.kind == "user"
        else {TrustDimension.IDENTITY_INTEGRITY, TrustDimension.RESOURCE_BEHAVIOR}
    )
    active_trigger_count = sum(
        1
        for row in [*local_rows, *remote_rows]
        if TrustDimension(row["dimension"]) in applicable_dimensions
        and row["category"] in _CATEGORIES_BY_DIMENSION[TrustDimension(row["dimension"])]
    )
    vouch_domains = _vouch_domains(db, subject, now_value)
    required_vouch_domains = 2 if subject.kind == "node" else 1
    explanation = {
        "age_requirement_met": age_ok,
        "activity_days": activity_days,
        "required_activity_days": 3,
        "active_trigger_count": active_trigger_count,
        "vouch_domains": sorted(vouch_domains),
        "required_vouch_domains": required_vouch_domains,
    }
    established = (
        age_ok and activity_days >= 3 and active_trigger_count == 0
        and len(vouch_domains) >= required_vouch_domains
    )
    if established:
        return TrustState.ESTABLISHED, "automatic_graduation", explanation
    return TrustState.PROBATIONARY, "probation_requirements_incomplete", explanation


def _recompute_dimension(
    db: Database,
    subject: TrustSubject,
    dimension: TrustDimension,
    now_value: str,
    now: datetime,
    actor_user_id: int | None,
) -> None:
    previous = db.connection.execute(
        """SELECT state, reason_code, recovery_started_at
           FROM link_trust_effective_states
           WHERE subject_id = ? AND dimension = ?""",
        (subject.subject_id, dimension.value),
    ).fetchone()
    override = _active_override(db, subject.subject_id, dimension, now_value)
    local, remote = _active_evidence(db, subject.subject_id, dimension, now_value)

    local_auto = [
        item for item in local
        if item["evidence_class"] == EvidenceClass.SELF_VERIFYING.value
        and item["category"] in IDENTITY_CATEGORIES
    ]
    remote_auto = [
        item for item in remote
        if item["evidence_class"] == EvidenceClass.SELF_VERIFYING.value
        and item["category"] in IDENTITY_CATEGORIES
    ]
    remote_domains: dict[str, float] = {}
    for item in remote_auto:
        domain_id = str(item["domain_id"])
        remote_domains[domain_id] = max(
            remote_domains.get(domain_id, 0.0), float(item["domain_weight"])
        )
    remote_weight = sum(remote_domains.values())
    remote_threshold_met = len(remote_domains) >= 2 and remote_weight >= 2.0
    automatic_trigger = bool(local_auto) or remote_threshold_met

    recovery_started_at: str | None = None
    if override is not None:
        state = TrustState(override["state"])
        reason_code = "manual_block" if state == TrustState.BLOCKED else "sysop_override"
        explanation: dict[str, object] = {
            "override_id": override["override_id"],
            "override_reason": override["reason"],
            "active_local_evidence": local,
            "active_remote_evidence": remote,
        }
    elif automatic_trigger:
        state = TrustState.QUARANTINED
        reason_code = (
            "local_self_verifying_evidence" if local_auto else "remote_domain_threshold"
        )
        explanation = {
            "active_local_evidence": local,
            "active_remote_evidence": remote,
            "counted_domains": remote_domains,
            "counted_weight": remote_weight,
            "required_domains": 2,
            "required_weight": 2.0,
        }
    elif previous is not None and previous["state"] == TrustState.QUARANTINED.value:
        started_value = previous["recovery_started_at"] or now_value
        started = _parse_timestamp(started_value, field_name="recovery_started_at")
        if now < started + _RECOVERY_HOLD:
            state = TrustState.QUARANTINED
            reason_code = "recovery_hold"
            recovery_started_at = started_value
            explanation = {
                "recovery_started_at": started_value,
                "release_at": _iso(started + _RECOVERY_HOLD),
                "active_local_evidence": local,
                "active_remote_evidence": remote,
            }
        else:
            state = TrustState.PROBATIONARY
            reason_code = "automatic_recovery"
            explanation = {"recovered_at": now_value, "returns_to": "probationary"}
    else:
        state, reason_code, explanation = _ordinary_state(db, subject, now_value, now)

    explanation_json = json.dumps(explanation, sort_keys=True, separators=(",", ":"))
    db.connection.execute(
        """
        INSERT INTO link_trust_effective_states (
            subject_id, dimension, state, reason_code, recovery_started_at,
            explanation_json, evaluated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(subject_id, dimension) DO UPDATE SET
            state = excluded.state,
            reason_code = excluded.reason_code,
            recovery_started_at = excluded.recovery_started_at,
            explanation_json = excluded.explanation_json,
            evaluated_at = excluded.evaluated_at
        """,
        (
            subject.subject_id, dimension.value, state.value, reason_code,
            recovery_started_at, explanation_json, now_value,
        ),
    )
    if previous is None or previous["state"] != state.value:
        db.connection.execute(
            """
            INSERT INTO link_trust_decision_audit (
                subject_id, dimension, previous_state, new_state, reason_code,
                explanation_json, actor_user_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subject.subject_id, dimension.value,
                previous["state"] if previous is not None else None,
                state.value, reason_code, explanation_json, actor_user_id, now_value,
            ),
        )


def record_trust_signal(
    db: Database,
    *,
    content_id: str,
    issuer_fingerprint: str,
    subject: TrustSubject,
    dimension: TrustDimension | str,
    category: str,
    evidence_class: EvidenceClass | str,
    observed_at: str,
    issued_at: str,
    expires_at: str,
    evidence: dict[str, object] | None = None,
    explanation: str | None = None,
    now_iso: str | None = None,
) -> bool:
    """Persist one already-signature-verified signal; return False on replay."""
    normalized_dimension = _dimension(dimension)
    normalized_evidence = _evidence_class(evidence_class)
    validate_category_evidence(normalized_dimension, category, normalized_evidence)
    issued = _parse_timestamp(issued_at, field_name="issued_at")
    observed = _parse_timestamp(observed_at, field_name="observed_at")
    declared_expiry = _parse_timestamp(expires_at, field_name="expires_at")
    now_value, now = _now(now_iso)
    if issued > now + _FUTURE_TOLERANCE:
        raise ValueError("issued_at is more than five minutes in the future")
    if declared_expiry <= issued:
        raise ValueError("expires_at must be after issued_at")
    if observed > issued + _FUTURE_TOLERANCE:
        raise ValueError("observed_at cannot be after issued_at")
    effective_expiry = min(declared_expiry, issued + _MAX_LIFETIME[normalized_evidence])
    with db.connection:
        _require_subject(db, subject)
        cursor = db.connection.execute(
            """
            INSERT OR IGNORE INTO link_trust_signals (
                content_id, issuer_fingerprint, subject_id, dimension, category,
                evidence_class, observed_at, issued_at, declared_expires_at,
                effective_expires_at, received_at, evidence_json, explanation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                content_id, issuer_fingerprint, subject.subject_id,
                normalized_dimension.value, category, normalized_evidence.value,
                observed_at, issued_at, expires_at, _iso(effective_expiry), now_value,
                json.dumps(evidence, sort_keys=True, separators=(",", ":")) if evidence else None,
                explanation,
            ),
        )
        if cursor.rowcount:
            _recompute_subject(db, subject, now_value, now, actor_user_id=None)
        return bool(cursor.rowcount)


def revoke_trust_signal(
    db: Database,
    content_id: str,
    *,
    revocation_content_id: str,
    now_iso: str | None = None,
) -> None:
    now_value, now = _now(now_iso)
    with db.connection:
        row = db.connection.execute(
            "SELECT subject_id FROM link_trust_signals WHERE content_id = ?", (content_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown trust signal: {content_id!r}")
        db.connection.execute(
            """UPDATE link_trust_signals
               SET revoked_by_content_id = ?, revoked_at = ?
               WHERE content_id = ? AND revoked_at IS NULL""",
            (revocation_content_id, now_value, content_id),
        )
        _recompute_subject(
            db, _subject_from_id(db, row["subject_id"]), now_value, now, actor_user_id=None
        )


def record_reproduced_signal_observation(
    db: Database,
    signal_content_id: str,
    *,
    observation_id: str,
    now_iso: str | None = None,
) -> bool:
    """Promote caller-reproduced self-verifying evidence to local fact.

    The caller owns bounded fetch/hash/parse/reproduction; this local-domain
    function records that successful result. The new observation intentionally
    has its own lifetime and survives later expiry or revocation of the remote
    reporter's signal.
    """
    row = db.connection.execute(
        """
        SELECT subject_id, dimension, category, evidence_class, evidence_json
        FROM link_trust_signals WHERE content_id = ?
        """,
        (signal_content_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown trust signal: {signal_content_id!r}")
    if row["evidence_class"] != EvidenceClass.SELF_VERIFYING.value:
        raise ValueError("only self-verifying signal evidence can be reproduced locally")
    subject = _subject_from_id(db, row["subject_id"])
    now_value, _ = _now(now_iso)
    return record_local_observation(
        db,
        observation_id=observation_id,
        subject=subject,
        dimension=row["dimension"],
        category=row["category"],
        evidence_class=EvidenceClass.SELF_VERIFYING,
        observed_at=now_value,
        evidence=json.loads(row["evidence_json"]) if row["evidence_json"] else None,
        explanation=f"reproduced from trust signal {signal_content_id}",
        now_iso=now_value,
    )


def record_vouch(
    db: Database,
    *,
    content_id: str,
    issuer_fingerprint: str,
    subject: TrustSubject,
    issued_at: str,
    expires_at: str,
    explanation: str | None = None,
    now_iso: str | None = None,
) -> bool:
    issued = _parse_timestamp(issued_at, field_name="issued_at")
    declared_expiry = _parse_timestamp(expires_at, field_name="expires_at")
    now_value, now = _now(now_iso)
    if issued > now + _FUTURE_TOLERANCE:
        raise ValueError("issued_at is more than five minutes in the future")
    if declared_expiry <= issued:
        raise ValueError("expires_at must be after issued_at")
    effective_expiry = min(declared_expiry, issued + _VOUCH_MAX_LIFETIME)
    with db.connection:
        _require_subject(db, subject)
        cursor = db.connection.execute(
            """
            INSERT OR IGNORE INTO link_trust_vouches (
                content_id, issuer_fingerprint, subject_id, issued_at,
                declared_expires_at, effective_expires_at, received_at, explanation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                content_id, issuer_fingerprint, subject.subject_id, issued_at,
                expires_at, _iso(effective_expiry), now_value, explanation,
            ),
        )
        if cursor.rowcount:
            _recompute_subject(db, subject, now_value, now, actor_user_id=None)
        return bool(cursor.rowcount)


def revoke_vouch(
    db: Database,
    content_id: str,
    *,
    revocation_content_id: str,
    now_iso: str | None = None,
) -> None:
    now_value, now = _now(now_iso)
    with db.connection:
        row = db.connection.execute(
            "SELECT subject_id FROM link_trust_vouches WHERE content_id = ?", (content_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown trust vouch: {content_id!r}")
        db.connection.execute(
            """UPDATE link_trust_vouches
               SET revoked_by_content_id = ?, revoked_at = ?
               WHERE content_id = ? AND revoked_at IS NULL""",
            (revocation_content_id, now_value, content_id),
        )
        _recompute_subject(
            db, _subject_from_id(db, row["subject_id"]), now_value, now, actor_user_id=None
        )


def set_trust_override(
    db: Database,
    subject: TrustSubject,
    dimension: TrustDimension | str,
    state: TrustState | str,
    *,
    reason: str,
    actor_user_id: int | None = None,
    expires_at: str | None = None,
    now_iso: str | None = None,
) -> int:
    normalized_dimension = _dimension(dimension)
    try:
        normalized_state = TrustState(state)
    except ValueError as exc:
        raise ValueError(f"unknown trust state: {state!r}") from exc
    if not reason.strip():
        raise ValueError("trust override reason must not be empty")
    now_value, now = _now(now_iso)
    if expires_at is not None and _parse_timestamp(expires_at, field_name="expires_at") <= now:
        raise ValueError("override expiry must be in the future")
    with db.connection:
        _require_subject(db, subject)
        db.connection.execute(
            """UPDATE link_trust_overrides SET cleared_at = ?
               WHERE subject_id = ? AND dimension = ? AND cleared_at IS NULL""",
            (now_value, subject.subject_id, normalized_dimension.value),
        )
        cursor = db.connection.execute(
            """
            INSERT INTO link_trust_overrides (
                subject_id, dimension, state, reason, actor_user_id,
                created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subject.subject_id, normalized_dimension.value, normalized_state.value,
                reason, actor_user_id, now_value, expires_at,
            ),
        )
        _recompute_subject(db, subject, now_value, now, actor_user_id=actor_user_id)
        return int(cursor.lastrowid)


def clear_trust_override(
    db: Database,
    override_id: int,
    *,
    actor_user_id: int | None = None,
    now_iso: str | None = None,
) -> None:
    now_value, now = _now(now_iso)
    with db.connection:
        row = db.connection.execute(
            "SELECT subject_id FROM link_trust_overrides WHERE override_id = ?", (override_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown trust override: {override_id}")
        db.connection.execute(
            """UPDATE link_trust_overrides SET cleared_at = ?
               WHERE override_id = ? AND cleared_at IS NULL""",
            (now_value, override_id),
        )
        _recompute_subject(
            db, _subject_from_id(db, row["subject_id"]), now_value, now,
            actor_user_id=actor_user_id,
        )


def recompute_all_trust_states(db: Database, *, now_iso: str | None = None) -> None:
    """Rebuild every persisted projection at startup or after policy changes."""
    now_value, now = _now(now_iso)
    with db.connection:
        _recompute_all(db, now_value, now, actor_user_id=None)


def maintain_trust_state(
    db: Database, *, now_iso: str | None = None, retention_days: int = 365
) -> dict[str, int]:
    """Recompute policy and prune old inactive inputs in one transaction.

    Effective-state transition audits keep the evidence content IDs which
    justified historical decisions, while bulky inactive input rows are
    removed after the design-default 365 days. An explicit retention hold is
    the narrow escape hatch for a SysOp's legal or diagnostic need.
    """
    if retention_days < 1:
        raise ValueError("trust retention_days must be positive")
    now_value, now = _now(now_iso)
    cutoff = _iso(now - timedelta(days=retention_days))
    with db.connection:
        _recompute_all(db, now_value, now, actor_user_id=None)
        signals = db.connection.execute(
            """
            DELETE FROM link_trust_signals
            WHERE retention_hold = 0 AND (
                (revoked_at IS NOT NULL AND revoked_at < ?)
                OR (revoked_at IS NULL AND effective_expires_at < ?)
            )
            """,
            (cutoff, cutoff),
        ).rowcount
        observations = db.connection.execute(
            """
            DELETE FROM link_trust_local_observations
            WHERE retention_hold = 0 AND (
                (cleared_at IS NOT NULL AND cleared_at < ?)
                OR (cleared_at IS NULL AND expires_at < ?)
            )
            """,
            (cutoff, cutoff),
        ).rowcount
        vouches = db.connection.execute(
            """
            DELETE FROM link_trust_vouches
            WHERE retention_hold = 0 AND (
                (revoked_at IS NOT NULL AND revoked_at < ?)
                OR (revoked_at IS NULL AND effective_expires_at < ?)
            )
            """,
            (cutoff, cutoff),
        ).rowcount
    return {"signals": signals, "observations": observations, "vouches": vouches}


def get_effective_trust_state(
    db: Database, subject: TrustSubject, dimension: TrustDimension | str
) -> EffectiveTrustState:
    normalized_dimension = _dimension(dimension)
    row = db.connection.execute(
        """
        SELECT state, reason_code, recovery_started_at, explanation_json, evaluated_at
        FROM link_trust_effective_states WHERE subject_id = ? AND dimension = ?
        """,
        (subject.subject_id, normalized_dimension.value),
    ).fetchone()
    if row is None:
        raise ValueError("trust subject has no effective-state projection")
    return EffectiveTrustState(
        subject=subject,
        dimension=normalized_dimension,
        state=TrustState(row["state"]),
        reason_code=row["reason_code"],
        recovery_started_at=row["recovery_started_at"],
        explanation=json.loads(row["explanation_json"]),
        evaluated_at=row["evaluated_at"],
    )


def _require_subject(db: Database, subject: TrustSubject) -> None:
    if not db.connection.execute(
        "SELECT 1 FROM link_trust_subjects WHERE subject_id = ?", (subject.subject_id,)
    ).fetchone():
        raise ValueError("trust subject must be registered before recording inputs")


def _subject_from_id(db: Database, subject_id: str) -> TrustSubject:
    row = db.connection.execute(
        """SELECT subject_kind, node_fingerprint, opaque_user_id
           FROM link_trust_subjects WHERE subject_id = ?""",
        (subject_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown trust subject ID: {subject_id}")
    return TrustSubject(row["subject_kind"], row["node_fingerprint"], row["opaque_user_id"])


def _audit_config(
    db: Database,
    object_kind: str,
    object_id: str,
    action: str,
    details: dict[str, object],
    actor_user_id: int | None,
    now_value: str,
) -> None:
    db.connection.execute(
        """
        INSERT INTO link_trust_config_audit (
            object_kind, object_id, action, details_json, actor_user_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            object_kind, object_id, action,
            json.dumps(details, sort_keys=True, separators=(",", ":")),
            actor_user_id, now_value,
        ),
    )


def record_local_observation(
    db: Database,
    *,
    observation_id: str,
    subject: TrustSubject,
    dimension: TrustDimension | str,
    category: str,
    evidence_class: EvidenceClass | str,
    observed_at: str,
    evidence: dict[str, object] | None = None,
    explanation: str | None = None,
    now_iso: str | None = None,
) -> bool:
    normalized_dimension = _dimension(dimension)
    normalized_evidence = _evidence_class(evidence_class)
    validate_category_evidence(normalized_dimension, category, normalized_evidence)
    observed = _parse_timestamp(observed_at, field_name="observed_at")
    now_value, now = _now(now_iso)
    if observed > now + _FUTURE_TOLERANCE:
        raise ValueError("observed_at is more than five minutes in the future")
    expiry = observed + _MAX_LIFETIME[normalized_evidence]
    with db.connection:
        _require_subject(db, subject)
        cursor = db.connection.execute(
            """
            INSERT OR IGNORE INTO link_trust_local_observations (
                observation_id, subject_id, dimension, category, evidence_class,
                observed_at, expires_at, evidence_json, explanation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation_id, subject.subject_id, normalized_dimension.value,
                category, normalized_evidence.value, observed_at, _iso(expiry),
                json.dumps(evidence, sort_keys=True, separators=(",", ":")) if evidence else None,
                explanation,
            ),
        )
        if cursor.rowcount:
            _recompute_subject(db, subject, now_value, now, actor_user_id=None)
        return bool(cursor.rowcount)


def clear_local_observation(
    db: Database, observation_id: str, *, now_iso: str | None = None
) -> None:
    now_value, now = _now(now_iso)
    with db.connection:
        row = db.connection.execute(
            "SELECT subject_id FROM link_trust_local_observations WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown local trust observation: {observation_id!r}")
        db.connection.execute(
            """UPDATE link_trust_local_observations SET cleared_at = ?
               WHERE observation_id = ? AND cleared_at IS NULL""",
            (now_value, observation_id),
        )
        _recompute_subject(
            db, _subject_from_id(db, row["subject_id"]), now_value, now, actor_user_id=None
        )


def configure_trusted_reporter(
    db: Database,
    fingerprint: str,
    *,
    domain_id: str,
    scopes: Iterable[tuple[TrustDimension | str, str]],
    can_vouch_nodes: bool = False,
    can_vouch_users: bool = False,
    actor_user_id: int | None = None,
    now_iso: str | None = None,
) -> None:
    if not fingerprint:
        raise ValueError("reporter fingerprint must not be empty")
    normalized_scopes = sorted({(_dimension(d).value, category) for d, category in scopes})
    now_value, now = _now(now_iso)
    with db.connection:
        if not db.connection.execute(
            "SELECT 1 FROM link_trust_domains WHERE domain_id = ?", (domain_id,)
        ).fetchone():
            raise ValueError(f"unknown trust domain: {domain_id!r}")
        exists = db.connection.execute(
            "SELECT 1 FROM link_trust_reporters WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        db.connection.execute(
            """
            INSERT INTO link_trust_reporters (
                fingerprint, domain_id, can_vouch_nodes,
                can_vouch_users, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                domain_id = excluded.domain_id,
                can_vouch_nodes = excluded.can_vouch_nodes,
                can_vouch_users = excluded.can_vouch_users,
                updated_at = excluded.updated_at
            """,
            (fingerprint, domain_id, int(can_vouch_nodes), int(can_vouch_users),
             now_value, now_value),
        )
        db.connection.execute(
            "DELETE FROM link_trust_reporter_scopes WHERE reporter_fingerprint = ?",
            (fingerprint,),
        )
        db.connection.executemany(
            """INSERT INTO link_trust_reporter_scopes
               (reporter_fingerprint, dimension, category) VALUES (?, ?, ?)""",
            [(fingerprint, dimension, category) for dimension, category in normalized_scopes],
        )
        details = {
            "domain_id": domain_id, "scopes": normalized_scopes,
            "can_vouch_nodes": can_vouch_nodes, "can_vouch_users": can_vouch_users,
        }
        _audit_config(
            db, "reporter", fingerprint, "updated" if exists else "created",
            details, actor_user_id, now_value,
        )
        _recompute_all(db, now_value, now, actor_user_id)

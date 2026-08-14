"""Phase-4 local trust policy tests (design doc §12, issue #126)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from netbbs.link.trust import (
    EvidenceClass,
    TrustDimension,
    TrustState,
    TrustSubject,
    clear_local_observation,
    clear_trust_override,
    configure_trust_anchor,
    configure_trust_domain,
    configure_trusted_reporter,
    get_effective_trust_state,
    maintain_trust_state,
    record_activity,
    record_local_observation,
    record_reproduced_signal_observation,
    record_trust_signal,
    record_vouch,
    recompute_all_trust_states,
    register_subject,
    remove_trust_anchor,
    revoke_trust_signal,
    set_trust_override,
)
from netbbs.storage.database import Database


NOW = datetime(2026, 7, 26, 12, tzinfo=timezone.utc)


def stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def register_old_node(db, fingerprint="subject"):
    subject = TrustSubject.node(fingerprint)
    register_subject(
        db, subject, first_accepted_at=stamp(NOW - timedelta(days=31)), now_iso=stamp(NOW)
    )
    return subject


def configure_reporter(
    db, reporter, domain, *, node_vouch=False, user_vouch=False, category="signed_equivocation"
):
    configure_trust_domain(
        db, domain, display_name=domain.title(), now_iso=stamp(NOW)
    )
    configure_trusted_reporter(
        db,
        reporter,
        domain_id=domain,
        scopes=[(TrustDimension.IDENTITY_INTEGRITY, category)],
        can_vouch_nodes=node_vouch,
        can_vouch_users=user_vouch,
        now_iso=stamp(NOW),
    )


def add_vouch(db, subject, reporter, number):
    record_vouch(
        db,
        content_id=f"vouch-{number}",
        issuer_fingerprint=reporter,
        subject=subject,
        issued_at=stamp(NOW - timedelta(days=1)),
        expires_at=stamp(NOW + timedelta(days=200)),
        now_iso=stamp(NOW),
    )


def add_signal(db, subject, reporter, number, *, now=NOW):
    return record_trust_signal(
        db,
        content_id=f"signal-{number}",
        issuer_fingerprint=reporter,
        subject=subject,
        dimension=TrustDimension.IDENTITY_INTEGRITY,
        category="signed_equivocation",
        evidence_class=EvidenceClass.SELF_VERIFYING,
        observed_at=stamp(now - timedelta(hours=2)),
        issued_at=stamp(now - timedelta(hours=1)),
        expires_at=stamp(now + timedelta(days=180)),
        now_iso=stamp(now),
    )


def test_node_and_user_subjects_are_independent_and_start_probationary(db):
    node = TrustSubject.node("home")
    user = TrustSubject.user("home", "opaque-42")
    register_subject(db, node, first_accepted_at=stamp(NOW), now_iso=stamp(NOW))
    register_subject(db, user, first_accepted_at=stamp(NOW), now_iso=stamp(NOW))

    assert node.subject_id != user.subject_id
    for subject in (node, user):
        for dimension in TrustDimension:
            assert get_effective_trust_state(db, subject, dimension).state == TrustState.PROBATIONARY


def test_anchor_role_does_not_grant_reporter_authority(db):
    configure_trust_anchor(
        db, "anchor-only", reason="Privately verified identity", now_iso=stamp(NOW)
    )
    assert db.connection.execute(
        "SELECT COUNT(*) FROM link_trust_anchors WHERE fingerprint = 'anchor-only'"
    ).fetchone()[0] == 1
    assert db.connection.execute(
        "SELECT COUNT(*) FROM link_trust_reporters WHERE fingerprint = 'anchor-only'"
    ).fetchone()[0] == 0

    remove_trust_anchor(db, "anchor-only", now_iso=stamp(NOW + timedelta(minutes=1)))
    actions = db.connection.execute(
        """SELECT action FROM link_trust_config_audit
           WHERE object_kind = 'anchor' AND object_id = 'anchor-only' ORDER BY audit_id"""
    ).fetchall()
    assert [row[0] for row in actions] == ["created", "removed"]


def test_node_graduation_requires_three_dates_and_two_vouch_domains(db):
    subject = register_old_node(db)
    configure_reporter(db, "reporter-a", "domain-a", node_vouch=True)
    configure_reporter(db, "reporter-b", "domain-b", node_vouch=True)
    for day in (1, 2, 3):
        record_activity(
            db, subject, activity_date=f"2026-07-{day:02d}", direct=True, now_iso=stamp(NOW)
        )
    add_vouch(db, subject, "reporter-a", 1)
    assert get_effective_trust_state(
        db, subject, TrustDimension.IDENTITY_INTEGRITY
    ).state == TrustState.PROBATIONARY

    add_vouch(db, subject, "reporter-b", 2)
    for dimension in TrustDimension:
        assert get_effective_trust_state(db, subject, dimension).state == TrustState.ESTABLISHED


def test_two_reporters_in_one_domain_do_not_satisfy_independence(db):
    subject = register_old_node(db)
    configure_reporter(db, "reporter-a", "shared", node_vouch=True)
    configure_trusted_reporter(
        db,
        "reporter-b",
        domain_id="shared",
        scopes=[(TrustDimension.IDENTITY_INTEGRITY, "signed_equivocation")],
        can_vouch_nodes=True,
        now_iso=stamp(NOW),
    )
    for day in (1, 2, 3):
        record_activity(
            db, subject, activity_date=f"2026-07-{day:02d}", direct=True, now_iso=stamp(NOW)
        )
    add_vouch(db, subject, "reporter-a", 1)
    add_vouch(db, subject, "reporter-b", 2)

    state = get_effective_trust_state(db, subject, TrustDimension.IDENTITY_INTEGRITY)
    assert state.state == TrustState.PROBATIONARY
    assert state.explanation["vouch_domains"] == ["shared"]


def test_remote_quarantine_requires_two_full_weight_domains(db):
    subject = register_old_node(db)
    configure_reporter(db, "reporter-a", "domain-a")
    configure_reporter(db, "reporter-a2", "domain-a")
    configure_reporter(db, "reporter-b", "domain-b")

    add_signal(db, subject, "reporter-a", 1)
    add_signal(db, subject, "reporter-a2", 2)
    assert get_effective_trust_state(
        db, subject, TrustDimension.IDENTITY_INTEGRITY
    ).state == TrustState.PROBATIONARY

    add_signal(db, subject, "reporter-b", 3)
    state = get_effective_trust_state(db, subject, TrustDimension.IDENTITY_INTEGRITY)
    assert state.state == TrustState.QUARANTINED
    assert state.reason_code == "remote_domain_threshold"
    assert state.explanation["counted_weight"] == 2.0


def test_probationary_reporter_contributes_no_trust_weight(db):
    subject = register_old_node(db)
    configure_reporter(db, "probation-reporter", "domain-a")
    configure_reporter(db, "established-reporter", "domain-b")
    probation_reporter = TrustSubject.node("probation-reporter")
    register_subject(
        db, probation_reporter, first_accepted_at=stamp(NOW), now_iso=stamp(NOW)
    )
    add_signal(db, subject, "probation-reporter", 10)
    add_signal(db, subject, "established-reporter", 11)
    state = get_effective_trust_state(db, subject, TrustDimension.IDENTITY_INTEGRITY)
    assert state.state == TrustState.PROBATIONARY

    for dimension in (TrustDimension.IDENTITY_INTEGRITY, TrustDimension.RESOURCE_BEHAVIOR):
        set_trust_override(
            db, probation_reporter, dimension, TrustState.ESTABLISHED,
            reason="reporter reviewed", now_iso=stamp(NOW),
        )
    recompute_all_trust_states(db, now_iso=stamp(NOW))
    assert get_effective_trust_state(
        db, subject, TrustDimension.IDENTITY_INTEGRITY
    ).state == TrustState.QUARANTINED


def test_colluding_domains_below_weight_threshold_do_not_quarantine(db):
    subject = register_old_node(db)
    for reporter, domain in (("reporter-a", "domain-a"), ("reporter-b", "domain-b")):
        configure_trust_domain(
            db, domain, display_name=domain, weight=0.75, now_iso=stamp(NOW)
        )
        configure_trusted_reporter(
            db,
            reporter,
            domain_id=domain,
            scopes=[(TrustDimension.IDENTITY_INTEGRITY, "signed_equivocation")],
            now_iso=stamp(NOW),
        )
    add_signal(db, subject, "reporter-a", 1)
    add_signal(db, subject, "reporter-b", 2)

    assert get_effective_trust_state(
        db, subject, TrustDimension.IDENTITY_INTEGRITY
    ).state == TrustState.PROBATIONARY


def test_local_self_verifying_evidence_quarantines_only_its_dimension(db):
    subject = register_old_node(db)
    record_local_observation(
        db,
        observation_id="local-proof",
        subject=subject,
        dimension=TrustDimension.IDENTITY_INTEGRITY,
        category="signed_equivocation",
        evidence_class=EvidenceClass.SELF_VERIFYING,
        observed_at=stamp(NOW),
        evidence={"conflicting_heads": ["a", "b"]},
        now_iso=stamp(NOW),
    )

    integrity = get_effective_trust_state(db, subject, TrustDimension.IDENTITY_INTEGRITY)
    assert integrity.state == TrustState.QUARANTINED
    assert integrity.reason_code == "local_self_verifying_evidence"
    assert get_effective_trust_state(
        db, subject, TrustDimension.RESOURCE_BEHAVIOR
    ).state == TrustState.PROBATIONARY


def test_subjective_content_report_never_quarantines_transport(db):
    subject = register_old_node(db)
    configure_trust_domain(db, "mods", display_name="Mods", now_iso=stamp(NOW))
    configure_trusted_reporter(
        db,
        "moderator",
        domain_id="mods",
        scopes=[(TrustDimension.CONTENT_CONDUCT, "spam")],
        now_iso=stamp(NOW),
    )
    record_trust_signal(
        db,
        content_id="spam-report",
        issuer_fingerprint="moderator",
        subject=subject,
        dimension=TrustDimension.CONTENT_CONDUCT,
        category="spam",
        evidence_class=EvidenceClass.SUBJECTIVE,
        observed_at=stamp(NOW - timedelta(hours=2)),
        issued_at=stamp(NOW - timedelta(hours=1)),
        expires_at=stamp(NOW + timedelta(days=60)),
        now_iso=stamp(NOW),
    )

    assert get_effective_trust_state(
        db, subject, TrustDimension.CONTENT_CONDUCT
    ).state == TrustState.PROBATIONARY
    assert get_effective_trust_state(
        db, subject, TrustDimension.IDENTITY_INTEGRITY
    ).state == TrustState.PROBATIONARY


def test_clear_starts_recovery_hold_and_release_returns_to_probation(db):
    subject = register_old_node(db)
    record_local_observation(
        db,
        observation_id="local-proof",
        subject=subject,
        dimension=TrustDimension.IDENTITY_INTEGRITY,
        category="signed_equivocation",
        evidence_class=EvidenceClass.SELF_VERIFYING,
        observed_at=stamp(NOW),
        now_iso=stamp(NOW),
    )
    cleared_at = NOW + timedelta(hours=1)
    clear_local_observation(db, "local-proof", now_iso=stamp(cleared_at))

    during_hold = get_effective_trust_state(db, subject, TrustDimension.IDENTITY_INTEGRITY)
    assert during_hold.state == TrustState.QUARANTINED
    assert during_hold.reason_code == "recovery_hold"
    recompute_all_trust_states(db, now_iso=stamp(cleared_at + timedelta(hours=23)))
    assert get_effective_trust_state(
        db, subject, TrustDimension.IDENTITY_INTEGRITY
    ).state == TrustState.QUARANTINED

    recompute_all_trust_states(db, now_iso=stamp(cleared_at + timedelta(hours=24)))
    released = get_effective_trust_state(db, subject, TrustDimension.IDENTITY_INTEGRITY)
    assert released.state == TrustState.PROBATIONARY
    assert released.reason_code == "automatic_recovery"


def test_fresh_evidence_restarts_recovery_hold(db):
    subject = register_old_node(db)
    record_local_observation(
        db, observation_id="proof-1", subject=subject,
        dimension=TrustDimension.IDENTITY_INTEGRITY, category="signed_equivocation",
        evidence_class=EvidenceClass.SELF_VERIFYING, observed_at=stamp(NOW), now_iso=stamp(NOW),
    )
    clear_local_observation(db, "proof-1", now_iso=stamp(NOW + timedelta(hours=1)))
    fresh_at = NOW + timedelta(hours=20)
    record_local_observation(
        db, observation_id="proof-2", subject=subject,
        dimension=TrustDimension.IDENTITY_INTEGRITY, category="signed_equivocation",
        evidence_class=EvidenceClass.SELF_VERIFYING, observed_at=stamp(fresh_at), now_iso=stamp(fresh_at),
    )
    clear_local_observation(db, "proof-2", now_iso=stamp(fresh_at + timedelta(hours=1)))

    state = get_effective_trust_state(db, subject, TrustDimension.IDENTITY_INTEGRITY)
    assert state.reason_code == "recovery_hold"
    assert state.recovery_started_at == stamp(fresh_at + timedelta(hours=1))


def test_manual_block_precedes_override_and_evidence_but_is_reversible(db):
    subject = register_old_node(db)
    block_id = set_trust_override(
        db,
        subject,
        TrustDimension.IDENTITY_INTEGRITY,
        TrustState.BLOCKED,
        reason="Known compromised key",
        now_iso=stamp(NOW),
    )
    assert get_effective_trust_state(
        db, subject, TrustDimension.IDENTITY_INTEGRITY
    ).reason_code == "manual_block"

    clear_trust_override(
        db, block_id, now_iso=stamp(NOW + timedelta(minutes=1))
    )
    assert get_effective_trust_state(
        db, subject, TrustDimension.IDENTITY_INTEGRITY
    ).state == TrustState.PROBATIONARY
    transitions = db.connection.execute(
        """SELECT previous_state, new_state FROM link_trust_decision_audit
           WHERE subject_id = ? AND dimension = ? ORDER BY audit_id""",
        (subject.subject_id, TrustDimension.IDENTITY_INTEGRITY.value),
    ).fetchall()
    assert [(row[0], row[1]) for row in transitions] == [
        (None, "probationary"),
        ("probationary", "blocked"),
        ("blocked", "probationary"),
    ]


def test_signal_replay_is_deduplicated_and_lifetime_is_clamped(db):
    subject = register_old_node(db)
    configure_reporter(db, "reporter", "domain")
    assert add_signal(db, subject, "reporter", 1)
    assert not add_signal(db, subject, "reporter", 1)

    row = db.connection.execute(
        """SELECT declared_expires_at, effective_expires_at
           FROM link_trust_signals WHERE content_id = 'signal-1'"""
    ).fetchone()
    assert row["declared_expires_at"] == stamp(NOW + timedelta(days=180))
    assert row["effective_expires_at"] == stamp(NOW - timedelta(hours=1) + timedelta(days=90))


def test_future_signal_and_invalid_category_evidence_pair_are_rejected(db):
    subject = register_old_node(db)
    configure_reporter(db, "reporter", "domain")
    with pytest.raises(ValueError, match="five minutes"):
        record_trust_signal(
            db,
            content_id="future",
            issuer_fingerprint="reporter",
            subject=subject,
            dimension=TrustDimension.IDENTITY_INTEGRITY,
            category="signed_equivocation",
            evidence_class=EvidenceClass.SELF_VERIFYING,
            observed_at=stamp(NOW),
            issued_at=stamp(NOW + timedelta(minutes=6)),
            expires_at=stamp(NOW + timedelta(days=1)),
            now_iso=stamp(NOW),
        )
    with pytest.raises(ValueError, match="content-conduct"):
        record_trust_signal(
            db,
            content_id="wrong-class",
            issuer_fingerprint="reporter",
            subject=subject,
            dimension=TrustDimension.CONTENT_CONDUCT,
            category="spam",
            evidence_class=EvidenceClass.OBSERVER_ATTESTED,
            observed_at=stamp(NOW - timedelta(hours=2)),
            issued_at=stamp(NOW - timedelta(hours=1)),
            expires_at=stamp(NOW + timedelta(days=1)),
            now_iso=stamp(NOW),
        )


def test_unknown_versioned_category_is_retained_but_has_no_policy_effect(db):
    subject = register_old_node(db)
    configure_trust_domain(db, "domain", display_name="Domain", now_iso=stamp(NOW))
    configure_trusted_reporter(
        db, "reporter", domain_id="domain",
        scopes=[(TrustDimension.IDENTITY_INTEGRITY, "future_category")],
        now_iso=stamp(NOW),
    )
    record_trust_signal(
        db, content_id="future-category", issuer_fingerprint="reporter", subject=subject,
        dimension=TrustDimension.IDENTITY_INTEGRITY, category="future_category",
        evidence_class=EvidenceClass.SELF_VERIFYING,
        observed_at=stamp(NOW - timedelta(hours=2)),
        issued_at=stamp(NOW - timedelta(hours=1)),
        expires_at=stamp(NOW + timedelta(days=1)), now_iso=stamp(NOW),
    )

    state = get_effective_trust_state(db, subject, TrustDimension.IDENTITY_INTEGRITY)
    assert state.state == TrustState.PROBATIONARY
    assert state.explanation["active_trigger_count"] == 0
    assert db.connection.execute(
        "SELECT COUNT(*) FROM link_trust_signals WHERE content_id = 'future-category'"
    ).fetchone()[0] == 1


def test_revocation_removes_remote_support_without_deleting_history(db):
    subject = register_old_node(db)
    configure_reporter(db, "reporter-a", "domain-a")
    configure_reporter(db, "reporter-b", "domain-b")
    add_signal(db, subject, "reporter-a", 1)
    add_signal(db, subject, "reporter-b", 2)
    revoke_trust_signal(
        db,
        "signal-2",
        revocation_content_id="revoke-2",
        now_iso=stamp(NOW + timedelta(minutes=1)),
    )

    state = get_effective_trust_state(db, subject, TrustDimension.IDENTITY_INTEGRITY)
    assert state.state == TrustState.QUARANTINED
    assert state.reason_code == "recovery_hold"
    row = db.connection.execute(
        "SELECT revoked_by_content_id FROM link_trust_signals WHERE content_id = 'signal-2'"
    ).fetchone()
    assert row[0] == "revoke-2"


def test_reproduced_signal_becomes_local_evidence_independent_of_revocation(db):
    subject = register_old_node(db)
    configure_reporter(db, "reporter", "domain")
    add_signal(db, subject, "reporter", 1)
    assert record_reproduced_signal_observation(
        db, "signal-1", observation_id="reproduced-1", now_iso=stamp(NOW)
    )
    revoke_trust_signal(
        db, "signal-1", revocation_content_id="revoke-1",
        now_iso=stamp(NOW + timedelta(minutes=1)),
    )

    state = get_effective_trust_state(db, subject, TrustDimension.IDENTITY_INTEGRITY)
    assert state.state == TrustState.QUARANTINED
    assert state.reason_code == "local_self_verifying_evidence"
    assert state.explanation["active_local_evidence"][0]["observation_id"] == "reproduced-1"


def test_startup_recompute_reconstructs_projection_from_persisted_inputs(db):
    subject = register_old_node(db)
    record_local_observation(
        db,
        observation_id="local-proof",
        subject=subject,
        dimension=TrustDimension.IDENTITY_INTEGRITY,
        category="signed_equivocation",
        evidence_class=EvidenceClass.SELF_VERIFYING,
        observed_at=stamp(NOW),
        now_iso=stamp(NOW),
    )
    db.connection.execute(
        "DELETE FROM link_trust_effective_states WHERE subject_id = ?", (subject.subject_id,)
    )
    db.connection.commit()

    recompute_all_trust_states(db, now_iso=stamp(NOW + timedelta(minutes=1)))

    state = get_effective_trust_state(db, subject, TrustDimension.IDENTITY_INTEGRITY)
    assert state.state == TrustState.QUARANTINED
    assert state.explanation["active_local_evidence"][0]["observation_id"] == "local-proof"


def test_inactive_inputs_are_pruned_after_365_days_unless_held(db):
    subject = register_old_node(db)
    configure_reporter(db, "reporter", "domain")
    old_issue = NOW - timedelta(days=500)
    for number in (1, 2):
        record_trust_signal(
            db,
            content_id=f"old-{number}",
            issuer_fingerprint="reporter",
            subject=subject,
            dimension=TrustDimension.IDENTITY_INTEGRITY,
            category="signed_equivocation",
            evidence_class=EvidenceClass.SELF_VERIFYING,
            observed_at=stamp(old_issue - timedelta(hours=1)),
            issued_at=stamp(old_issue),
            expires_at=stamp(old_issue + timedelta(days=1)),
            now_iso=stamp(NOW),
        )
    db.connection.execute(
        "UPDATE link_trust_signals SET retention_hold = 1 WHERE content_id = 'old-2'"
    )
    db.connection.commit()

    pruned = maintain_trust_state(db, now_iso=stamp(NOW))

    assert pruned == {"signals": 1, "observations": 0, "vouches": 0}
    assert db.connection.execute(
        "SELECT content_id FROM link_trust_signals"
    ).fetchone()[0] == "old-2"


def test_append_only_trust_migration_preserves_existing_link_data(tmp_path, monkeypatch):
    from netbbs.storage import database as database_module
    from netbbs.storage.migrations import MIGRATIONS

    db_path = tmp_path / "pre-phase-4.db"
    monkeypatch.setattr(database_module, "MIGRATIONS", MIGRATIONS[:-1])
    old_db = Database(db_path)
    old_db.connection.execute(
        """INSERT INTO link_peers
           (fingerprint, root_public_key, transitions_json, descriptor_json, updated_at)
           VALUES ('peer-a', 'cm9vdA==', '[]', '{}', ?)""",
        (stamp(NOW),),
    )
    old_db.connection.execute(
        """INSERT INTO link_events
           (content_id, sender_fingerprint, object_type, envelope_json, received_at)
           VALUES ('event-a', 'peer-a', 'key_transition', '{}', ?)""",
        (stamp(NOW),),
    )
    old_db.connection.execute(
        """INSERT INTO link_reliability
           (fingerprint, attempts, successes, updated_at)
           VALUES ('peer-a', 3, 2, ?)""",
        (stamp(NOW),),
    )
    old_db.connection.commit()
    old_db.close()
    monkeypatch.undo()

    upgraded = Database(db_path)
    try:
        assert upgraded.connection.execute("SELECT COUNT(*) FROM link_peers").fetchone()[0] == 1
        assert upgraded.connection.execute("SELECT COUNT(*) FROM link_events").fetchone()[0] == 1
        score = upgraded.connection.execute(
            "SELECT attempts, successes FROM link_reliability WHERE fingerprint = 'peer-a'"
        ).fetchone()
        assert tuple(score) == (3, 2)
        assert upgraded.connection.execute(
            "SELECT COUNT(*) FROM link_trust_effective_states"
        ).fetchone()[0] == 0
    finally:
        upgraded.close()

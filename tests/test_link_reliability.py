"""Tests for `netbbs.link.reliability` (design doc §12, issue
#58) -- the from-scratch, direct-observation-only dial reliability
tracker, built because §6's own reputation system (which the design
doc assumed relay scoring would reuse) doesn't exist anywhere in this
codebase."""

from __future__ import annotations

import pytest

from netbbs.link.reliability import rank_by_reliability, record_dial_outcome, reliability_score
from netbbs.storage.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "node.db")
    yield database
    database.close()


def test_score_is_neutral_for_a_never_dialed_fingerprint(db):
    assert reliability_score(db, "never-dialed") == 0.5


def test_score_reflects_the_observed_success_rate(db):
    record_dial_outcome(db, "alice", succeeded=True)
    record_dial_outcome(db, "alice", succeeded=True)
    record_dial_outcome(db, "alice", succeeded=False)
    assert reliability_score(db, "alice") == pytest.approx(2 / 3)


def test_score_is_zero_after_every_attempt_fails(db):
    record_dial_outcome(db, "bob", succeeded=False)
    record_dial_outcome(db, "bob", succeeded=False)
    assert reliability_score(db, "bob") == 0.0


def test_score_is_one_after_every_attempt_succeeds(db):
    record_dial_outcome(db, "carol", succeeded=True)
    assert reliability_score(db, "carol") == 1.0


def test_record_dial_outcome_is_a_running_tally_not_a_snapshot(db):
    record_dial_outcome(db, "alice", succeeded=True)
    assert reliability_score(db, "alice") == 1.0
    record_dial_outcome(db, "alice", succeeded=False)
    assert reliability_score(db, "alice") == pytest.approx(0.5)
    record_dial_outcome(db, "alice", succeeded=False)
    assert reliability_score(db, "alice") == pytest.approx(1 / 3)


def test_rank_by_reliability_sorts_most_reliable_first(db):
    record_dial_outcome(db, "alice", succeeded=True)
    record_dial_outcome(db, "alice", succeeded=True)
    record_dial_outcome(db, "bob", succeeded=False)
    record_dial_outcome(db, "bob", succeeded=False)

    ranked = rank_by_reliability(db, ["bob", "alice", "carol"])

    assert ranked[0] == "alice"  # best observed track record
    assert ranked[-1] == "bob"  # worst observed track record
    assert "carol" in ranked  # never-dialed, neutral score, still included


def test_rank_by_reliability_is_stable_for_tied_scores(db):
    # Every one of these is unobserved (tied at the neutral score) --
    # input order must be preserved, not reshuffled, so a caller that
    # already applied its own fairness tiebreak (e.g. a random shuffle)
    # has that order respected.
    fingerprints = ["carol", "alice", "bob"]
    assert rank_by_reliability(db, fingerprints) == fingerprints

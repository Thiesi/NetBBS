"""
Local, direct-observation peer reliability tracking (design doc §12
round 95, issue #58) -- the "reliability scoring reuses §6's existing
local-reputation mechanism" the design doc describes turned out, on
inspection, to have nothing existing to reuse: §6's own reputation/
trust system is design-only, no data model or table exists anywhere in
this codebase for it (confirmed by tracing, not assumed). This module
is a genuinely new, minimal, from-scratch tracker -- deliberately not a
web-of-trust/scoring system, just "how often have my own dial attempts
against this fingerprint succeeded," matching the design doc's own
scoped-down framing: "is this candidate a reachable full peer, and how
reliable have I personally found it" is judged sufficient; true
hop-count/topology awareness was explicitly rejected as disproportionate
complexity for this project's declared scale (§14).

Direct observation only, exactly like `node.candidate_descriptors`
itself (§12 round 95's own "second-hand reliability claims... treated
as a weak prior worth trying, not trusting outright" framing) -- this
module has no notion of a peer *telling* this node how reliable a third
party is; every row here reflects only this node's own dial attempts.

Plain, synchronous, `db`-first functions, dispatched via `DatabaseLane.
run` -- the same calling convention `netbbs.link.store`'s persistence
functions already use.
"""

from __future__ import annotations

from netbbs.storage.database import Database
from netbbs.timeutil import utc_now_iso

# A fingerprint never yet dialed gets this score -- a neutral prior, not
# the bottom of the ranking, so a brand-new candidate isn't permanently
# out-ranked by one with a merely-mediocre track record (design doc
# §12's own "a weak prior worth trying" framing, applied here to "worth
# ranking alongside," not just "worth accepting a claim about").
_UNOBSERVED_SCORE = 0.5


def record_dial_outcome(db: Database, fingerprint: str, *, succeeded: bool) -> None:
    """
    Record one direct dial attempt against `fingerprint` -- called once
    per *candidate* (not once per address; see `netbbs.link.sync.
    _try_candidate_fallback`, which already collapses a multi-address
    attempt into one success/failure outcome via `_try_addresses_via`
    before this is ever called) after every fallback/relay-selection
    dial this node makes.

    Unconditional upsert, matching `netbbs.link.store.save_peer`'s own
    "harmless no-op" tolerance for this project's declared scale (§14)
    rather than owning extra bookkeeping to detect whether anything
    actually changed.
    """
    db.connection.execute(
        """
        INSERT INTO link_reliability (fingerprint, attempts, successes, updated_at)
        VALUES (?, 1, ?, ?)
        ON CONFLICT(fingerprint) DO UPDATE SET
            attempts = attempts + 1,
            successes = successes + excluded.successes,
            updated_at = excluded.updated_at
        """,
        (fingerprint, 1 if succeeded else 0, utc_now_iso()),
    )
    db.connection.commit()


def reliability_score(db: Database, fingerprint: str) -> float:
    """
    `fingerprint`'s observed success rate (`successes / attempts`), or
    `_UNOBSERVED_SCORE` if this node has never dialed it. Always in
    `[0.0, 1.0]`.
    """
    row = db.connection.execute(
        "SELECT attempts, successes FROM link_reliability WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    if row is None or row["attempts"] == 0:
        return _UNOBSERVED_SCORE
    return row["successes"] / row["attempts"]


def rank_by_reliability(db: Database, fingerprints: list[str]) -> list[str]:
    """
    `fingerprints`, most-reliable first (design doc §12: relay
    candidates are "ranked by observed reliability"). Stable sort --
    fingerprints tied on score (including every never-dialed one,
    sharing `_UNOBSERVED_SCORE`) keep their relative input order rather
    than being reshuffled, so a caller that already applied its own
    tiebreak (e.g. a random shuffle for fairness among untested
    candidates) has that order respected.
    """
    scores = {fingerprint: reliability_score(db, fingerprint) for fingerprint in fingerprints}
    return sorted(fingerprints, key=lambda fingerprint: scores[fingerprint], reverse=True)

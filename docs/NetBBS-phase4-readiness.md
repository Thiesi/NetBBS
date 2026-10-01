# NetBBS Phase 4 public-readiness gate

This checklist is the operational evidence record for design document §12.10
and issue #131. It is intentionally stricter than a passing test suite. A row
marked **pending** prevents a public/untrusted federation claim.

## Automated adversarial validation

| Required scenario | Current evidence | Status |
|---|---|---|
| Sybils in one domain | Unit policy tests plus `test_sybil_reporters_share_one_domain_vote_over_real_transport_and_restart`, which pulls three independently signed reports from isolated SQLite nodes over loopback HTTP and proves two same-domain identities count once | covered |
| Colluding domains below and above threshold | `test_colluding_domains_below_weight_threshold_do_not_quarantine` and `test_remote_quarantine_requires_two_full_weight_domains` | covered |
| Compromised reporter and sole-authority recovery | `test_compromised_reporter_removal_is_audited_and_releases_after_recovery_hold` and `test_category_scoped_sole_authority_is_visible_audited_and_reversible` | covered |
| Expiry, revocation, replay, stale/future input | `test_revocation_removes_remote_support_without_deleting_history`, `test_signal_replay_is_deduplicated_and_lifetime_is_clamped`, `test_future_signal_and_invalid_category_evidence_pair_are_rejected`, and real-transport pull freshness/nonce coverage | covered |
| Oversized signal/evidence and storage/request amplification | `test_oversized_embedded_and_digest_evidence_are_rejected_before_signing`, per-subject signal quota, bounded pull pagination/response, request-rate, and real oversized-body tests | covered |
| Reproducible and false evidence | `test_digest_evidence_stays_inactive_until_verified_and_reproduced` | covered |
| Invalid-signature attribution | wrong-key signed-object rejection plus the rule that invalid signatures are not attributed as signer-authored evidence | covered |
| Subjective-report isolation | trust-policy and enforcement tests prove content-conduct state cannot quarantine node transport | covered |
| Restart reconstruction and preservation | trust projection restart tests, real-transport enforcement, and the multi-reporter Sybil scenario prove accepted signed objects and the effective quarantine projection remain stored | covered |
| User/node scoping | subject independence and read-time user suppression tests | covered |
| Containment and recovery | quarantine containment pull, recovery hold, manual block precedence, and restart reconstruction tests | covered |
| Real SQLite, loopback transport, and resource bounds | `test_link_transport.py` uses independent SQLite files, database lanes, and loopback `aiohttp` servers; trust quotas and request/body limits are exercised | covered |
| Deterministic partitions, reorder, duplicates, and healing | `test_link_convergence.py` and `tests/link_harness.py` exercise isolated node databases with scripted delivery and recovery | covered |

Run the focused automated gate from the repository root:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_link_trust.py tests/test_link_trust_wire.py tests/test_link_enforcement.py tests/test_link_transport.py tests/test_link_convergence.py tests/test_remote_attestation.py tests/test_admin_flow.py
```

Then run the complete suite:

```powershell
.venv\Scripts\python.exe -m pytest
```

## Human and deployment validation

| Gate | Required evidence | Status |
|---|---|---|
| SysOp explanation and configuration | A SysOp can inspect domains, reporters, anchors, authorities, subjects, effective decisions, evidence, overrides, recovery requirements, and audit history | implemented; automated UI coverage. Gaps found in the 2026-09 exercise: a `*` scope category was accepted but authorized nothing (#745, fixed: it now expands to every known category of its dimension); below the threshold the explanation shows no counted domains, weight or release condition (#752) |
| Manual quarantine/block/recovery exercise | Follow “Phase 4 trust and recovery exercise” in `docs/NetBBS-link-dogfood-plan.md`; record the visible reason and effects, restart while restricted, clear the trigger or override, observe the recovery hold, and record release | **done 2026-09-26 to 09-29, with one defect.** Two signals from two domains quarantined `identity_integrity` only, and a new post by the subject was refused. The state survived a restart during a partition. A mandatory-reason override was scoped, audited, restart-safe and cleared. The signal's revocation started the 24 h hold, and the subject returned to probationary with `automatic_recovery`. An ordinary caller sees nothing. **But release happened only at a restart (#802).** Record: issue #131 |
| Independently administered multi-node exercise | Using that same runbook, at least two administrators configure separate nodes; introduce a trust trigger across a partition; inspect quarantine on the receiving node; heal, revoke/remove the trigger, restart, and verify convergence without deleting accepted objects | **done 2026-09**, on three live nodes on three networks, two of them outgoing-only and one behind a corporate proxy. Two separately run sessions administered them, which the operator counts as independent (the gate is about network and configuration diversity). Every step of the row held, and no accepted object was deleted. Record: issue #131 |
| Operational-key rotation and compromise response | The rotation rows of that runbook: a routine signing-key rotation leaves the node's earlier content usable by a new subscriber, a transport rotation's sessions reconnect, and an offline compromise response re-signs the node's own content while stale carrier copies are skipped per object (issue #624) | **exercised 2026-09 on v7.13.0; one requirement fails.** Routine signing rotation: content signed before it was accepted on first fetch after it. Transport rotation: the live session reconnected in 17 s (live chat is one-way, #860). Compromise response: 11 objects re-signed, and the new content was accepted. **But a node that knows the rotating node only by introduction accepted a carrier's stale copy signed by the compromised key: #914.** Fixed (carriers serve the signer's key history); to be re-run on real nodes. Since #672 a carrier that learns the compromise also stops serving its stale copies and takes the re-signed ones in their place, so they spread past the origin's own peers; also to be seen on real nodes |
| Sustained private dogfood | Complete and record issue #83's duration, restart, partition, quota, and operator-observation checklist | pending |

## Decision

NetBBS remains private/experimental federation. The automated §12.10 gate is
necessary evidence. The 2026-09 exercise closed the real-node quarantine and
independently administered rows. Phase 4 and issue #131 are still not complete,
and no public-network readiness claim is justified while issue #83's sustained
run has not been recorded.

The compromise response did not reach nodes that knew the compromised node only
by introduction (#914). A carrier now serves each signer's key history beside
its content, and the receiver merges it; a later exercise should see row 9's
stale copy skipped on real nodes.

Automatic recovery waited for a restart (#802). A running node now re-evaluates
trust on every Link sync pass; a later exercise should see a hold release on a
real node without one.

One probationary caller's chat line used to stop their whole node's event
push to the peer that refused it (#897). Events are now judged one by one, and
a refused one is set aside by its sender; a later exercise should see the rest
arrive on real nodes.

The exercise's other findings affect operation rather than the trust model:
#700, #745, #752 and #860.

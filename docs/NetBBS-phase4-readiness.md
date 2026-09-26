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
| SysOp explanation and configuration | A SysOp can inspect domains, reporters, anchors, authorities, subjects, effective decisions, evidence, overrides, recovery requirements, and audit history | implemented; automated UI coverage |
| Manual quarantine/block/recovery exercise | Follow “Phase 4 trust and recovery exercise” in `docs/NetBBS-link-dogfood-plan.md`; record the visible reason and effects, restart while restricted, clear the trigger or override, observe the recovery hold, and record release | pending real exercise |
| Independently administered multi-node exercise | Using that same runbook, at least two administrators configure separate nodes; introduce a trust trigger across a partition; inspect quarantine on the receiving node; heal, revoke/remove the trigger, restart, and verify convergence without deleting accepted objects | pending |
| Operational-key rotation and compromise response | The rotation rows of that runbook: a routine signing-key rotation leaves the node's earlier content usable by a new subscriber, a transport rotation's sessions reconnect, and an offline compromise response re-signs the node's own content while stale carrier copies are skipped per object (issue #624) | pending real exercise |
| Sustained private dogfood | Complete and record issue #83's duration, restart, partition, quota, and operator-observation checklist | pending |

## Decision

NetBBS remains private/experimental federation. The automated §12.10 gate is
necessary evidence, but the pending real-node and independently administered
dogfood rows mean Phase 4 and issue #131 are not complete and no public-network
readiness claim is justified.

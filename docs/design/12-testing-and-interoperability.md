# NetBBS design — 14. Testing and interoperability requirements

This is the normative design reference for this area of NetBBS. Section numbers are preserved from the design index so existing `§n.n` references remain stable.

---

## 14. Testing and interoperability requirements

### 14.1 Deterministic distributed testing

Every implemented Link event family must be exercised through independent node
instances and serialization under applicable scenarios:

- duplicate delivery;
- reordering;
- dropped messages;
- partition and healing;
- restart and state reconstruction;
- malformed or forged events;
- key rotation/revocation;
- convergence after valid resends.

The harness grows with real event families. A generic harness which cannot drive
the real protocol is not sufficient.

**Cross-subsystem end-to-end scenarios (issue #80).** The deterministic
harness above proves protocol/verification logic; it does not, by
itself, prove that a caller-visible guarantee survives the seam between
subsystems (protocol verification, persistence, transport, local-domain
materialization, outbound work tracking, user-visible state). Issue #69
was exactly that: individually correct subsystems, but a self-composed
Link message was never registered where its acknowledgement needed to
find it. `tests/test_link_end_to_end.py` is the named home for this
class of test: a complete real-transport (real `LinkServer`, real
SQLite, real node identities), real-domain-read-path (an ordinary
inbox/board read, not a raw row or `known_event_ids` check) vertical
slice per currently implemented Link product surface — linked boards,
Link mail, (issue #87) linked channels, and (issue #89) remote file
catalogue/chunk transfer — each covering restart-between-stages and
duplicate-delivery. A future Link vertical slice is not complete until it
adds or extends a scenario in that file, the same way it is not complete
without unit tests for its own protocol logic. Tier-2 message routing is
deliberately not in that list — see §10.6 for why it remains deferred
rather than an active future slice.

### 14.2 Real boundaries

Use real:

- SQLite files and independent connections for concurrency and migration tests;
- sockets for transport adapters;
- serialization between separate protocol objects;
- reconstructed objects after restart;
- bounded readiness polling instead of arbitrary sleeps.

Mocks may isolate failures but do not prove the boundary being claimed.

### 14.3 Prove regression tests

When practical, demonstrate that a new regression test fails without the fix.
A test which passes both before and after the supposed fix has not proved the
bug.

Scripted terminal tests must fail fast on input exhaustion and confirm they
reached the intended path after menu or signature changes.

### 14.4 External validation

Automated tests cannot prove visual behavior or third-party interoperability.
Before calling affected functionality production ready, test as applicable with:

- a real OpenSSH client;
- real Telnet terminals;
- SyncTERM/lrzsz or another external Zmodem implementation;
- a real browser/xterm.js session;
- resize, color, CP437 art, editor, bell, and echo behavior;
- long-running operation across midnight and DST changes;
- update, restart, backup, and restore on NetBSD.

### 14.5 Canonical format compatibility vectors

Any change to the canonicalization rule (§7.2) must update
`tests/fixtures/link_canonical_vectors.json` and keep
`tests/test_link_canonical_vectors.py` passing. A vector's canonical bytes or
content ID may only change alongside a deliberate, documented
canonicalization change — never as the side effect of an unrelated
refactor.

---

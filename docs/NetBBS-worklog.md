# NetBBS engineering record

This file is a **curated engineering record**, not a chronological development
diary. It keeps the implementation facts, invariants, failure modes, and
operational lessons that can still affect future work.

The former round-by-round log—including test-count snapshots, debugging
transcripts, superseded intermediate states, and commit narration—remains
available in Git history. Do not reconstruct it here.

Use these sources in this order:

1. [`NetBBS-design-doc.md`](NetBBS-design-doc.md) for current product and
   architecture decisions.
2. Current GitHub issues for outstanding work and acceptance criteria.
3. This file for implementation constraints and lessons that are easy to miss
   by reading only the design document.
4. Source, tests, migrations, and Git history for exact implementation detail
   or archaeology.

## Maintenance rule

Add an entry here only when it is likely to remain useful after the current
commit and issue are forgotten. Appropriate material includes:

- a non-obvious invariant that future code must preserve;
- a platform, protocol, or SQLite behavior which previously caused a real bug;
- a deliberate implementation boundary not obvious from the module names;
- a migration or compatibility constraint;
- a known limitation that remains true;
- an operational or testing procedure required to validate the system;
- a short summary of a major subsystem's current implementation.

Do **not** add:

- passing-test totals;
- commit-by-commit or round-by-round narration;
- exhaustive lists of changed files or tests;
- transient status such as “next up” or “70% complete”;
- debugging transcripts after their durable lesson has been extracted;
- explanations already stated normatively in the design document;
- closed defects whose fix introduced no lasting constraint.

Keep entries current. Replace superseded statements rather than appending a
new historical correction below the old one.

---

## 1. Current implementation state

NetBBS is a modular Python 3.11+ application targeting NetBSD, using asyncio,
SQLite in WAL mode, PyNaCl/libsodium identity primitives, and Telnet, SSH, and
web/xterm.js user transports.

### Standalone node

Phases 1 and 2 are complete as working software. The local node includes:

- password and key-based authentication, self-registration modes, pending
  approval, account administration, live revocation, and lockout prevention;
- message boards with categories, pagination, moderation, expiry, immutable
  edit revisions, threading foundations, and fullscreen or simple composition;
- file areas with metadata in SQLite, content-addressed filesystem storage,
  Zmodem upload/download on byte-capable transports, moderation, expiry, and
  bounded transfer handling;
- real-time chat with bounded scrollback, membership and invitations,
  moderation, aliases, presence, online private messages, command completion,
  editable input, pinned status/input rows, timestamps, and verified-name
  display;
- local asynchronous personal mail with inbox/sent state, quotas, reply and
  deletion semantics;
- topic-first local Communities above boards, channels, and file areas,
  including inheritance, navigation, administration, and Community-scoped
  blanket moderation;
- user profiles, identity attestations, minimum-age and verified-name gates;
- SysOp administration, shutdown/session control, ANSI welcome art, a
  screen-buffer/TUI foundation, WYSIWYG ANSI editing, and a nano-like prose
  editor;
- a self-update subsystem with release checking, safe archive extraction,
  database snapshot/restore primitives, and persisted update state.

The design document, current code, and tests are authoritative for exact
surface behavior. This section is a subsystem map, not a duplicate
specification.

### NetBBS Link

Phase 3 has begun. The repository currently contains real, tested Link
infrastructure rather than only placeholders:

- canonical JSON bytes with recursive Unicode NFC normalization and float
  rejection;
- node root identity plus separate signing and transport operational keys;
- signed, chained key-transition events and operational-key rotation;
- signed endpoint descriptors and a self-authenticating hello exchange;
- transport-independent `LinkNode` protocol logic;
- a real `aiohttp` client/server adapter;
- optional Link listener integration in normal node startup;
- configured-seed background synchronization;
- persistent peer and accepted-event storage;
- foreground and background database execution lanes;
- deterministic multi-node fault injection covering duplicates, reordering,
  partitions, restart, and convergence;
- linked-board genesis, post, and self-authored edit events;
- receive-side verification, persistence, restart reconstruction, local
  origination, admin linking, propagation, and convergence coverage for those
  board events;
- Link messages (tier1_home_node_key only): compose/encrypt, receive-side
  decrypt/deliver/bounce, acknowledgement round trip, targeted per-recipient
  sync delivery (not the configured-seed flood-fill model boards use), and
  convergence coverage;
- peer-list exchange (unverified candidate discovery, nothing auto-dials a
  candidate yet) and live supplementary seed-list refresh, merged into the
  configured-seed list every sync pass;
- a scheduled background release-check task, closing a gap where the admin
  menu's "daily automatic check" switch previously had nothing behind it.

Important boundaries of the current Link implementation:

- It is still private/experimental federation. Phase 4 trust and quarantine are
  the public-readiness gate.
- The working topology is direct pairwise synchronization. Nodes do not yet
  provide general multi-hop relay or anti-entropy catch-up.
- A hello carries key-lifecycle state, not arbitrary board history. Healing a
  connection does not itself transfer missed board events; they must be sent.
- Only implemented event types are supported. Do not infer generic federation
  support for every local object from the existence of the envelope.
- Linked-board moderator edits, tombstones, closure/transfer events, advanced
  governance, and other author-signing tiers remain future work.
- Tier2_personal_key Link messages are reserved but not offered: the server
  can never hold a tier-2 user's decryption key, and nothing in this codebase
  does client-side decryption yet.
- Automatic relay selection remains undesigned in one real respect: whether
  its reliability metric is a new, narrow Phase-3 tracker or genuinely needs
  Phase 4's reputation system.
- Current GitHub issues, not this file, are the task-status authority.

---

## 2. Sources of truth and code boundaries

### Design, implementation, and task tracking

- The design document is normative: what the system should mean and why.
- Source and migrations are normative for what the current build actually
  does.
- Tests describe protected behavior but may become stale or accidentally
  vacuous; they are evidence, not an independent specification.
- GitHub issues contain active acceptance criteria and dependency tracking.
- This record explains implementation constraints which do not belong in the
  product design.

When these disagree, investigate rather than choosing whichever is convenient.
Update stale documentation or tests as part of the same change.

### Module ownership

Keep domain logic in its subsystem and transport/session orchestration in
`netbbs.net`:

- `netbbs.auth`: accounts and authentication;
- `netbbs.identity`: cryptographic identity primitives;
- `netbbs.link`: Link event, identity, protocol, transport, persistence, sync,
  and local-to-Link bridge logic;
- `netbbs.boards`, `netbbs.files`, `netbbs.chat`, `netbbs.mail`: domain state;
- `netbbs.communities`: Community CRUD and inherited-value resolution;
- `netbbs.moderation`: shared permission and audit primitives;
- `netbbs.rendering`: ANSI, reflow, screen-buffer, and editor-independent
  rendering primitives;
- `netbbs.net`: user-facing flows, sessions, server adapters, and orchestration;
- `netbbs.storage`: migrations, database ownership, and execution lanes.

Do not teach storage modules how terminal output should look, or teach generic
transport/hub code the semantics of every event it carries.

---

## 3. Storage and SQLite invariants

### Migrations

Migrations are append-only. Never edit an already-shipped migration. The
database `user_version` is compared with the build's migration set, and a
database from a newer build must fail startup with a clear error.

A matching `user_version` does not prove the physical schema is intact if
someone manually changed or rewrote old migrations. There is currently no
stored schema fingerprint. Treat manual schema changes as unsupported.

### Table rebuild hazard

SQLite table rebuilds require special caution when the table being dropped is
a parent in live foreign-key relationships. With foreign keys enabled,
`DROP TABLE` can apply cascade or `SET NULL` effects to referencing rows during
the rebuild, before the replacement table exists. This can silently destroy or
rewrite data.

Prefer, in order:

1. `ALTER TABLE ADD COLUMN` where possible;
2. a new index, including partial unique indexes;
3. explicit application-level cleanup for delete behavior;
4. a carefully tested, dependency-ordered multi-table rebuild only when no
   safer option exists.

Never copy an earlier rebuild pattern merely because it worked for a different
table. Verify the actual parent/child graph and seed realistic related rows in
the migration test.

### Nullable uniqueness

SQLite treats `NULL` values as distinct in ordinary compound `UNIQUE`
constraints. Use partial unique indexes when the intended rule is “only one
row where these nullable scope columns are absent/present in this combination.”
Moderator blanket grants depend on this.

### Transactions and fresh state

Any invariant spanning a check and mutation across independent connections
must be enforced in one explicit write transaction. The last-usable-SysOp
guard is the reference pattern:

- `BEGIN IMMEDIATE` before reading;
- re-fetch the target inside the transaction;
- evaluate no-op and safety conditions against fresh state;
- mutate and insert the audit record within the same transaction;
- roll back on every exception, including cancellation-shaped exceptions.

Do not trust a dataclass passed into a mutator for fields owned by another
actor or operation. Re-fetch before deciding when stale state could resurrect,
overwrite, or bypass a previously committed transition.

`record_action_without_commit` exists for caller-owned transactions.
Auto-committing helpers must not be used inside a wider atomic operation.

A released outermost SQLite savepoint is already committed. Do not append an
unconditional `commit()` to a savepoint-based helper: when nested, that would
commit the caller's transaction prematurely.

### Database execution lanes

Interactive network flows use a foreground `DatabaseLane`; Phase 3 background
Link work uses a separate background lane. Each lane owns:

- one `ThreadPoolExecutor(max_workers=1)`;
- one SQLite connection created and used on that worker thread;
- bounded submission via a semaphore;
- explicit close on the owning worker.

Business-logic functions remain synchronous and `db`-first; async flow code
calls them through `await lane.run(function, ...)`.

Do not:

- use a lane-owned connection from the event-loop or test thread;
- call async session I/O from inside a lane job;
- put database access inside synchronous picker/completer callbacks;
- assume cancelling the awaiting coroutine stops an already-running worker
  function.

Prefetch data required by synchronous callbacks, then close over plain values.
Split mixed “DB read + session write” functions at the boundary.

Introducing `lane.run()` adds real suspension points to code which may
previously have been effectively atomic on the event loop. Re-audit
`try/finally` coverage whenever migrating a path: cleanup must begin before
the first new `await`, not where the old synchronous span happened to end.

### Persistent data versus projections

Store structured domain data, never terminal-rendered ANSI. Rebuild derived
indexes and projections on restart:

- peer and event rows reconstruct `LinkNode` state;
- persisted board genesis events must rebuild the `board_id -> genesis` index;
- chained post edits must reconstruct in causal order;
- self-originated board events stored on local rows must be loaded alongside
  peer-received events.

Whenever a new in-memory index is added to a persistent subsystem, add its
restart reconstruction in the same change.

---

## 4. Identity, authentication, and account invariants

### Canonical identity

Canonical usernames drive login, authorization, moderation, blocking,
addressing, audit logs, and persistent ownership. Username comparisons and
uniqueness are case-insensitive. Usernames are immutable after creation.

Display names and chat aliases are presentation metadata. They must never
replace canonical identity in security decisions.

### Usable SysOps

`SYSOP_LEVEL` is 255. A usable SysOp:

- has level at least 255;
- is not disabled;
- is not pending approval.

The node refuses to start with zero usable SysOps. Pending accounts cannot be
promoted directly to SysOp. Demotion, disable, and deletion share the atomic
last-SysOp guard described above.

### Registration

Registration mode is one of:

- open;
- approval required;
- closed.

The reserved `new` username selects self-registration on supported transports
when registration is available. Pending accounts are rejected uniformly by
password, application-level keypair, and SSH public-key authorization paths.

Registration and login share throttling before expensive password hashing.

### Transport authentication

Authentication proof belongs at the layer which possesses it:

- SSH public-key authentication has already proved possession at the SSH
  transport layer; use the authorization path rather than inventing a second
  application challenge.
- Application-level signature login is a separate challenge/verification path.
- Local admin CLI access treats local filesystem/shell access as the trust
  boundary. Selecting `--as` is action attribution, not another login.

### Node key lifecycle

A node has a stable root identity and distinct operational signing and
transport keys. Root-signed `key_transition` events authorize and revoke
operational keys. Rotation produces a chained revoke-plus-authorize sequence;
both events must propagate.

Resolve chains by signed predecessor links, never list position. Reject forks,
disconnected chains, wrong subjects, and key files which disagree with the
verified transition history. Fail startup rather than operate under ambiguous
or mismatched key state.

Keep root-key use narrow. Ordinary Link content uses the current operational
signing key.

---

## 5. Permissions, moderation, and Communities

### Permissions

Numeric user levels and moderator grants solve different problems:

- levels are broad eligibility gates;
- grants convey scoped capabilities.

SysOps pass `has_permission` without stored grant rows. Functions which list
literal grants must remain literal and must not synthesize SysOp grants.

Board/file permissions and channel permissions are separate enums. Validate
the object type and permission combination before applying any SysOp bypass.

Moderation actions are audited. Audit history survives account deletion;
denormalized author/uploader labels and fingerprints preserve content
attribution after account removal.

### Membership is not moderation

Channel membership/invitations are authorization state, not moderator grants.

- `hidden` controls listing visibility.
- `members_only` controls join policy.
- The two axes are independent.
- An invitation is durable pending account state.
- Live notification is convenience only and cannot be the sole delivery
  mechanism.
- Invitation acceptance must be atomic and shared by every entry route,
  including picker-driven entry and `/join`.

### Communities

A local resource has zero or one Community. `NULL` means Uncategorized.
Categories remain independent metadata inside a resource type.

Community deletion never deletes contained resources. It reassigns them to
Uncategorized and removes Community-scoped blanket grants.

Community inheritance is explicit and nullable:

1. an explicit resource value wins, including explicit `0`;
2. otherwise use the Community default when present;
3. otherwise use the system default.

Always call the effective-value resolvers at enforcement and display points.
Passing a nullable raw threshold into a comparison can crash or produce an
enforcement/display mismatch.

Current inherited properties are the documented read/write, age, and
name-requirement values. Channel `min_level` is not Community-inherited.

Community-scoped browsing must filter resources first and derive visible
categories from that filtered set, preventing categories used only by another
Community from leaking into navigation. Preserve the same scope through
re-picks and channel switching.

Community-blanket grants fit between per-object and local-blanket fallback:

1. per-object;
2. Community-blanket;
3. local-blanket.

Node carry policy, user follow state, and resource membership are distinct
concepts when Link Communities arrive.

---

## 6. Boards, files, mail, and chat

### Stable identity and pagination

User-visible numeric `goto` identifiers are stable database IDs, never
positions in a currently sorted list. Mixed lists from different tables need
disjoint picker identities; current category pickers use negative category IDs.

Use deterministic keyset pagination with a stable tie-breaker, not `OFFSET`,
for unbounded feeds. Editing or pinning must not change a logical post's feed
identity or cursor position.

Direct operations must query their full logical scope. For example, downloading
a named file cannot be limited to the current visible page.

### Board revisions and lifecycle

Post edits append immutable revision rows. The logical post retains the root
post's identity and feed position while projecting the newest approved content.

On moderated boards, an edit re-enters moderation and the last approved version
remains visible until approval. Self-authored linked edits form a linear event
chain. Moderator edits and tombstones are deliberately separate future
governance work.

Expired content is delisted but remains directly reachable. Hard-deletion
sweeps must not remove rows still referenced by replies or edit chains; such
rows may remain expired indefinitely.

Ranking queries must account for effective expiry even when lazy sweeping has
not yet materialized the status change.

### Files and transfers

File contents are content-addressed filesystem blobs; SQLite stores metadata.
Deleting metadata does not automatically mean a blob is unreferenced. Blob
garbage collection is a separate operation.

Incoming transfers:

- stream directly to a same-filesystem `.incoming` path;
- hash incrementally;
- atomically rename into content-addressed storage;
- enforce transfer size and idle-time limits;
- remove the temp path on all ordinary failures and cancellation;
- purge stale regular staging files before listeners start;
- never follow symlinks or recursively delete unexpected entries.

The implemented Zmodem subset is intentionally limited. Keep the limitations
explicit rather than implying full protocol coverage.

### Local mail

Local asynchronous mail is distinct from real-time `/msg` and future Link
messages.

- `/msg` is online-only and ephemeral.
- Local mail has durable inbox/sent/read/deletion state.
- Sender and recipient deletion markers are independent; hard-delete only
  after both sides delete.
- Mutators re-fetch current deletion state rather than trusting stale message
  objects.
- Recipient quotas evict the oldest read mail; if all retained mail is unread,
  sending fails clearly rather than silently dropping unread content.
- Read receipts are not part of the current model.

### Chat state and rendering

`ChatHub` routes opaque objects and owns bounded per-participant queues. It
does not render messages or decide moderation semantics.

Ordinary overflow discards old traffic and inserts an honest overflow notice.
Mandatory state-transition events such as kick, ban, or access revocation use
priority delivery and displace ordinary traffic rather than being replaced by
the overflow notice.

Presence is node/account-session state and is separate from channel
participation. Session identifiers are structured `ParticipantId` values, not
encoded strings requiring parsing.

Chat event storage is structured `ChannelMessage` data. The shared channel
message renderer is used for:

- the sender's local copy;
- live recipients;
- scrollback replay.

This is required for live/replay parity and recipient-specific preferences.

Chat aliases are current presentation metadata. Scrollback resolves the
current alias when the account still exists, while falling back to the stored
canonical label if it does not. Moderation and addressing remain
canonical-only.

For channels requiring `verified_and_displayed`, the verified real-name unit
is generated only from the trusted attestation record. The server-produced
color and reserved marker are the anti-forgery boundary. Live send and `/me`
revalidate current participation requirements so a session cannot keep
posting after policy or attestation changes.

---

## 7. Rendering, input, and transport rules

### Sanitization and ANSI composition

Store raw user text and sanitize at the final output boundary.

Sanitize each untrusted segment **before** adding trusted ANSI. Never run the
terminal sanitizer over a completed ANSI string; it may strip legitimate
server-generated control codes.

SGR reset returns to terminal default, not to the previous nested color. Never
wrap a string containing colored subsegments in one outer `colored()` call and
expect the outer style to resume. Compose prefix, trusted middle segment, and
suffix as independently styled segments, reapplying the surrounding style
after an embedded reset.

ANSI art is trusted SysOp content and intentionally bypasses ordinary
untrusted-text sanitization. Keep that trust distinction explicit.

### Text and byte boundaries

Core text utilities use `\n`. CRLF normalization belongs in the transport.

Telnet byte output must IAC-escape literal `0xff`, including negotiation option
payloads. Telnet sockets use `TCP_NODELAY` for interactive single-byte
echo/bells.

Byte-oriented Telnet/SSH input shares `char_input`; web input is
character-oriented and maintains its own decoder. Share transport-agnostic
editing primitives, but do not force the web path through byte assumptions.

UTF-8 input must read complete code points. Escape and optional-terminator
lookahead must have bounded time and length. A standalone Escape key must not
be confused with an unknown multi-byte escape sequence.

Masked input remains a simple non-history, non-cursor-editing path so redraws
cannot expose password characters.

### Pinned chat UI

The pinned status/input rows and line editor share one write lock. The live
buffer must be updated while that lock is held.

The Enter transition—capturing submitted text, clearing the buffer, updating
the live state, and writing the final newline—must remain atomic under the
same lock.

Terminal dimensions can change at any moment. Pinned UI state is dynamic:

- shrinking below the minimum resets the scroll region before helpers compute
  invalid coordinates;
- growing back re-establishes and repaints both rows;
- every transition is serialized under the shared lock;
- rendering helpers retain defensive minimum-height checks.

Cleanup resets the scroll region and clears the screen best-effort without
masking the original exception.

### Editors

The ANSI editor and prose editor share a screen-buffer/diff shell but have
different data models:

- ANSI editing is fixed-grid, overwrite-oriented, CP437-capable;
- prose editing uses logical lines, insertion, soft wrap, scrolling, and
  visual/logical cursor conversion.

Do not conflate them into one editor core.

ANSI parsing uses deferred wrap at the last column: filling the final cell
marks wrap pending; a subsequent printable character performs the wrap;
explicit movement or CR/LF clears the pending state.

Clip status/chrome lines to the canvas or terminal width before emitting them.
A terminal auto-wrap outside the cleared row can accumulate visual corruption.

Editor autosave tasks are owned by the editor and must be cancelled/gathered
before any cleanup write which may itself fail on a disconnected session.

---

## 8. Async ownership, shutdown, and background tasks

The component which creates a task owns it on every exit path:

- cancel it;
- await or gather it;
- retrieve/log failures;
- ensure its exception cannot skip higher-priority cleanup.

Cancelling `asyncio.wait()` does not cancel the tasks being waited on.

Iterate snapshots of mutable participant/session collections across any
operation which may yield. Never hold an iterator over a live dict while
another coroutine can join or leave.

Avoid mutual-wait and self-cancellation designs. An account-revocation watcher
cancels its target without awaiting the target's full unwind; the target's own
cleanup then cancels and gathers the watcher. A SysOp-triggered shutdown runs
as an independent task so the issuing session is not awaiting its own
cancellation.

Ancillary background tasks use an explicit policy. A cosmetic task may
gracefully degrade after logging its exception, but its failure must never
prevent listener shutdown or database closure.

Graceful shutdown:

1. stop admitting work / enter maintenance;
2. notify users as configured;
3. wait the bounded grace period when requested;
4. cancel and await sessions/background tasks;
5. stop listeners;
6. close lanes, database connections, and HTTP sessions.

Cleanup writes to an already-dead client are best-effort and may not replace
the exception which caused cleanup.

---

## 9. Link protocol invariants

### Canonical events

All signed and hashed Link objects use the same canonical JSON-byte function.

Current rules include:

- recursive Unicode NFC normalization;
- deterministic compact JSON representation;
- no floats, including nested floats;
- explicit object/protocol typing;
- optional fields omitted where the event schema says omission, not replaced
  casually with `null`;
- nonces where two otherwise-identical actions must remain distinct.

Builders, verifiers, content IDs, and golden fixtures must never maintain
independent canonicalization implementations.

### Hello and peer state

A hello self-authenticates a root identity, its signing-key transition history,
and the current signed endpoint descriptor. Repeated or stale descriptors are
idempotent/no-op according to the protocol's freshness rule.

Seeds introduce addresses; they do not confer trust.

A full peer must advertise a usable address. Outgoing-only nodes may have no
inbound address. Link-only startup does not count as an interactive BBS
listener: at least one user-facing transport must start.

### Event acceptance

Resolve the sender's current signing key from its verified transition chain
before accepting operationally signed events.

Event handling must be idempotent even if a retention policy later purges the
fast dedup table. For key transitions, the verified chain itself is durable
evidence of membership; a resend of an identical transition is a no-op, while
a different transition extending the same predecessor remains a fork.

Do not rely on tuple/list position when multiple key purposes are interleaved.
Resolve by purpose and chain.

Batch handling must not let an expected duplicate masquerade as a fork and
abort all genuinely new events which follow it.

### Linked boards

A linked board uses the existing local board ID in its signed genesis; linking
does not mint a parallel local identity.

Local origination is explicit. Linking an existing board creates and persists
one genesis. Approved local posts on that board create signed `board_post`
events. Self-authored approved revisions create chained `board_post_edit`
events.

No pre-Link history backfill is implied. Parents or revision predecessors are
linked only when the corresponding local event already exists. Broken or
pre-Link chains are not silently fabricated.

Receive-side rules currently include:

- the genesis origin must match the actual sender;
- one board ID cannot acquire a conflicting genesis;
- a post requires a known verified genesis;
- currently supported posts use the node-vouched-user author tier;
- the vouched home node must match the sender;
- edits require a known root, matching author, and exact previous-event head;
- out-of-order edits are rejected and converge after an ordered resend;
- duplicate events are no-ops.

Self-originated Link events are stored on the local board/post rows and loaded
at restart; peer events are stored in `link_events`. Both contribute to the
live `LinkNode` projection and outbound push list.

LinkNode mutation remains on the event-loop side. Database-lane functions may
build and persist events, but must not mutate the shared live LinkNode from a
worker thread.

### Current distribution limit

Configured-seed sync currently sends the complete supported outbound event set
on each pass. This is deliberately simple and relies on idempotent acceptance.

Peer-list exchange exists (a node shares its own verified peers' endpoint
descriptors with anyone it has itself completed a hello with) but only feeds
an unverified candidate pool (`LinkNode.candidate_descriptors`); nothing
auto-dials a candidate yet. There is still no generic inventory/pull protocol,
automatic relay selection, or multi-hop propagation. A node which learned data
from another peer does not automatically relay that board state to a third
node.

Persistent dedup/event retention policy still needs a correctness-preserving
implementation. Purging the fast cache must not make old control events
re-applicable or deleted/suppressed content spontaneously reappear.

---

## 10. Operational constraints

### Backup and restore

Use SQLite's online backup API; never copy a live WAL database as if it were a
single inert file.

Back up in this order:

1. database snapshot;
2. content blobs;
3. node identity material as part of the same recoverable set.

This ordering can leave harmless unreferenced blobs, but must not leave a
restored database referring to blobs absent from the backup.

Restoration resumes the same node identity. Running the old and restored
instances simultaneously is unsupported and can produce two active instances
of one cryptographic identity.

Before an update which can migrate the schema, snapshot the database so binary
and schema can be rolled back together.

### Bounds and visibility

Every remotely influenced queue, mailbox, transfer, retry set, and retained
event collection needs:

- an explicit bound;
- clear reject/drop/backpressure behavior;
- retry and terminal-failure rules;
- SysOp-visible state;
- safe defaults.

Do not silently discard security-relevant state or unread user data.

### Startup and crash recovery

Startup should fail clearly for:

- zero usable SysOps;
- unsupported newer database version;
- corrupt or inconsistent key-transition state;
- operational key files which disagree with the verified chain;
- listener/configuration failures;
- database integrity failures once that gate is implemented.

Purge only known-safe staging artifacts before accepting traffic. Unexpected
directories and symlinks are not ordinary stale upload files.

---

## 11. Testing and validation policy

### Prove the regression test

When practical, verify a new regression test fails against the pre-fix code or
a deliberately disabled fix. A test which passes both ways has not proved the
bug.

This is especially important for:

- concurrency and task leaks;
- authorization paths;
- persistence/restart state;
- protocol ordering and deduplication;
- security rendering boundaries.

### Test the path, not merely the final symptom

Scripted UI tests can keep passing after a signature or menu migration while
silently taking a fallback branch and blocking somewhere else. Confirm that
the test reached the path its name claims.

When adding a prompt or menu level, trace all scripted inputs. Configure fake
sessions to fail fast on input exhaustion instead of returning empty values
forever.

Assertions should be scoped to the relevant rendered fragment. A global
assertion that an escape sequence or word never appears may fail when trusted
UI chrome legitimately uses the same bytes.

### Avoid timing guesses

Do not use a fixed `sleep()` as proof that a listener, participant, or watcher
is ready. Poll an observable readiness condition with a bound.

A fake async primitive must genuinely yield. A coroutine containing `await`
does not necessarily suspend; for example, queue operations can complete
synchronously while capacity remains.

Thread-pool lane round trips add real latency. Tests coordinating chat
participants or recipient rendering must wait for the relevant state/output,
not one arbitrary event-loop turn.

### Use real boundaries where possible

Use:

- real SQLite files and independent connections for transaction/concurrency
  behavior;
- real sockets for network adapters;
- serialization round-trips between independent LinkNode instances;
- restart tests which construct new objects from persisted state;
- multi-node scripted transport for duplicate/reorder/drop/partition behavior;
- platform-specific tests on NetBSD/POSIX for terminal and filesystem behavior.

Mocks are appropriate for isolated failures but do not replace tests of the
boundary being claimed.

### External verification still matters

Automated byte/transcript tests cannot establish visual or third-party
interoperability. Before declaring affected areas production-ready, perform
direct checks as applicable with:

- a real OpenSSH client;
- a real external Zmodem implementation such as SyncTERM/lrzsz;
- the browser/xterm.js client;
- actual Telnet/SSH terminals for scroll regions, colors, CP437 art, editors,
  resize, and bell/echo behavior;
- a long-running node across real local midnight and DST changes;
- update/restart and backup/restore procedures on the target platform.

Record only unresolved findings here. Successful one-off test transcripts
belong in issues, commits, or Git history.

---

## 12. Outstanding architectural areas

This list is intentionally broad. GitHub issues are authoritative for current
status, ownership, and acceptance criteria.

Near-term Phase 3 work includes:

- completing canonical event schemas and compatibility rules as new object
  types are added;
- persistent event/dedup retention without replay or resurrection bugs;
- linked-resource closure, transfer, succession, orphan, and fork behavior;
- automatic relay selection for outgoing-only nodes (needs its own reliability-
  metric scope decision: a narrow Phase-3 tracker, or genuinely gated on
  Phase 4's real reputation system) and pull-based catch-up; peer-list
  exchange and live supplementary seed-list refresh are done, but nothing yet
  auto-dials a discovered candidate;
- Link messages: tier1_home_node_key only (server-side decryption; tier2
  needs a real client-side decryption story first) -- send/receive/read,
  bounce, and acknowledgement are done; link_message_expired and active
  blocklist-backed sender blocking are not;
- user-key and node-author signing tiers beyond current node-vouched users;
- linked-board moderator grants and later moderator edits/tombstones;
- unread/follow/activity state for Communities;
- operator-visible quotas, retry/dead-letter control, peer health, backup,
  restore, and disaster recovery;
- the trust, reputation, and quarantine model required before public
  federation.

Later work includes Link chat, advanced governance and Link Communities,
door-game sandboxing/API versioning, and other roadmap phases defined in the
design document.

When an item is implemented, replace or remove the relevant statement here.
Do not append a victory narrative.

---

## 13. Historical lessons worth retaining

These are recurring failure patterns, not a defect catalogue:

- Cross-cutting plumbing is cheaper before its consumers than as a retrofit.
- A shared abstraction should be designed against a real consumer, not an
  imagined future one.
- “Looks read-only” is not proof: nested helpers may write.
- “Contains an await” is not proof that a coroutine yields.
- “The test passed” is not proof that it exercised the intended branch.
- “The object is immutable” does not mean the projection over immutable events
  has no conflict or ordering rules.
- “WAL permits concurrency” does not make a read-check-write sequence atomic.
- “The bytes are correct on loopback” does not prove an interactive protocol
  behaves correctly over a real client/network path.
- “The schema version matches” does not prove nobody changed the schema behind
  it.
- “Cleanup is in finally” is not enough if new awaits occur before entering
  that try/finally region.
- “One shared rendered string” is incompatible with recipient-specific display
  preferences and resource-scoped trusted identity rendering.
- “Same event resent” and “different event extending the same predecessor” must
  be distinguished before fork detection.
- Explicit failure, bounded resource use, and visible degradation are preferred
  over silent fallback for security, administration, and federation state.

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
- an opt-in, per-channel bridge to the external MRC (Multi Relay Chat)
  network (issue #275), configured live from the SysOp console;
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

Phase 3 is active and already contains working Link product surfaces, not
only protocol scaffolding:

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
- linked boards with genesis, post/edit propagation, carry materialization,
  origin transfer/orphan/fork handling, closure, origin-authorized moderator
  edits, and tombstones;
- linked channels with promotion, materialization, live message propagation,
  and inventory catch-up;
- linked file-area catalogues with promotion/materialization, inventory
  catch-up, and explicit resumable/deduplicated file fetching;
- authenticated, bounded inventory/pull catch-up across all three linked
  resource types, including empty-inventory discovery through a carrier;
- Link messages (tier1_home_node_key only): compose/encrypt, receive-side
  decrypt/deliver/bounce, acknowledgement round trip, targeted per-recipient
  sync delivery (not the configured-seed flood-fill model boards use), and
  convergence coverage;
- peer-list exchange (unverified candidate discovery) and the reliable-nodes
  roster (issue #219: a built-in fallback plus a daily live list from
  netbbs.org, dialed only once the SysOp accepts participation), both merged
  into the sync loop every pass -- the
  configured/cached seed list first, falling back to a small random sample of
  discovered candidates only when every one of those fails (or none are
  configured) for that pass, never as a first resort;
- a scheduled background release-check task, closing a gap where the admin
  menu's "daily automatic check" switch previously had nothing behind it;
- WAN reachability for outgoing-only nodes (issue #58): direct-observation
  dial-reliability scoring, automatic relay candidate selection/consent (a
  synchronous request/response route, not gossiped events) and self-healing,
  a bounded relay store-and-forward mailbox for `link_message` only, and an
  operator opt-out/resource cap on serving as a relay for others;
- operator-facing Link status/outbox/diagnostics, quotas, work-item retry and
  dead-letter handling, backup/staged restore, startup integrity checking, and
  graceful drain.

Important boundaries of the current Link implementation:

- It is still private/experimental federation. Phase 4 trust and quarantine are
  the public-readiness gate.
- Synchronization is still seed/candidate driven rather than a public routing
  fabric, but bounded multi-hop metadata/content-event catch-up works through
  carriers. Relay mailboxes remain the separate single-hop reachability path
  for outgoing-only Link-message recipients.
- Relay delivery only works between nodes that have already met directly at
  some point -- see "WAN reachability and relay selection" below. It is not a
  way to reach or message a total stranger.
- A hello carries key-lifecycle state, not resource history. Inventory after
  hello is what transfers missed board/channel/file-area events.
- Only implemented event types are supported. Do not infer generic federation
  support for every local object from the existence of the envelope.
- Delegated linked-resource moderation, channel/file-area origin succession,
  advanced governance, and other author-signing tiers remain future work.
- Tier2_personal_key Link messages are reserved but not offered: the server
  can never hold a tier-2 user's decryption key, and nothing in this codebase
  does client-side decryption yet.
- Public interoperability remains unclaimed pending the independent Link v1
  implementation and sustained real-world multi-node dogfood work.
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
- `netbbs.mrc`: the MRC (Multi Relay Chat) wire protocol, DB-backed settings,
  and the one-per-node `MrcBridge`; attached from `netbbs.net.chat_flow` at
  the same join/send/leave sites as the live-Link bridge, never imported by
  `netbbs.chat` or `netbbs.link`;
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

SSH's own keyboard-interactive registration (`netbbs.net.ssh.
_NetBBSSSHServer`) is single-shot, unlike Telnet/web's `netbbs.net.
login_flow._register_new_account`: every *fixable* validation failure
(password too short, mismatch) ends the whole attempt with "reconnect to
try again" rather than looping in place. This isn't a missing feature to
fix -- it follows from the auth-layer's own protocol shape (registration
happens *during* an auth exchange that must always end by failing, so
there is no live connection left to retry against) -- but any future
change assuming SSH registration retries the way Telnet/web's does will
be wrong. Two distinct text channels exist pre-auth over SSH, not one:
`send_auth_banner` (called from `begin_auth`, before any auth method is
tried) is the proven mechanism for real multi-line content -- the
welcome banner itself uses it, though as **plain text, not ANSI**
(issue #203: `SSH_MSG_USERAUTH_BANNER` is shown during authentication,
before any pty/terminal channel exists, and real clients commonly route
it through a display path that never runs an ANSI parser over it at
all -- `netbbs.rendering.ansi.strip_ansi` removes every escape sequence
from this specific call site's content before it's sent, unlike every
other banner in the app); a kbdint challenge's own `instruction` field
(the second tuple element `get_kbdint_challenge`/`_finish_registration`
return) is the only channel available *during* the kbdint exchange
itself, once registration is already underway, and is more client-
rendering-dependent for anything beyond a short paragraph.

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

### Signature auto-append: idempotency, not a "first compose only" flag

`netbbs.signature.append_signature` is called on every successful board-
post/mail compose (`netbbs.net.board_flow._compose_new_post`/`netbbs.net.
mail_flow._compose_mail`), not gated by "is this the first attempt" —
that heuristic was tried first and is wrong. A board post's `/exit`-then-
resume draft cycle can hand back a `body` that either never got the
signature (saved before compose ever completed) or already has it
(saved mid-review, after a first successful append) — the caller can't
cheaply tell which apart from string inspection, so `append_signature`
itself is idempotent (a `body` already ending in the exact signature
block is returned unchanged) and is simply called unconditionally
every time. Mail has no equivalent draft-resume entry point (its own
draft file is crash-recovery only, never offered back to the caller as
a resumable draft the way `_show_board` does for posts), so this
edge case is specific to board posts, but the idempotent design is
applied uniformly rather than special-cased per caller.

### Chat state and rendering

- `ChatHub` keys live membership by channel *name*, and a session inside a
  channel holds that name for its sends, receives and leave (issue #277). A
  rename while callers are inside splits them from everyone who joins later,
  so the SysOp channel screen refuses it with the occupant count until the
  channel is empty (`NodeControls.chat_hub`, `None` from the standalone
  CLI, which instead states what a rename does to anyone inside). Re-keying
  the hub by channel id would remove the restriction; nothing has needed it.

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

`_chat_loop` holds one `Channel` snapshot (a frozen dataclass) for the whole
session. Anything rendered from it that can change mid-session — topic,
`min_age`/name-requirement gates, visibility — must re-fetch fresh via
`get_channel_by_name` at render/check time rather than trust the snapshot,
or the change silently never appears (or never takes effect) until the
channel is re-joined. Hit twice already: `_meets_live_participation_
requirements` (age/name gates) and `_render_chat_status_line` (topic).
Anything new that reads channel-level mutable state from a long-lived chat
session needs the same treatment.

### MRC bridge (issue #275)

- The MRC hub echoes a site's own room traffic back to it. `MrcBridge` drops
  every inbound packet whose `from_site` equals its own wire site name
  (case-insensitively) before anything else; without that, every local line
  would be recorded twice.
- The hub knows callers only through `NEWROOM`/`IAMHERE`/`LOGOFF` from this
  node. The set of announced callers is derived from `ChatHub.participant_ids`
  at connect time and on every mapping refresh, and `local_leave` must run
  *after* `hub.leave` so a second session of the same account keeps the one
  MRC user alive. A caller who enters a bridged channel through a path that
  passes no `mrc_bridge` (only `mrc_bridge=None` callers -- tests, the admin
  CLI) is still announced on the next reconcile, not never.
- Wire bounds are the hub's, not ours: names 30 chars / ASCII 33-125 with
  spaces underscored, bodies 140 chars / ASCII 32-125, lines 512 bytes, no
  escaping for the `~` delimiter. `netbbs.mrc.protocol.parse_line` strips
  ANSI and control bytes from every field at the parse boundary; nothing
  downstream may assume otherwise. Pipe codes (`|NN`) are stripped from the
  identity fields at parse time and never translated there; what a body
  keeps is the colour rule below (issue #298).
- Inbound room lines are recorded through the ordinary `record_message`
  (author label `user@site (MRC)`, `author_fingerprint=NULL`,
  `external_source='mrc'`) and so are bounded by the scrollback limit and
  search-indexed; server notices, topics and join/part chatter are broadcast
  as plain strings and never stored. Private (`to_user`) lines are never
  delivered. Join/part chatter is recognised only by the hub's anchored
  templates (`*** ...`, `- nick has joined|left|timed out`, `- nick was
  renamed`), never by bare keywords: a caller's "I'm leaving after dinner" is
  a chat line with an author.
- Provenance boundary: `channel_messages.external_source` (migration 61) is
  the one marker that a row is not this node's own content. Trusted-scrollback
  snapshots filter such rows *before* applying their entry cap, Link queueing
  refuses to sign them, and `record_message` captures the inserted row by id
  inside its own transaction -- never "the channel's newest row" (a bridge
  write on the background lane can land beside it) and never after the commit
  (with a scrollback limit of 1 the other writer's trim can delete it first).
- Disclosure invariant: a caller is told that their handle and words leave the
  node before their first relayed line, whichever way the channel became
  bridged -- on joining a bridged channel, when a SysOp maps or remaps an
  occupied channel, and on the first connection after MRC is enabled
  node-wide. Reconnects re-announce silently.
- Announced callers are tracked together with the room they were announced
  in, so a remap moves them (`NEWROOM old:new`) instead of leaving the hub
  roster in the old room. The bridge re-reads its mappings once per keepalive
  tick because the standalone admin CLI edits them in SQLite with no way to
  signal the running node; pause, unmap, delete and rename therefore converge
  within a tick, and the standalone screens say so. A storage error while
  recording an inbound line drops that mapping, never the connection.
- Inbound bound: every non-empty line is charged to the inbound bucket before
  it is parsed, before and after HELLO alike; only HELLO itself is recognised
  ahead of the bucket, or a flood during the handshake could block it forever.
  USERLIST state is retained only for mapped rooms. The remote roster hides an
  entry only when nick *and* site match this node. A multi-chunk local line is
  relayed all-or-nothing under the per-caller bucket, and local delivery
  precedes the relay notice so a sender's dropped socket cannot take the
  already-recorded message from other readers.
- Reconnect "stability" is judged from the connected-at timestamp:
  `_connect_and_serve` never returns normally, so a flag set on return could
  never fire. USERLIST request timestamps are cleared with the rosters, or a
  reconnect inside the request guard shows an empty room until the next
  refresh. The diagnostic log handler is attached on every run because MRC can
  be enabled without Link.
- Known limitations: one account with simultaneous sessions in two different
  bridged channels appears in only one hub room (a node-wide per-account
  identity would be a design change); renaming an occupied channel splits
  `ChatHub` membership for all delivery, MRC included, which is a chat
  subsystem question rather than a bridge one.
- `OLDVERSION` from the hub stops the connector until `reload_settings()`;
  retrying would open a fresh rejected session each time. The advertised
  protocol version (`netbbs.mrc.protocol.PROTOCOL_VERSION`) tracks the MRC
  protocol revision the reference clients send, not NetBBS's release number.
- `tests/mrc_fake_hub.py` is the only MRC test double: a real loopback TCP
  server speaking the tilde protocol (HELLO, PING, echo, USERLIST,
  OLDVERSION, and plain-text replies to the informational commands). Bridge,
  chat-flow, admin-screen and `run()` lifecycle tests all drive it over real
  sockets; there is no in-memory transport stub.
- Body convention (issue #298): an MRC body carries the *sender's own*
  handle, in colour, and every client displays a body verbatim -- the hub
  adds no name. Outbound, every chunk is `protocol.format_room_body` /
  `format_action_body` and `split_body(reserve=...)` pays for the prefix out
  of the 140-character budget. Inbound, `split_sender_prefix` peels a prefix
  only when the embedded name equals `from_user` (the underscored and the
  spaced spelling both count); a body naming anyone else is recorded whole.
  The fake hub's bare bodies hid this for a release: any new body test must
  use one of the four reference templates.
- Pipe codes have two fates at the parse boundary: identity fields lose every
  `|XX`; a body keeps `|00`-`|23` and loses the rest. Nothing after
  `parse_line` may strip colour from a body it will store; nothing may render
  a body without `sanitize_text` first. `netbbs.rendering.pipe_codes.
  render_pipe_codes` is the only producer of pipe-derived SGR and always
  resets after itself; `_render_channel_message` composes it beside the label
  as an independent span. `record_message(index_body=...)` carries the plain
  words to the search index. Hub roster entries and server-command words are
  stripped before comparison (`parse_userlist`, `parse_server_command`).
- Per-caller delivery: a `SERVER` packet addressed to an announced nick is
  looked up through `_announced` (`_caller_for_nick`), never through the
  account name -- the hub knows nicks only, and `USERNICK` can change one
  mid-session. `MrcNotice` (text with codes, kind, created_at) is the one
  object the bridge hands `ChatHub` for ephemeral lines; the receive loop
  renders it per viewer. A shared pre-coloured string would defeat the
  per-viewer colour preference.
- Bounds added: reply lines per caller (`REPLY_BURST`/`REPLY_RATE_PER_SECOND`,
  one "cut short" notice per burst), CTCP replies per remote sender
  (`CTCP_BURST`), `USERROOM` re-announce at most once per keepalive tick
  (`_rehomed`, cleared on the tick). A CTCP request for a nick this node never
  announced is ignored without a reply.
- An empty `to_room` is treated as a network broadcast (shown in every active
  bridged channel), following ENiGMA½; if the live hub ever sends ordinary
  room traffic with an empty `to_room`, this is the switch to revisit.
- Open rooms (issue #300): `channels.mrc_origin = 'caller'` is the one marker
  that a row was materialized for a caller rather than mapped by a SysOp;
  everything that treats an open room differently (the picker section, the
  sweeper, `Re[t]ire`/`[A]dopt`, the Link refusal, exclusion from the plain
  channel list) reads that column, never the `mrc:` name prefix, which is
  only the collision guard. Only `netbbs.mrc.settings.materialize_open_room`
  may insert such a row (direct INSERT, content-addressed on the lower-cased
  room), and `create_channel`/`update_channel` refuse the prefix for everyone
  else -- a database from before this release cannot contain a `mrc:` channel
  because the prefix was not reachable through any screen.
- The sweeper is its own bridge task (`_sweeper_loop`, keepalive cadence),
  started by `start()` and independent of the hub connection and of the
  open-room switch: rooms must age out during a long outage or after MRC is
  switched off, or the cap strands. It re-reads mappings and settings first
  (the standalone CLI edits them without telling the node), uses
  `mrc_last_active_at` (or `created_at` for a never-touched row), the
  `ChatHub`'s participant counts as the occupancy source and `user_follows`
  as the keep-alive, goes through `purge_channel_rows` (the row half of
  `delete_channel`, which also prunes the search index) and reports to the
  MRC diagnostic log because `moderation_log.actor_user_id` is NOT NULL and
  the node has no user. `_forget_mapping` drops the room's roster, USERLIST
  timestamp and activity stamp too -- open rooms churn at callers' pace, so a
  retired room must leave nothing behind. Activity stamps are written at most
  once a minute per channel (`_touch`), never per line.
- One identity per account is decided from the `ChatHub`'s occupancy of every
  active bridged channel (`identity_room_elsewhere`), never from `_announced`:
  the announced set is empty during backoff and only catches up on reconnect.
  The one authoritative check sits in `_chat_loop` immediately before
  `hub.join` with no await in between, so two sessions of one account cannot
  both pass for two rooms; the picker and `/join` run the same check earlier
  only for a friendlier refusal. A session switching rooms with `/join` passes
  its current channel as `leaving` so its own presence does not block it --
  unless another session of the same account is still there.
- Gates before rows: `_open_room_gate_denial` checks the caller against the
  node-wide open-room defaults *before* `open_room` materializes anything, or
  an account the gates turn away could fill the cap with rooms it can never
  enter. The section's list is built with `_may_enter_quietly`, never
  `_authorize_channel_entry`, because the latter accepts a pending invitation
  as a side effect and a listing must write nothing.
- The blocklist is consulted on every way into an existing open room (the
  section's list and selection, bare and explicit `/join`, and the `_chat_loop`
  pre-join check), not only when a room is first opened; a room blocked after
  it was opened admits nobody until the sweeper retires it. It is stored as a
  JSON list because a room name may contain a comma.
- `_open_or_find_room` is the one resolution step for the picker and `/join`:
  an existing channel for the room (SysOp-mapped or open) is returned as is
  and entered under its own gates; only a *new* row is subject to the
  node-wide defaults, the blocklist and the one-identity rule
  (`identity_room_held`, the target-agnostic form), all before the write.
  Otherwise stricter defaults would lock callers out of the SysOp's own
  mapped channel, and a second session could spend the cap on rooms it is
  refused.
- An open room's `channel_id` is content-addressed on the room *and* a
  per-node secret (`mrc_open_room_namespace` in `node_config`, minted once):
  two nodes opening the same room must not share an id, or an adopted-and-
  Linked room on one would alias the open room on the other in
  `materialize_carried_channel`, and a peer must not be able to compute a
  room's id. `materialize_carried_channel` refuses a genesis named into the
  `mrc:` prefix or claiming an open room's id (`ChannelCarryRefusedError`, a
  `ChannelCarryLimitError` so the transport's tolerance applies), and
  `materialize_carried_channel_message` projects only into rows with a
  genesis on file. The migration renames any pre-existing `mrc:` channel to
  `local-mrc:...` -- the prefix was typeable before this release.
- Activity is stamped before the connectivity check in `local_join` and
  `local_message`: a room in use during a hub outage is not idle. A session
  that passed the pre-join checks holds its room against the sweeper for a
  minute (`note_entry`, `_entering`), which covers the lane hop between the
  sweeper's occupancy snapshot and its delete; a caller arriving inside that
  hop is the residual, accepted window.
- Join/leave chatter is matched up to the last `: ` (`(?P<room>\S+):\s+\S`)
  because a colon is legal inside a room name.
- `remote_roster` hides every entry at this node's own site, not only nicks
  currently announced: a USERLIST fetched moments before a caller left still
  names them, and "0 here, 1 on MRC" for one's own ghost is wrong.
- Observed rooms are bridge memory (200 entries, least recently seen
  evicted), fed by openings, `USERROOM` targets and the anchored `*** Joining
  <room>:` / `*** Leaving <room>:` templates; the hub only sends join chatter
  for rooms this node is in, so the list mostly reflects this node's own
  history until the `LIST` reply format is known and parsed.
- The picker's section entry uses stable id 0 (channels are positive,
  categories negated), the section's own picker uses -1 for "open by name"
  and -2-n for observed rooms; `pick_item` shows the id beside every entry,
  so these must stay small.

### Read cursors and follows (issue #56)

A per-user read cursor is a position marker `(user_id, object_type,
object_id) -> (last_seen_created_at, last_seen_stable_id)`
(`netbbs.activity`), not a per-item flag table — deliberately reusing the
exact `(created_at, stable_id)` tuple boards/file areas already
keyset-paginate with, so "unread" is the identical tuple comparison
`list_posts_page`/`list_files_page` already perform for their own `after=`
parameter, just anchored at the user's cursor instead of a page boundary.

A channel's cursor stores a message's plain integer `id` as text (uniform
column type across object types), but every comparison against it must cast
back to `int` explicitly — comparing it as a string ranks `"9" > "10"`,
silently losing every double-digit-and-beyond message the moment a channel's
retained scrollback passes nine messages. Boards/file areas don't have this
hazard: their stable ids are fixed-length content-addressed hex, so ordinary
string comparison is safe there.

A cursor must never retreat: paging backward into history, or an
older-page redraw, must not un-mark already-read newer content. Every
`record_*_seen` call reads the existing cursor first and only writes when
the new position is strictly newer — never a blind upsert.

Jump-to-first-unread (`_show_board`/`_show_area`'s `initial_cursor`
parameter) must fall back to the ordinary newest-page view when nothing is
newer than the supplied cursor (a caught-up user, not a genuinely empty
board/area) — treating an empty `after=` result as "board has no posts"
would falsely claim an active board is empty and could even prompt the
viewer to compose its first post.

Follow/favourite state (`user_follows`) and read cursors both key on
`object_id` with no FK (`object_type` is polymorphic across
board/channel/file_area/community, the same shape `moderator_grants`
already uses for the identical reason) — every polymorphic-cleanup
`delete_board`/`delete_channel`/`delete_file_area`/`delete_community`
function needs its own explicit `DELETE` for both tables, the same way it
already does for `moderator_grants`; nothing in the schema cascades this
automatically.

**Authored chronology and node-local arrival order are different axes
(issue #72).** The `(created_at, stable_id)` tuple above is correct for
locally originated content, but a Link-carried post's `created_at` is
the *remote author's own claimed* timestamp, which can be arbitrarily
old if the post only reaches this node after a partition or delayed
catch-up — comparing against it can let a genuinely new arrival
silently sort behind an already-advanced cursor and never surface as
unread. `user_read_cursors.last_seen_arrival_id` tracks a second,
independent axis: the `posts`/`files` row's own `INTEGER PRIMARY KEY`
rowid, which SQLite assigns in strict insertion order regardless of
whether the row was created locally or materialized from a carried
Link event (`netbbs.link.boards.materialize_carried_post` inserts via
a plain `INSERT`, same as `netbbs.boards.posts.create_post` — no new
column needed on `posts`/`files` themselves, this is the same rowid
property GitHub issue #68 already relies on). `unread_post_count`/
`unread_file_count`/`unread_replies_to` (`netbbs.activity`) compare
against `last_seen_arrival_id`, not `created_at`; `board_read_cursor`/
`file_area_read_cursor` (feed-position jump-to) are deliberately
unchanged, still `created_at`-based — jump-to precision to a specific
out-of-order arrival is an accepted, documented scope boundary (design
doc §6.6), not silently unhandled. `record_board_seen`/
`record_file_area_seen` record the *specific* post/file's own arrival
id passed to them, never a container-wide maximum — this is what lets
a late-arriving historical post keep its own high arrival id above the
cursor even after a user visits the board's ordinary newest page,
since that specific old post is never "the newest post shown" on a
normal feed view. A pre-#72 cursor row has no `last_seen_arrival_id` of
its own; the migration backfills it from the post/file its existing
`last_seen_stable_id` already names (`tests/test_activity.py`'s
migration-backfill tests exercise this directly, by monkeypatching
`netbbs.storage.database.MIGRATIONS` to a shorter list, writing a
cursor row in the pre-migration shape, then reopening with the real
list). `_get_cursor`'s returned `_Cursor.arrival_id` can only be `None`
for a backfilled row whose named post/file was already hard-deleted at
migration time — the one case every `unread_*_count` function falls
back to the legacy tuple comparison for.

### Local search (issue #56)

FTS5 availability on this project's actual NetBSD/pkgsrc target was
confirmed by tracing the pkgsrc build chain, not by empirical access to a
NetBSD box: `lang/python312` buildlinks against `databases/sqlite3` rather
than bundling its own amalgamation, and that package's Makefile passes
`--fts5` unconditionally in `CONFIGURE_ARGS`. If a future pkgsrc/Python
version change ever alters that chain (a different SQLite dependency, a
Python build that bundles its own SQLite instead of buildlinking), re-verify
before relying on FTS5 again — this project has no runtime feature-detection
for it; a missing module simply fails the schema migration loudly.

`post_search`/`file_search`/`channel_message_search` (`netbbs.search`) are
kept in sync by explicit calls from every write path in
`netbbs.boards.posts`/`netbbs.files.entries`/`netbbs.chat.scrollback`, not
SQL triggers — this schema has no triggers anywhere else, and keeping the
sync logic as visible Python calls (mirroring `record_action`'s own explicit-
call convention) was chosen deliberately over the trigger-based pattern
SQLite's own FTS5 documentation recommends for external-content tables.
Any new write path added to those three modules in the future (a new
status transition, a new bulk/sweep operation) must add its own reindex
call; nothing enforces this structurally. If one is missed, or a crash
lands between an authoritative commit and its reindex call, `netbbs.
search.check_index_integrity`/`rebuild_indexes` (issue #74) is the
supported repair path: both are computed from the same "what should be
indexed" query, so a rebuild always converges to a clean check
immediately after, and the check reports only which ids drifted
(missing/stale/extra), never the indexed content itself. Deliberately
not wired into node startup, unlike `Database.check_integrity` -- see
design doc §6.6 for why.

A bulk/sweep statement (`_sweep_expired_posts`/`_sweep_expired_files`) has
to collect the affected root/file ids with a `SELECT` *before* running the
bulk `UPDATE`/`DELETE`, since `reindex_post`/`reindex_file` need to be
called once per affected id afterward and a set-based statement doesn't
otherwise expose which rows it touched.

**Content-hash IDs are not orderable by recency (GitHub issue #68, fixed).**
`_resolve_current_version` and `edit_post`'s own "current revision" lookup
both pick the newest approved revision of a post's edit chain by ordering
candidate rows. They used to tie-break on `post_id DESC` — but `post_id` is
a content-addressed hash, not a recency-ordered value, so when two
revisions land in the same `created_at` instant (confirmed to happen often
enough in fast automated tests to matter, e.g.
`tests/test_link_boards.py::test_queue_board_post_edit_chains_a_second_edit`
flaked roughly 40% of the time before this fix), that tie-break picked
whichever revision happened to hash lexicographically larger — not
necessarily the one actually created last, silently resolving to the wrong
"current" content and occasionally mislinking a Link edit event's
`previous_event_id`. Fixed by tie-breaking on each row's own `id`
(`INTEGER PRIMARY KEY`/rowid) instead — SQLite assigns it in strict
insertion order whenever a row's `INSERT` never supplies an explicit value
(true of every `posts` insert here), so no new column or migration was
needed. `netbbs.search.reindex_post` mirrors the same corrected query.
**The general lesson**: any "pick the most recent of several rows sharing
a timestamp" query needs a genuinely monotonic tie-break (an autoincrement
id, a sequence column) — a content hash, UUID, or other identifier with no
relationship to insertion order will eventually pick wrong under a
same-instant collision, and won't be caught by tests unless timestamps are
either pinned to strictly increasing values or deliberately collided (see
`tests/test_post_editing.py::test_feed_shows_latest_content_when_an_edit_
collides_with_the_original_timestamp` for the deliberate-collision pattern).
This is distinct from `list_posts_page`'s own `(created_at, post_id)`
cursor tie-break, which orders *distinct* root posts' feed positions
(an accepted rare-tie display-order pick, not "which revision is current")
and was correctly left unchanged.

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

### Terminal prose wrapping

Every ordinary terminal line must pass through `Session.write_line`, whose
display-column-aware safety net wraps styled ANSI text without splitting escape
sequences. Interactive prompts use `netbbs.net.session.write_prompt`; it wraps
without a final newline and reserves one column for the first input character.
Wrap points consume their whitespace so neither the preceding line ends in a
blank nor the continuation begins with one. Screen-specific renderers should
still wrap sanitized plain text before adding color when practical, but must
never opt into `break_long_words=False` for terminal output because that turns
an over-width token into invisible overflow. An indivisible token wider than
the available row is hard-split only as the unavoidable bounded fallback; all
ordinary prose wraps at word boundaries. `write_preformatted_line` is reserved
strictly for trusted ANSI banners/mastheads: it retains authored rows that fit
but still bounds over-width rows, and is never used for product copy. Tabs must
be normalized before width measurement, while ANSI horizontal cursor controls
must update a bounded cursor model; treating either as zero-width can still
produce invisible overflow. Preserve the original cursor controls so absolute
positioning never blanks cells already drawn on that row. Absolute placement,
save/restore, and bare carriage returns update column accounting, while numeric
parameters are clamped without per-column iteration so a tiny ANSI file cannot
create unbounded CPU work. Prompt output reserves two columns because one input
character may itself occupy two. CLI tools use `print_wrapped`, and exception
messages sent to stderr measure stderr rather than stdout. Bundled doors remain
standalone but apply the same ANSI-aware bound in their local
`out_line`/`out_prompt` boundary, including combining-mark width and reapplying
active content styling after colored borders on wrapped box continuations.

Shared semantic roles (`LABEL_COLOR`, `VALUE_COLOR`, `METADATA_COLOR`,
`SUCCESS_COLOR`, and `ERROR_COLOR`) are the presentation contract for mature
line-oriented surfaces. Caller and SysOp Who use the same picker palette;
mail detail and outcomes, vCards and profiles, Last sessions, picker feedback,
and welcome-banner/main-menu-masthead administration use the shared roles
instead of inventing screen-local colors. When styled fields must fit a terminal width, build the
trusted ANSI segments independently and use `colored_truncate`; slicing the
completed string by Python length can split an escape sequence and counts SGR
bytes as visible columns.

Truecolor support is transport capability, not a guarantee inferred from the
configured preference. SSH records `COLORTERM`, web declares its built-in
xterm.js support, local CLI reports that it has no negotiation, and Telnet
records the NEW-ENVIRON result. Expose this provenance through the profile and
welcome-banner preview so fallback behavior is diagnosable. The Telnet login
banner is rendered before the bounded lazy NEW-ENVIRON reply may arrive and
therefore must remain safe at 256 colors; later screens can use and report the
negotiated result. A custom SysOp welcome file intentionally bypasses the
generated truecolor showcase and its preview must say so.

`netbbs.net.node_theme.effective_{accent,header,clock}_color`/`_256`
(issue #162) resolve the SysOp's node-wide accent/header/clock overrides in
place of the matching bare `theme.py` constant. Every affected shared
rendering helper (`layout.screen_title`/`double_frame`/`empty_state`,
`picker.pick_item`, `resource_editor.edit_resource_draft`,
`composition.review_composition`, `chat_flow.run_direct_chat_loop`,
`help_overlay.show_help`, and the bespoke field/section renderers in
`login_flow`, `mail_flow`, `file_flow`, and `admin_flow`) gained matching
`accent_color: int = ACCENT_COLOR` / `header_color: int = HEADER_COLOR`
parameters rather than reaching into `node_theme` internally -- these are
DB-agnostic presentation functions by design, so the caller resolves the
value once (the same "resolve once in the caller, pass down" shape
`unicode_style`/`redraw_in_place`/`collapsed` already established) and
threads it down. Any new call site into these shared functions must follow
the same shape rather than importing the bare constant directly, or a
SysOp's override silently stops applying to that one spot. A screen sharing
one rendered string across many simultaneous recipients (chat broadcast, the
direct-chat status line) always uses the `_256` variant -- since no single
per-viewer truecolor decision is meaningful for content composed once and
sent to several sessions with potentially different capabilities; a
single-recipient screen with real `session`/`db` access in scope uses the
full `session`-aware variant instead. `netbbs.net.admin_flow`'s Settings >
[C]olors screen is the SysOp-facing write path for all three overrides --
it previews a candidate RGB at both truecolor and 256-color depth against
real sample text before applying. One deliberate exception:
`ansi_editor.py`'s own internal glyph/color-picker chrome (`edit_ansi_art`
and everything it calls) stays on the unresolved default -- that module is
intentionally database-free (it edits raw bytes for a caller-supplied path,
used by both the welcome banner and main-menu masthead editors), and
threading a node-wide color through its own meta-UI for editing colors is
disproportionate to the value.

`rendering.layout.screen_title(clear=True)` prepends its own `clear_screen()`
*inside the string it returns* — the clear-and-home sequence is not a separate
step a caller can order independently. A screen that needs to show something
above the title/breadcrumb itself (e.g. `main_menu._draw_main_menu`'s
optional main-menu masthead, issue #161) cannot just write that content before
calling `screen_title(clear=True)`: the embedded `clear_screen()` fires when
the title string is later written, wiping anything already-written first. Pass
`clear=False` to `screen_title` in that case and issue `clear_screen()` by
hand immediately before the prepended content instead — never rely on
`screen_title`'s own `clear` for a screen whose real first line isn't the
title.

Prepending optional content (a masthead) above a *live, self-redrawing*
screen is a different problem from prepending it above a screen drawn
once per outer-loop iteration (`_draw_main_menu`'s own case above): issue
#176 extended `netbbs.net.picker.pick_item` with a `masthead` parameter,
not a caller writing the masthead once before its first call into
`pick_item` — `pick_item` redraws its own screen from scratch, via its
own internal closure, on every state change it handles itself (paging,
search, sort, `Ctrl-R` refresh) that the caller never sees or gets a
chance to re-prepend anything for. A parameter threaded through that
closure is the only way the content survives every one of those redraws,
not just the first paint. Any future "show optional content above a
shared, self-contained interactive component" feature should check
whether that component owns its own redraw loop before assuming a
`_draw_main_menu`-style "caller writes it once, above one function call"
shape will work.

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

A repeated Tab press with an unresolved multi-candidate completion must not
reprint an identical candidate list — `char_input.LastCandidateList` (mirrored
in `web.WebSession._read_line_editable`) suppresses it. This cannot be
detected by comparing the completed *word* before and after: a multi-candidate
Tab press extends the word to the shared prefix as a side effect, so backspacing
a word away to nothing and pressing Tab again can reconstruct the exact same
word the previous press already showed, even though a real edit happened.
Detect it instead by tracking whether the *immediately preceding keystroke*
was itself an unresolved Tab — every other keystroke (including ones that
change nothing, like Left then Right) must clear that flag before its own
handling runs.

### `edit_resource_draft` save contract: raise to retry, return to leave

`edit_resource_draft` returns whatever the caller's `save(draft)` returns,
`None` included -- so a `save` that prints a validation message and
`return`s closes the editor and discards the draft, which is what the
shutdown/drain screens want for a declined final confirmation but not
what a "fill in X first" rejection wants. The only retry path is an
exception of the caller's `error_type`: the editor prints
`Could not save: <message>` and redraws with the draft intact. Every
issue #282 editor therefore raises its `error_type` (`ValueError`,
`ModeratorGrantError`, the category error, ...) for missing fields and
for a declined in-save safety confirmation whose draft should survive,
and reserves `return None` for "leave the screen, nothing saved". A
scripted test only proves the difference if it keeps typing after the
rejected save (the next key would otherwise be swallowed by the parent
menu either way).

### `edit_resource_draft` section-based pagination

A sectioned screen (`FieldSpec.section` set) whose full field list, at
whichever menu-row tier the existing descriptive/sectioned-compact/flat
fallback lands on, doesn't fit `session.terminal_height` paginates by
section rather than letting the top of the field list scroll off. The
fit-check is computed fresh every redraw (never cached), the same way
the pre-existing menu-tier upgrades already treat `terminal_width`/
`height` as live values -- a mid-session terminal resize can un-paginate
or re-paginate a screen correctly rather than sticking with whatever
was decided on first paint.

`_field_value_lines` (the pure, no-I/O value-list renderer both the
fit-check and the real per-page render call) matches its `selected`
parameter by **identity** (`f is selected`), not position index -- a
page-scoped call and a full-list call have different valid index
ranges for "the currently highlighted field," so identity is the only
primitive that works correctly for both without the caller
reinterpreting what a given index means depending on which list it's
indexing into.

Hotkey dispatch (typing a field's own letter) always searches the
*full* field list regardless of which page is currently shown --
paginating never breaks "every hotkey keeps working exactly as
before," this component's own repeated design philosophy since cursor-
nav was added. Activating a field on a different page also switches
`current_page` to match, so the caller sees what they just changed
rather than a screen that looks unchanged. This has a real test-writing
consequence: activating *any* field's hotkey also marks it cursor-nav-
selected on the next redraw (a different `colored()` string --
accent-colored/bold with a `> ` marker -- than its unselected
rendering), so a test asserting an *exact* colored-string match against
a field that must stay unselected (not just a plain-text substring
search, which the marker prefix doesn't break) needs to reach that
field's page via a *different* field's hotkey, not the one under test.
`Page Up`/`Page Down` navigate without selecting anything, avoiding
this entirely, but many of this suite's lighter-weight `Session` test
doubles don't implement `read_editor_key` at all (or don't map
`"PAGE_UP"`/`"PAGE_DOWN"` sentinels even when they do) -- check which
`FakeSession` a given test file actually uses before assuming a
`PAGE_DOWN` press can be scripted there; where it can't, activating an
*adjacent* field on the target page (one with an immediate, no-sub-
screen `prompt` like `live_choice_field`, so no extra keystrokes are
needed to back out again) is the workaround, not the field the
assertion is actually about.

### Picker line width

`netbbs.net.picker.pick_item` truncates each rendered row to terminal width
(`truncate`, `netbbs.net.picker.py`) — the 2-digit selector, `name_of`, and
`description_of` all share that one line. This is invisible until an item's
`name_of` is naturally long: a Link peer fingerprint (32+ characters) plus
its `(#<id>)` reference already consumes most of an 80-column line, so a
`description_of` packing in more than one short field (issue #60's SysOp
Link-status peer picker originally tried "mode, reliability, last contact"
in one string) silently truncates mid-word with no error or indication
anything was cut. Keep `description_of` to one short field for any picker
whose `name_of` is itself long; put additional detail in the full-width
post-selection screen instead, where `truncate` doesn't apply.

### A `Session.read_editor_key` test double must accept `distinguish_ctrl_h` and preserve `read_key()`'s sentinel returns (issue #171)

`pick_item`'s Up/Down/Enter highlight required switching its main loop
from `session.read_key()` to the structured `read_editor_key()` reader
(`read_key()` "discards every escape sequence outright," so arrows are
fundamentally undetectable through it -- see that method's own
docstring). Every real transport (`TelnetSession`/`SSHSession`/
`WebSession`/`LocalCLISession`) already implements `read_editor_key`
correctly, but `pick_item` is reused far more widely than
`edit_resource_draft` (the only prior `read_editor_key` consumer), and
two latent gaps across the test suite's lightweight `Session` doubles
only surfaced once *this* screen finally exercised them:

1. **`distinguish_ctrl_h` isn't universally accepted.** ~20 test-only
   `read_editor_key` overrides across the suite predate that parameter
   (e.g. `async def read_editor_key(self):` with no kwarg at all) --
   calling them as `read_editor_key(distinguish_ctrl_h=True)` raises
   `TypeError` before their body (an unconditional `raise
   NotImplementedError`) ever runs, which the
   `except NotImplementedError` fallback doesn't catch.
   `picker._read_navigable_key` (deliberately its own copy, not shared
   with `resource_editor._read_navigable_key` -- see its own
   docstring) retries the call with no kwarg at all before giving up,
   rather than requiring every one of those ~20 fixtures to be
   updated for a parameter their bodies never actually use.
2. **The `read_key()` fallback must still recognize the four unechoed
   sentinels.** A `Session` double with no working `read_editor_key`
   at all falls back to wrapping `session.read_key()`'s return as a
   plain `EditorKeyKind.CHAR` -- but several fixtures script
   `REDRAW_KEY`/`REFRESH_KEY`/`HELP_KEY`/`CANCEL_KEY` directly (or, like
   `test_who_online.py`'s Ctrl-R-triggered registration, override
   `read_key()` to inspect one for a side effect), and naively wrapping
   the sentinel string as an ordinary *echoed* character both breaks
   the Ctrl-combo's own handling (it's no longer recognized as `CTRL`)
   and visibly echoes a raw control byte that real `read_key()` never
   would. The fallback path translates these four known strings back
   to the matching `EditorKeyKind.CTRL` event before falling through to
   the generic `CHAR` wrap.

Any future caller moving from `read_key()` to `read_editor_key()` in a
screen with broad test-double reuse should expect both gaps, not just
whichever one its own first test run happens to hit.

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

Any code that writes to the terminal while the pinned rows are active must go
through the scroll-region-aware primitives (`_print_and_redraw_input`/
`_enter_content_region`/`_repaint_*`), never a bare `write("\r\n" + ...)`. A
raw newline has no idea the cursor may be sitting on a pinned row outside the
scroll region, and lands whatever it writes on — and overwrites — that row
instead of scrolling normally above it. `netbbs.net.char_input.
apply_tab_completion`'s multi-candidate listing hit exactly this: written
against a bare terminal, predating the pinned-row feature, its raw-newline
default was still the only path in use once pinned rows shipped, corrupting
the status line every time completion listed more than one candidate. Fixed
by giving it an optional `list_candidates` hook (same shape as `live_buffer`/
`lock`: threaded through `read_line`, `None`/no-op everywhere except chat's
`send_loop`) that callers with reserved rows can use to redraw correctly
instead. Any future generic `char_input` primitive that can print more than
one line needs the same hook, not an assumption that a bare newline is safe.

Node-wide out-of-band writes hit the identical bug from a different angle:
`ActiveSessionRegistry.broadcast_to_all` (a shutdown notice reaching every
connected session regardless of screen) called a bare `session.write_line`
directly, with no idea a target session might currently be `_chat_loop` with
pinned rows active — landing the notice on the pinned input row and letting a
subsequent Backspace edit it, since chat's own input-editing state never knew
it was written. Fixed the same way: `Session.pinned_notice_hook`, `None` for
every screen except chat (which installs its own already-correct pinned-row
delivery closure on entry and clears it on exit), checked by `broadcast_to_all`
before falling back to a plain write. Any future node-wide broadcast to
arbitrary sessions needs the same hook, not an assumption that a session is at
a plain scrolling prompt.

Fullscreen session owners must never be nested inside another fullscreen
owner's input task. Direct chat entered through channel `/dm` first returns an
explicit action to `browse_channels`; only after the channel receive/clock
tasks are cancelled and gathered, presence removed, the pinned hook cleared,
and the scroll region reset may the invite/direct-chat screen begin. Returning
reauthorizes and re-enters the original channel. The same boundary applies to
any future screen which owns session reads or reserved terminal rows.

In a pinned input UI, Enter clearing the logical `LiveInputBuffer` is only half
of submission: repaint the input row from that empty buffer before moving into
the content region and rendering the committed line. Otherwise the terminal
still displays the old input below the committed copy even though internal
state is already empty. Keep conversational identity and body as separately
sanitized, separately styled spans; sanitizing a fully colored line would
strip trusted ANSI, while coloring one combined untrusted string prevents
field-level semantics and makes reset behavior harder to reason about.

Room-lifecycle signals are not lossy chat traffic. A direct-chat close notice
uses priority queue delivery so a full recipient queue evicts an older ordinary
line rather than substituting an overflow notice and leaving the peer blocked.
Synthetic direct-chat room keys and per-session arrival events are also
ephemeral resources: remove an empty synthetic room immediately and use weak
session-key storage where production session objects permit it.

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

The shared line reader already provides cursor editing inside the current line
(Left/Right, Home/End, Backspace/Delete, Insert/overwrite). That does not make a
multi-line prompt an editor: once Enter submits a logical line, revisiting it
requires a caller-owned body buffer and explicit line operations. Keep that
buffer transport-independent and separate from the fullscreen editor's screen
model. Both editor paths must return a draft to a review/commit boundary; they
must not persist or dispatch merely because editing ended.

The shared line composer owns logical lines and uses explicit `/list`,
`/insert N`, `/edit N`, `/delete N`, `/done`, and `/cancel` operations; a blank
line retains the familiar finish gesture but now enters review rather than
committing. `//` escapes a literal leading slash. Enforce domain byte and line
limits against each candidate buffer mutation so an invalid edit never
destroys the last valid draft.

Review is a state, not a one-shot prompt. Subject/body and mail recipient
changes return to a freshly rendered preview; validation or delivery failure
does the same with the draft intact. Only the domain's explicit commit action
may call persistence, and fullscreen-editor save output enters this identical
state. A fullscreen cancel while revising a body means "keep the reviewed body
unchanged"; cancellation of the overall composition remains a review action.

### Confirmation and visual interaction primitives

`read_key()` deliberately swallows CR/LF and must keep doing so for ordinary
menu hotkeys. Standard yes/no prompts therefore read the already-shared
structured editor-key vocabulary instead: it preserves Enter on Telnet, SSH,
web, and local CLI without adding a second transport decoder. The confirmation
primitive owns accepted-key echo and the terminating CRLF because editor keys
are intentionally unechoed; invalid keys bell and retry. After Y/N, each
interactive transport performs one bounded, pushback-safe lookahead to absorb
a habitual trailing Enter (``y`` plus Enter) without losing non-Enter input or
letting the leftover line ending select the next prompt. Keep typed-name
destructive confirmations separate and stronger.

Current `main` already field-colors every ordinary `pick_item` row, so caller
and SysOp Who screens share selector/reference/name/metadata roles by
construction. The generated default login banner already uses a truecolor
gradient when session capability negotiation says it is safe; an enabled
custom SysOp ANSI banner intentionally bypasses that generator. If dogfood does
not show either behavior, first verify deployed revision, banner configuration,
and Telnet/SSH/web capability negotiation before adding duplicate rendering.

Ordinary screen composition now has a separate rendering-layer vocabulary in
`netbbs.rendering.layout`. Keep those helpers pure: they accept already-safe
display strings and terminal width, and must not query databases, sessions, or
permissions. The home menu uses the same option set and hotkeys at every width;
80 columns receives grouped columns and the 40-column minimum receives the same
groups stacked vertically. Layout tests strip SGR before asserting visible
width, because raw escape-byte length is not terminal column width.

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

A cooperative `stop_event` checked only at the top of a polling loop is not
enough to drain that loop promptly if the loop's own idle wait (a sleep
between passes) is long relative to the shutdown grace budget — the loop
won't re-check the event until the sleep itself returns. If the idle wait
has no in-flight work to protect (unlike a live network call mid-pass), make
the wait itself interruptible by the same event (e.g. `asyncio.wait_for(stop_
event.wait(), timeout=interval_seconds)` in place of a plain `asyncio.sleep`)
rather than only gating the top of the loop. `netbbs.link.sync.run_link_
sync`'s `stop_event` parameter does this: it lets its own 5-minute-default
`sync_interval_seconds` sleep be woken early, while still letting an
in-flight dial/push pass finish untouched.

Bounding `asyncio.wait_for(task, timeout=...)` on a task that reaches a
blocking synchronous call via `asyncio.to_thread`/`loop.run_in_executor`
bounds *that await*, not the underlying OS thread — cancellation cannot
stop a thread already inside a blocking call (`urllib.request.urlopen`,
raw socket I/O, etc.), which runs to its own completion (or its own
`timeout=` kwarg, if it has one) regardless. Verified by direct repro:
`asyncio.run(main())` itself still blocks for the thread's full duration
even when `main()`'s own coroutine returns immediately after giving up on
its bounded wait — `asyncio.run`'s cleanup phase (`shutdown_default_
executor`) joins the default executor's outstanding work, and separately,
Python's interpreter-exit machinery (`concurrent.futures.thread`'s
`atexit` hook) joins every thread any `ThreadPoolExecutor` in the process
has ever spawned, independent of any asyncio-level bookkeeping. A task's
own bounded cancellation-await (see `netbbs.__main__`'s shutdown teardown,
`background_task_drain_seconds`) makes *that step* return promptly; it
does not, by itself, make the *process* exit promptly if a thread is still
running when it fires. Two verified mitigations, neither applied yet
(design doc §13.11, "Known residual gap"): give the blocking call its own
`timeout=` (already true for every real `urlopen` call in this codebase,
capping the actual worst case at that timeout rather than indefinitely);
or have the shutdown path call `os._exit()` once its own bounded waits are
exhausted, which repro confirms fully bypasses both the executor-shutdown
join and the interpreter-exit thread join — at the cost of skipping every
other kind of cleanup (stdio flushes, other `atexit` handlers, any other
still-pending async work) along with it, a real tradeoff that needs a
deliberate decision, not a reflexive fix.

---

## 9. Link protocol invariants

### LinkNode internal state organization (issue #78)

`LinkNode` (`netbbs.link.protocol`) grew a live projection or piece of
protocol bookkeeping for every Phase 3 feature landed on it, one flat
dict/set field at a time, until it held eleven independent state
families with nothing but "this is Link state" in common. Before adding
the next one (inventory/pull catch-up, linked-channel lifecycle), the
existing families were grouped by actual coherence, each behind its own
small dataclass:

- `PeerDirectory` (`peer_directory`): `peers` (verified, from a
  completed hello) and `candidate_descriptors` (unverified, from
  peer-list exchange) -- grouped together because a fingerprint's
  presence in one changes what the other means for it (`admit`
  supersedes a candidate the moment the same fingerprint completes a
  real hello).
- `BoardEventState` (`board_events`): `boards` (verified board_genesis
  per board_id) and `post_edits` (each post's verified edit chain).
- `BoardLifecycleState` (`board_lifecycle`): `board_origin`/
  `board_lifecycle_head`/`pending_origin_transfers` -- origin
  succession, issue #53. Kept separate from `BoardEventState` because it
  has its own chain (starting from the board's own genesis) with its
  own mutual-consent rule, distinct from a `board_post_edit`'s per-post
  chain.
- `RelayState` (`relay_state`): `pending_own_relay_requests`/
  `relaying_for`/`relays_serving_me`, issue #58. Mostly mutated by
  callers *outside* `LinkNode` (`netbbs.link.transport`'s relay-consent
  routes) -- grouping it still gives that externally-driven policy
  state one named home instead of three loose fields.

`known_event_ids`/`events` deliberately stay directly on `LinkNode`
itself, not inside any of the above: they are the shared dedup/event
store every object type uses (`key_transition`, `link_message`, board
events alike), not owned by one family. `identity` stays there too, as
the façade's own irreducible state.

**Every external consumer keeps reading the old flat names, unchanged.**
`netbbs.link.store`'s restart reconstruction, `netbbs.link.sync`'s
background loop, `netbbs.link.transport`'s HTTP handlers,
`netbbs.link.relay_selection`, `netbbs.net.admin_flow`'s SysOp screens,
and every existing test all access `node.peers[x]`, `node.boards.get(...)`,
`len(node.relaying_for)`, `node.board_lifecycle_head[board_id] = ...`,
etc. directly, exactly as before this split -- confirmed by grepping
every file that references `LinkNode` for direct field access before
starting, then running the entire existing Link test suite afterward
with **zero test changes**. This works because `LinkNode` exposes each
old name as a `@property` returning the *same live dict* the new
grouped object owns (never a copy) -- `node.peers` after the split is
`self.peer_directory.peers`, the identical mutable object, so
`node.peers[x] = y` from outside still mutates the real state. The
split moves where each dict is *defined*; it does not change what it
means to read or mutate it from outside `LinkNode`.

Internally, `LinkNode`'s own methods were only rewired at the specific
points that are a real invariant, not merely a container: `handle_hello`
now calls `PeerDirectory.admit` (peer admission + superseding a
candidate, one operation); `handle_peer_list`'s loop calls
`PeerDirectory.record_candidate` (staleness + cap check, previously
~10 inline lines per iteration); the `board_genesis`/`board_post_edit`/
`board_origin_transfer_offer`/`_accepted` branches of `handle_events`
call `BoardEventState`/`BoardLifecycleState`'s own narrow methods
(`record_genesis`, `extend_edit_chain`, `record_offer`,
`record_acceptance`, etc.) instead of mutating three or four dicts by
hand inline. Plain reads with no owned invariant (e.g. `self.boards.get(
board_id)` used only to check existence) were left as direct property
access rather than rewritten for uniformity's own sake -- the goal was
giving real invariants a named, narrow home, not maximizing how much
code routes through the new types.

Adding a future Link state family should follow the same shape: a new
small dataclass with narrow methods for its own invariants, not a
fourteenth flat field on `LinkNode` and not a generic "state container"
framework applied uniformly to everything above.

### Canonical events

All signed and hashed Link objects use the same canonical JSON-byte function.

Current rules include:

- recursive Unicode NFC normalization, applied to object member names as
  well as values (issue #70) -- two source keys that normalize to the same
  string are a rejected collision, not a silent last-one-wins overwrite,
  the same "ambiguity must fail loudly" treatment already given to
  duplicate wire-JSON keys below;
- deterministic compact JSON representation;
- no floats, including nested floats;
- integers bounded to `[-(2^53 - 1), 2^53 - 1]` (the IEEE-754-double-safe
  range), including nested integers -- issue #11's cross-language numeric
  policy, enforced in the same `_normalize_for_hashing` pass as the float
  ban (`netbbs.boards.content_id.ContentIdError`);
- explicit object/protocol typing;
- optional fields omitted where the event schema says omission, not replaced
  casually with `null`;
- nonces where two otherwise-identical actions must remain distinct;
- duplicate keys within one wire JSON object, at any nesting depth, rejected
  before parsing completes (`netbbs.link.events.strict_json_loads`, wired
  into every `request.json()`/`response.json()` call in
  `netbbs.link.transport` via its `loads=` parameter) -- never resolved by
  whichever "last one wins" behavior the parsing language happens to pick,
  since two different parsers can disagree about which duplicate value wins.

Builders, verifiers, content IDs, and golden fixtures must never maintain
independent canonicalization implementations. Design doc §7.2's golden
vectors (`tests/fixtures/link_canonical_vectors.json`) pin exact canonical
bytes/content IDs for representative payloads; update them only alongside a
deliberate canonicalization change.

Every envelope's `netbbs_protocol` field is checked for an exact match
against this build's own `NETBBS_PROTOCOL_VERSION` (`LinkNode._check_
protocol_version`, design doc §13.11) at `handle_events`' single per-event
`object_type`-extraction point and against `handle_hello`'s embedded
transitions/descriptor envelopes -- before signature verification, so a
mismatched version is rejected on its own terms rather than surfacing as a
signature failure. Exact match only, never a supported range, since version
1 has been the only version to ever exist; a future protocol bump that means
to support mixed-version peers during a rollout needs to deliberately design
that compatibility window here, not assume one already exists.

### Shared local-domain/Link limits must have exactly one definition

`netbbs.link.protocol` deliberately never imports `netbbs.boards.posts`
(or any other local-domain module with real business logic/DB
dependencies) — that boundary is real and worth keeping. But a numeric
admission limit that must mean the same thing on both sides of that
boundary (issue #79: a `board_post`'s subject/body byte limits, checked
both by local `create_post`/`edit_post` and by `handle_events`' receive-
side validation) is a different kind of value than "business logic" —
it is safe, and necessary, to share a single definition for exactly that
value. `netbbs.boards.limits` holds just the two integers with zero
other imports, so `netbbs.link.protocol` can depend on it without
acquiring any of `netbbs.boards.posts`' actual dependencies, while
`netbbs.boards.posts` re-exports the same names so every existing
caller/test importing them from that module keeps working. Before
duplicating a numeric constant across the local-domain/Link boundary
again "to preserve module direction," check whether a similarly narrow,
dependency-free module already exists or should be created instead —
duplication makes an accidental future divergence (content valid on one
side, rejected on the other) possible by construction; a shared
single-purpose module makes it impossible.

### Chain-order reconstruction must not trust `created_at` alone

A per-object chain's authoritative order comes from its own
`previous_event_id`/head-pointer links, verified at acceptance time --
`created_at` is descriptive metadata, not an ordering mechanism, and two
genuinely successive edits *can* share one clock's timestamp resolution
(confirmed in practice already: `tests/test_boards.py`'s own
`test_list_posts_page_returns_all_in_order` comment records real successive
`utc_now_iso()` calls landing on the same microsecond). Any code that
reconstructs a chain from storage instead of re-verifying it live (restart
reconstruction, not `handle_events`) must sort on a locally-assigned,
genuinely monotonic column -- `netbbs.link.store.load_link_node`'s
peer-received `board_post_edit` loop already does this correctly via
`link_events.received_at`; its self-originated counterpart (reading
`posts.link_event_json`) sorted only by the payload's own `created_at` until
a tie-break on `posts.id` (the table's own rowid, assigned in true insertion
order) was added alongside issue #11's spec work. SQLite does not guarantee
a stable sort on tied `ORDER BY` keys; do not assume a tie "happens to" sort
in insertion order without an explicit secondary key, even though it may in
a given build/query plan.

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

**A self-originated event is not automatically in `LinkNode.events`.**
`known_event_ids`/`events` are populated by `handle_events` (peer-received)
and restart reconstruction (`netbbs.link.store`, itself reading back what
`handle_events`/an explicit `save_event` call persisted) -- never implicitly
by whatever composed the event in the first place. A DB-only composer
(`netbbs.link.mail.compose_link_message`, deliberately never touching a live
`LinkNode`) has no way to register its own output at all. Anything that later
needs to recognize "an event this node itself originated" (e.g.
`_resolve_own_link_message`, validating an incoming acknowledgement) must have
that registration done explicitly, by whatever code path first has both a
live `LinkNode` and the composed event in hand -- for Link mail this is
`netbbs.link.sync._push_pending_link_mail`, chosen because it is the one
point every composer funnels through before the event ever leaves the node,
not any individual call site of the composer (issue #69: the missing
registration meant a sender's own outbound `link_message` could never be
recognized when its acknowledgement came back, so it was rejected
unconditionally, every time).

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

**"Carrying" a board and having a locally browsable copy of it are different
things.** `netbbs.link.boards.materialize_carried_board` (issue #53) gives a
node accepting a `board_genesis` it didn't originate a real local `Board`
row; `materialize_carried_post`/`materialize_carried_post_edit` (issue #73)
do the equivalent for `board_post`/`board_post_edit` into real `posts` rows.
All three reuse the signed event's own `content_id` verbatim as the local
`board_id`/`post_id` -- never minted fresh (`netbbs.boards.boards.create_
board`/`netbbs.boards.posts.create_post` can't be reused for this, since
both always mint a new content-addressed ID from the *local* creator/
timestamp) -- which is what lets `posts.root_post_id`/`edit_of_post_id`
resolve directly from a `board_post_edit`'s own `root_post_id`/`previous_
event_id` payload fields with no separate ID-translation table. Any future
Link object type that needs a caller-facing local projection should check
whether that projection actually exists yet, rather than trusting a
"carrying" or "default-carry" description of intent alone -- `link_events`
proves the protocol accepted something; it says nothing about whether any
other table has ever heard about it.

**Post/edit materialization closed a crash-window genesis materialization
still has.** `materialize_carried_board` is a separate `lane.run` call from
the `save_event` that persists its own underlying signed event -- a crash
between the two leaves an accepted-but-unmaterialized genesis, with no repair
path today. `materialize_carried_post`/`_edit` do both writes in one call,
one transaction, closing that window for posts/edits specifically (and
`rebuild_carried_post_materialization` repairs the one-time gap on a node
upgrading from before this existed) -- genesis's own gap is unfixed, and
worth remembering before assuming "it's accepted" implies "it's carried."

**A self-originated Link event's effect on `LinkNode` state must be applied
directly by whichever caller built it -- it never flows through that same
node's own `handle_events`.** This already held for `board_genesis`/
`board_post`/`board_post_edit`; origin-transfer's `board_origin_transfer_
offer`/`_accepted` follow the identical shape, and it is easy to forget on
both sides of a transfer: the *offering* node must set its own `pending_
origin_transfers`/`board_lifecycle_head` the moment it builds the offer (never
waiting to see its own event echoed back, which never happens), and the
*accepting* node must set its own `board_origin` the moment it builds the
acceptance, for the identical reason. Missing either produces exactly the
kind of test failure that looks like a real protocol bug (a node's own view
of "who currently owns this board" silently wrong) but is actually a test/
caller setup gap -- confirmed by tracing, not assumed, while writing this
round's own multi-node convergence test.

**Known, reproducible flaky test, not caused by this round, not yet
diagnosed:** `tests/test_link_boards.py::test_queue_board_post_edit_chains_a_
second_edit` fails intermittently (including in total isolation, no other
tests involved) with a `previous_event_id`/`content_id` mismatch between two
back-to-back `queue_board_post_edit_if_linked` calls on the same post chain.
Reproduced multiple times across unrelated sessions; root cause not yet
found. Worth a dedicated investigation before trusting that test as a
regression signal.

**Inventory-pull's per-kind request dict is documented as exhaustive; a
responder must treat an absent ID as "requester has never seen this," not
merely "requester didn't ask" (issue #94, found via a real 3-node dogfood
deployment, not a unit test).** `InventoryRequest.boards`/`.channels`/`.
file_areas` (`netbbs.link.protocol`) each list *every* board/channel/file-area
ID the requester currently carries -- not a curated subset. Before issue #94,
the responder side (`board_event_diff`/`channel_event_diff`/`file_area_event_
diff`) only ever answered IDs present as *keys* in that dict, so a board this
node carried but the requester never mentioned was silently skipped -- correct
for "requester deliberately excluded it," wrong for the far more common
"requester has zero carried boards and its request is honestly empty," which
is exactly the state of any newly onboarded node. Combined with `netbbs.link.
sync`'s own early-exit (never sending the request at all when the local
inventory was empty) and the fact that direct origin-push (`load_own_board_
events`) only re-pushes *self-originated* events, a node not directly seeded
by a board's origin had no path to ever discover a genuinely new board,
channel, or file area -- regardless of how many verified peers or sync cycles
passed. Fixed by having each `*_event_diff` walk `requested ∪ carried` (an ID
this node carries but the request omits gets full disclosure, since omission
now unambiguously means "unknown to requester") and by always sending the
inventory request even when empty. Safe under existing invariants without new
gating: `handle_events`'s "no relay from a stranger" check already requires a
`board_genesis`'s *origin* (not sender) to be independently known to the
*receiver*, and `persist_accepted_events` already enforces `max_carried_
boards`/etc. on intake -- both were already wired up for exactly this case,
just never exercised. The existing deterministic regression test for this
scenario (`test_a_node_converges_via_multi_hop_inventory_when_the_origin_is_
already_known`) had hand-constructed the requester's inventory request
(`{"existing-local-board-id": []}`) instead of deriving it from the real
`build_inventory_request`, which is exactly what let this ship unnoticed --
a lesson for any future multi-hop/relay test: build the request through the
same code path a real node would, not a hand-picked stand-in for "the
requester already knows what to ask about."

### WAN reachability and relay selection

**The trust model and dial reliability are deliberately separate.** Phase 4's
local foundation lives in `netbbs.link.trust`: stable node/user subjects,
dimension-scoped inputs, explicit reporter domains, vouches, overrides, and a
transactionally maintained effective-state/audit projection. It accepts only
inputs a caller has already authenticated; signed wire objects and ingestion
remain the next protocol layer, not hidden inside this local domain API.
`netbbs.link.reliability` remains a minimal direct-observation tracker (dial
attempts/successes per fingerprint, neutral prior when unobserved) for fallback
and relay selection. Its score must never become a security or content
reputation input.

**Signed trust transport remains an explicit-subscription boundary.** Durable
`trust_signal`/revocation/vouch objects live in their own immutable carrier
store, never in `link_events`; a carrier serves the original envelope and
signature unchanged and gains no reporter authority. Pull requests are signed,
fresh, replay-bounded, page/byte-limited exchanges with completed peers. The
stable issuer fingerprint is distinct from its currently authorized
operational signing key. Digest-only evidence counts against ingress quotas but
does not enter the trust-policy tables until a bounded same-origin fetch has
matched its signed size/hash and category-specific code has independently
reproduced the claim. A pending digest object can still be revoked, and that
revocation prevents later activation.

**Link trust enforcement is a pre-persistence, attribution-aware gate.** The
real node runtime enables it explicitly on both the HTTP server and background
sync; low-level transport/sync constructors retain an opt-out only for isolated
legacy protocol harnesses. Manual block denies even verified hello containment;
quarantine permits hello/key lifecycle and revocation-only trust pulls but no
ordinary service; probation permits hello/key lifecycle and quarter-budget
inventory. Stable public reason codes never include reporter configuration or
evidence notes. Policy follows the independently verified author/home node, not
the carrier diagnostic in `link_events.sender_fingerprint`, and browsing checks
retained Link authorship dynamically so suppression and recovery never mutate
accepted bytes. A probationary user's valid board content is persisted as
`pending`; Link surfaces with no approval projection refuse it.
Peer-list and file-chunk pull routes reuse a fresh signed empty inventory
request for current-key attribution, responder binding, freshness, and replay
protection; a URL fingerprint alone is never a policy identity.

Trust projections are derived caches, recomputed at node startup before any
listener binds. Input mutation and projection/audit transition share one SQLite
transaction. Inactive signals, observations, and vouches are pruned after 365
days by default unless an explicit retention hold exists; decision audits keep
the content IDs and rule explanation needed to understand historical
enforcement without retaining unbounded evidence blobs. Unknown versioned
categories may be retained for diagnostics but contribute no automatic policy
effect.

**Trust-domain independence needs a real acquisition-path test, not only
policy arithmetic.** The adversarial gate pulls separately signed objects from
independently keyed reporter nodes, each with its own SQLite database and real
loopback HTTP server. Two identities assigned to one local trust domain must
leave the subject probationary even though both signed objects are retained; a
report from a second domain may cross the threshold. Reopening the subscriber
database must preserve both the effective restriction and every accepted wire
object. Keep this vertical test when changing trust pulls, reporter-domain
weighting, projection reconstruction, or immutable carrier storage.

**Trust administration is a database-domain workflow, not live-Link state.**
The shared SysOp System menu dispatches every trust read and mutation through
its foreground `DatabaseLane`; it remains usable from the standalone local
admin CLI even when no live `LinkContext` exists. Subject restrictions expose
the persisted per-dimension explanation and decision history, while ordinary
callers receive only stable non-leaking policy reason codes. Manual overrides
and every safety relaxation require a reason and are audited in the same
transaction as policy recomputation. A category-scoped sole-authority
exception is a separate persisted policy object, never an overloaded domain
weight: it requires an existing matching reporter scope, visibly weakens the
two-independent-domain rule only for that exact category, and is confirmed,
audited, and reversible. Operator screens must catch stale mutations (for
example, an override already cleared elsewhere) and report them as concurrent
state changes rather than claiming success.

**Remote attestations do not turn Link identities into local users.** The
signed carrier and local acceptance projection use the stable
`TrustSubject.user(home_node_fingerprint, opaque_user_id)` identity throughout.
Local `user_attestations.link_visible` is per attribute and defaults off;
re-attestation clears it. Export code refuses a missing or non-visible local
attestation rather than trusting a caller-supplied consent flag. On receipt,
signature verification precedes persistence, while acceptance separately
checks the local attestation-authority/attribute grant, current issuer trust,
expiry/revocation, and any reasoned local override. General trust reporters are
never identity verifiers. Removing or distrusting an authority, expiring or
revoking a record, and clearing an override recompute future gate satisfaction
without deleting signed history. A SysOp accept override still requires a
current cryptographically valid record; it cannot revive expired or revoked
bytes. Remote real-name values are composed only by the resource-scoped trusted
renderer for `verified_and_displayed`, never placed in general identity labels.

**Relay consent needed a synchronous route, not a gossiped event pair.** Every
other mutual-consent exchange in this codebase (origin transfer, channel
invitations) is two independent gossiped events with no reply requirement.
Relay consent cannot work that way: the requester may itself be outgoing-only
and permanently undialable, so the *only* way it can ever learn the answer is
in the same HTTP response as its own request -- `netbbs.link.transport`'s
`/relay-consent` route, mirroring `/hello`'s own "reply carried in the
response body" shape. When designing a new request/response exchange, check
whether either party could be permanently unreachable by the other before
defaulting to the gossip-pair pattern.

**A sender can never resolve a genuinely outgoing-only recipient's relays
through `LinkNode.peers` alone.** A hello is a real TCP connection; a node
with no dialable address can never complete one with a sender who can't reach
it. The *only* way such a sender ever learns that recipient's `relays` field
is secondhand, via ordinary peer-list exchange with someone who has met them
directly -- landing in `candidate_descriptors`, never `peers`. Any relay-
routing resolution function must check both, not just the completed-peer
table other Link code paths default to.

**Relay delivery only works between nodes that have already met directly at
some point; it is not stranger discovery via introduction.** Composing a
`link_message` at all requires a known peer to resolve the recipient's
encryption key from (`netbbs.link.mail.compose_link_message`), and delivery
requires the recipient already knows the sender as a peer (`handle_events`'s
"no relay from a stranger" boundary, unchanged and still enforced even when
the bytes arrive via a relay pickup rather than directly). A relay only ever
changes *how* the bytes travel, never who is allowed to talk to whom.

**Self-healing republication needs no separate mechanism, but only takes
effect on a node's *next* hello, not its current pass's.** `LinkNode.
build_hello` reads `relays_serving_me` live, so any future hello already
reflects the current set with no explicit "republish" step. But a node's own
hello for the *current* sync pass goes out before that same pass's relay
selection runs later in the pass -- a relay granted mid-pass is not reflected
until the node's *next* pass sends its *next* hello. A test (or any other
code) that needs a freshly-granted relay visible to a third party within one
observation window must account for this one-pass lag.

**A test/setup helper's "known peer at address X" record is a live dial
target for relay selection too, not just whatever it was added for.** Giving
a node a `PeerRecord` with a real (even fabricated/unroutable) address, to
satisfy some unrelated precondition (e.g. enabling `compose_link_message`'s
encryption-key lookup), makes that fingerprint a legitimate-looking relay
*candidate* as well -- `netbbs.link.relay_selection` has no way to know the
address was never meant to be dialed. Dialing a genuinely unroutable address
can stall an entire sync pass for the length of the HTTP client timeout.
Any peer record constructed purely for an unrelated test precondition should
be `outgoing_only=True` with no address unless the test actually needs that
peer to be dialable.

**The relay mailbox's original "only `link_message`" scope silently starved
an outgoing-only sender's own acknowledgement, found live during issue #83's
dogfood run, fixed by issue #94.** `netbbs.link.relay_mailbox`'s own module
docstring used to document this as a deliberate, not-yet-built follow-up --
but "documented as a known boundary" and "harmless in practice" are different
claims, and only the first was actually true. Concretely: an outgoing-only
node's own sent mail could be delivered to a full peer just fine (the sender
dials out itself, no relay needed for that hop), but the recipient's
`link_message_accepted`/`_bounced` reply had no relay fallback at all --
`_push_pending_link_mail`'s ack loop only ever attempted a direct push, which
can never reach a genuinely outgoing-only target. The ack retried until dead-
lettered, and the *sender's own* view of mail it sent stayed on "pending"
forever, with nothing surfaced to the SysOp beyond an eventual diagnostic-log
warning -- a real, operator-visible product gap, not merely an internal one.
Fixed by widening `deposit_relay_mailbox_envelope`/`pickup_relay_mailbox_
envelopes` (and the transport-layer deposit/pickup routes) to the full
`link_message`-family shape, reconstructing the right dataclass from each
row's own stored `object_type`, and giving the ack-delivery loop the identical
relay-fallback the message-delivery loop already had. Safe under the same
existing invariants issue #94's board/channel/file-area sibling fix relied
on: `handle_events`'s "no relay from a stranger" check already requires an
ack's *signer* (`payload.recipient_node_fingerprint`, not the depositing
relay) to be an independently-known peer, so nothing new needed to gate
against abuse -- the capability was already wired up, just never exercised
from this direction. **A "documented known limitation" is a claim about
scope, not a proof that the gap is tolerable in practice** -- worth an actual
dogfood exercise before assuming either.

**`aiohttp` raises a bare `TimeoutError`, not a `ClientError` subclass, when
a `ClientTimeout` elapses -- every client-side dial in `netbbs.link.transport`
had this wrong until issue #95's own sibling fix, found live during issue
#83's dogfood run.** Each of `dial_hello`/`push_events`/`request_inventory`/
`request_peer_list`/`request_relay_consent`/`request_file_chunk`/`deposit_
into_relay_mailbox`/`pickup_from_relay_mailbox` caught `except (ClientError,
...)` only -- several of these functions' own docstrings already promised
"timeout" specifically surfaces as `LinkTransportError`, but the actual
`except` clause never matched what a real timeout raises. Net effect: a
merely *slow* peer (not even a hard failure -- exactly the "real internet
latency/jitter" class of condition this project's own dogfood plan exists to
exercise, and something loopback/deterministic-harness tests essentially
never hit) propagated an uncaught exception straight out of `run_link_sync`,
killing that node's *entire* sync loop for the rest of its uptime -- caught
and logged clearly at the top level (`netbbs.__main__` does not crash, and
says so explicitly), but Link sync itself never resumes without a manual
process restart. Fixed by adding `TimeoutError` to all eight `except` tuples.
**When wrapping a third-party async HTTP client's exceptions into a project's
own transport-error type, verify what a real, elapsed client-side timeout
actually raises for that library -- do not assume it's a subclass of that
library's own generic connection-error base class.** `tests/test_link_
transport.py::test_dial_hello_raises_link_transport_error_on_a_genuine_
timeout` proves this with a real hanging TCP listener, not a mock, and is
confirmed to fail without the fix -- the pattern any future dial-timeout
regression test in this codebase should follow, rather than trusting a
`ClientError`-only mock to stand in for what a real elapsed timeout does.

### Live relay is a raw proxy below Noise; the async relay model still does not transfer to it (issue #168)

Shipped: `netbbs.link.realtime_relay` (design doc §8.10.3). Invariants that
constrain future changes:

- **The relay holds no key material and parses nothing.** A bridge is two
  TCP legs and two byte pumps; every bound on it is a byte or time bound,
  never a frame bound. Anything that needs to *see* live traffic at a relay
  is the rejected hybrid double-hop design (§16 "Issue #168"), not an
  extension of this one.
- **The party verifies the authenticated fingerprint against the one
  `relay_ready` named, on both roles** (`transport.attach_relayed_session`).
  The responder-side check matters as much as the initiator's: Noise XX
  authenticates *someone*, and the relay chooses who shows up.
- **Consent is the standing session.** The relay bridges only two nodes
  both currently connected to it live; an outgoing-only node makes itself
  reachable by standing by at reliable nodes (`realtime_direct.
  run_reliable_anchor_connectors`, only while participation is accepted).
  A standing anchor session needs the reliable node to be `ESTABLISHED` in
  local trust -- a fresh node whose roster entries are still
  `PROBATIONARY` cannot anchor (the connector retries with backoff and
  logs nothing louder than `INFO`); this is Phase 4's existing gate, not a
  relay limitation, and is the main reason a brand-new node may see "can't
  be reached for live chat" for a while.
- **The invitation is a `relay_request` whose target is the invitee's own
  fingerprint.** Every receiver classifies a `relay_request` by that field:
  target == own fingerprint -> invitation (party side); requester == sender
  -> a request to relay (relay side); anything else is not owned by either
  and is ignored (never a strike). Adding a distinct invitation frame type
  would change the classification for every deployed peer -- don't.
- **The bridge-attach preamble is the only plaintext a real-time listener
  ever accepts** (`transport.BRIDGE_ATTACH_MAGIC`), read by peeking the
  first record before the responder handshake; `establish_noise_xx_
  responder(first_message=...)` exists so that peek is never a double
  read. A listener without a relay closes an attach connection unread.
- **Version 3, then version 4 -- never "new fields/types without a bump".**
  Version 4 makes the invitation-id echo on agreements/declines mandatory;
  a version-3 relay rejects that new field, so compatibility would otherwise
  degrade to an opaque timeout. `RealtimeFrame.__post_init__`
  rejects an unknown type *before* `_reader_loop`'s strike-counting block, so
  an older peer closes the whole session on the first new frame. Any future
  frame-type addition is a version bump for the same reason; the "extend the
  frozenset without bumping" pattern the #168 design entry cited was wrong.
- **A party honours only correlated relay frames** (`RealtimeRelayClient.
  owns_frame`): `relay_ready`/`relay_reject` from the relay it asked for the
  target it asked, or from the relay whose invitation it accepted (bounded by
  the rendezvous timeout), one attach in flight per counterpart. Without this
  any REALTIME-admitted peer could drive unbounded outbound connections to
  attacker-chosen addresses; `relay_reject.origin` exists so a node that is
  both relay and party can classify a reject.
- **A `relay_ready` attach address is pinned to the relay's own advertised
  real-time addresses** (`RealtimeRelayClient`'s `allowed_attach_addresses`,
  wired to `dialable_realtime_addresses_for_peer`). Correlation proves which
  authenticated relay spoke; the pin proves the address is that relay's --
  without it a malicious relay could make a node open a connection (and send
  the plaintext preamble) to any service it names. Every session reuse in
  `LiveDirectChat.ensure_session` re-runs the `REALTIME` policy check too, so
  a SysOp block takes effect before the next private message, not at the next
  reconnect.
- **Every relay answer echoes the requester's `relay_request` message id**
  (`request_id` on `relay_waiting`/`relay_ready`/`relay_reject`), and the
  party honours an answer only for the attempt it is currently waiting on.
  Without it a late reject to an earlier, timed-out attempt fails the retry
  (TCP orders each direction, not the cross-direction retry race).
- **The relay half re-decides `REALTIME` policy for the requester on every
  request** (`policy_refused`), and **every participating node -- full peers
  too -- stands by at the reliable nodes**; a full peer sending to an
  outgoing-only peer needs that relay session as much as the reverse, and
  `_relay_sessions` dials a reliable node on demand when none is up. A
  standing connector cycles through *every* advertised address across
  attempts; a stale first address must not pin it.
- **`attach_relayed_session` closes its socket on any exit before a session
  owns it -- cancellation included.** A caller's own rendezvous timeout can
  fire mid-handshake; the `except BaseException` there is deliberate.
- **Receiving a node-presence snapshot tracks the session.** Server-side
  `track_session` used to be reached only via `subscribe`; direct-message and
  anchor sessions never subscribe, so their presence was stored with no close
  watcher and never answered. `_handle_node_presence_snapshot/_delta` now
  track (idempotent).
- **Chained bridges (issue #270) classify frames by their fields, never by
  who sent them alone.** A `relay_request` is: a direct request when
  `requester == sender`; an invitation when `target == own`; a *forwarded*
  request when `hops >= 1` and neither of the above. A `relay_ready` with
  `for_fingerprint` is addressed to a forwarding relay, which correlates it
  on `(sender, for_fingerprint, peer_fingerprint)`; `relay_reject.origin`
  plus the forwarded table decides whether the relay half or the party half
  owns a reject. Adding a hop means adding a field that classifies, not a
  new frame type (the bump lesson above still applies).
- **A forwarding relay's upstream leg is a raw pipe with no token of its
  own.** R1 opens the attach connection to R2 itself, so R1's own
  rendezvous for the pair carries exactly one attach token (the requester's)
  and the upstream socket is stored as the "target" leg. `_start_bridge`'s
  pair-cap re-check and `_fail_rendezvous`'s leg cleanup cover it like any
  other leg; nothing in `attach()` assumes two tokens.
- **A known peer's descriptor can be refreshed secondhand, but only when it
  verifies.** `handle_peer_list` used to ignore any fingerprint already in
  `peers`; two outgoing-only nodes never exchange a direct hello, so an
  upgraded peer's `live_relays` could never reach a requester. A newer
  descriptor for a known peer now replaces the record iff it verifies
  against the signing key already on file (the same check a repeated
  hello gets), and `request_peer_list` persists the peer record; a
  stranger's descriptor stays an unverified candidate as before.
- **Rejects toward a forwarding relay name the requester.** Two requests
  through the same upstream toward the same target are otherwise
  indistinguishable; an un-named reject is honoured only when exactly one
  forwarded entry matches. A forwarded request reserves its pending slot
  *before* the upstream dial can yield, so concurrent requesters cannot
  all pass the cap and then dial.
- **A reliable node is whoever answered a hello at the roster URL**
  (`reliable_nodes.record_observed_reliable_identity`, written by
  `sync._sync_one_seed`; read via `get_observed_reliable_identities`).
  `reliable_node_fingerprints` no longer matches a peer's *self-advertised*
  HTTP address against the roster -- any completed peer could claim a roster
  address and become an anchor/approved relay on its own say-so. A test
  that wants a node treated as reliable must record the observed identity,
  not just advertise the address. The observation key includes normalized
  scheme, authority, and path: one authority may host multiple roster nodes.
- **Three attempt ids, all echoed:** the requester's `relay_request` id on
  every relay answer; the invitation's id on the target's agreement/decline
  (`_Rendezvous.invitation_id`); the forwarding relay's upstream request id
  on the upstream's answers (`_Forward.upstream_request_id`). A retry with
  a new id supersedes a still-pending stale rendezvous instead of being
  parked behind it; after supersession, an id-less legacy agreement or
  decline is ambiguous and ignored.
- **Session establishment is serialized per peer** (`LiveDirectChat.
  _establishing`): the registry's newer-wins rule would otherwise let a
  second concurrent dial close the first caller's fresh session. Both
  waiters and the winner re-check policy inside the lock, and the
  reference-counted lock entry is retired when the last user leaves. Both
  `dial_realtime_session` and `attach_relayed_session` close whatever they
  started on *any* exit before admission, cancellation included.
- **Declined participation means no on-demand relay dial either**, and
  every reused relay or target session is re-checked against `REALTIME`
  policy first -- the relay half re-checks the *target* (reported only as
  `target_unreachable`) and the forwarding peer, not just the requester.
- **A forward stays reserved (and counted) across the upstream attach**,
  and a dial or attach that outlives the reservation is dropped, never
  turned into a late rendezvous. The relay half claims only *named*
  upstream rejects (`requester_fingerprint`); an un-named reject is by
  construction addressed to the node's own party half, so a node that is
  both relay and party never misroutes one.
- **A secondhand descriptor refresh runs the same protocol-version gate a
  hello does** before the record is replaced.
- **`live_relays` is advertised from `AnchorState`, not the registry
  alone.** The anchor task sets which reliable nodes it *intends* to stand
  by at; the hello provider filters that to sessions actually up. A relay
  that appears in a peer's `live_relays` but refuses this node is an
  ordinary failed rendezvous, never a trust signal.
- **Both session handlers must carry the same `LinkContext`.** The SSH
  handler had been constructed without the real-time registry/bridge since
  issue #148 (an SSH caller silently had no live chat); fixed while adding
  `relay`/`direct_chat`. A test that asserts live-chat behavior through
  one transport proves nothing about the other.

Testing: `tests/test_link_realtime_relay.py` runs three real nodes on
loopback (relay + two parties with no listener of their own) through the
whole rendezvous, the Noise handshake through the relay, delivery, and
every bound -- including a forged rendezvous that pairs a party with the
wrong counterpart, which the fingerprint check refuses.

Historical context, still accurate:

`relay_mailbox.py`/`relay_selection.py` are pure store-and-forward: a relay
accepts one opaque, already-encrypted envelope it structurally cannot
decrypt, bounded per recipient, held until polled and deleted on pickup --
zero ongoing cost once deposited. Real-time chat sits on
`LinkRealtimeSession`, a mutually-authenticated Noise XX channel between
exactly two directly-handshaked endpoints; the send/receive ciphers are
known only to those two parties, so **a third node cannot transparently
pass frames through an existing session -- it structurally lacks the key
material.** Neither the async model's bounded-storage shape nor its
reliability-ranked relay-selection shape transfers to live traffic, which
must hold open bidirectional state and consume live bandwidth for an
entire conversation's duration instead. Design doc §8.10 already names
this as a deliberately deferred, separate future protocol, not a gap
nobody noticed.

The security-model fork this implies -- double-hop relay-as-participant
(needs an *additional* application-layer encryption hop between the two
actual chat participants to keep true end-to-end confidentiality once a
relay can decrypt/re-encrypt) versus a raw-socket/below-Noise blind proxy
(preserves the existing two-party mutual-auth property untouched, but the
relay isn't a Link-protocol participant in this shape and doesn't fit the
existing consent/reliability-selection model at all) -- is now decided:
raw-socket/TCP-level proxy, recorded in the design doc (§16, "Issue #168").
Whichever design ships, the completed handshake's authenticated fingerprint
must still be checked against the fingerprint that was actually requested
before either endpoint uses the session -- a relay that terminates the
connection itself, or pairs the requester with a different locally-trusted
node, is otherwise indistinguishable from the intended peer even though
Noise itself authenticated correctly (the same binding gap
`dial_realtime_session`'s `expected_fingerprint` parameter closes for
today's direct, non-relayed dialing). Implementation (rendezvous protocol,
bounded-resource limits, the pre-ship fallback experience for two mutually-
unreachable nodes) has not been built -- see issue #168 for what remains.

### Node-wide presence and scrollback trust visibility (issue #164)

`LiveChannelBridge.track_session` only ever gets called for a session that
became relevant to *some* channel (an inbound `subscribe`, or an outbound
dial via `ensure_live_subscription`) -- a session can exist in `LinkRealtime
SessionRegistry` (admitted at the Noise handshake, before any application
frame) without the bridge ever learning about it if literally no channel
activity ever occurs on it. Node-wide presence's push-on-connect snapshot
therefore only actually fires for sessions channel activity already
establishes -- an accepted v1 scope boundary, not a bug, since real-time
sessions in this vertical are inherently tied to channel linking in the
first place (nothing dials a peer "just for presence"). `broadcast_node_
presence_live` itself does *not* have this limitation -- it reads from the
registry directly, reaching every connected peer regardless of channel
subscription.

No new trust check was needed for node-wide presence specifically:
establishing a live session at all already requires `ESTABLISHED` transport
trust (`decide_node_action(..., LinkPolicyAction.REALTIME)`), so the gate
is inherited for free at the transport layer. Presence frames still
re-check fresh per design doc §8.10.2's "authorization is checked again at
message delivery" principle, since a session can outlive the peer's trust
degrading below `ESTABLISHED` -- it isn't force-closed the instant that
happens.

**`channel_messages.author_fingerprint` is not the author's identity for a
Link-materialized message -- it is always `NULL` for one.**
`materialize_carried_channel_message` hardcodes it `NULL` on insert; the
real author identity lives in the signed event `channel_messages.
link_content_id` points at (`link_events.content_id`), the same column
`materialize_carried_channel_message`'s own docstring already documents as
existing purely for idempotency dedup. A trust/visibility check for
channel scrollback must key on `link_content_id` and reuse
`netbbs.link.enforcement.link_content_visible` (exactly as board posts
already do via `post_id`) -- not attempt to read `author_fingerprint`
directly, which would silently pass every linked message as "untrusted
author unknown" or worse, appear to work for local messages while doing
nothing for the linked ones a filter is actually meant to catch.

### Scrollback-on-join is a race against `ensure_live_subscription`'s own no-reply-wait contract (issue #194)

`ensure_live_subscription` sends `subscribe` and returns as soon as that send
completes -- it never waits for any reply frame (design doc §8.10.1:
"message-passing, not request/response"). Both `presence_snapshot` and
`scrollback_snapshot` arrive later, asynchronously, via the live session's own
background frame reader. A caller that wants to *render* the scrollback
snapshot (unlike presence, which is read on demand by `/who` and tolerates
arriving whenever) must therefore poll for it after subscribing rather than
assume it is already there -- `_deliver_remote_scrollback_snapshot`
(`netbbs.net.chat_flow`) does a short bounded poll (~2 seconds,
`_REMOTE_SCROLLBACK_POLL_ATTEMPTS` x `_REMOTE_SCROLLBACK_POLL_INTERVAL_
SECONDS`) against `LiveChannelBridge.pop_channel_scrollback`, then gives up
silently. Giving up is correct, not a degraded failure mode: the existing
async catch-up path (issue #85) still fills in anything missed, on its own
schedule, exactly as it already does for a caller that gets no live
subscription at all.

Each subscribe frame's message ID is also the snapshot `request_id`.
`LiveChannelBridge` registers that attempt before sending, stores replies by
request ID rather than channel ID, and invalidates the attempt on pickup,
timeout, cancellation, send failure, or session close. A late reply therefore
cannot become history for a later caller joining the same channel.

**A frame bundling many entries needs much tighter per-entry bounds than a
frame carrying exactly one.** `channel_message`'s single-message body bound
(4000 bytes) would blow `scrollback_snapshot`'s 16 KiB frame ceiling many
times over if reused per-entry across up to
`_REALTIME_MAX_SCROLLBACK_SNAPSHOT_ENTRIES` (20) bundled entries --
`protocol.py` gives the bundled case its own, much smaller per-entry body
bound (400 bytes), but field bounds are not sufficient: JSON escaping can
expand quotes and backslashes. The builder serializes the complete frame
before it may enter the transport queue, and the sender drops oldest catch-up
entries until the encoded frame fits. A shortened body carries an explicit
`body_truncated` flag and renders a visible notice; its full durable copy is
still independently in flight through the existing async path only for durable
message events. The notice therefore says only that the join snapshot was
truncated; actions and other transient events must not promise later sync.

**Snapshot attribution is identity-bearing, not label-only.** Carried entries
preserve their already-qualified display label and derive author node/user
identity from the locally retained signed event. Origin-local entries qualify
their bare stored label as `user@origin` before transmission. The subscriber
uses those identity fields for its own trust decision and the content ID for
deduplication, then reconstructs the displayed `user@node` label from those
attested fields rather than trusting `author_label`. Moderation rows store the
target label for sentence rendering, so snapshots carry them as authorless
system events; target trust policy must not hide audit history. This payload is
real-time protocol v2. Version 1 already carried request correlation and
separate author node/user identity; the incompatible v2 changes are that
authored display labels are reconstructed from those identity fields and
moderation target rows now carry null author identity. The Noise identity
payload advertises the application version so mixed peers fail the
authenticated handshake before a session is reported live. That typed mismatch
propagates through `ensure_live_subscription` to a caller-visible upgrade
message; ordinary network failures remain best-effort `None`. Per-frame version
checks remain defense in depth.

### Reliable-node onboarding: tri-state Link enablement and the name gate (issue #219)

`netbbs.net.nodeconfig.LinkConfig.enabled` is `bool | None`, and `None` is
the shipped default. Only `netbbs.__main__.run` resolves it -- once, right
after the node identity loads, via `netbbs.link.onboarding.resolve_link_
enabled` (explicit wins; silent config defers to the SysOp's node-wide
participation decision) -- and then writes the effective bool back into
`config` with `dataclasses.replace`. Every consumer after that point
(`load_link_node`, the diagnostic handler, `_start_servers`, the sync task)
reads a plain bool; nothing else may read the tri-state. `NodeConfig.
validate()` runs the Link-field checks (`validate_link()`) only for an
explicit `True` -- a stray `[link]` table must not stop a local-only node
that loaded fine before -- and `run()` calls `validate_link()` again once a
silent config resolves to enabled, so a bad Link value still fails clearly
before anything binds. `describe_insecure_bindings()` is likewise logged
only after resolution, or a silent-config full peer would never get its
warning.

The reliable-nodes roster (`netbbs.link.reliable_nodes`) is dialed only
while `participation_accepted(db)` -- re-read every sync pass, never
captured at startup -- regardless of *how* Link came to be enabled. An
explicit `enabled = true` with a declined (or never-answered) participation
keeps operator seeds only; a node upgraded in place therefore never starts
dialing project infrastructure until a SysOp says so. A successful roster
fetch replaces the built-in fallback outright; the fallback is only ever
served while no fetch has succeeded, so a node removed from the live list
actually stops being dialed.

Decision 6 (no Link participation under the placeholder display name) is
enforced in `run()` as a `StartupError`, after enablement is resolved and
before `load_link_node`. Consequences for tests: any lifecycle test that
enables Link must set a display name (`tests/test_main_lifecycle.py`'s
`_config` helper does this centrally), and any scripted flow that reaches
the first-run screen (`netbbs.net.onboarding_flow.offer_onboarding`, at
`netbbs.admin`'s bootstrap and the first SysOp login) must budget its
keystrokes: participation first (with a name prompt when the node is still
the placeholder and the answer is yes), then the managed-DNS choice. Both
choices now default to accept on a bare Enter.

The onboarding screen's "what did accepting do" explanation reads the
configured tri-state back from `link_configured_enabled`, a config-table
cache `run()` writes each startup -- `netbbs.admin` and the login flow have
a `db` but never a `NodeConfig`, so this is the only way they can tell the
truth about whether the answer decides Link on this node. It reads as
"unknown" until the node has started once on a build with this feature.

### Current distribution limit

Configured-seed sync currently sends the complete supported outbound event set
on each pass. This is deliberately simple and relies on idempotent acceptance.

Peer-list exchange exists (a node shares its own verified peers' endpoint
descriptors with anyone it has itself completed a hello with), feeding an
unverified candidate pool (`LinkNode.candidate_descriptors`). `run_link_sync`
falls back to a small random sample of it (bounded,
`_MAX_CANDIDATE_FALLBACK_ATTEMPTS`) only when every configured/cached seed
fails a given pass -- never a first resort, and never more than one
successful reconnection per pass.

**Inventory/pull-based catch-up and multi-hop relay (design doc §8.8, issue
#85).** Each sync pass, in addition to the push above, a node also sends
every reached seed one `InventoryRequest` listing every linked board,
channel, and file-area catalogue it carries plus the known content IDs for
each. The responder walks its carried resources too, so an ID absent from
the request is treated as wholly unknown and returned from genesis onward.
That makes both missed-event catch-up and empty-inventory discovery genuinely
multi-hop: a node that merely carries resource X can introduce it to a third
node, subject to the receiver already knowing and verifying X's origin.

Inventory enumeration is security-sensitive. The requester's operational key
signs the requester, intended responder, timestamp, random 128-bit nonce, and
all inventory dictionaries. The responder requires a completed hello, exact
destination binding, a five-minute freshness window, and a nonce not present
in its bounded 4,096-entry process-local replay cache. Timestamp freshness
keeps captured requests short-lived across restart; the nonce cache closes
same-window exact replay. Keep these checks ahead of diff/database work.

**This required one correctness fix to `handle_events` itself, not zero
protocol changes.** Every board-scoped acceptance branch previously
resolved the signing key to verify against from the wire-level
`sender_fingerprint`, and required it to equal the content's own claimed
origin/author -- correct for direct delivery, but structurally
incompatible with relay, since a relayed event's wire sender is a
different node than its author. Each branch now resolves against the
content's own claimed origin/author fingerprint instead, gated on that
fingerprint *independently* already being a peer this node has completed
a hello with at some point (`self.peers.get(...)`, raising the same "no
relay from a stranger" error otherwise) -- the wire-level sender must
still itself be a completed peer too, unchanged. This preserves the exact
same safety property (nothing accepted whose signing key can't be
independently verified via this node's own prior trust) while correctly
relocating which fingerprint that check applies to. A real implication:
a receiving node can only accept relayed content whose author it has
*at some point* directly verified via its own hello -- relay substitutes
for content delivery, never for identity verification. `key_transition`
and the `link_message` family are untouched -- messages remain
point-to-point by design and were never in scope for this.

**Event/dedup retention (design doc §8.9, issue #86).** Before any purging
could be provably safe, `handle_events`' own chain-idempotency had a real
gap: `board_origin_transfer_offer`/`_accepted` were the only two
board-scoped types whose resend-safety depended solely on the fast
`known_event_ids` cache, unlike `key_transition`/`board_post_edit`'s own
self-heal against authoritative state (`sender.transitions`/`post_edits`).
A cache purge would have made a legitimate resend of a still-pending offer
or an already-accepted transfer misread as a genuine conflict and rejected
-- never mis-applied, but not the idempotent no-op it should be either.
Closed with the same self-heal shape: check the incoming event's own
`content_id` against `pending_offer`/`board_lifecycle_head` before
treating a second sighting as a conflict.

Tracing what depends on each object type's `link_events` row surviving
(restart reconstruction via `load_link_node`, and issue #85's own inventory
diff) found only `key_transition` genuinely redundant with an
already-durable separate source: `link_peers.transitions_json`, not the
`link_events` row, is what `load_link_node` actually reconstructs `sender.
transitions` from. Every board-scoped type -- including `board_genesis`,
which turned out to already be redundant with `boards.link_genesis_json`
but is deliberately left unpurged anyway to keep the rule simple -- stays
unbounded: `board_post`/`board_post_edit`'s `link_events` row is the *only*
durable record for a peer-received (not self-authored) post/edit, needed
both by `board_post_edit`'s own root-post lookup and by inventory serving;
`board_origin_transfer_offer`/`_accepted` are the *only* source
`board_lifecycle_head`/`pending_origin_transfers` reconstruct from for a
peer-received transfer. `netbbs.link.store.purge_expired_key_transitions`
purges `key_transition` rows past a fixed 90-day window, called inline on
every accepted `key_transition` write -- the same "purge on write, same
table" shape `LinkDiagnosticLogHandler.emit` already established for
`link_diagnostic_log`, not a separate scheduled task.

**Linked channels (design doc §9.6, issue #87).** `netbbs.link.channels`
mirrors `netbbs.link.boards` closely -- two differences follow directly
from how local channels already differ from local boards, not from
anything Link-specific: no edit chain (channel messages have no local edit
concept at all) and no origin-succession event types (reused by reference
from §9.4's existing model, not built). `channel_messages.id` is a plain
autoincrement with no existing content-addressed column the way
`posts.post_id` already is one -- idempotent materialization needed a new
`link_content_id` column to key off instead. Carried channel content is
subject to the same bounded-scrollback trim local content already has;
this is a deliberate consequence of treating a linked channel as genuinely
the same kind of resource as a local one, not a data-loss surprise unique
to Link. `queue_channel_message_if_linked` (the self-authored outbound
path, mirroring `queue_board_post_if_linked`) exists and is tested but is
**not wired into `netbbs.net.chat_flow`'s live interactive send path** --
that file's message-send code has no existing `link_context` threading at
all (unlike `netbbs.net.login_flow`'s board-post path), and adding it
means threading a new parameter through several nested layers. A
self-authored message on a linked channel does not yet actually leave the
node through the live TUI as a result; received content still
materializes and is browsable correctly. Worth a small, scoped follow-up
issue rather than silently assuming this gap doesn't exist.

**Board closure, moderator edits, tombstones (design doc §9.5, issue #88).**
All three new event types (`board_closure`, `board_post_moderator_edit`,
`board_post_tombstone`) reuse `board_origin_transfer_offer`'s existing
verification shape (resolve the board's current origin, confirm it's an
independently-known peer, verify against its current signing key) rather
than inventing a new authorization primitive -- the two post-scoped types
carry no `author` field at all, since verification is against the origin,
never the edited post's own author. The actual gate is a local
`BoardPermission.EDIT`/`DELETE` check performed once, on the origin node,
before `netbbs.link.boards.queue_board_post_moderator_edit_if_linked`/
`queue_board_post_tombstone_if_linked` ever build and sign the event -- a
carrying (non-origin) node's own local moderator action on a post it
doesn't own stays purely local, never propagated, since it has no origin
authority to assert. This is deliberately *not* the general "linked-board
moderator grants/revocations" feature (delegating that authority to a
non-origin node) -- that remains out of scope.

`board_closure` extends the *same* `board_lifecycle_head` chain
`board_origin_transfer_offer`/`_accepted` already extend, and is terminal:
`handle_events` refuses any further lifecycle event (a fresh offer, or a
second closure) for a closed board_id. `board_post_moderator_edit`/`board_
post_tombstone` extend the *same* per-post chain `board_post_edit` already
does -- a tombstone is terminal for its own chain the same way, refusing any
further edit of any kind past it (`BoardEventState.is_tombstoned`, checked
by all three edit-chain-extending branches, not just the new ones).

`posts.tombstoned_at` is a plain, nullable `ALTER TABLE ADD COLUMN` --
deliberately **not** a `posts.status` CHECK-widening rebuild, the pattern
used ~4 times elsewhere for that same column. A much earlier migration
(the one adding `root_post_id`/`edit_of_post_id`) already found and
documented that rebuilding `posts` is specifically unsafe: it's a live
*self-referencing* FK parent (`parent_post_id`/`root_post_id`/`edit_of_
post_id` all reference `posts.post_id`), and SQLite's `DROP TABLE` (the
rebuild pattern's first step) applies FK cascade/SET-NULL side effects to
any row still referencing the dropped table, independent of that column's
declared `ON DELETE` behavior. **Any future issue that wants to add a new
`posts` (or similarly self-referencing) status/state value should reach for
an additive nullable column first, not assume the existing CHECK-widening
rebuild pattern is still safe to reuse.** `netbbs.boards.posts.
tombstone_post` is a genuinely new local function, not a repurposed
`delete_post` -- it inserts a further content-addressed revision
(placeholder content, `tombstoned_at` set) rather than removing the row, so
the edit chain and any reply's `parent_post_id` stay intact; `delete_post`
itself is unchanged, still reserved for a still-`'pending'` post's
rejection.

A real bug found by writing the UI-level test for this issue, not by
inspection: `netbbs.boards.posts._resolve_current_version` (which builds
the `Post` a reader/menu actually sees, substituting the latest revision's
`subject`/`body` onto the root row via `dataclasses.replace`) forgot to
also substitute `tombstoned_at` -- a tombstoned post displayed its
placeholder content correctly, but `_can_edit_post`/`_can_tombstone_post`
still read `tombstoned_at=None` off the never-tombstoned root row, wrongly
keeping `[E]dit`/`[T]ombstone` on offer for it. Worth remembering for any
future field added to a post revision: `_resolve_current_version` must
explicitly carry over *every* field that can legitimately differ on the
latest revision, not just the ones the feature adding it happened to think
of first.

**Remote file catalogue and chunk transfer (design doc §11, issue #89).**
The catalogue half (`file_area_genesis`/`file_descriptor`) mirrors boards/
channels exactly and gossips through `handle_events` the same way; chunk
transfer is structurally different from everything else in `netbbs.link`
built so far and is worth remembering as its own category: a direct
point-to-point pull against one specific peer (the file's own origin),
never gossiped, never a candidate extension of a shared chain, and
therefore never routed through `handle_events` at all. `netbbs.link.
file_transfer` is deliberately `db`-first and I/O-free, the same
`netbbs.link.boards`/`.channels` split; `netbbs.link.transport` is the only
place holding the real `aiohttp` session, mirroring `request_inventory`'s own
"I/O and parsing only, caller verifies" division of responsibility.

A real bug, not caught until writing tests: `materialize_carried_file_
descriptor` first keyed `remote_files.file_id` off `descriptor.content_id`
(the signed *event's* envelope hash) instead of `descriptor.payload
["file_id"]` (the file's own local content-addressed identity, computed by
`netbbs.files.entries.upload_file` the same way it always has been) — two
different hashes for the same conceptual object. This class of mistake is
easy to make for any future event type that (unlike `BoardPost`, where the
event's own `content_id` *is* the object's whole identity) carries an
independently-computed local id in its payload specifically because the
underlying local resource already had one before Link existed — always key
local materialization off the payload's own declared id in that case, never
the event's `content_id`, and add a test that actually exercises the
materialized row's cross-reference back to the origin's own local table
(this session's bug produced a `remote_files` row that looked correct in
isolation but could never actually resolve a chunk request, since
`build_chunk_for_serving` looks the id up against `files.file_id` on the
origin, which never matches an event's own envelope hash).

`remote_files` (catalogue metadata, possibly not yet fetched) is
deliberately a separate table from `files`, never a row in it — `netbbs.
files.entries`'s own stated invariant ("a file row is only ever created
after its bytes are already safely written to storage") stays true
unconditionally this way, rather than special-casing that table to tolerate
a state it was never designed for. Once a chunk transfer completes and
verifies, content is promoted into a genuine `files` row via the *existing*
`netbbs.files.storage.move_temp_file_into_storage` content-addressed path
— the same "reuse existing storage rather than parallel plumbing" choice
made throughout this codebase.

Bounding chunk transfer needed one new shape not used elsewhere yet:
`LinkServer`'s per-peer concurrent-transfer counter
(`_active_transfers_by_peer`) is in-memory only, deliberately never
persisted, since serving one chunk is otherwise fully stateless and a
restart harmlessly resets every peer back to zero in flight — worth
remembering as a legitimate alternative to a DB-backed quota for any future
bound whose only purpose is limiting concurrent *service*, not tracking
durable state.

**Wiring linked-channel messages into the live chat send path (issue
#91).** A small, mechanical follow-up, not a new design: `netbbs.net.
chat_flow._chat_loop`/`browse_channels` gained an optional `link_context`
parameter, threaded down exactly the way `netbbs.net.login_flow._show_
board`'s own parameter already works for board posts, and `netbbs.link.
channels.queue_channel_message_if_linked` is called right after a
self-authored message is recorded — fire-and-forget, no user-facing
success/failure distinction at the queue step, matching `queue_board_post_
if_linked`'s own call site precedent exactly (the real propagation/failure
handling lives entirely in `netbbs.link.sync`'s background loop). Worth
noting for any future "wire X into an existing interactive loop" issue: the
loop itself (`_chat_loop`) needed no other change at all once the
parameter existed — the actual queuing call is one `if link_context is not
None: await lane.run(...)` block at the single message-persist call site,
not a refactor. Testing it required driving the *real* `_chat_loop` with a
scripted `FakeSession` (borrowed from `test_chat_flow_moderation.py`) rather
than calling `queue_channel_message_if_linked` directly, since the point of
the issue was proving the interactive path itself, not the already-tested
domain function underneath it.

**Interactive browse/fetch UI for remote file catalogues (issue #92).**
`netbbs.net.file_flow` (and every other `netbbs.net.*` module) loads
unconditionally on every node, including ones with no `aiohttp` installed
(it is an optional extra, `pip install netbbs[web]`) — so `aiohttp` and
`netbbs.link.transport` must never be imported at that module's top level.
The existing convention (already used in `netbbs.__main__`'s own Link-
server startup) is to import them lazily inside the one function that
actually performs a transfer; `_fetch_remote_file` follows the same
pattern rather than inventing a new one. A `RemoteFile` catalogue entry
needed no additional per-file access check beyond what already gates
reaching the containing area — the caller's own picker has already applied
`meets_level`/`meets_age`/etc. before a user ever sees the area, so merely
knowing a descriptor exists cannot bypass local policy. One UI-shape lesson
worth keeping for any future "attach a command to an existing listing"
work: `_show_area` had two separate display paths (the normal paginated
loop and a separate fallback prompt for "no files yet"), and a Linked area
can have remote catalogue entries while having zero local uploads — the
new `/remote` command had to be wired into *both* branches, not just the
main loop, or it would silently be unreachable for exactly the areas most
likely to have something worth fetching.

**Extending inventory/pull catch-up to file-area catalogues (issue #93).**
Adding a third scope to an existing generalized mechanism (`InventoryRequest.
file_areas`, alongside `boards`/`channels`) surfaced two easy-to-miss spots
that a mechanical "just mirror channels" pass would otherwise skip: first,
`link_events.file_area_id` needed populating from *two* call sites, not
one, because `file_area_genesis` and `file_descriptor` don't share a single
insert path the way `board_genesis`/`channel_genesis` alone needed handling
for `board_id`/`channel_id` — `file_descriptor` (like `board_post`/
`channel_message` before it) skips `netbbs.link.store.save_event` entirely
and does its own direct `link_events` insert in `netbbs.link.files.
materialize_carried_file_descriptor`. Any future object type that both (a)
needs a new scoping column and (b) has a sibling type that bypasses
`save_event` must check both insert sites, not just extend `save_event`
and assume that's the whole story.

Second, `netbbs.link.sync`'s own inventory-response handling had never been
threaded with `max_carried_file_areas`/`max_remote_files_per_area` at all
(unlike `max_carried_boards`/`max_carried_channels`, threaded since issues
#85/#87) — a real pre-existing gap, not a hypothetical one: `LinkServer`'s
direct-push path already enforced both quotas (§13.9), but the inventory
pull path silently didn't, simply because inventory had never asked about
file areas before this issue existed for it to matter. Worth checking for
on any future issue that adds a new carried-resource type to `netbbs.link.
sync`'s inventory step: grep for where its sibling quotas are threaded
through `_sync_one_seed`/`run_link_sync`/`__main__.py`, since a quota that
already exists for the direct-push route is easy to assume is "already
handled everywhere" when it was only ever wired for the routes that existed
when it was added.

No restart-reconstruction changes were needed for this issue at all —
issue #89's own `load_link_node` work already rebuilt `node.file_areas`
from both sources this issue's own diff query reads, and `file_descriptor`
has no chain state to rebuild in the first place. Confirmed by tracing
before writing any code, not assumed: the same "what does load_link_node
and the inventory diff depend on" check issue #86's own worklog entry
recommends doing before assuming a new event type's state is purgeable or
already restart-safe.

### Not every retry-shaped mechanism fits a generic work-item/DLQ model

Designing issue #60's outbound-work-item abstraction (§13.7) required
auditing every existing retry-shaped mechanism in `netbbs.link` first, and
two of them turned out not to fit despite looking superficially similar:

- **Board/identity event gossip** (`netbbs.link.sync`) re-pushes every
  node-owned event to every seed, every pass, forever, with no per-peer
  state — deliberate, not a gap, since the receiving side's own dedup
  (`link_events`) makes redundant delivery free, and there is no correct
  "give up" state for a node's own content.
- **Relay selection/consent maintenance** continuously re-evaluates
  candidates against an evolving reliability score — ongoing
  re-optimization among many candidates, not one item that must resolve
  once. It already has its own working retry-like model (score-driven
  re-ranking); a second, differently-shaped retry abstraction bolted on
  top would just compete with it.

Only Link mail delivery and Link mail acknowledgement delivery actually
fit: a specific payload addressed to a specific fingerprint, needing
confirm-or-abandon semantics, currently missing exactly that (both retry
forever today with zero cap — a real gap, not a deliberate choice, unlike
gossip above). **The lesson for future "let's generalize this" work**:
resemblance in surface behavior ("this also retries on failure") isn't
enough — check whether the mechanism has a per-target item with a
meaningful terminal state before folding it into a shared abstraction, or
the abstraction ends up modeling a failure mode that was never real.

A second, easy-to-miss distinction found in the same design pass: a work
item resolving successfully means "the payload was pushed to the
recipient's transport/relay," never "the recipient confirmed receipt."
For Link mail specifically, confirmed receipt is a separate, existing
concept (`apply_link_message_accepted`/`apply_link_message_bounced`,
driven by a genuine signed event coming back) that has nothing to do with
whether a given push attempt succeeded. Conflating "pushed" with
"delivered" was a real mistake in an early draft of this design, caught
before implementation — worth remembering for any future retry/delivery
abstraction: transport-level success and domain-level confirmation are
almost always two different questions with two different failure modes.

### Every outbound Link `aiohttp.ClientSession` must set `trust_env=True`

`aiohttp.ClientSession()` defaults to `trust_env=False` — it silently
ignores `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`. A node whose only
outbound path is a forward proxy (a corporate Squid array, for
instance — the scenario that surfaced this) cannot dial any seed/peer
at all under that default, with no error pointing at the cause. Both
production construction sites (`netbbs.__main__`'s Link sync session
and `netbbs.net.file_flow`'s per-fetch session for linked-file
downloads) now pass `trust_env=True`; regression coverage in
`tests/test_main_lifecycle.py::test_link_sync_session_honors_forward_proxy_env_vars`
asserts this against the real session `run()` constructs, not just in
isolation. Any future call site that constructs its own
`ClientSession` for outbound Link traffic needs the same flag — it is
easy to add a new session and forget this, since everything works
identically in every environment except one with no direct egress.

---

## 10. Operational constraints

### Backup and restore

Use SQLite's online backup API; never copy a live WAL database as if it were a
single inert file.

The live SysOp Backup screen invokes the same synchronous, path-based backup
primitive via a worker thread, using the database lane's path and the running
node's effective configured identity directory. Never guess the default
identity path in this flow: a custom identity directory omitted from a
nominally successful backup would make it non-recoverable. Standalone admin
therefore remains status-only; custom destinations and scheduling use the
CLI, and restore remains offline/CLI-only.

Revalidate that configured identity directory in the backup worker immediately
before creating the destination. Because cancellation cannot stop an
`asyncio.to_thread` filesystem operation, the live session must keep ownership
of that task, await and retrieve its outcome, and only then propagate session
cancellation. Once the backup primitive returns, report success before writing
the ancillary SysOp audit row; an audit write failure must not make a completed
filesystem backup look unsuccessful. Store the last-success timestamp and path
as one transaction so the dashboard can never pair a new timestamp with an old
generation, and reload dashboard state when the Backup quick action returns.

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

A node's recoverable state is five artifacts, not three — beyond the
database, content blobs, and node identity already named above, a complete
backup also needs the SSH host key
(`db_path.parent / f"{db_path.stem}_ssh_host_key"`, `netbbs.net.ssh.
ensure_host_key`) and the welcome banner
(`db_path.parent / f"{db_path.stem}_welcome_banner.ans"`) — both derived,
`db_path`-relative paths with no dedicated config field, easy to miss if a
backup procedure is designed by re-deriving "what does a node write to disk"
from memory rather than grepping for every `db.path.parent /` call site.
`netbbs.selfupdate.snapshot_database`/`restore_database` are the proven
primitive for the database half of this (`sqlite3.Connection.backup()`,
never a raw file copy) — reuse them rather than re-implementing; see design
doc §13.4 for the full `netbbs.backup` module design (blobs must be copied
strictly after the DB snapshot, never before or concurrently — the
DB-references-a-blob invariant only holds in that direction) and the
restore-time precondition check (`sqlite3.connect(db_path, timeout=0)` +
`BEGIN IMMEDIATE`, refusing loudly if a live process already holds the
write lock, rather than silently overwriting bytes out from under it).
Implemented and verified against a real, separately-started node process
(`netbbs.backup`): confirmed the precondition check does catch a real
concurrent writer holding `BEGIN IMMEDIATE`, and confirmed a full
create-wipe-restore cycle round-trips the database, blobs, and node
identity (same Link fingerprint, no new SSH host key generated) intact.

**Restore is staged and validated (design doc §13.10, issue #75) --
never a direct copy onto a live path again.** The `BEGIN IMMEDIATE`
probe alone only ever catches a write actually in flight at that exact
instant, not an idle-but-running node (SQLite's WAL-mode locking holds
the write lock only for a transaction's duration) -- closed by a PID
file `netbbs.__main__` writes/removes across every real exit path
(including a hard kill, which leaves it behind as a stale, correctly-
tolerated leftover rather than a permanent block -- verified live by
actually `taskkill /F`-ing a running node and confirming the next
restore both refused while the PID was genuinely still alive, and later
proceeded once it wasn't). Two invariants worth remembering for any
future validation-before-mutation code in this codebase:

- **Validating a backup must never itself mutate it.** Opening the
  original snapshot as a real `netbbs.storage.database.Database`
  applies any pending migration in place -- fine, even desirable, for a
  disposable staged copy about to be switched into place, but it would
  silently invalidate the manifest's own recorded checksum if run
  against the original backup directory, which must stay byte-identical
  across repeated validation runs. `_validate_backup_source`'s
  `allow_migrate` flag exists specifically to keep these two cases from
  being accidentally conflated into one code path.
- **A content-addressed store needs no separate manifest checksums at
  all.** `netbbs.files.storage` already names every blob after its own
  sha256 (`root/{hash[:2]}/{hash}`), so integrity verification is just
  recomputing and comparing against the filename -- no bookkeeping that
  grows with the file area, unlike every other backed-up artifact,
  which genuinely does need an explicit checksum recorded somewhere
  else.

The switch itself is a same-filesystem atomic rename per artifact (old
live content renamed into a dated rollback directory first, staged
content renamed into place second), not a copy -- proven live by
monkeypatching a mid-sequence failure and confirming every already-
switched artifact rolls back automatically, restoring the exact
pre-restore state. A small state file records progress across the
switch and is removed only once every artifact has switched or every
switched artifact has been rolled back -- if the process is killed
outright (not a catchable exception) partway through, that file is the
one deliberately-left-behind trace of an in-progress restore, and a
subsequent restore attempt refuses to start a second one over it rather
than compounding the mess.

### Managed netbbs.org subdomain + dynamic DNS is two independently-deployed components, not one (issue #201)

Design doc §16 locks seven decisions; `services/managed_dns/` (the
project-operated backend, one instance for all of netbbs.org) and
`src/netbbs/managed_dns/` (the node-side client every opted-in SysOp's
own BBS runs) are genuinely separate deployables that happen to share
this repo, the same way the already-live netbbs.org website is deployed
independently of any node install. `services/` sits outside `src/`
specifically so `pyproject.toml`'s `[tool.setuptools.packages.find]`
scoping (see the `examples/` entry just below) never packages it into a
node's own install — a SysOp who opts in talks to the backend over
HTTP, never imports it.

- **The bearer credential is not the node's Ed25519 key, and the server
  never stores it in recoverable form.** `POST /register` mints a
  separate per-registration secret server-side, returns it once, and
  persists only `hash_credential()` (SHA-256) thereafter. Every
  subsequent `/heartbeat` or `/release` call presents the raw secret;
  the server re-hashes and compares. This keeps a managed-DNS backend
  compromise from ever exposing anything that could impersonate a
  node's actual Link identity.
- **Reclaim reuses `/register` rather than adding a fourth endpoint.**
  Presenting the credential of a still-in-cooldown `released` or
  `abandoned` row (Decision 5's shared ~90-day cooldown, both exit
  paths) reactivates that exact row — `created_at`/`matured_at` history
  preserved, skipping straight back to `matured` with an immediate
  republish if it had matured before. Reclaim bypasses the admission
  *rate* limiter, but still obeys both the cumulative active cap and
  one-active-name-per-node cap because it consumes a real live slot. A
  short voluntary-release/reclaim cycle preserves a pending row's earned
  `contact_started_at`; only abandonment, no prior contact, or a gap beyond
  the abandonment threshold resets that maturation window.
- **The originally-locked human-review queue for over-cap registrations
  was dropped during implementation planning, not built.** Both the
  service-wide rate limiter and the cumulative active-registration cap
  now hard-reject immediately once exceeded, symmetric with each other.
  The reasoning (recorded in full in design doc §16 Decision 3): once a
  request would have landed in a queue at all, the realistic resolution
  is identical either way — the maintainer hears the SysOp's own
  out-of-band explanation and decides by hand — so a capacity-bounded
  queue would have added real code and a genuine single point of
  (human) failure without actually simplifying that manual conversation.
  Don't resurrect a review-queue table for this service without
  re-reading that decision first.
- **The rate limiter is a small standalone token bucket, not an import
  of `netbbs.net.throttle`'s private `_TokenBucket`.** Reaching across
  the `services/`↔`src/netbbs/` package boundary to grab a private
  symbol would couple two independently-deployed components through an
  implementation detail neither one exports; re-implementing the same
  small primitive locally (`services/managed_dns/server.py`'s
  `GlobalRateLimiter`) is worth the few duplicated lines.
  Persist its state only after a token is consumed. A rejected request does
  not change the durable bucket and must not turn a cheap over-limit flood
  into one SQLite commit per response.
- **A `pending` registration does not resolve.** Only `/heartbeat`
  transitions `pending → matured` (Decision 3's age gate,
  `min_age_seconds` of heartbeat contact) and calls
  `DnsProvider.upsert_record` for the first time; `/register` itself
  never publishes anything, no matter how the request was decided.
- **Maturation measures uninterrupted successful contact, not age since
  registration.** `contact_started_at` begins at the first heartbeat
  and resets after a gap longer than the abandonment window. A node
  cannot register, stay offline through the age gate, and mature on its
  first later contact.
- **DNS provider I/O never runs on the aiohttp event loop, and failed
  mutations stay retryable.** Static publication retries while no
  address is recorded; voluntary release remains active until deletion
  succeeds; sweep abandonment likewise waits for deletion rather than
  orphaning a stale record. Address-family changes remove the obsolete
  A/AAAA family in the same RFC 2136 update. Provider I/O and its
  surrounding SQLite transition share one bounded service-wide lane:
  sweep, heartbeat, release, and reclaim cannot commit from stale
  pre-await state, and concurrent HTTP mutations receive a retryable 503
  before any additional executor work is queued. A sweep acquires that lane
  for one revalidated row at a time and yields between rows; a large stale
  backlog must not monopolize live mutations for the duration of every DNS
  timeout in the pass.
- **The service sits behind its own reverse proxy for TLS, so
  `request.remote` is the proxy's address, not the caller's.**
  Dynamic-address change detection compares against a trusted
  `X-Forwarded-For` chain instead
  (`ManagedDnsServer(..., trust_x_forwarded_for=...)`), off by default —
  turning it on without an actual trusted proxy in front lets any
  caller spoof its own source address into the DNS record.
- **`Rfc2136DnsProvider` is real BIND integration (`dnspython`,
  RFC 2136 dynamic updates, TSIG-signed) and is backend-only** —
  `dnspython` is a `services/managed_dns/` dependency, never added to
  the installable `netbbs` package's own dependency list. The zone's
  `named.conf` needs an `allow-update` policy scoped to this TSIG key;
  that server-side BIND configuration is the operator's own step (see
  `services/managed_dns/README.md`), not something any code here
  performs. `LoggingDnsProvider` (in-memory, records intended
  mutations) is the default until TSIG env vars are actually set, and
  is what every automated test uses — no test talks to a real BIND
  server.
- **The credential is the backup manifest's 13th artifact**, via the
  same `credential_path_for(db_path)` derived-path formula every other
  plain-file artifact already uses (see `Backup and restore` above) —
  needed no new path-helper shape in `netbbs.backup`, just an import
  and one `extra_path` tuple entry. Restore recognizes that artifact by
  role and rebases it through the target database stem; preserving the
  source filename would make a `--db` rename silently lose the secret.
- **The service-wide admission bucket is durable.** Its tokens and last
  refill timestamp live in the managed-service database, so a restart
  does not mint a fresh burst of registrations.
- **Standard-ports confirmation (Decision 6) is purely informational,
  never server-enforced.** The admin screen and the opt-in prompt both
  ask whether the web listener sits behind an HTTPS-terminating reverse
  proxy on 443 before registering; a "no" still registers the record
  (useful for the Telnet/SSH dynamic-IP-tracking half alone) but shows
  a caveat that no bare web address is implied. This can't be verified
  remotely — the service has no way to confirm a proxy actually exists
  in front of a given node — so don't add a server-side check that
  pretends otherwise.

### `examples/` is not installed package data (issue #169)

`pyproject.toml`'s `[tool.setuptools.packages.find]` is scoped to
`src/` only -- nothing under repo-root `examples/` reaches a built
wheel. A file that must be reachable from a real install (not just a
source checkout) has to live under `src/netbbs/...` and be listed in
`[tool.setuptools.package-data]`, loaded via `importlib.resources`
(`netbbs.net.banner_presets` is the precedent), not read from a path
computed relative to the repo root.

This was a real, previously-unnoticed product gap, not just a style
preference: the banner/masthead sample `.ans` files used to
live only in `examples/`, so a SysOp running the actually-supported
install path (a release wheel) had no sample files on their filesystem
to copy into place at all -- `[E]nable` was reachable but had nothing
to enable without a separate GitHub checkout just to fetch art. Moving
the samples into `netbbs.net.banner_presets` and adding a `[G]allery`
picker on both admin screens closed that gap by construction (zero
filesystem access needed, identical behavior for a wheel install or a
source checkout) -- see that module's own docstring. Do not duplicate
this kind of bundled asset back into `examples/` alongside the package
copy: a second, stale copy of the same files re-documents the exact
manual-copy workflow the packaged version exists to make unnecessary.

Every banner/masthead surface with a Gallery owns a separate preset
registry and package-data directory under `netbbs.net.banner_presets`.
Keep the registry, its loader directory, and `pyproject.toml`'s package-
data globs aligned. Curate for materially different composition, density,
and silhouette within each registry; palette-only recolors create the
illusion of choice and should not be retained as separate samples.

### Self-update: checking is wired up, applying is not (issue #82)

`netbbs.selfupdate` has real, fully unit-tested plumbing to check GitHub Releases
(`check_latest_release`/`is_newer`) and download/extract a new release
tarball with a DB-snapshot-before-migration safety net and a pending/
confirm/rollback state machine (`prepare_update`/`confirm_update`/
`roll_back_update`/`download_and_extract_release`). Grepping the whole
`src/` tree confirms these four functions have **zero callers anywhere
outside `selfupdate.py` itself** — only `check_latest_release` is
actually wired into product code, and only as a read-only "is a newer
release available" check surfaced in the SysOp menu's manual
update-check screen and the daily scheduled check
(`run_scheduled_update_check`). Nothing anywhere calls `prepare_update`
to actually start applying an update.

This is confirmed intentional, not an overlooked gap:
`run_scheduled_update_check`'s own docstring already states the
apply/restart flow "isn't safely wired up yet, a real, substantially
higher-stakes decision deliberately not bundled into this." The
operator-facing upgrade path documented in
`docs/NetBBS-operator-guide.md` is therefore installing the selected
official GitHub-release wheel with pip (relying on `Database.__init__`'s
own automatic-migration-or-fail-clearly behavior for schema safety), not this
module's tarball/execv mechanism. Wiring `prepare_update`/
`confirm_update`/`roll_back_update` into an actual command someday
needs its own deliberate design pass (process re-exec semantics under a
service supervisor in particular), not an assumption that it's most of
the way there just because the pieces already exist and are tested in
isolation.

### GitHub's unauthenticated release-check rate limit, and what actually fixes it

`check_latest_release` queries `api.github.com`'s unauthenticated REST
API, capped at 60 requests/hour **per source IP**, not per-repo or
per-install. `run_scheduled_update_check` fires this immediately on
every node startup, then once every 24h — in steady production use
that's one request/day, nowhere near the limit, but rapid restarts
(an ordinary dev-loop, or a genuine crash-restart loop in production)
burn the same budget independently on every restart and exhaust it
easily. This was dogfooded directly: repeated local node
restarts during a single session reliably produced "HTTP Error 403:
rate limit exceeded."

**Conditional requests (ETag/`If-None-Match`) do not fix this.**
Confirmed directly against the real API (not from documentation): a
`304 Not Modified` response still decrements the same rate-limit
counter as an ordinary `200`, checked across several repeat requests
with the response's own `X-RateLimit-Remaining` header. This
contradicts a real but incorrect assumption made mid-implementation
(GitHub's REST API does not exempt conditional requests from the
primary limit, whatever may be true for other endpoints or eras of
the API). `load_release_cache`/`save_release_cache`'s etag caching is
still worth keeping — it saves bandwidth/JSON-parsing and gives a
clean "nothing changed" signal — but it does not bound request
*volume*, only cost-per-request. Two things actually do:

- `run_scheduled_update_check`'s `min_recheck_interval_seconds`
  cooldown (default 15 minutes) against the last recorded check
  attempt — skips the immediate on-entry check on a rapid restart.
- An optional GitHub PAT (`get_github_pat`/`set_github_pat`,
  Self-update admin screen), sent as `Authorization: Bearer <token>` —
  raises the ceiling itself, 60/hour → 5000/hour. A 401 (revoked/
  expired token) gets its own specific `UpdateError` message rather
  than the generic "could not reach" one, since the node did reach
  GitHub — the stored credential is what's wrong.

**Where a real secret goes, established here for the first time
outside node/transport identity keys:** the PAT is stored as a plain,
owner-only (`chmod 0600`) file next to the database
(`github_pat_path`), never in the plaintext `node_config` SQLite
table — the same "real secret is a colocated file, not a DB row"
pattern `netbbs.net.ssh.ensure_host_key`/`netbbs.link.node_identity`
already established for the node's own key material. No passphrase
encryption at rest (unlike `Identity.save`'s optional path): a PAT is
revocable/rotatable GitHub-side and read by an unattended background
task on every startup, so encrypting it would reintroduce the
headless-key-unlock problem that module's own docstring already flags
as unsolved, for a credential whose minimal-scope prompt copy ("Public
Repositories, read-only") is what actually bounds its blast radius.

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
- database integrity failures (`Database.check_integrity`, `PRAGMA integrity_
  check`, called once by `netbbs.__main__.run()` right after opening the
  database — deliberately not from `Database.__init__`, which every admin
  script and the entire test suite also goes through and would otherwise pay
  a full-scan cost for on every construction).

Purge only known-safe staging artifacts before accepting traffic. Unexpected
directories and symlinks are not ordinary stale upload files.

A corruption regression test must corrupt bytes actually reachable only by a
full scan, not bytes near the file header. Corrupting near the start of the
file (e.g. offset ~100) breaks `PRAGMA journal_mode = WAL` itself during
`Database.__init__`/`_configure_pragmas`, so the test never reaches the
integrity check it means to exercise — it fails for the wrong reason, before
`check_integrity()` is ever called. Insert enough real rows to span multiple
pages first, then corrupt bytes near the *end* of the file, so the damage
lands in table data an already-fully-migrated `Database.__init__` never
touches, and only an explicit full-table-scanning `PRAGMA integrity_check`
catches it.

### NetBSD/pkgsrc: a successful `cryptography` build can still fail to import at runtime

A source-built `cryptography` (operator guide §1b) can build cleanly against
pkgsrc's `openssl` and still fail every later `import asyncssh`/`import
cryptography` with `ImportError: ...bindings/_rust.abi3.so: Shared object
"libssl.so.3" not found` — confirmed on real NetBSD hardware. The build
toolchain (pkgconf) finds pkgsrc's `libssl.so.3` under `/usr/pkg/lib` fine at
*build* time; NetBSD's runtime linker does not search `/usr/pkg/lib` by
default, and the base system ships its own, differently-versioned
`/usr/lib/libssl.so.16`, which cannot satisfy the SONAME the extension was
linked against. This is a build-vs-run search-path split, not a missing
package — `pkg_info`/`find` will show `libssl.so.3` present and correct.

Fix: put `/usr/pkg/lib` on the runtime linker's search path (`/etc/ld.so.conf`
+ `ldconfig`, or `LD_LIBRARY_PATH` for a single invocation). `examples/
netbbs.rc` sets this for the rc.d service.

This is exactly the failure class `__main__.py`'s SSH-startup import handling
must not misreport: only "asyncssh is genuinely absent" may produce the
"asyncssh is not installed" warning; any other import-time failure inside
`netbbs.net.ssh` (this one included) must propagate with its real traceback,
or the actual cause — a linker search-path gap, not a missing package — is
undiagnosable from the log alone.

### Platform-specific code stays in exactly three narrow places (issue #81)

A full-repo audit (`grep` for `sys.platform`/`os.name` across `src/`)
found only three call sites with any platform branching at all:
`netbbs.net.local_terminal` (raw-mode terminal input for the local
SysOp CLI), `netbbs.backup._process_is_running` (restore's real-vs-
Windows-dev liveness probe), and `netbbs.__main__`'s signal-handler
setup (`add_signal_handler` with a `signal.signal` fallback). All three
already existed with the same shape before design doc §2.1's platform-
tier policy was written down: a narrow, isolated function, its own
`sys.platform`/`os.name` check, and a comment naming Windows as this
project's own dev/test environment, never the deployment target. The
tier policy is a formalization of practice that was already consistent
across three independently-written modules, not a correction. Any new
platform branch should keep this shape: a small, named function/module,
never a `sys.platform` check inline inside domain logic (`netbbs.
boards`, `netbbs.link`, etc., none of which have any platform
branching today and should stay that way).

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

### Python 3.12's `asyncio.Server.wait_closed()` can hang a real-socket test forever if the client never closed its own side first

A failed assertion mid-scenario in one of the many real-`TelnetServer`
integration tests (`test_picker.py` and its siblings) skips the
scenario's own `writer.close()`/`await writer.wait_closed()` lines --
the exception jumps straight to `finally: await server.stop()`. On
Python 3.12+, `asyncio.Server.wait_closed()` no longer returns once the
listening socket itself closes; it waits for every still-open accepted
connection to finish too (a deliberate CPython behavior change, not a
bug). If the *client* side of that connection was never closed, this
wait never resolves -- not a slow test, a genuine indefinite hang, with
no traceback and no output even under `-s`, easily mistaken for a bug
in whatever was actually being tested. Diagnosed here by instrumenting
`server.stop()`'s own call site with flushed `print`s: the trace showed
every other step (connect, negotiate, read, assert) completing, then
silence forever right after entering `finally`.

The actual lesson isn't "add a workaround" -- it's diagnostic: **when a
real-transport integration test in this suite hangs with zero output,
suspect a failed assertion upstream of an unclosed client socket
first**, before assuming the code under test itself is stuck. A stale
byte-exact assertion (this case: `netbbs.net.picker`'s stable-id
references gained per-page alignment padding, issue #171, and one
`test_picker.py` assertion still expected the old unpadded spacing) is
a far more common cause than a real deadlock.

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

Current work spans the Phase 3 operational-validation track and active Phase 4
implementation:

- the bounded product-track dogfood interleave before #127 is implemented:
  direct-chat polish (#134), safe composition (#133), confirmation consistency
  (#135), and visual/capability verification plus named-surface polish (#136);

- independent non-Python interoperability validation is deprioritized and
  deferred (issue #71); Python canonical vectors remain authoritative and
  public external interoperability is unclaimed;
- deciding and proving safe retention for event families which still require
  their accepted rows for restart reconstruction or inventory serving;
- completing linked-channel and linked-file-area succession/governance where
  their semantics intentionally do not yet match boards;
- Link messages: tier1_home_node_key only (server-side decryption; tier2
  needs a real client-side decryption story first);
- user-key and node-author signing tiers beyond current node-vouched users;
- local search over carried board/file/channel content (issue #56's
  remaining piece -- read/unread cursors, follows, and `[N]ew scan` are
  done, see §6 below);
- sustained multi-node dogfood continues independently, including restart and
  partition recovery (issue #83);
- implementing §12 in bounded slices: local persistence/policy (#126), signed
  subscriptions (#127), enforcement (#128), SysOp explanation/recovery
  workflows (#129), and remote attestation trust (#130) are implemented;
  automated adversarial validation/public-readiness evidence (#131) is covered,
  including real-transport domain-independence; its real-node manual and
  independently administered exercises remain pending.

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
- “The domain function and its tests exist” does not mean any live UI path
  reaches it — a passing test suite proved `link_channel`/`link_file_area`
  (issues #87/#89) worked, but a plain `grep -rn "link_channel\("
  src/netbbs/net` turned up zero call sites: the board admin screen's own
  `[L]ink this board` action was never mirrored for channels or file areas,
  so a SysOp had no way to Link either one at all (fixed in `netbbs.net.
  admin_flow`, mirroring `_link_board_screen` exactly). Worth checking for
  on any future object type that reuses an existing subsystem's domain
  layer without also reusing its UI layer — passing tests only prove the
  function works when called, not that anything calls it. The same audit
  also caught a related but different mistake in `netbbs.net.file_flow`'s
  `/remote` hint: it was gated on Link being enabled *node-wide*
  (`link_context is not None`) rather than on the specific area actually
  being Linked (`is_area_linked`), unlike the board admin screen's own
  `[L]ink`/`[T]ransfer`/`[C]lose` gating, which already drew that exact
  distinction — offering `/remote` on an area that structurally can never
  have a remote catalogue is a small honesty gap, not a crash, but the
  same class of "capability enabled somewhere in the stack" vs "capability
  applies to *this* specific resource" confusion.

**Drain/shutdown stacking, cancellation, staged reminders, visibility
(design doc §13.8.1, found during real dogfood use, not by reasoning about
the code).** A single missing piece of state (`netbbs.net.shutdown.
SequenceScheduler` — one instance per node per sequence kind) turned out
to be the root cause behind five separately-reported symptoms at once:
two independent `asyncio.create_task(run_drain_sequence(...))` calls
stacking with zero coordination, no way to cancel a scheduled sequence, no
staged reminders as the deadline approached, no way for a freshly-
connecting/freshly-logged-in user to learn a drain/lockdown was already in
effect, and no persistent on-screen indicator once a SysOp toggled
something and moved on. Worth remembering as a pattern: a cluster of
seemingly separate operational-UX complaints ("this feels buggy," "I keep
forgetting X is on," "nobody warned me twice") is worth checking for one
missing shared piece of state before treating each as its own patch —
here, one scheduler object made four of the five fixes nearly free once it
existed.

Two narrower implementation lessons from building it:

- `asyncio.Task.cancel()` only *requests* cancellation — it does not
  settle synchronously. A test (or any code) that cancels/replaces a task
  and then immediately inspects `.cancelled()` needs an intervening
  genuine event-loop suspension (`await asyncio.sleep(0)`, or any other
  real `await` that actually yields) before the cancellation has visibly
  taken effect. This bit twice while testing `SequenceScheduler.schedule`'s
  own cancel-and-replace behavior driven through a `FakeSession` whose
  `read_key`/`read_line`/`write_line` never genuinely suspend (they
  return a scripted value directly, with no real `await` inside) — the
  entire admin-menu call chain executed as one synchronous burst with no
  point where the event loop could run the replaced task's own pending
  cancellation callback, unless something *else* in the same call chain
  (a real `await lane.run(...)` dispatching to a background thread, for
  instance) happened to force a genuine suspension first.
- `MaintenanceMode.activate()`/`is_active()` had a docstring claiming
  unconditional "no way back" before this round — true of the *original*
  design (shutdown was always immediate-and-final), but a new feature
  (cancelling a still-counting-down graceful shutdown) can turn a
  previously-true "no way back" claim into "no way back, past this one
  specific point" without that being a contradiction, as long as the
  docstring is updated to say exactly where the line now is rather than
  silently falling out of date. `deactivate()` was added as a narrowly-
  scoped, single-caller exception (only `run_shutdown_sequence`'s own
  cancellation handling, only for the pre-disconnect countdown window),
  not a general-purpose undo.

**`netbbs.net.picker.pick_item`'s 2-digit-selection path was missing its
own newline (found via dogfood testing, not code review).** Every other
state-changing branch (`[B]ack`, `[N]ext`, `[P]rev`) wrote an empty line
before acting; the valid-2-digit-selection `return` did not. The
observable effect only showed up one level up the call stack, in whatever
the *caller* printed immediately after `pick_item` returned (e.g. `[W]ho`'s
own "Disconnect 'x'? [y/N]: " landing directly after the echoed "02" with
no separation at all) — `pick_item`'s own tests never caught it because
none of them asserted on what came *after* a successful return, only on
the returned value itself. Worth remembering for any shared
interaction-loop helper: a missing trailing newline on a *success* path is
easy to miss because the bug is invisible in isolation and only manifests
in whatever an unrelated caller does next — test what a realistic caller
would output immediately afterward, not just the return value.

**Consolidating four separate single-purpose SysOp user-management
screens into one central editor (design doc -- Thiesi's own dogfood-
testing report).** `[L]ist users`/`[P]romote/demote`/`[E]nable/disable`/
`[D]elete user` each used to pick a target user through their own
separate `pick_item` call, then perform exactly one hardcoded action
inline with no way to do a second thing without leaving and re-picking
the same user again. Replaced with one `_pick_and_edit_user` (varies only
by the picker's own title text) landing on one shared `_user_detail_screen`
-- a real redraw-on-action menu loop (`[A]pprove`/`[L]evel`/`[T]oggle
enable-disabled`/`[I]dentity verification`/`[D]elete`/`[B]ack`), mirroring
the board/channel/file-area admin detail screens' own already-established
shape rather than inventing a new one. `[L]ist users` used to call
`_show_user_detail` for read-only-plus-two-prompts detail; that function
no longer exists as a separate linear pass -- it's now exactly the same
loop every other entry point reaches. The user picker itself
(`_pick_target_user`) gained `netbbs.auth.users.list_users`'s new
`order_by` values (alphabetical/alphabetical_desc/registered/
registered_desc/level_asc/level_desc, mirroring `list_boards`'s own
`order_by` convention) exposed as three live in-place toggle keys --
`[A]lphabetical`/`[R]egistration`/`[L]evel` -- pressing the
already-active mode's key flips ascending/descending without leaving the
screen; pressing a different mode's key switches to it, always starting
ascending; a `Sorted by: {label} {↑/↓}` line shows the current mode.

**Bespoke picker vs. extending the shared `pick_item` (Thiesi's own
follow-up dogfood request).** The live A/R/L sort-toggle screen was built
as its own loop in `admin_flow.py` (`_pick_target_user`), deliberately
duplicating `netbbs.net.picker.pick_item`'s pagination/search/goto
machinery rather than adding sort-toggle support to `pick_item` itself.
`pick_item` is a shared component used by boards, channels, and file
areas too, none of which have asked for live sort toggles; extending it
for a single consumer's need risks over-fitting its interface around one
caller's shape before a second real consumer exists. Matches this
project's own established convention (see `netbbs.link.files.
_file_area_from_row`'s own docstring) of duplicating a small private
helper across modules rather than reaching into another module's private
internals -- a shared abstraction should be designed against a real
second consumer, not an imagined future one.

**`list_users(order_by="registered"/"registered_desc")` had no tie-break
on `created_at` (found via a flaky new test, not by inspection).**
`ORDER BY created_at ASC` alone is nondeterministic in SQLite when two
rows share an identical timestamp -- which genuinely happens under
`tests/conftest.py`'s autouse `_fast_argon2id` fixture, since downgrading
Argon2id to minimum cost makes back-to-back `create_user` calls fast
enough that Windows' clock resolution can't always separate them (outside
pytest, with Argon2id at real cost, the same calls are slow enough to
never collide). Fixed by adding `id ASC`/`id DESC` as a secondary
tie-break -- the same fix already applied elsewhere in this codebase for
the identical reason (`channel_messages.id`/`created_at`). Any future
`order_by` clause sorting on a `created_at`-style column needs the same
tie-break from the start, not just once a test happens to catch the
collision.

**Testing a redraw-in-place screen: assert against the text *after* the
triggering keystroke, not `in`/`.index()` over the whole cumulative
output.** A `FakeSession` accumulates every `write`/`write_line` call
across an entire scripted key sequence into one growing buffer; a screen
like `_pick_target_user` (the A/R/L/V user-picker toggles) redraws the
*same* listing header/labels/rows in place on every toggle keystroke,
so an earlier (e.g. pre-toggle, default-state) render of the same label
text is still sitting earlier in that buffer. `str.index()`/`in` find
the *first* match anywhere in the buffer, which is the stale pre-toggle
render whenever the default and toggled states happen to share
vocabulary (e.g. both `[V]`'s "all" state and its "active-only" state
list the same usernames, just a different subset) -- this produced a
real, confusing false failure once already (the A/R/L toggle tests
comparing `text.index("sysop") < text.index("alice")`) and was caught
proactively a second time while adding the `[V]` visibility toggle's
own tests, specifically by using `text.rindex(marker)` (or slicing to
`text[text.rindex(marker):]` before checking what else appears) to
scope an assertion to the *last* render rather than the buffer as a
whole. Any new test against a screen that redraws rather than appending
fresh output needs this from the start, not just after a flaky-looking
failure surfaces it.

**`netbbs.selfupdate.record_check_outcome` was only ever called on a
successful check, never a failed one (found from a real SysOp's own
console report of a transient TLS error reaching GitHub's API, not by
code review).** Both `run_scheduled_update_check`'s daily background
pass and `_update_settings_screen`'s manual "check now" path caught
`UpdateError` and either logged a `_logger.warning` (scheduled) or wrote
one line to the session (manual) -- neither ever recorded the failure
via `record_check_outcome`, so `get_last_check_summary`'s `(checked_at,
outcome)` pair only ever reflected the *last successful* check. A node
whose daily checks had been silently failing for weeks would still show
"Last check: 3 weeks ago -- up to date" on the admin update screen, with
the only evidence anything was wrong sitting in a console warning line
nobody reliably tails. This violates this project's own stated "fail
clearly" convention (CLAUDE.md) as much as any user-facing failure mode
does -- a background operational check is still something a SysOp needs
to trust is either working or visibly not, and "quiet" must not be
ambiguous between those two. Fixed by calling `record_check_outcome(db,
f"check failed: {exc}")` from both failure branches, the same as every
success path already does -- one line each, not a new mechanism.
Worth remembering as a general check for any other "periodic background
check, log-and-continue on failure" loop in this codebase: log-only
error handling is a silent-failure gap by definition whenever the
result is also supposed to be visible through a persisted, SysOp-facing
status field elsewhere.

**Adding a new byte-emitting step to `TelnetSession.negotiate_initial_options`
(e.g. NEW-ENVIRON for truecolor detection, added alongside NAWS) silently
broke ~70 existing tests across `tests/test_telnet.py` and
`tests/test_picker.py`, as failures or hangs, not import errors.** Every
test that opens a real socket against `TelnetServer` and does
`await reader.readexactly(9)` to skip past the initial negotiation before
asserting on exact application-level bytes was hard-coding "9" as the
total negotiation size. Once negotiation grew (a second `IAC DO` plus a
`SB ... SE` subnegotiation, sent unconditionally on every connect), the
leftover unread bytes stayed buffered in the socket and were consumed by
the *next* `readexactly(N)` call instead -- silently corrupting
byte-for-byte content assertions (misleading `FAILED` diffs) or, worse,
throwing off a later test's read-count bookkeeping enough to leave a
`readexactly` call blocked forever waiting for bytes the server had
already sent earlier in the stream (an apparent hang with zero pytest
output, not a clean failure). A test that only checks a handler-side
side effect (e.g. `tests/test_telnet_idle_timeout.py`, which never reads
the echoed bytes back) or reads with `read(4096)`/`in` substring checks
rather than an exact `readexactly` count is unaffected.

**Fixed (issue #105) by centralizing the "skip past all negotiation
bytes" intent into one place** rather than continuing to audit every
hardcoded count by hand: `tests/test_telnet.py` now exposes
`_FULL_NEGOTIATION_LEN` (derived from the same IAC/WILL/DO byte
constants `netbbs.net.telnet` itself uses, not a literal number) and an
`async def skip_initial_negotiation(reader)` helper built on it. Every
other integration test module that only needs to get past negotiation
(`test_picker.py`, `test_telnet_idle_timeout.py`, `test_shutdown.py`,
`test_main_lifecycle.py`) imports and calls that helper instead of its
own hardcoded/re-derived byte count. `test_telnet.py` itself keeps its
own byte-content constants (`_INITIAL_NEGOTIATION`, `_NEW_ENVIRON_
REQUEST`) locally, since a couple of its own tests deliberately assert
on the exact negotiation bytes rather than skip past them -- those two
call sites must still never be pointed at the shared helper. A future
negotiation addition to `negotiate_initial_options` therefore only
requires updating `_FULL_NEGOTIATION_LEN` in one file; no more grepping
every integration test file for its own magic number.

**A persisted table using "NULL means still live" as its own liveness
convention must be reconciled at startup, before any listener can
create a new ambiguous row (issue #110, `netbbs.session_history`).**
`session_history.disconnected_at` starts NULL at login and is only ever
filled in by `record_session_end`, called from `run_authenticated_
session`'s own `finally:` block. That block never runs across a hard
kill, power loss, or crash -- exactly the same "a previous process
instance was interrupted mid-something" gap issue #34 already named for
`.incoming` upload staging files (`netbbs.files.storage.
purge_incoming_staging`). The fix follows that exact precedent:
`netbbs.session_history.reconcile_interrupted_sessions`, called from
`netbbs.__main__.run()` immediately after `purge_incoming_staging`,
before any listener starts -- at that exact point, every remaining
row with both `disconnected_at IS NULL` and `interrupted_at IS NULL` is
guaranteed to belong to some earlier
process instance, since this one hasn't accepted a connection yet. A
dedicated `interrupted_at` column (added via a new migration, not
by reusing `disconnected_at`) records when reconciliation ran, so the
caller-facing display can distinguish "ended cleanly at this real
timestamp," "still genuinely connected in this process," and
"connection was lost, detected at startup" -- collapsing the third case
into either of the first two would either fabricate a fake disconnect
time or keep claiming a session is live that cannot possibly be. Any
future feature with a similar "row created now, finalized later, only
reliably finalized while the same process survives" shape should reach
for the identical two-part fix: a schema field distinguishing "ended
normally" from "process never got the chance," plus a startup
reconciliation pass that runs strictly before new instances of that same
row shape can be created.

**A denormalized label preserved across account deletion must not also
resurrect a privacy choice the deletion erased (issue #111,
`netbbs.session_history`).** `session_history.username_label` is
deliberately denormalized so a historical row survives `delete_user`'s
cascade (issue #100) -- but `_session_history_display_name` decided
whether to actually *show* that label by re-checking the account's live
`session_history_name_visible` preference, which is also removed by the
same cascade. The naive fallback ("no account left, so show the label
unconditionally") silently reveals a name the user had explicitly opted
to hide, the instant their account is deleted. The fix is a persisted
`name_visible_fallback` column that tracks the *live* preference for as
long as the account exists (updated across every one of that user's
rows on every `set_session_history_name_visible` call, not merely
recorded once at each row's own connect time -- a row can predate a
later opt-out and must still honor it), consulted only once `user_id`
is `None`. The general shape: whenever a denormalized field is kept
specifically to survive a foreign-key cascade, check whether any *other*
row elsewhere (a preference, a permission, a visibility flag) governed
how that field was actually presented -- if so, that governing decision
needs its own persisted, cascade-surviving snapshot too, kept in sync
with the live value up until the moment survival becomes necessary, not
just the denormalized data itself. The preference UPSERT and fallback update
must be one SQLite transaction: a trigger/error between them must roll both
back. Startup reconciliation also backfills missing fallbacks from the current
live preference before accounts can be deleted, so databases created before
the fallback migration do not preserve a stale default merely because no later
preference edit happened.

**A live operations dashboard must not insert slow refresh work into a
shutdown/drain return path.** The SysOp console snapshot is loaded through the
`DatabaseLane` on entry and on explicit refresh. Ordinary user/content/outbox
changes may refresh it after returning, but a node-control or operations menu
redraw reuses the last snapshot: an immediate drain can be disconnecting tasks
while the issuing SysOp unwinds, and adding a fresh executor wait at that exact
boundary creates a cancellation point the former lightweight menu redraw did
not have. Live in-memory badges (maintenance, lockdown, session count, peer
count) are still recomputed on every render. Preserve this split when adding
dashboard metrics: durable snapshot data may be momentarily stale until `[D]`
refresh, while urgent process state must never be cached.

**The real-time Link cipher suite stays inside the existing PyNaCl/libsodium
dependency boundary.** `Noise_XX_25519_ChaChaPoly_BLAKE2s` uses PyNaCl's
X25519 scalar multiplication and IETF ChaCha20-Poly1305 bindings plus Python's
BLAKE2s/HMAC; it does not add `cryptography`, Rust, or a separate Noise package.
The state machine is pinned to the public Cacophony XX/25519/ChaChaPoly/BLAKE2s
vector byte-for-byte (all handshake messages and final handshake hash), in
addition to a real loopback-socket test. Both encrypted identity payloads must
verify root fingerprint -> root-signed transport transition chain -> current
Ed25519 transport key -> presented X25519 static key before a caller may apply
trust policy or accept application frames. Keep cryptographic verification,
trust admission, and session supervision as distinct gates.

**`asyncio.Server.wait_closed()` can block on an already-admitted connection
under Windows' Proactor event loop even after `close()` has already stopped
new accepts.** `LinkRealtimeServer.stop()` (real-time Link, design doc §8.10)
learned this the hard way: calling `stop()` while a `LinkRealtimeSession`
accepted through that listener was still open hung indefinitely, because
`close()` alone does not close already-admitted client sockets -- session
lifecycle is owned independently by `LinkRealtimeSession`/`LinkRealtimeSession
Registry`, never by the listener. `wait_closed()` is therefore called with a
short bounded `asyncio.wait_for` timeout and the `TimeoutError` is swallowed --
it is a defensive ceiling on releasing the listening socket's own resources,
not a normal-path wait for every connection it ever accepted to finish.
`TelnetServer.stop()` and `SSHServer.stop()` lacked it and produced a real
~9-minute Ctrl+C hang on ReLink (design doc §13.11, item 5): they now track
every admitted connection themselves (the session registry never sees a
connection still in auth or option negotiation) and judge the
`background_task_drain_seconds` deadline on *that set*, then `abort()` the
rest, with `wait_closed()` only as a bounded final release of the listening
socket. `wait_closed()` itself is the wrong drain signal on every supported
interpreter, in opposite directions: 3.12+ blocks on admitted connections,
3.11 returns immediately with them still attached -- a "wait, and abort on
timeout" shape silently never aborts anything on 3.11 (Codex review, PR #283). Two invariants
behind that fix: exiting an SSH *process/channel* (`SSHSession.close`) does
not close the SSH *connection* -- only the client or `conn.abort()` does; and
asyncssh sends no transport keepalives by default, so a peer that vanished
without a FIN is invisible until the kernel's TCP retransmission timeout
(minutes) unless `keepalive_interval` is set, which the SSH listener now does.
`TelnetSession.close()` bounds its own `wait_closed()` too, since
`StreamWriter.close()` drains buffered output first and shutdown gathers every
session's close. Any other `asyncio.start_server`-based listener in this
codebase needs the same treatment if its `stop()` can ever run while a
still-open connection exists.

**Testing method for this class:** no dead network is needed. A loopback
client that connects and simply never disconnects while the handler blocks
reproduces the hang deterministically on Python 3.12+ (`stop()` never
returns; bound the test with an outer `asyncio.wait_for`), and the fix is
proven by constructing the listener with a sub-second `stop_timeout_seconds`
and asserting the client then sees its connection dropped -- see
`test_stop_aborts_a_connection_the_client_never_closes` in
`tests/test_ssh.py` and `tests/test_telnet.py`.

**Real-time Link session authorization is two independent, direction-specific
checks, not one.** `LiveChannelBridge`'s subscribe/message/presence handlers
re-run `decide_node_action(..., LinkPolicyAction.REALTIME)` against the
*local* db for the *remote* peer's fingerprint on every frame -- this proves
only that "the peer I received this from is currently trusted by me." A node
serving live traffic to subscribers must separately re-check, before every
outbound push, that each subscriber is still trusted (`LiveChannelBridge.
_live_subscribers` does this and drops a now-untrusted subscriber outright)
-- otherwise a peer quarantined mid-session keeps receiving pushes until it
disconnects on its own. Tests exercising the authorized path must establish
trust in *both* directions (each side's db, for the other side's
fingerprint) since a freshly-seen node subject defaults to `PROBATIONARY`,
which `LinkPolicyAction.REALTIME` never allows.

**`netbbs.link.transport` (and anything importing it, e.g. `netbbs.link.
realtime_channels`) requires `aiohttp` at module import time, not just at
call time.** `netbbs.__main__` and `netbbs.net.chat_flow` are both imported
unconditionally regardless of which optional extras are installed, so
neither may import either module at top level -- every reference is a lazy,
function-local `import`, guarded by the exact same `try/except ImportError`
(or, once already proven available earlier in the same call path, an
unguarded local import) that `LinkServer`'s own construction already
established this pattern for. `netbbs.link.boards.LinkContext`'s
`realtime_registry`/`realtime_bridge` fields exist for exactly this reason:
`netbbs.link.boards` is imported by `netbbs.link.transport` (board
materialization), so `boards.py` cannot import `transport.py` back without a
cycle -- the field types are declared under `TYPE_CHECKING` only, relying on
`from __future__ import annotations` to keep the annotation itself
unevaluated at runtime. Verify this contract holds (a real regression is
otherwise silent until someone runs without the `web`/`link` extras
installed) by blocking `aiohttp` from `sys.meta_path` and importing both
modules -- no test currently automates this check.

**A live real-time subscribe attempt from an interactive session must be a
background task, never awaited inline, and must keep running for the
channel view's whole lifetime, not just until connected.** `_chat_loop`'s
`_subscribe_live` (issue #148) dials/subscribes, announces "is up" via
`deliver`, then keeps awaiting the session's own `closed` event so it can
also announce "was lost" if the peer disconnects while the caller is still
in the channel -- the design doc's connecting/live/degraded-offline
requirement is a *live status*, not a one-time check. Because that task is
therefore essentially never `done()` with a return value by the time a
caller leaves, the obtained session (if any) is tracked in a small mutable
holder the task writes into, not read back from the task's own result.

**`LinkConfig.realtime_port` must never default to a fixed constant, or even
to `port + 1`.** Caught by actually running the README's own two-node
loopback quickstart after adding the field: a fixed default collides
whenever an operator's `link.port` happens to equal it (silent until
upgrade, since existing configs never set the new field at all); `port + 1`
is *still* unsafe for the specific multi-node-per-host pattern the
quickstart itself documents (sequential HTTP ports 7862/7863) -- node A's
`port + 1` (7863) then equals node B's own `port`, a real OS-level bind
collision at startup instead of a config-time error. `effective_realtime_
port` (`netbbs.net.nodeconfig`) resolves to `port + 1000` instead, the one
place this fallback is computed; every reader (`validate()`, the listener's
own bind, the hello provider's advertised address) calls it rather than
re-deriving the default inline, so it can never drift between them. General
lesson: a new port field's default must be checked against every documented
multi-node-per-host deployment pattern, not just validated in isolation --
`config.validate()` passing proves internal consistency, not the absence of
a cross-node collision when two instances of the same defaults run
together.

**`LinkRealtimeServer`/`LinkRealtimeConnector` hold a fixed `NodeIdentity`
reference from construction and reuse it for every future handshake, inbound
accept or outbound dial alike -- `rotate_operational_key` alone has no way
to reach either of them, so calling it in isolation changes nothing about
what a live process actually presents on the wire.** `rotate_realtime_
transport_key` (design doc §8.10, issue #148) is the one place this is
wired up: it calls `server.update_identity()`/`connector.update_identity()`
*before* `registry.close_all(reason="transport_key_rotated")`, never after
-- closing first would let a peer's own fast reconnect (or, for a
`LinkRealtimeConnector`, this node's *own* automatic reconnect) race ahead
of the identity swap and complete against the very chain being retired,
silently defeating the rotation instead of enforcing it. Also: a live Noise
session's post-handshake symmetric keys never touch the static key again,
so an already-open session needs an explicit close to retire it -- nothing
about its ongoing traffic would ever notice that the `NodeIdentity` object
it was built from has since changed. Any future code that swaps a node's
live identity (signing-key rotation, not just transport) needs to ask the
same two questions this one already answers: what already-constructed
objects hold a stale reference to the old identity, and what already-
established sessions/connections were authenticated using it.

**A caller-facing "resume a saved draft?" prompt must consume (delete) the
draft file before handing its text to the editor as `initial_text`, or the
editor's own crash-recovery check double-prompts for the same file.**
Issue #149's board-entry prompt (`board_flow._offer_saved_draft_if_any`) and
both editors' pre-existing crash-recovery offer
(`edit_line_body`/`edit_prose`'s own `draft_path.exists()` check, via
`netbbs.net.draft_storage.offer_draft_recovery`) read the *same* file
convention (`board_flow._post_draft_path`) for two genuinely different
purposes -- proactively announcing an intentional `/exit`/"Keep draft &
exit" the moment the board is entered, versus recovering from a connection
that simply dropped mid-edit. Both fire from the identical trigger
(`draft_path.exists()`), so if the board-entry prompt's own "[E]dit"
choice left the file in place while also passing its contents through as
`initial_text`, the editor it opens into would immediately re-offer
"a draft was found, resume it?" for the very draft the outer prompt just
handed over -- a redundant second prompt for the same content. The fix is
ordering, not a flag: read the file, delete it, *then* call into the
editor with the loaded text as `initial_text`. This also means the two
recovery paths never overlap in practice for a `kind="new"` draft --
whichever one reaches the file first (board entry, if `can_post`, else
whatever later re-enters the editor) always consumes it -- while a
`kind="edit"` draft (one specific existing post) has no board-entry
counterpart at all and is only ever recoverable through the editor's own
crash-recovery path when that exact post is reopened.

**Ctrl-H cannot be given new meaning inside any `read_line()`-editable
context, only at `read_key()`'s single-keystroke layer -- both send the
identical byte (0x08) as Backspace, and only the latter has nothing real
for that byte to mean.** Issue #150's contextual-help key was suggested as
Ctrl-H, and `char_input.read_key()` previously discarded 0x08 exactly like
every other "no meaning as a standalone key" control byte (same bucket
Backspace/Delete/CR/LF already sat in) -- safe to repurpose there
specifically because a single-keystroke menu has no in-progress typed text
for Backspace to delete in the first place, so nothing is actually taken
away from a client whose own Backspace key happens to send 0x08. The
*editable* `read_line()` path (real free-text input, real in-progress
buffer) cannot make the same trade -- 0x08 there is live, load-bearing
backspace-editing and must stay exactly that, so `HELP_KEY`'s carve-out is
deliberately narrower than `REDRAW_KEY`/`REFRESH_KEY`'s own issue #102
precedent: added only to `read_key()`, `_read_line_editable`'s own
`_BS`/`_DEL` handling is untouched. This is also why the fullscreen prose
editor's own help key is Ctrl+G, not Ctrl-H, despite both editors sharing
one nano-derived keybinding scheme (`netbbs.net.ansi_editor`'s module
docstring) -- `read_editor_key()`'s structured `EditorKeyKind.BACKSPACE`
already claims 0x08 unconditionally for real backspacing inside a
fullscreen editor, so reusing it there the way `read_key()` safely can
would break actual editing, not just look inconsistent.

**A "universal cancel key" is not one design decision, it is at least two,
and only one of them has a codebase-wide-safe answer today.** Issue #157:
Ctrl-C (0x03) as `CANCEL_KEY` was added the same `read_key()`-only,
opt-in-per-screen way as `REDRAW_KEY`/`REFRESH_KEY`/`HELP_KEY` -- safe
because a single-keystroke menu has no free-text buffer for Ctrl-C to
interrupt, so returning it there costs nothing regardless of which
screen actually wires it in. `read_line()`'s editable path is the
second, harder decision, deliberately *not* made in this pass: there is
no single meaning for "the user pressed Ctrl-C while typing" that is
correct for every caller, because a bare blank line (the closest
existing analog) already means different things to different callers --
`netbbs.net.composition.edit_line_body` treats it as "finish and enter
review," not "cancel," while a plain single-line prompt like "Subject
(or press Enter to cancel)" treats it as an explicit cancel. Giving
Ctrl-C one hardcoded behavior inside `read_line()` itself would be
silently wrong for whichever caller's blank-line convention doesn't
match it. Extending cancellation into real text entry needs a
per-caller opt-in mechanism (e.g. a parameter, not a blanket byte
reinterpretation) and is intentionally left for a future, separately-
scoped increment.

**A manual `BLOCKED` trust verdict on `HELLO` doesn't just refuse the
request -- `LinkServer._handle_hello` actively evicts the peer from
`self._node.peers` (`self._node.peers.pop(peer.fingerprint, None)`) the
moment policy rejects it, even though `LinkNode.handle_hello` had just
protocol-verified and added it a line earlier.** A previously-completed,
already-known peer that gets manually blocked therefore stops satisfying
"has completed hello" for every other route (`push_events`, peer-list,
etc. -- `LinkProtocolError: ... which has no completed hello`) the instant
the block takes effect, not only for the hello route itself. Recovery
(clearing the block, restoring trust) must re-dial a fresh hello before
anything else will work again -- exactly like introducing yourself to a
stranger, not a resumed session. Caught extending issue #131's real-
transport quarantine test to also recover: pushing events immediately
after restoring trust (without a fresh hello first) fails with the
completed-hello error, not a trust-policy one, which is easy to
misdiagnose as a bug in the recovery path itself.

`netbbs.doors.runtime`'s sandbox model (issue #63/#167/#172) is
deliberately same-OS-user subprocess isolation, not containers or a
privilege-separated user -- see that module's own docstring for the
full reasoning. Two non-obvious constraints that follow from that
choice, both real and both accepted rather than overlooked:
`resource.RLIMIT_NPROC` is a ceiling on the *real UID's* total process
count, not a per-process-tree limit -- since every door and NetBBS
itself share one OS user by design, `DOOR_MAX_PROCESSES` bounds one
door's own runaway forking but also stacks across every concurrent door
session and NetBBS's own process count; it's a coarse fork-bomb backstop,
not a precise per-door quota. And `resource` itself is POSIX-only --
guarded with a plain `try/except ImportError` (this project's dev
sandbox routinely runs on Windows), so a door still runs during local
development, just without the CPU/memory/process-count ceilings a real
NetBSD/Linux node enforces; only the async wall-time watchdog (pure
`asyncio`, cross-platform) applies unconditionally. Real verification of
the `resource.setrlimit` ceilings themselves needs to happen on an
actual POSIX target, not this Windows dev box.

`netbbs.doors.runtime.run_door` builds a deliberately minimal child
environment and passes it straight to `asyncio.create_subprocess_exec`'s
`env=` -- which *replaces* the child's environment outright rather than
merging with NetBBS's own. A door gets `NETBBS_DOOR_INFO` plus the
platform's home-directory locator (`HOME` or `USERPROFILE`), resolved by
the parent so persistent doors do not mistake their disposable scratch
working directory for durable storage. No other parent variables are
inherited, and there is no way for a SysOp to hand a door custom
configuration through the door registry (`Door` has no env/config field
at all, only `executable_path`/`args`). Any door wanting an
operator-tunable setting either needs a config file at a fixed path
relative to its own script, a
CLI flag folded into `args`, or a wrapper launcher script that sets env
vars before exec'ing the real interpreter -- not a registry-level env
override, because that mechanism doesn't exist in v1.

`netbbs.doors.bundled.voidrunner` (a second, larger real door alongside
Retro Trivia; both ship as real installed package data under
`src/netbbs/doors/bundled/`, not loose `examples/` files -- see that
package's own docstring) persists a per-caller save file itself, since
the sandbox
gives a door no database access and deletes its scratch working
directory after every session (see this file's own entry above). Two
invariants that follow, both load-bearing for anyone touching that file:
(1) its save only stores the galaxy's random seed, not the galaxy
itself, and reconstructs the ~48-system map by replaying
`generate_galaxy(seed)` on every load -- which only stays correct if the
*exact sequence* of `random.Random` calls inside that function never
changes; reordering, adding, or removing a call anywhere before the end
of that function would silently regenerate a different galaxy for every
existing save (system ids drifting to different names/economies/
connections underneath a `discovered`-ids list that no longer matches).
New randomness there is safe to add only at the very end of the
function. (2) state is written to disk after every player action, not
only on quit -- a door can be killed at any moment (caller disconnect,
the wall-time watchdog) with no graceful-shutdown guarantee, so
save-on-quit-only would routinely lose real progress; the write itself
is a temp-file-plus-`os.replace` to stay atomic against exactly that
kind of mid-write kill.

`netbbs.net.admin_flow._door_field_specs`' `args` field is parsed with
`shlex.split(draft["args_line"])` -- deliberately POSIX-mode (the
default), matching the "never a shell, always an argv list" posture
`netbbs.doors.runtime` already documents. POSIX-mode `shlex` treats a
bare backslash as an escape character and silently drops it, which
mangles a raw Windows path the instant it contains one (`C:\Users\...`
round-trips as `C:Users...`) -- caught building the doors gallery
(`_door_gallery_screen`), which prefills this field with a resolved
`Path`: `str(path)` on this project's own Windows dev box corrupted
immediately, `path.as_posix()` doesn't (forward slashes are inert to
that escaping, and both Windows and every POSIX target accept them as
real path separators). Anything that programmatically fills this field
from a `Path` -- now or later -- needs `.as_posix()`, never `str()`.
This is a real latent trap for a SysOp manually typing a Windows path
into this field too, not just this call site, but that's out of scope
of what prefilling could fix and is unchanged here.

A one-shot status message (a validation error, a "not applied"/"loaded
and enabled" confirmation) that a screen prints and then falls straight
through to its caller's own redraw is invisible whenever the current
session has `redraw_in_place` on (`netbbs.net.redraw_preference`,
default for every account created since issue #160's follow-up): the
very next redraw's `screen_title(..., clear=True)` wipes the terminal
before the message can be read, so the feature *looks* broken even
though the underlying logic ran correctly (dogfood report against
`netbbs.net.admin_flow`'s welcome-banner/masthead "From disk" pickers --
their empty-directory and reject-and-loop-back messages had exactly this
bug, `_preview_welcome_banner_screen` had already independently
discovered and fixed the same class of bug for its own preview output).
Any code path that prints a message and then returns/continues into a
redraw -- not just an explicit `return` to the caller, but also a
`continue` back into `pick_item`'s own next `_render()` -- needs its own
`colored("Press any key to continue...", ...)` + `session.read_key()`
pause first. `FakeSession`-based tests don't catch this on their own
(they only assert substrings landed in the accumulated output, with no
concept of "cleared" or "still on screen"); a test exercising a newly-
paused path needs an extra scripted keystroke (this codebase's own
convention is a trailing `"x"`) to dismiss the pause, or it will consume
whatever key was meant for the next real action instead.

`netbbs.net.help_overlay.show_help`'s `unicode_style` boxed-frame mode
drew full-width top/bottom borders (`╭──...─╮` / `╰───╯`) but never
closed its content rows with a matching right-hand `│` -- `inner_width`
only ever reserved columns for the left margin, none for a closing
border, so every boxed help screen was open on that side (dogfood
report). Fixed by reserving one more column (`width - 4` instead of
`width - 3`) and appending the border after each (padded) line. Kept
that reservation to exactly one column, not two: the natural "mirror the
left margin's two spaces" fix (`width - 5`, space-then-border) shifts
existing callers' already-tuned wrap points further than necessary and
broke an existing test whose expected help text happened to wrap right
at the old boundary (`test_create_channel_ctrl_h_shows_real_help_text_
for_every_field`) -- any future change to this box's margins should
re-run the full suite, not just this module's own tests, since wrapped
text elsewhere may be relying on the current exact `inner_width`.

`netbbs.net.char_input.read_key` (and every real transport's
`Session.read_key`) deliberately treats CR/LF as meaningless noise and
keeps reading past it -- correct for its actual purpose, a hotkey menu
where Enter alone selects nothing -- but every "Press any key to
continue..." pause in the codebase (`netbbs.net.help_overlay.show_help`
and ~60 `admin_flow`/`chat_flow`/`login_flow`/`door_flow` siblings) was
also calling `read_key` for its dismissal read, meaning Enter -- probably
the single most reached-for key -- silently did nothing there (dogfood
report). `FakeSession`-based tests could never have caught this: every
test double's own `read_key` just pops the next scripted string
unconditionally, with no concept of CR/LF being special, so this class
of bug is invisible to the whole existing test suite by construction.
Fixed with a sibling primitive, `read_any_key` (`char_input.py`,
`Session.read_any_key`, overridden per-transport in `telnet.py`/
`ssh.py`/`web.py`), that treats every byte including CR/LF/Backspace/
Delete as a valid, immediately-returned dismissal -- `read_key` itself
is completely unchanged, so no existing hotkey-menu behavior is at risk.
`Session.read_any_key` has a *concrete*, non-abstract default that
delegates to `self.read_key(...)` -- this is why the fix needed zero
changes in ~51 of the ~53 `FakeSession`-shaped test doubles across the
suite (anything subclassing the real `Session` ABC inherits the
default automatically); the two exceptions
(`tests/test_last_sessions_screen.py`, `tests/test_composition.py`)
don't subclass `Session` at all and needed the same two-line delegating
override added by hand. Any *new* freestanding `Session`-shaped test
double (not subclassing `Session`) will need the same one-line
addition if it ever exercises a "press any key" pause -- check for this
specifically if a similar `AttributeError: ... has no attribute
'read_any_key'` shows up after adding one.

A `tests/test_picker.py` real-loopback-socket test combining an arrow-
key press (`_DOWN = b"\x1b[A"`/`b"\x1b[B"`, issue #171's highlight
navigation) with `pick_item`'s `name_segments_of` (multi-colored row
rendering, dogfood report -- the admin audit log wanting independent
timestamp/action/actor colors) hung indefinitely *only* under `pytest`
-- the identical scenario, run as a standalone script importing this
same test file's own helpers (`_run_server`/`_read_until_quiet`/
`_visible`), completed correctly in well under a second, 3/3 repeated
runs. Every other arrow-key test in this file (not combined with
`name_segments_of`) and every other `name_segments_of` test (not
combined with an arrow key) passed fine under `pytest` individually --
only this specific combination hung, and only when pytest ran it.
Root cause not found (`pytest-asyncio` isn't even installed, ruling out
the most obvious event-loop-conflict suspect) -- the regression test
for this combination was dropped rather than shipped hanging (a hung
test blocking the whole suite is worse than a coverage gap), and the
underlying behavior (`item_name_color if is_highlighted else color`, a
one-line override in `pick_item`'s row-rendering loop) was verified
correct by the standalone script instead. If this resurfaces --
especially if a *real* deadlock shows up outside tests -- start from
this exact combination (arrow-key nav + multi-segment row color) rather
than re-deriving it from scratch.

Running the test suite from inside a `.claude/worktrees/...` checkout
with the repo's shared `.venv` can silently exercise the *main*
checkout's code instead of the worktree's own edits: `netbbs` is
installed editable, and that editable pointer resolves to the main
working tree's `src/`, not whichever worktree the interpreter happens
to be invoked from -- `python -c "import netbbs.net.admin_flow as m;
print(m.__file__)"` from inside a worktree confirmed this directly. A
worktree-local edit that "doesn't take effect" under a normal `pytest`
invocation there is the first thing to suspect, not a broken edit --
verify with that same one-liner before debugging further. Fixed for a
given invocation by prepending the worktree's own `src/` via
`PYTHONPATH` (`PYTHONPATH="$(pwd)/src" .venv/Scripts/python.exe -m
pytest ...`), which takes priority over the editable install's
site-packages entry.
### Link node presentation identity

Link fingerprints remain the sole cryptographic identity, trust, protocol, and
persistence key even though ordinary UI uses the friendly name and canonical
DNS claim from the signed endpoint descriptor. Treat both names as mutable
authenticated presentation. Same-fingerprint name changes preserve identity;
reuse of a friendly or DNS name by a new fingerprint must remain visible as a
non-blocking security warning and must never inherit trust automatically.

Managed `netbbs.org` renames are make-before-break. Keep the old registration
and its bearer credential until the new name has matured and its DNS record was
successfully published; heartbeat both registrations and make cancellation
remove any already-published replacement before deleting its row. Rename
admission remains subject to the global active-registration cap. The old
credential can cancel or safely retry a replacement whose one-time credential
was lost before local persistence. Backup/restore must carry both credential
files during that interval. External DNS remains operator-managed: publish its
replacement before changing `link.advertised_host`.

Authenticated node-profile observations are remotely influenced retained
state. Keep both a per-peer bound and a global bound, and check collisions on
profile changes as well as first sight; a familiar name adopted by an already-
known fingerprint is still a cryptographic-identity warning. Retained previous
names participate in that collision check because familiarity outlives the
current descriptor. An exact full fingerprint always resolves before any name
claim, and an empty administrative reference never acts as a fingerprint
prefix.

The node-side managed-DNS updater must reconcile rename state from both bearer
credentials and the service heartbeat response, not rely solely on the local
configuration transaction: a crash can occur after credential replacement but
before the new and previous names are committed. While the service has a
pending replacement, `/release` rejects both registrations; the explicit
cancel-rename operation is the only safe way to unwind that pair.

Managed-DNS recovery is bidirectional: if cancellation succeeds remotely but
the node crashes before restoring the old credential locally, a successful old
credential heartbeat plus a definitive inactive-primary response promotes the
old credential and clears the transition. Retrying an already-reserved rename
rotates only that replacement's credential and is exempt from new-registration
admission; it must never delete the reservation before a throttle decision.
Abandonment withdraws any row with a recorded published address, including a
partially published replacement which intentionally remains pending.

Every presentation claim shares one collision namespace: a DNS claim can
collide with a friendly-name claim and vice versa. Exact fingerprints across
both persisted peers and active-only real-time sessions take precedence over
that namespace. UI rendering resolves current friendly identities at display
time while retaining fingerprints in protocol and persistence fields; when no
authenticated profile is available, it falls back to the technical identity.

Identity-change notices remain non-blocking but must be acknowledgeable from
the SysOp Link-status surface; acknowledgement dismisses the retained notices
so direct-message and Link-mail warnings do not repeat forever. Link mail shows
undismissed cryptographic-identity warnings both before sending and when
browsing received mail. Durable channel rows keep their technical author
address, while rendering recovers the signed author fingerprint from the saved
event and resolves its current presentation, so historical scrollback follows
later benign renames without rewriting persistence.

Pending managed-DNS replacements are cleaned up with unconditional idempotent
provider deletion during cancellation and abandonment. A null local publication
marker cannot prove that the provider mutation never happened because the
service may have crashed between those two operations.

Friendly node names reserve the UI's middle-dot label delimiter and reject
Unicode control/format characters, including C1 terminal controls and bidi
overrides. Apply this canonical profile validation to every authenticated
descriptor ingress, including verified peer-list refreshes. Resolver input
normalizes a trailing dot only for DNS comparison, never for friendly-name or
fingerprint comparison. Spaced `/msg` addresses require explicit double quotes
around the complete `user@node name` target so message words can never be
reinterpreted as a longer peer name.

When a managed-DNS rename retry finds its same-target replacement abandoned,
reactivate that row as pending before rotating and returning its credential.
During a pending rename, advertise the previous managed name only when its
saved status proves it had already matured and been published.

Managed-DNS credential replacement is a journaled two-file transition: stage
both bearer secrets atomically before changing either live credential, and
finish any staged swap before heartbeat or interactive rename/cancellation.
Transient failure of the old-name heartbeat must not erase its last known
status. Reactivating an abandoned replacement remains a capacity admission;
cancelling against an abandoned previous name restores that previous
registration before reporting success.

The canonical friendly-name grammar is enforced at both local configuration
and authenticated descriptor ingress. It reserves the composite-label
delimiter and the quoted-address delimiter as well as Unicode control/format
characters. Administrative fallback to an unseen technical identity accepts
only a complete 32-character lowercase-base32 node fingerprint (case-insensitive
on input); arbitrary unmatched names are errors.

Identity-collision detection includes this node's own currently advertised
friendly and DNS claims, not only remote peer history. Content whose
authenticated origin has no admitted profile falls back to that origin's
fingerprint on every catalogue and roster surface.

An authoritative inactive response for a previous managed-DNS credential
withdraws its cached publication claim; only transient failures preserve the
last known state. Reviving an abandoned DNS row clears its publication marker
inside the same transaction so a crash cannot suppress the required
republish. Address-discovery operations, including cancellation revival,
bypass forward proxies. Backup and restore treat the credential-transition
journal as recoverable state and restore its absence as well as its presence.

Undismissed cryptographic-identity observations outrank newer benign profile
changes when choosing the warning shown at an interaction boundary; only
explicit acknowledgement suppresses them. The `Unnamed linked node` fallback
is a reserved, case-insensitive sentinel and cannot be advertised as a real
friendly name.

Managed-DNS HTTP 401 responses are authoritative inactive state: clear the
corresponding cached publication claim even when no alternate credential is
live. Keep both bearer secrets during a pending rename, including after either
row is abandoned, because the replacement credential can still cancel the
transition and the previous secret is required to use the revived old row.
Classify that response from the client's structured HTTP status, never by
searching provider-controlled error text.

Managed-DNS cancellation revalidates cooldown and capacity synchronously after
awaiting provider deletion: fresh registrations do not take the transition lock
and can consume the last slot while the provider call runs. A previous abandoned
name is not revivable once its cooldown has elapsed, even if the periodic sweep
has not deleted its row yet. Rename completion clears the old row's publication
marker before deleting its provider record so a crash in between forces the old
credential's next heartbeat to republish rather than trusting stale state.

Bounded identity-observation pruning gives undismissed security warnings first
claim on both per-peer and global retention budgets. Retain a bounded history of
the local node's replaced friendly and DNS claims too, and resolve friendly and
DNS matches as one ambiguity set rather than giving either claim kind precedence.

Managed-DNS rename configuration is a single `node_config` transaction; never
publish a new name while retaining the old registration's matured/published
flags. Cancellation commits the revived old-name state before removing its
retained credential, then journals the reverse file swap. Backup restore treats
absence as state for the primary credential, previous credential, and transition
journal alike.

Endpoint hosts in signed `addresses` entries remain transport metadata and do
not enter the friendly/DNS identity-claim namespace. High-impact selectors such
as board-origin transfer expose the full fingerprint when presentation labels
collide, with the fingerprint placed first so terminal truncation cannot hide it.
Status screens which describe current authenticated presentation load the live
node name from configuration rather than a session's login-time breadcrumb cache.
The last locally advertised friendly claim participates in collision detection
until the next own-hello build moves it into bounded history. Parse persisted
peer descriptors by their explicit `canonical_dns_name` only; endpoint hosts
remain transport data after restart as well as in memory. Live linked-channel
events retain their authenticated sending-node fingerprint in memory so an
undismissed identity collision can be rendered at that interaction boundary.

An abandoned managed-DNS replacement may be retried only inside its cooldown;
after expiry the old credential has no privileged reclaim path while the sweep
waits to delete the row. Keep servicing a retained previous credential when the
replacement is locally abandoned, since a transient old-name failure followed
by a definitive replacement 401 must not stop all later old-name heartbeats.
Live backup double-collects all three credential artifacts around the SQLite
snapshot and compares the snapshot's `managed_dns_*` rows with live state;
retry a moving generation rather than combining database and secrets from
opposite sides of a rename or cancellation.

Heartbeat reconciliation commits the managed-DNS active and previous names,
statuses, publication flags, and contact timestamp as one transaction. When an
inactive replacement makes the retained previous credential authoritative,
commit that state before journaling and applying the reverse credential swap;
never delete the only working fallback secret ahead of the database commit.
Cancellation likewise consumes the service's authoritative revived-publication
state instead of restoring a cached flag which may predate a failed republish.

A rename's `replaces_name` is not proof that the row currently at that name still
belongs to the same node: cooldown expiry permits reissue. Check the previous
row's node fingerprint before provider deletion or release, and guard the store
mutation as well. Reactivating an abandoned replacement restarts both contact
timestamps so the next sweep cannot immediately abandon the recovered row.

Complete fingerprint-shaped strings are reserved friendly names at both local
configuration and signed-profile admission. Unchanged peer hellos still
re-evaluate collisions with current local claims after a local rename, while
identical recorded collisions are deduplicated. Where a user picker contains
identical remote node presentation labels, put the full fingerprint first so
terminal truncation cannot hide the distinction. Durable channel rendering
recovers the signed author fingerprint from the retained Link event for both the
friendly label and any undismissed cryptographic-identity warning.

Ephemeral live-scrollback snapshots must likewise keep each authenticated
author's home-node fingerprint in memory; the durable event may not have arrived
yet, but identity-collision warnings still apply. Prime this node's current DNS
claim before opening the Link listener, then refresh the shared own-hello cache
through the background database lane before inbound peer persistence and once
per outbound sync pass. The synchronous hello builder must never touch SQLite.

Long-lived user and network waits invalidate identity-warning snapshots. Re-read
a selected file origin after its picker returns, and re-read a warned trust
subject immediately before applying an override; the latter requires explicit
default-no confirmation of the full fingerprint. Resolve presentation claims and
fingerprint prefixes as one ambiguity set after exact-full-fingerprint
precedence. Verified peer-list refreshes must reload local presentation claims
after the HTTP wait and before persisting an updated known peer, so a local rename
during that wait still participates in collision detection.

Outbound hello persistence has the same post-network local-claim refresh
invariant, including candidate fallback. Lost-rename recovery refreshes both the
current registration and an existing pending replacement before returning a
rotated credential; otherwise a sweep can immediately abandon the recovered
replacement. A successful standalone register/reclaim result clears all local
previous-name metadata atomically and removes the obsolete previous credential,
so an expired rename cannot remain visible as a phantom transition.

Treat each managed-DNS credential's structured HTTP 401 independently: a
previous-name 401 remains authoritative when the replacement heartbeat fails
transiently, and simultaneous inactive results update both cached registrations
in one transaction. Before cancellation performs any provider deletion, verify
that the row named by `replaces_name` still has the replacement's node
fingerprint even when that previous row is active rather than abandoned.

Managed-DNS updater passes and interactive registration, release, rename, and
cancellation transitions share a process-local lock scoped by event loop and
node database. Hold it across the remote mutation and local reconciliation, but
never across a human prompt; otherwise a heartbeat begun from stale local state
can overwrite the result of a completed SysOp transition. On the service,
successful replacement maturation is one publication operation: carry its
observed address into the remaining heartbeat logic so the generic address
change path cannot publish the same replacement twice.

The `Unnamed linked node` and `Unknown linked node` presentation fallbacks are
both reserved, case-insensitive friendly-name sentinels at local configuration
and signed-profile admission. Bounded local identity-claim history is ordered by
most recent retirement or reuse: remove an existing normalized claim before
appending it again, so pruning does not discard a recently reused name merely
because it first appeared early.

A managed-DNS replacement selected through `replaces_name` remains recoverable
only when its node fingerprint matches the currently authenticated registration.
An old name can be reissued after cooldown while its former replacement row still
exists; the new owner must never receive a rotated credential for that stale row.
Treat both rename sides' heartbeat outcomes independently: when the previous-name
heartbeat succeeds but the primary times out, preserve the primary cache while
atomically applying the old name's authoritative status and publication result.

Presentation ambiguity must end in a usable address. When friendly and DNS
claims still collide, show each candidate's full fingerprint and direct the
caller to `user@technical-identity`; do not recommend the same ambiguous DNS
claim. Trust-role selectors are a higher-impact boundary: when a presentation
name resolves to a fingerprint with an undismissed security observation, show
that technical identity and require a default-no confirmation before mutation.
A complete fingerprint entered directly needs no second confirmation.

Rename admission deletes a released or abandoned target as soon as its cooldown
has elapsed, including an expired former replacement, then treats the attempt as
a fresh rename subject to normal limits. Cancellation is authenticated contact
for the retained previous registration: refresh `last_contact_at` even when that
row is still pending or matured, while preserving a pending row's original
`contact_started_at` so cancellation does not reset maturation progress.

Every hello-bearing ingress must refresh the lane-backed local identity claims
before `save_peer`; the ordinary hello endpoint and relay-mailbox pickup share
that ordering invariant. Durable Link post/file attribution must retain its
technical suffix long enough to query undismissed identity observations and
render a caution plus the full fingerprint when the current friendly label is
cryptographically ambiguous. Board-origin acceptance is likewise a high-impact
consent boundary and discloses the warned origin fingerprint before confirmation.

When cooldown reissue leaves another fingerprint's pending replacement pointing
at the newly owned previous name, ignoring the stale row in memory is
insufficient: detach its `replaces_name` in SQLite before inserting the new
owner’s replacement, or the partial unique index still denies an unrelated
rename. Detachment changes neither the stale row's owner nor its credential.

Origin-transfer identity disclosure is symmetric: both offering and accepting
consult the undismissed observation for the signed fingerprint before their
default-no confirmation. An incoming offer with no live peer profile falls back
to its signed `old_origin_fingerprint`, never a generic unknown-node label.
Remote-file catalogues similarly preload warned origin fingerprints through one
database-lane call, put the technical identity in the catalogue row, and repeat
the caution immediately before fetch consent.

An authoritative replacement-credential 401 plus a successful previous-name
heartbeat does not by itself prove that the server removed the replacement's
`replaces_name` relationship. The updater first attempts `/cancel-rename` with
the working previous credential. Promote and journal the reverse credential swap
only after cancellation succeeds or a 401 proves the relationship is already
absent; on transient or policy failure, atomically retain the transition and both
credentials while recording the replacement as abandoned and the previous
heartbeat's authoritative state.

Managed-DNS rename and cancellation are authenticated contact. Before a
successful rename response, refresh the current row with heartbeat-equivalent
gap semantics so a waiting abandonment sweep cannot immediately withdraw it.
Cancellation preserves an uninterrupted pending previous name's maturation
window, but must restart a stale window whose last contact crossed the threshold.
Interactive registration/reclaim stores name, status, conservative unpublished
state, dynamic choice, and opt-in as one transaction.

Identity-notice ordering is security-sensitive: undismissed cryptographic
observations sort ahead of benign changes before the SysOp screen applies its
five-item display and acknowledgement bound, so repeated friendly-name changes
cannot bury the actionable warning.

A local claim change is itself a collision event. Persisting a new local
friendly or canonical-DNS claim re-runs the identity check over every stored
peer descriptor, so a peer which preclaimed that name and then stays silent is
still recorded as a cryptographic-identity warning. The SysOp board-detail
screen presents an origin, an incoming offer's source, and an outstanding
offer's target by fingerprint whenever the peer profile is unavailable, which is
the same rule every other surface already follows.

This is the last review-driven change on this branch. The remaining
crash-between-two-writes finding in the interrupted-cancellation path was
declined: the managed-DNS service is a single-operator service where manual
repair of one row is the realistic recovery, and the branch already carries
twice the feature's own size in such safeguards.

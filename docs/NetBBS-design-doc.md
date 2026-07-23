# NetBBS architecture and product design

This document is the **current normative design** for NetBBS. It describes what
the system means, what users and node operators may rely on, and the boundaries
future implementations must preserve.

It is not a chronological decision diary. The former numbered sign-off rounds,
superseded alternatives, corrections, and intermediate implementation status
remain available through Git history. Do not reconstruct that chronology here.

Use project sources in this order:

1. this document for product, protocol, authority, and long-lived UX decisions;
2. current GitHub issues for unresolved work and acceptance criteria;
3. `docs/NetBBS-worklog.md` for durable implementation constraints and lessons;
4. source, migrations, tests, and Git history for exact implementation detail.

When these sources disagree, investigate and update the stale source. Do not
choose whichever answer is most convenient.

## Current status

- Phases 1 and 2 are complete as working standalone BBS software.
- The post-Phase-2 local additions—Communities, identity attestation,
  asynchronous personal mail, and self-update foundations—are substantially
  implemented.
- Phase 3 is active. NetBBS Link has real identity, canonical event encoding,
  authenticated HTTP transport, persistent peer/event state, seed and peer
  discovery, linked boards, tier-1 Link messages, outgoing-only-node relays,
  and deterministic multi-node fault testing.
- Phase 3 remains **private and experimental federation**. Phase 4 trust,
  reputation, and quarantine are the public-federation readiness gate.
- Later phases—real-time Link chat, advanced Link governance and Link
  Communities, and door-game compatibility—remain future work.

Implementation status belongs beside the relevant design rule below and must be
updated in place. Do not append victory narratives or test-count snapshots.

---

## 1. Product identity, terminology, and principles

### 1.1 Names

- **NetBBS** is the software project.
- **NetBBS Link** is the decentralized network connecting NetBBS nodes. “The
  Link” is acceptable informal shorthand.
- **Board** always means a message board.
- **Area** always means a file area. Never call a file area a board.
- **Link** prefixes features which exist specifically because of NetBBS Link:
  **Link message**, **Link Community**, **Link-wide** presence or chat.
- An ordinary local resource which participates in NetBBS Link keeps its normal
  noun with the adjective **linked**: linked board, linked channel, linked file
  area. Do not rename such resources into “Link boards” or similar proper
  nouns.

### 1.2 Foundational principles

NetBBS Link is foundational, not an add-on. Every durable local feature should
be designed with its possible network extension in mind, even when the local
version ships first.

The standalone BBS must remain complete and useful without NetBBS Link. Local
packages must not import Phase-3 federation code merely to perform ordinary
local work.

Node sovereignty is non-negotiable:

- no master node exists;
- no node, moderator, or majority vote can force another operator to store,
  display, or delete content;
- carrying remote content is always a local decision;
- moderation and trust signals may propagate, but enforcement remains local;
- remote closure or suppression events cannot remotely erase bytes already
  stored by another node.

The design prefers correctness, explicit authority, bounded resource use, and
visible failure over cleverness or silent degradation.

### 1.3 Non-goals

NetBBS is not intended to:

- recreate a centralized social network behind a terminal interface;
- promise anonymity from a user’s own home-node operator for ordinary
  password-only accounts;
- make every local feature network-wide immediately;
- replicate every file byte to every node;
- hide unresolved trust decisions behind signature verification alone;
- preserve historical BBS protocol constraints when they conflict with the
  native NetBBS model.

---

## 2. Platform, architecture, and scale

### 2.1 Target platform and stack

- Primary target: **NetBSD**, distributed eventually through pkgsrc.
- Runtime: Python 3.11+ with asyncio.
- Storage: one SQLite database per node, using WAL mode.
- Cryptography: PyNaCl/libsodium. Avoid dependencies which impose a Rust
  toolchain on the primary NetBSD target without a compelling benefit.
- User transports: Telnet, SSH, and web/xterm.js.
- Asynchronous Link transport: signed HTTP+JSON.
- Future real-time Link chat transport: Noise Protocol Framework.

### 2.2 Modular boundaries

The system is a modular package, not a monolithic script.

- `netbbs.auth` owns accounts and authentication.
- `netbbs.identity` owns cryptographic identities and addressing.
- `netbbs.boards`, `netbbs.files`, `netbbs.chat`, and `netbbs.mail` own local
  domain state.
- `netbbs.communities` owns Community state and inherited-value resolution.
- `netbbs.moderation` owns shared authorization and audit primitives.
- `netbbs.rendering` owns ANSI, reflow, screen-buffer, and editor-independent
  rendering behavior.
- `netbbs.net` owns user-facing flows, sessions, and transport orchestration.
- `netbbs.link` owns Link events, protocol, transport, persistence, discovery,
  synchronization, relaying, and local-to-Link bridges.
- `netbbs.storage` owns migrations, database connections, and execution lanes.

Domain functions are normally synchronous and `db`-first. Async session or
network code dispatches blocking work through a `DatabaseLane`. Link bridge
modules may depend on local domains; local domains must remain Link-unaware.

Rendering, protocol, storage, and transport concerns remain distinct. A generic
transport must not learn every event’s product semantics, and domain storage
must not decide how terminal output looks.

### 2.3 Expected scale

The primary deployment remains one modest self-hosted node operated by one
SysOp. The architecture targets:

- dozens to low hundreds of concurrent interactive sessions;
- small-to-medium Link deployments initially;
- correctness across multiple nodes even before large deployments exist.

SQLite is appropriate at this scale. The first expected scaling pressure is
write contention and queued background work, not raw interactive connection
count. Scale decisions beyond demonstrated workloads must be based on measured
behavior using the deterministic multi-node harness, not estimates alone.

---

## 3. User connectivity, rendering, and interaction

### 3.1 Connection methods

Telnet, SSH, and web/xterm.js are first-class user transports. Product behavior
should be transport-independent unless a capability genuinely requires a byte
stream, browser code, or another transport-specific primitive.

### 3.2 Rendering model

Use hybrid terminal rendering:

- ordinary screens use ANSI/VT100 text with reflow;
- cursor-addressed screen-buffer rendering is reserved for interfaces which
  benefit from it, such as fullscreen editors and pinned chat rows;
- the minimum supported terminal is 40x24;
- screens must degrade clearly rather than corrupting output when a terminal is
  too small or lacks a capability.

Untrusted user text is sanitized before styling. Trusted ANSI is added only
after sanitization. Nested colored fragments are composed independently because
an SGR reset does not restore an outer color.

The project intentionally provides two composition paths:

- a robust simple/line-oriented editor available everywhere;
- a nano-like fullscreen prose editor as a convenience preference.

ANSI art editing and prose editing are separate concerns. Syntax highlighting,
spell checking, and similar enhancements remain optional modules rather than
core editor assumptions.

### 3.3 Future presentation pass

Feature correctness and architectural stability take precedence over visual
polish during active roadmap work. A dedicated presentation and usability pass
is planned after feature completion. This does not excuse inaccessible,
ambiguous, or broken UI in the meantime; it only postpones broad theming and
cosmetic redesign.

---

## 4. Accounts, authentication, identity, and addressing

### 4.1 Account authentication

Local users may authenticate with:

- username and password, the default path;
- an optional personal Ed25519 keypair for passwordless challenge-response
  login.

The server never needs a personal user private key. Password-only users are
expected to remain the majority and must not be treated as second-class users.

### 4.2 Registration modes

A node has one registration mode:

- `open`: self-registration creates an immediately usable account;
- `approval_required`: self-registration creates a pending account which
  cannot authenticate until approved;
- `closed`: the public registration option is absent and accounts are
  SysOp-created.

Registration determines whether an account may exist and log in. Link
probation and reputation determine what an active identity may do; these are
separate axes.

### 4.3 Account levels and the usable-SysOp invariant

One integer level drives ordinary level gating. `SYSOP_LEVEL = 255` is the
reserved top level; SysOp is not a parallel role flag.

Promote, demote, disable, enable, approve, and hard-delete operations must never
leave the node with zero **usable SysOps**. A usable SysOp:

- has level at least `SYSOP_LEVEL`;
- is not disabled;
- is not pending approval.

The invariant is enforced transactionally against fresh database state, not
against a stale object supplied by a caller.

Hard deletion preserves content provenance through denormalized display labels
or nullable author/uploader references. Personal access rows and private state
which cannot meaningfully outlive the account are deleted according to explicit
foreign-key policy.

### 4.4 Human-facing Link addresses

The native cross-node address is:

`user@node-fingerprint`

The node fingerprint is derived from the node’s long-lived root identity, not a
DNS name. Network location may change without changing identity.

### 4.5 Identity tiers

NetBBS has three author/identity tiers.

#### Password-only user

A password-only user has no personal cryptographic identity. Link events use a
`node_vouched_user` author reference containing:

- the home-node fingerprint;
- an opaque local user identifier.

The home node signs on the user’s behalf. Key rotation and recovery are entirely
the node operator’s responsibility.

#### Personal-key user

An opt-in user keypair provides passwordless login and may later support
personally signed events. It is one key, not a root/operational hierarchy.
There is no bespoke recovery mechanism. Losing the key loses that key-based
identity and reputation continuity; the local account may still use ordinary
account recovery policy.

#### Node identity

Every node has:

- one long-lived root key whose fingerprint is the stable node identity;
- one operational signing key for events and content;
- one operational transport key reserved for Noise-based real-time transport.

The root authorizes and revokes operational keys through signed transition
records. Historical signatures remain verifiable by walking the transition
chain back to the root.

Routine operational-key rotation and compromise response do not change the
node address. Root-key loss or compromise has no cryptographic recovery in the
current design. Social/M-of-N recovery remains a possible future extension, not
an assumed capability.

Root and operational keys are generated at initial bootstrap. Rotation is a
guided SysOp action. Root-key custody is part of ordinary node backup and
restore rather than requiring an HSM or offline ceremony.

---

## 5. Authorization, moderation, and identity attestation

### 5.1 Resource gates

Boards, file areas, channels, and Communities may apply:

- minimum user level;
- minimum age;
- verified-name requirements;
- visibility and membership policy appropriate to the resource type.

Resource-level scalar settings are nullable:

- `NULL` means inherit the containing Community’s default, if any, otherwise
  use the system default;
- an explicit value, including `0` or `none`, overrides inheritance.

A Community default is a default, not a mandatory floor or ceiling. A child
resource may currently loosen or tighten it explicitly.

### 5.2 Moderator authority

Boards and file areas distinguish read and write access. Channels use a join/
participation gate rather than asynchronous read/write separation.

Moderator permissions are composable primitives such as read, write, edit,
delete, approve, manage members, mute, ban, and topic control as appropriate.
Moderators need not be SysOps.

Authority scopes are:

1. per-object;
2. Community-blanket, applying to present and future matching resources in one
   Community;
3. local-blanket, applying to local-only matching resources on one node;
4. Link-blanket, applying to linked matching resources carried by a node.

Link-blanket authority does not imply local authority. A person who needs both
must receive both explicitly.

Only a SysOp can grant or revoke blanket authority or change node
configuration. A suitably authorized Link-blanket moderator may initiate a new
linked resource, but:

- the node identity signs and owns the genesis event;
- the initiating human is recorded separately for audit;
- initiation grants no power to appoint further blanket moderators or alter
  unrelated resources.

Every moderation action is audited. Moderator changes to immutable Link content
must be represented as new authorized events, never silent mutation.

### 5.3 Board and file moderation

A board or file area may require approval before new posts/uploads become
visible. Local maintenance follows:

`active -> expired -> deleted`

with a grace period between expiration and deletion. Pin and expiry-exemption
currently use the existing edit permission. Local pruning never becomes a
network-wide deletion instruction.

### 5.4 Channel visibility and membership

Channel visibility and join policy are separate:

- listed or hidden;
- open to otherwise eligible users or members-only.

`hidden + open` is permitted but is obscurity, not access control.

Local invitations may be immediate for online users and retained with expiry
for offline users. Membership persists until revoked unless a channel defines
otherwise. Linked-channel membership eventually becomes signed governance;
it is not represented as end-to-end confidential from participating node
operators.

### 5.5 Identity attestation

Age and verified-name policy is local and jurisdiction-specific. NetBBS provides
mechanism, not a universal legal definition.

Users may provide nullable, independently visible:

- birthdate;
- display name;
- location;
- other profile fields.

Age is computed from birthdate at check time. It is never stored as a derived
current age. If a resource has an age gate and no usable birthdate or verified
age attestation exists, access fails closed.

A `user_attestation` records:

- subject user;
- attribute (`age` or `name`);
- attested value;
- verifier identity;
- signature;
- creation time;
- Link-visibility preference.

A verifier may use a personal key, or the node may vouch for a password-only
verifier. Verified values take precedence over self-reported values.

`can_verify_identity` is a separate SysOp-granted boolean, not another content
moderator tier.

Name requirements are:

- `none`;
- `verified`, requiring a verified name without compulsory display;
- `verified_and_displayed`, requiring resource-scoped visible disclosure.

A verified name never overwrites the user’s self-chosen display name. When a
resource requires display, render:

`display_name_or_username (=Verified Real Name=)`

The complete `(=...=)` unit uses a dedicated trusted color. The `=` marker
remains visible when color is stripped or inaccessible. User-controlled display
names may not contain the reserved `=` marker, and untrusted text cannot inject
ANSI styling.

Disclosure is resource-scoped. A resource which requires visible identity must
not cause the real name to leak into unrelated screens.

Remote propagation of attestations requires:

- the subject’s explicit opt-in;
- Phase-4 trust rules allowing the receiving node to decide whether to trust the
  remote verifier.

A carrying node may always apply its own local attestations to its own users
when enforcing a carried resource’s local age/name policy.

---

## 6. Local product domains

### 6.1 Message boards

Local boards provide:

- categories and stable navigation IDs;
- posts and replies;
- moderation and pending approval;
- expiry, pinning, and exemption;
- immutable revision history for edits;
- simple and fullscreen composition.

Read/unread state, follows, activity discovery, and local search across
boards, file areas, channels, and Communities are specified together in
§6.6, not per-domain.

A visible edit is a revision, not destructive replacement of history. Any
threading or revision semantics which affect Link event IDs or propagation must
be settled in Phase 3; only presentation refinements may wait until Phase 7.

### 6.2 File areas

Local file metadata lives in SQLite; file bytes use content-addressed filesystem
storage. Areas support permissions, moderation, expiry, and Zmodem transfer on
byte-capable transports.

File bytes are node-local. NetBBS Link will distribute catalogue/descriptor
information and fetch content on demand in bounded resumable chunks. It will
not replicate every file to every node.

### 6.3 Real-time chat

Local chat is typed event traffic, not preformatted strings. Initial event types
include:

- ordinary message;
- `/me` action;
- online private message;
- join/leave;
- alias change;
- system notice.

An optional `/nick` alias is presentation metadata only. Every context retains
the authenticated canonical identity, and permissions, moderation, blocking,
reputation, and addressing always use canonical identity.

Local chat includes bounded persistent channel scrollback, presence, away
state, invitations/membership, `/who`, `/whois`, `/names`, `/list`, `/join`,
`/leave`, `/topic`, completion, and online private conversation.

`/msg` and `/private` remain ephemeral and online-only. They never silently
fall back to asynchronous mail.

Phase 2 uses one active channel per session. Multiple simultaneous memberships,
background delivery, and Link-wide presence wait for Phase 5.

### 6.4 Personal mail

Local asynchronous mail is a persistent domain distinct from chat `/msg`.
Messages have sender/recipient views, subject, body, read state, and independent
delete state. The row is removed when neither side retains it.

Recipient mailboxes are bounded. When full:

- the oldest already-read message may be evicted to make room;
- unread mail is never silently discarded;
- if no safe eviction exists, delivery fails explicitly.

Local mail is the domain extended by Link messages; Link mail does not create a
parallel mailbox UI.

### 6.5 Communities

A Community is a topic-oriented coordination/container object above boards,
channels, and file areas. It does not merge those domains or change their
behavior.

Each board, channel, or file area has zero or one Community. “Uncategorized” is
the absence of a Community, not a synthetic row. Categories remain a separate
layer below Communities.

Communities provide:

- topic-first navigation;
- description and visibility;
- inherited level, age, and name-verification defaults;
- Community-scoped blanket moderator grants;
- a future unit for Link carry and governance.

The main navigation exposes:

- Communities;
- Uncategorized resources;
- Jump/search by resource type.

Each path leads to the same resource-type submenu and then the normal board,
channel, or area browser. Resources unrelated to Communities—mail, directory,
profiles, preferences, and administration—retain their own navigation.

Community-scoped category views must filter at the query layer so a category
used by resources in several Communities does not leak another Community’s
resources into the current view.

Deleting a Community:

- sets member resources to no Community;
- revokes Community-scoped blanket grants;
- shows the blast radius before confirmation.

Existing nodes migrate safely because the nullable Community reference leaves
all existing resources Uncategorized until a SysOp assigns them.

#### Link Communities

A Link Community is the same Community object announced through a signed Link
event, not a separate table or local type.

Two same-named Link Communities from different origins remain distinct.
Existing local Communities may be promoted into Link scope.

Carrying a Link Community is intended to carry its present and future member
resources by default, while retaining visible per-resource and whole-Community
local exclusions. Origin defaults are recommendations; carrying-node overrides
win locally.

Actual Link Community event schemas, signed membership changes, and advanced
governance are Phase 6 work.

### 6.6 Activity, unread state, follows, and search (issue #56)

A topic-first Community hierarchy is only more useful than a plain directory if
a user can tell what changed since their last visit. This section is the
complete answer to issue #56: read/unread semantics, follow state, a
new-activity surface, and local search. It replaces §6.1's earlier vague
"local search/navigation foundations" phrase.

#### Read/unread state

Local mail already has a complete, working model: a per-message `read_at`
timestamp, a live `unread_count` query, and independent sender/recipient
deletion. A delivered Link message is a normal row in the same table, so it
already has full read tracking the moment it lands in a mailbox. Nothing new
is needed for mail; issue #56's mail bullet is already satisfied.

Boards, file areas, and channels need a per-user, per-container **read
cursor**, not a per-item flag — per-item read state for a potentially
unbounded board would itself be an unbounded table. One new table holds it:
`(user_id, object_type, object_id)` primary key, where `object_type` is
`board`/`channel`/`file_area` and `object_id` is that resource's own local
integer id (the same id `community_id`/category columns already reference —
never the content-addressed `post_id`/`file_id`, which only identifies one
item, not a container). Its payload is the newest item's ordering key the
user has already seen:

- boards and file areas already page with a stable `(created_at, post_id)` /
  `(created_at, file_id)` keyset cursor (the existing `list_posts_page`/
  file-listing implementation) — the read cursor stores exactly that same
  tuple shape, so "what's unread" is the identical tuple comparison keyset
  pagination already performs for `after=`, just anchored at the user's own
  cursor instead of a page boundary;
- channel scrollback has no revision concept and is already ordered by a
  plain monotonic message id, so a channel's cursor is just that id.

An **edit never resets read state**: an edit's root post keeps the original
`created_at`/`post_id` (§6.1), which is exactly what the cursor comparison
keys on — a post a user has already scrolled past stays "read" after a later
typo fix, matching normal reader expectance. **Expiry and deletion cannot
corrupt a cursor**: an expired post keeps its `post_id` reachable until
nothing references it, and even final hard-deletion only ever removes an
already-fully-dereferenced row — a stored cursor value is a stable position
marker being compared against, never a live foreign key, so it cannot dangle
or resurrect deleted content.

A resource with no cursor row for a user has never been visited by them.
First visit — not a retroactive backfill — establishes the baseline: viewing
a board/file-area page or a channel's current scrollback advances that user's
cursor to the newest item they were just shown. This is also the complete
migration story for existing accounts (issue #56's last acceptance
criterion): the read-cursor table starts empty for everyone, including
existing users, at upgrade time. Nobody's history is scanned or backfilled;
the first real visit after upgrade sets the baseline, so only genuinely new
activity from that point forward counts as unread — never a flood of
years-old "unread" content on the first login after this ships.

A never-visited resource is surfaced as **not yet visited**, not as a
specific (and potentially enormous, meaningless) unread count — a real
numeric unread count only exists once a baseline cursor is established.

Channel scrollback is a bounded ring buffer (§6.3): a channel's cursor can
only ever express "unread among what's still retained." A message trimmed
out of scrollback before a user's next visit is simply gone, the same as it
already is for a session that was never connected to see it live — this is
an existing, accepted limitation of chat's ephemeral model, not a new gap
introduced here.

**Replies and mentions** need no new schema. A board post's existing
`parent_post_id` already names the post it replies to; "replies to me,
unread" is the same cursor-filtered query further restricted to posts whose
`parent_post_id` belongs to one of the user's own posts, run across every
board the user can read rather than one at a time. A channel "mention" is a
lightweight, unverified `@username` substring match against
`channel_messages.body` for messages newer than the user's channel cursor —
a convenience heuristic, not a structured or security-relevant feature; a
literal `@alice` typed with no intended addressee is an accepted false
positive, and a message directed at someone without using their exact
username is an accepted false negative.

**Node-local arrival order for carried content (issue #72).** The
model above compares a post/file's own `created_at` against the
cursor. That is correct for locally originated content, created in the
same order it becomes visible, but not for a Link-carried post: a
remote author's claimed `created_at` can be arbitrarily old if the post
only reaches this node after a partition or a delayed catch-up, and
comparing against it can let a genuinely new arrival silently sort
behind an already-advanced cursor. `posts`/`files` rows already carry a
second, distinct ordering with no schema addition needed: SQLite's own
`INTEGER PRIMARY KEY` rowid, assigned in strict insertion order
regardless of whether a row was created locally or materialized from a
carried Link event (the same property GitHub issue #68 already relies
on for edit-chain tie-breaking). `user_read_cursors` gains
`last_seen_arrival_id`, populated from that rowid; `unread_post_count`/
`unread_file_count`/`unread_replies_to` compare against it instead of
`created_at`, while `board_read_cursor`/`file_area_read_cursor` (feed-
position jump-to) are unchanged and still compare `created_at` -- the
two concerns use different orderings on purpose, per this section's own
distinction between authored chronology and node-local availability.
Existing cursors are backfilled from the post/file their existing
`last_seen_stable_id` already names, so an upgrade preserves exactly
what a user had already read rather than resetting anyone to
all-unread.

**Accepted scope boundary:** jump-to-first-unread can still land on the
board/area's ordinary newest page rather than navigating precisely to
an out-of-order arrival buried elsewhere in feed history, since the
jump cursor stays `created_at`-based. Unread *counting* and `[N]ew
scan`'s "has unread" detection are correct either way; only precise
jump navigation to that specific item is not yet solved. Reconciling
jump-to with arrival order, if ever wanted, is future work, not implied
by this fix.

#### Follows and favourites

Follow state is a new, separate table — `(user_id, object_type, object_id)`
where `object_type` is `community`/`board`/`channel`/`file_area` — deliberately
independent of every existing access concept it sits beside:

- **not** channel membership/invitations (`netbbs.chat.membership`), which
  govern *whether you may enter*, never *whether you care about it*;
- **not** node carry policy (`netbbs.link.boards.materialize_carried_board`),
  which is a per-node, all-or-nothing decision about whether Linked content
  exists locally at all, made with no per-user awareness whatsoever today;
- **not** Community membership, since a Community has no membership concept
  to begin with — it is a browsing/navigation container, not a joined group.

Following an object a user can no longer read (level raised, Community/
channel access changed, or — for a Linked board — this node stopping carrying
it) is never actively revoked; it simply stops being resolvable and is
filtered out of every follows-aware view at display time, the same
lazy-filter approach category/board listings already use elsewhere for
resources no longer visible.

#### Activity summary and direct jump ("new scan")

A single new main-menu entry — `[N]ew scan`, the traditional BBS term for
exactly this feature — is the fast, always-shown surface issue #56 asks
for, following the same unconditional-visibility
precedent `[J]ump to...` already sets.

New scan covers **every board, channel, and file area the user can currently
access**, not only followed ones — matching the traditional meaning of a
new-scan pass, and avoiding a chicken-and-egg problem where a brand-new
account has followed nothing yet and a "new scan" would show nothing at all.
Followed objects are surfaced first / distinguished within that same list; a
follows-only filtered view remains one keystroke away for a user who wants to
narrow it. Within new scan, a dedicated "replies to you" pass (described
above) runs across every board regardless of follow state, since a reply is
always worth surfacing.

Selecting an item from new scan jumps directly into that resource
pre-positioned at the first unread item — mechanically, calling the
resource's own existing keyset-pagination entry point with `after=` set to
the user's stored cursor, not a new navigation primitive.

#### Local search

Implemented (issue #56's last piece). Local search is a new, separate
capability from the item picker's simple, per-call substring name match
(`pick_item`'s own search command, unrelated and unchanged — see below):

- **scope**: only this node's own already-stored content — approved board
  posts (subject/body), approved file entries (filename/description), and
  retained channel scrollback (message body). Never content this node does
  not itself carry — there is no Link-wide query protocol, and this design
  does not imply or require one;
- **mechanism**: SQLite FTS5 virtual tables (`post_search`, `file_search`,
  `channel_message_search`), kept in sync with `posts`/`files`/
  `channel_messages` by explicit calls from `netbbs.boards.posts`/
  `netbbs.files.entries`/`netbbs.chat.scrollback` at every write path
  (create/edit/approve/delete/expire/trim) — deliberately not SQL
  triggers, matching this schema's existing convention of zero triggers
  anywhere else and keeping the sync logic visible in Python. `post_search`
  holds only the *resolved current* approved revision of a post's edit
  chain (mirroring `_resolve_current_version`'s own "newest approved row
  for this root" query) — a superseded revision, a still-pending edit, or
  a root with no approved revision left is never indexed.
  `channel_message_search` is pruned in the same statement that trims
  scrollback's own ring buffer, so a search can never surface a message
  already gone from retained scrollback.
  FTS5 availability was traced, not just assumed, for this project's actual
  NetBSD/pkgsrc target: `lang/python312`'s Makefile buildlinks against
  `databases/sqlite3` (not an amalgamation bundled into Python itself), and
  that package's own Makefile passes `--fts5` unconditionally in
  `CONFIGURE_ARGS` — so pkgsrc's Python `sqlite3` module should always have
  it. A build lacking it fails the schema migration loudly
  (`sqlite3.OperationalError: no such module: fts5`) rather than degrading
  silently, consistent with this project's "fail clearly" convention;
- **authorization**: a search result set passes through the exact same
  visibility rules (level/age/Community gates for boards and file areas,
  `netbbs.net.chat_flow.list_visible_channels_for` for channels) normal
  browsing already enforces — search can never be a side-channel that
  reveals a restricted resource's existence or content;
- **privacy, explicit**: a user's search query text is never transmitted to
  any peer or broadcast over Link, by default and without exception in this
  design. Searching a Linked board only ever searches this node's own
  locally carried copy of it. A future Link-wide search capability, if ever
  built, is a distinct protocol extension requiring its own explicit design
  (rate limits, query exposure, opt-in) — never an implied consequence of
  local search existing;
- **UI**: a new, always-shown `[F]ind` main-menu entry (`netbbs.net.
  login_flow._find_screen`), alongside `[N]ew scan` — prompts for one
  free-text query, matches it against all three content types at once, and
  jumps straight to a selected hit: a post/file lands on the exact matched
  item (`netbbs.search.post_jump_cursor`/`file_jump_cursor` compute the
  `after=` cursor that makes it the first item shown, reusing the same
  `initial_cursor` parameter `[N]ew scan` already threads through
  board/file-area viewing) rather than just opening its board/area at the
  default newest page. A channel message instead just enters its channel —
  channels have no "jump to one message" concept, the same limitation
  `[N]ew scan`'s own channel dispatch already accepts.

Local, in-page substring matching over a short list (`pick_item`'s own
search command) is unrelated and unchanged — it is not "search" in this
section's sense, just incremental filtering of an already-open, already
access-checked list.

**Integrity checking and rebuild (issue #74).** Because the three FTS
tables above are synced by explicit per-write-path calls rather than one
shared transaction with the authoritative write, a crash between the two,
a future write path that forgets to call the right reindex function, or a
restored older backup can leave them stale with no prior way to detect or
repair it. `netbbs.search.check_index_integrity(db)` reports drift
(missing/stale/extra entries, by id only — never the drifted content
itself) for all three tables against authoritative `posts`/`files`/
`channel_messages` data; `netbbs.search.rebuild_indexes(db)` replaces
their contents outright, using the exact same "what should be indexed"
computation the check compares against, so a rebuild always converges to
a clean check immediately after. Exposed as a standalone maintenance
command, `python -m netbbs.search check|rebuild --db PATH`, mirroring
`python -m netbbs.backup`'s own subcommand shape.

**Explicit decision: startup detects nothing automatically.** Unlike
`Database.check_integrity`'s `PRAGMA integrity_check` (a full-database
scan run once at every node startup, §13's startup/crash-recovery
rules), FTS drift checking is *not* wired into node startup. The
database-corruption check is cheap relative to node startup and guards
against a failure mode (disk-level corruption) that can occur at any
time regardless of how careful this codebase's own write paths are; FTS
drift is a narrower, rarer failure (a missed reindex call, a crash in
one specific window) whose check cost scales with indexed content
rather than staying close to constant. Treat it as an operator-run
maintenance action for now — after a crash, an interrupted migration,
or a restored backup — rather than a mandatory gate on every start.
Wiring a summary into the `[D]iagnostic log`/SysOp status surface is
possible future follow-up, not required by this decision.

### 6.7 Self-update

The updater uses explicit GitHub Releases over HTTPS rather than arbitrary
branch HEAD. Current foundations include version comparison, release checking,
safe archive extraction, persisted state, and database snapshot/restore
primitives. Scheduled release checking exists; complete apply/re-exec and
rollback orchestration still requires operational validation.

Before an update which may migrate the schema, snapshot the database so binary
and schema can roll back together.

The intended apply model is:

- check at startup, manually, and on a daily schedule;
- drain live sessions before a live-node restart;
- replace the on-disk release and re-exec the process;
- retain the previous release and restore it, together with the database
  snapshot, after failed startup.

HTTPS and GitHub are currently the update trust boundary. Additional release
signing is not required by the present design, though it remains a possible
hardening step.

The automatic check/apply policy must have an operator-visible off switch.
Future pkgsrc packaging will need an explicit ownership policy rather than
silently competing with the self-updater.

---

## 7. NetBBS Link: identity, events, and compatibility

### 7.1 Phase-3 safety boundary

Phase 3 is for private, controlled federation. It may use local blocklists as an
interim abuse control, but it is not safe to expose broadly to unknown peers
until Phase 4 defines and implements trust, probation, reputation, and
quarantine.

### 7.2 Canonical event envelope

A durable Link event uses a signed envelope:

```json
{
  "netbbs_protocol": 1,
  "object_type": "...",
  "payload": { ... }
}
```

The content ID and signature cover the entire canonical envelope, including the
object type. Object type is therefore **mandatory domain separation**: it is
intrinsic to the exact bytes that get hashed and signed, not a caller
convention a future event type could accidentally bypass by reusing another
type's shape.

**Canonicalization rule** (binding and language-independent — issue #11):

- Compact JSON: no insignificant whitespace, `":"`/`","` separators only.
- Object keys sorted by exact Unicode codepoint sequence, at every nesting
  depth, after normalization (below).
- Every string is recursively normalized to Unicode NFC before serialization —
  object member names as well as values, at every nesting depth, not values
  alone. Two payloads differing only in normalization form (precomposed versus
  combining-mark sequences) canonicalize identically and share one content ID,
  whether the difference is in a value or in a key. Two distinct source keys
  that would normalize to the same string are a normalization collision and
  are rejected outright, the same way a duplicate wire key is (below) — never
  silently resolved by whichever one happens to overwrite the other.
- Floating-point values are forbidden anywhere in a hashed or signed field:
  float serialization is not reliably deterministic across languages and
  platforms.
- Any other JSON number is an integer and must fall within
  `[-(2^53 - 1), 2^53 - 1]` — the widest range exactly representable as an
  IEEE-754 double, matching JavaScript's/JSON's own safe-integer bound. No
  current field approaches this bound; the rule exists so a future field
  cannot silently produce bytes only an arbitrary-precision-integer language
  can hash consistently.
- `true`/`false` are booleans, never conflated with the integers `1`/`0`.
- A field that does not apply to a given event omits the key entirely.
  Storing it as an explicit JSON `null` is a **different, distinct canonical
  value** — `{"parent_post_id": null}` and `{}` must never share a content ID.
  Each event schema states, field by field, which behavior applies; a builder
  must not choose between omission and `null` ad hoc.
- Wire JSON containing the same key twice in one object, at any nesting
  depth, is rejected outright before it is canonicalized, hashed, or
  verified — never silently resolved by a "last one wins" rule. Two
  different JSON parser implementations can disagree about which duplicate
  value wins; a sender and receiver that disagree would each reconstruct a
  different object from what they would both call "the same bytes."

`netbbs.boards.content_id.canonical_json_bytes` is the sole canonicalization
implementation this codebase uses to produce these bytes. Anything that
signs, verifies, or content-addresses a Link event reuses it directly, never
a second, independently-maintained implementation that could quietly drift.
`netbbs.link.events.strict_json_loads` is the reference implementation of the
duplicate-key rule, applied to every message this node's transport reads off
the wire before that JSON becomes a candidate envelope.

Golden test vectors (`tests/fixtures/link_canonical_vectors.json`, checked by
`tests/test_link_canonical_vectors.py`) pin exact canonical bytes and content
IDs for representative payloads, including Unicode normalization,
omitted-versus-null, and integer-boundary cases. An independent,
non-Python implementation of this format is compatible with NetBBS Link if
and only if it reproduces every vector's canonical bytes exactly.

Existing Python behavior implements the rule above; it is not a separate,
looser specification of its own.

### 7.3 Author references

An event author is a tagged union:

- `node_vouched_user`;
- `user_key`;
- `node`.

The verifier resolves the appropriate current signing key and, for node-owned
keys, validates its transition history back to the root identity.

Only the author tiers implemented for a specific event type are accepted. The
existence of the tagged union does not imply every tier already works for every
feature.

A `node_vouched_user` author (or a `link_message` sender/recipient) is
identified by the pair `(home_node_fingerprint, local_user_id)`, never by
`local_user_id` alone — a username is unique only within its own node, so the
pair, not the bare name, is the globally-scoped identity issue #11 asks for,
matching the `user@node-fingerprint` addressing form already used elsewhere.
`local_user_id` is the account's canonical, immutable, stored-case username
(§5); it participates in canonical bytes exactly as stored, after the same
NFC normalization every string field receives — never case-folded the way
local login/uniqueness lookups are, since a signed event fixes one exact
string forever, not a case-insensitive equivalence class.

### 7.4 Immutable content and state-changing chains

There are two event classes.

#### Immutable creation events

Examples include a board post or file descriptor. Their content ID identifies
the complete immutable object. Nodes may differ only in whether they possess or
locally suppress it.

A random nonce distinguishes two intentional posting actions with otherwise
identical visible content.

#### Per-object state chains

Edits, metadata changes, grants/revocations, key transitions, origin transfer,
closure, and membership changes extend a per-object chain. Each new event
references the state/event it extends.

Effective state is the projection/fold of the valid chain. An incoming event is:

- a valid extension of the current state;
- an already integrated ancestor, therefore an idempotent no-op;
- a genuine competing extension/fork requiring the object’s defined policy.

Transport deduplication is only a performance optimization. Permanent replay
safety comes from the authoritative object state or chain, not from a purgeable
“seen ID” cache.

Tombstones are chain events, not deletion of history. Local byte pruning cannot
resurrect state if the permanent projection rules remain intact.

Two events both validly extending the same predecessor at the same instant is
impossible by definition: a chain has exactly one current head, and an
incoming event either extends it (accepted) or does not (rejected as
reordering, or handled as a fork, per the object's own policy). `created_at`
is descriptive metadata for display and audit, never the mechanism that
orders or authorizes a chain extension — two genuinely successive events can
legitimately share one clock's timestamp resolution. Reconstructing a chain
from storage (for example, after a restart) must walk the same
`previous_event_id`/head-pointer links original acceptance already verified,
or rely on the storage layer's own locally-assigned, monotonic receipt
ordering — never re-sort on the payload's own claimed `created_at` alone.
Ordering among unrelated immutable events for local presentation (for
example, a board's post listing) is a separate, local concern with its own
stable tie-break, not a protocol question.

### 7.5 Version and unknown-event behavior

`netbbs_protocol` changes only for incompatible wire semantics. Additive event
types or optional fields need not force a protocol bump when old peers can
safely preserve them.

Peers exchange supported protocol information during authenticated contact.
Unknown event types or unsupported versions may be stored and relayed opaquely,
but must not be projected, displayed, or treated as authority by a node which
cannot interpret them.

Unknown fields within a known signed event must be preserved in the original
signed representation. A node must not strip and reserialize them in a way
which changes the signed bytes.

---

## 8. NetBBS Link transport, discovery, and distribution

### 8.1 Traffic-family split

Asynchronous/store-and-forward features use signed HTTP+JSON:

- key and endpoint state;
- boards and Link messages;
- future file catalogues and chunk requests;
- governance events.

Real-time Link chat will use a persistent mutually authenticated Noise channel
with the node transport key. Do not force asynchronous and real-time traffic
through one protocol merely for uniformity.

### 8.2 Hello and endpoint state

A hello is self-authenticating and carries enough root and transition state to
resolve the current signing key, plus a signed endpoint descriptor.

Endpoint descriptors may advertise ordered addresses and relay information.
The newest valid descriptor wins; stale repeats are harmless.

The protocol logic remains transport-independent. The `aiohttp` adapter is the
boundary translating protocol messages to real HTTP requests and responses.

### 8.3 Bootstrap and peer discovery

Bootstrap sources are combined, not exclusive:

1. operator-configured seeds;
2. software-shipped fallback seeds;
3. a live supplementary seed list fetched over the existing GitHub update
   channel;
4. signed/verified peer-list exchange after contact;
5. bounded fallback attempts to discovered candidates when normal seeds fail.

Seed or peer introduction never implies trust. Identity verification is
cryptographic and independent of the network address which introduced a peer.

A compromised bootstrap source can attempt an eclipse or steer connection
attempts, but cannot impersonate an existing node without its key.

### 8.4 Full and outgoing-only nodes

A full peer advertises reachable addresses and accepts inbound Link traffic.
An outgoing-only node initiates connections but cannot be dialed directly.

Multiple addresses are tried in order. Simultaneous HTTP dials require no
connection-role tiebreak because they are independent idempotent request/
response exchanges, not competing persistent sessions.

### 8.5 Relay service for outgoing-only nodes

Outgoing-only nodes select a small redundant set of reachable full peers based
on direct-observation reliability. Relay participation requires signed consent.
A node may opt out of serving relays and may cap the clients/resources it serves.

Accepted relays are published through endpoint state and replaced when observed
reliability degrades.

The relay mailbox currently supports opaque encrypted Link-message envelopes:

- relays see routing metadata and size, not message content;
- storage is bounded;
- pickup authenticates the intended recipient;
- the recipient re-runs normal event verification rather than trusting the
  relay’s claim;
- relaying does not introduce strangers or weaken the rule that sender and
  recipient identities must already be known sufficiently to verify and
  encrypt.

Reliability scoring is direct-observation operational data, not Phase-4 social
reputation.

### 8.6 Current synchronization model

Current background sync:

- contacts configured/cached seeds and candidates;
- performs hello/peer discovery;
- pushes the complete locally originated supported event set;
- relies on idempotent acceptance;
- sends targeted Link mail directly or through a selected relay.

This is intentionally simple but incomplete.

Not yet present:

- generic inventory exchange and pull-based anti-entropy;
- efficient per-peer deltas;
- general multi-hop propagation of arbitrary carried content;
- complete retained-event and dedup-purge policy;
- public-network backpressure and abuse handling.

A node which receives Alice’s board events does not automatically relay them to
Carol under the current direct-pairwise model unless explicit future relay/
anti-entropy behavior is added.

### 8.7 Store-and-forward goal

The eventual model supports nodes which are offline for extended periods and
resume synchronization later. Causal relationships come from parent/chain
references; timestamps are secondary ordering data, and content IDs provide a
deterministic final tiebreak for truly concurrent siblings.

Persistent dedup uses exact IDs, not Bloom filters. False-positive data loss is
unacceptable. Retention cleanup must never turn an old state-changing event into
something re-applicable.

---

## 9. Linked boards and resource lifecycle

### 9.1 Promotion and genesis

An existing local board may be promoted into Link scope. Promotion creates one
signed `board_genesis` referencing the existing stable board ID; it does not
replace the board with another local object.

The node identity is the origin authority. The genesis includes descriptive
metadata and recommended defaults for carrying nodes.

### 9.2 Posts and edits

Only approved local posts are originated as `board_post` events. Password-only
users currently use the `node_vouched_user` author tier.

Self-authored edits become chained `board_post_edit` events. The original post
remains immutable. Moderator edits and tombstones require separate authorized
event types and advanced governance.

### 9.3 Carry and local materialization

A peer accepting a valid board genesis materializes a real local board copy so
users can browse carried content through the normal board UI. Carrying is more
than retaining raw protocol events — the same principle extends to a carried
board's *content*, not just the board shell itself (issue #73): an accepted
`board_post`/`board_post_edit` must become an ordinary local `posts` row, not
remain a protocol-layer record a caller-facing screen can never reach. Before
this, a carried board could verifiably receive posts while still showing
empty to every reader — `link_events` is necessary for protocol verification
and replay safety, but it is not the product database.

**Mechanism.** `netbbs.link.boards` gains `materialize_carried_post`/
`materialize_carried_post_edit`, mirroring `materialize_carried_board`'s own
shape: idempotent (keyed on the event's own `content_id`), bypassing
`netbbs.boards.posts.create_post`/`edit_post` entirely (those require a local
`User` author and mint a fresh local ID, neither of which fits received
content) in favor of a direct insert using the **event's own `content_id`
verbatim as the local `post_id`** — the same "never mint a second ID for the
same thing" precedent `materialize_carried_board` already established for
`board_id`. This has a valuable side effect: since a `board_post_edit`'s own
`root_post_id`/`previous_event_id` payload fields already name other events'
`content_id`s, and those become the corresponding local `post_id`s verbatim,
`posts.root_post_id`/`edit_of_post_id` resolve directly from the Link
payload with no separate ID-translation table.

Unlike `materialize_carried_board` (a separate `lane.run` call from the
`save_event` that persists the underlying signed event — a real, pre-existing
crash-window gap for genesis materialization, not newly introduced but not
closed here either), the new functions perform the `link_events` insert and
the `posts` projection in the same call, one transaction, one commit: a crash
between them is no longer possible for posts/edits specifically. `LinkServer.
_handle_events` calls the combined function once per accepted `board_post`/
`board_post_edit`, replacing today's separate `save_event` dispatch for those
two object types.

A reply's `parent_post_id` is set only if that parent is *already* locally
materialized — the same "no backfill, no speculative storage" rule this
project already applies everywhere gossip can arrive out of order (§8, §9.1):
an orphaned reply is materialized as a top-level post rather than blocked or
queued waiting for a parent that may never arrive.

**Author identity.** A materialized post's `author_user_id` is `NULL` — no
local account is implied or required by carrying content (issue #73's own
required test scenario) — with `author_label` synthesized as `local_user_id@
home_node_fingerprint` (the same address shape Link mail already uses) and
`author_fingerprint` left `NULL` (that column is a *local* user's own
personal keypair fingerprint, a different concept from a remote node's
fingerprint — never conflated). This requires a prerequisite fix: `Post.
author_user_id` is currently typed as a required `int` even though the
column has been nullable since the account-deletion migration (round 60's
`ON DELETE SET NULL`) — display code (`netbbs.net.login_flow`'s post-reading
screen, notably) currently calls `get_user_by_id(db, post.author_user_id)`
unconditionally. Widening the type to `int | None` and guarding every such
call site is corrected as part of this work, not deferred — a locally
deleted user's own old posts were already silently exercising this exact gap
before a remote author's posts could.

**Display and resolution.** No new resolution logic is needed:
`_resolve_current_version`'s existing `root_post_id`/`created_at DESC, id
DESC` query already picks the correct latest revision for a materialized
chain, since materialization always processes a verified edit chain in
accepted order — local `id` (strict insertion order) therefore agrees with
logical edit recency even when the remote-claimed `created_at` doesn't (clock
skew, out-of-order network delivery), the same tie-break reasoning issue #68
already established for purely local edits. `created_at` on a materialized
row is the *authored* timestamp from the signed event, never the local
arrival time — see the separate node-local-arrival-order issue (#72) for why
unread/New Scan ordering is a distinct concern from this display field.

**Local moderation stays event-history-safe by construction.** `delete_post`
only ever touches `posts`, never `link_events` — deleting a materialized
post's local row (subject to its own existing FK-blocker rules: no deleting a
post with local replies or edit-chain descendants) already cannot rewrite or
lose the signed record needed for replay safety, with no new mechanism
required. Origin recommendations (§9.1) never override this local policy,
exactly as they never override any other local access/moderation/retention
decision on a carried board.

**Idempotency, New Scan, and search.** Duplicate delivery of an
already-materialized event is a no-op (existing `post_id` found, row returned
unchanged) — no duplicate local posts or revisions. `[N]ew scan`/unread
counts (`netbbs.activity`, issue #56) need no new wiring at all: they compare
a stored cursor against `posts` rows directly, with no separate
"mark as new" call site, so any newly materialized row is automatically new
activity. Local search does need an explicit call — `netbbs.search.
reindex_post(db, board_id, root_post_id)`, the same call every other
`posts` write path already makes, right after each materialization.

**Repairing a gap.** Because persistence and projection are now atomic for
new events, the only way a `board_post`/`board_post_edit` in `link_events`
can lack a corresponding `posts` row is a node that carried boards *before*
this feature shipped. A repair pass — scan `link_events` for `board_post`/
`board_post_edit` rows with no matching `posts.post_id`, and materialize them
in chain order — closes that one-time gap and doubles as the "supported
rebuild path" issue #73's own acceptance criteria ask for, the same
"derived state must be rebuildable from authoritative data" principle issue
#74 applies to FTS indexes. Exposed as `[R]epair carried posts` in the
SysOp `[S]ystem` submenu (only shown when Link is enabled), the same
explicit-SysOp-trigger-only shape `netbbs.files.gc`'s reference-aware blob
reclaim already established — purely additive (fills in a missing row from
an already-verified signed event, never deletes or rewrites anything), so
unlike blob reclaim it needs no dry-run/confirm step.

Linked resources are carried by default within the supported topology, with a
visible local exclusion option. A local exclusion must be represented honestly
as “not carried on this node,” not indistinguishable disappearance.

Origin recommendations never override the carrying node’s local access,
moderation, retention, or legal policy.

### 9.4 Origin succession

Routine node signing-key rotation is handled by the node key-transition chain
and does not transfer resource ownership.

Voluntary board-origin transfer requires mutual consent:

1. the current origin signs an offer naming the proposed new origin;
2. the proposed origin signs acceptance;
3. peers project the new origin only after both valid events.

Only one outstanding transfer offer is meaningful at a time.

If the current origin loses all valid signing authority and cannot publish a
transfer, the board is locally recognizable as orphaned. Existing content
remains available, but no new origin-authorized state is accepted.

A fork is a new resource/genesis with a new origin and an optional
non-authoritative `forked_from` reference. Each node independently chooses
whether to carry the original, the fork, both, or neither.

Channel-side Link lifecycle will reuse these principles after linked channels
exist; there is currently no channel genesis protocol.

### 9.5 Remaining board governance

Still future or incomplete:

- signed closure/archive projection where not already implemented;
- linked-board moderator grants and revocations;
- moderator edits and tombstones;
- general relay/anti-entropy beyond direct peers;
- Link-blanket governance surfaces and audit feeds.

---

## 10. Link messages

### 10.1 Product model

A Link message extends the ordinary local mailbox. The user composes to a
`user@node-fingerprint` address and reads the result in the same inbox/sent UI
as local mail.

The message is point-to-point to one recipient node, not flood-filled public
content.

### 10.2 Confidentiality guarantee

The implemented confidentiality tier is `tier1_home_node_key`:

- subject and body are encrypted to the recipient node’s current signing-key
  material converted to X25519;
- network peers and relays cannot read the content;
- the recipient’s home-node operator technically can decrypt it.

This is not end-to-end encryption against the home node and must not be marketed
as such.

The X25519 key is derived from the existing Ed25519 key through libsodium’s
supported conversion. This deliberately couples encryption and signing-key
rotation and accepts the larger compromise blast radius in exchange for a much
simpler lifecycle and no separate key-distribution protocol.

Static recipient keys do not provide forward secrecy. A later compromise can
expose previously captured messages.

`tier2_personal_key` remains reserved vocabulary but is permanently out of the
planned product scope unless NetBBS gains a real client-side decryption
architecture. Server-side terminal sessions cannot render content which the
server is forbidden to decrypt, and a web-only feature is not sufficient to
justify a parallel mail system.

### 10.3 Delivery state

Transport receipt is not user delivery.

Separate signed events represent:

- accepted into the recipient mailbox;
- bounced because of unknown recipient, full mailbox, blocking, or another
  defined terminal failure;
- future expiry where retry policy requires it.

Outbound messages remain pending until an accepted or bounced event arrives.
Delivery through a relay does not change the acceptance semantics.

### 10.4 Routing limitations

Direct delivery requires a known peer with a usable endpoint. An outgoing-only
recipient may be reached through relays it has selected and published.

The current system does not introduce total strangers. The sender must already
know enough authenticated peer/key state to encrypt to the destination, and the
recipient must know enough sender state to verify the message.

### 10.5 Metadata and abuse controls

Only routing information needed by transport or relay infrastructure should be
visible outside the encrypted body. Subjects and bodies remain encrypted.

Mailboxes, relay storage, retries, and pending acknowledgements are bounded.
Blocking and quota failures must be explicit; unread data is not silently
removed to make delivery appear successful.

---

## 11. Remote file areas

A linked file area remains owned and stored by its source node.

NetBBS Link should propagate catalogue/descriptor events sufficient to discover:

- area identity and metadata;
- files, hashes, sizes, and source availability;
- access policy needed before attempting transfer.

File content is fetched on demand in bounded resumable chunks. Chunk transport
may use signed JSON metadata plus raw HTTP bodies; it is not required to embed
large bytes as base64 JSON.

Chunk IDs and transfer IDs provide exact deduplication. Completed content is
stored once by hash even when referenced from several catalogues.

Remote file catalogue discovery and chunk transfer are Phase-3 work and are not
yet implemented end to end.

---

## 12. Trust, reputation, probation, and quarantine

Phase 4 defines the public-network security model. The design direction is
settled; the precise threat model remains issue #55.

### 12.1 Local trust view

There is no global reputation ledger or authoritative network vote. Each node
computes its own trust view from:

- direct observation;
- optional signed signals from identities it already trusts.

Node reputation provides a baseline; user reputation may refine it.

### 12.2 Probation

New nodes and users begin with restricted capability, normally read-only.
Graduation combines elapsed time and vouching by already-established identities.
Exact thresholds and what qualifies as established remain to be specified.

### 12.3 Objective and subjective reports

The protocol must distinguish:

- objectively verifiable abuse, such as invalid signatures, malformed events,
  conflicting claims, or measurable flooding;
- subjective moderation judgments, such as spam, harassment, illegality, or
  off-topic content.

Objective reports may carry evidence which another node can verify. Subjective
reports remain local opinions and must not become a disguised network-wide
verdict.

### 12.4 Quarantine

Quarantine is a local circuit-breaker decision based on signed observations and
the receiving node’s own policy. No node objectively “enters quarantine” for
the entire network.

The eventual mechanism must define:

- report categories and evidence;
- expiry, revocation, and replay protection;
- independent-flagger rules;
- Sybil/collusion resistance;
- reputation weighting;
- bounded signal propagation;
- reversible recovery.

A node may locally give an external jurisdictional authority key unusually high
weight, but this is an operator opt-in policy and has no effect on nodes which
do not configure it.

---

## 13. Runtime, persistence, and operations

### 13.1 Database execution model

Interactive and background Link work use separate single-worker database lanes,
each with its own SQLite connection and bounded submission depth.

This isolates human-paced foreground work from sustained background federation
traffic while preserving simple synchronous domain functions.

SQLite retains its normal single-writer behavior. `busy_timeout` handles short
cross-lane contention; no application-wide write mutex is introduced.

A cancelled awaiting coroutine does not abort an already-running worker-thread
operation. The database operation completes or rolls back even when the caller
no longer receives the result. Callers must account for that semantic when
performing follow-up state changes.

Shared live `LinkNode` projections are event-loop-owned. A lane-dispatched
function may build and persist events but must not mutate live Link state from a
worker thread.

### 13.2 Atomic invariants

A read-check-write invariant across connections requires one explicit write
transaction:

- begin the write transaction before reading;
- re-fetch current state;
- evaluate safety and no-op conditions from fresh rows;
- write the mutation and audit record atomically;
- roll back on every failure.

The last-usable-SysOp guard is the reference pattern.

### 13.3 Migrations

Migrations are append-only. Never edit a migration which may already have
shipped.

SQLite table rebuilds are dangerous when the rebuilt table is a foreign-key
parent: dropping it can trigger cascade or `SET NULL` actions before the
replacement exists. Prefer `ALTER TABLE ADD COLUMN`, indexes, and explicit
cleanup over rebuilds. When a rebuild is unavoidable, test it against realistic
related rows and the actual dependency graph.

A database from a newer build must fail startup clearly. A matching
`user_version` cannot prove an operator has not manually changed old schema;
manual schema mutation is unsupported unless a future schema fingerprint is
introduced.

### 13.4 Backup and restore (issue #60's first operational slice)

A node's recoverable state is not only its database — it is five artifacts,
today scattered across derived, `db_path`-relative filenames with no single
existing tool that treats them as one recoverable set:

| Artifact | Location | Written by |
|---|---|---|
| Database | `db_path` | every domain write |
| Content blobs | `db_path.parent / f"{db_path.stem}_files"` (git-style `xx/xxxx...` sharding; excludes its own `.incoming/` staging subdirectory, which is always crash-orphan garbage — see `purge_incoming_staging`) | `netbbs.files.storage` |
| Node identity | `identity_dir` (`root.identity`, `signing.identity`, `transport.identity`, `transitions.json`) | `netbbs.link.node_identity` |
| SSH host key | `db_path.parent / f"{db_path.stem}_ssh_host_key"` | `netbbs.net.ssh.ensure_host_key`, once, at first startup |
| Welcome banner | `db_path.parent / f"{db_path.stem}_welcome_banner.ans"` | SysOp, via the welcome-banner menu screen |

A backup covering only the database silently loses the SSH host key (every
client gets a MITM warning on next connect after restore) and, far more
seriously, the Link node identity (root-key custody is explicitly "part of
ordinary node backup and restore" per §4.5's node identity model, not a
separate ceremony) — so this design treats all five as one atomic backup
operation, never a DB-only one.

**Mechanism**: a new `netbbs.backup` module (synchronous, path-based — no
`Database` wrapper needed, since a backup must be safely takeable against a
*live, running* node, not only an offline one) with two entry points, plus a
`python -m netbbs.backup {create,restore}` CLI in the same spirit as
`python -m netbbs.admin` — deliberately a standalone process rather than an
interactive SysOp-menu action, since backups need to be cron-schedulable
(this project has no background scheduler anywhere, and won't grow one just
for this — matching `_sweep_expired_posts`'s and `files.gc`'s own precedent
of "the operator/an external trigger drives it, not a built-in timer").

`create_backup(*, db_path, identity_dir, destination)`:

1. **Database**: reuses `netbbs.selfupdate.snapshot_database` verbatim
   (`sqlite3.Connection.backup()`, already proven safe against a live WAL
   database in `test_snapshot_and_restore_database_round_trip`) — written to
   `destination/netbbs.db`. Never a raw file copy.
2. **Content blobs**: `shutil.copytree` of the blob root into
   `destination/files/`, `.incoming/` excluded. Must run strictly *after*
   step 1, not before or concurrently — this is what makes the DB-then-
   blobs ordering below actually safe, not just a stated convention:
   `netbbs.files.entries`'s own invariant is that a `files` row is only ever
   created after its bytes are already durably written to storage, never
   the other way around. So every blob a given DB snapshot's rows could
   possibly reference was already on disk before that snapshot was even
   taken — copying blobs afterward is guaranteed to include all of them,
   plus possibly a few newer, still-unreferenced ones from uploads that
   landed in between (harmless — an orphaned blob a future GC pass could
   still reclaim, never a dangling reference). Reversing the order would
   risk the opposite, genuinely broken case: a DB snapshot referencing a
   blob the copy hadn't reached yet.
3. **Node identity, SSH host key, welcome banner**: plain file copies (each
   is either static after creation or already rewritten via its own
   atomic-replace pattern — `node_identity.py`'s `transitions.json`, notably
   — so no read-tearing hazard). The welcome banner is the one exception
   with no atomicity guarantee on its own writes; a backup landing mid-edit
   could capture a half-written banner. Accepted as-is: purely cosmetic, no
   correctness consequence, not worth an atomic-write retrofit just for
   backup's sake.

Writes `destination/manifest.json` last (timestamp, `netbbs.__version__`,
the database's own `PRAGMA user_version`, and the five source paths as
recorded) — lets an operator (or a future restore-time check) confirm what a
given backup directory actually is before trusting it. Also records
`last_backup_at`/`last_backup_path` into the live node's own `node_config`
table (same key-value store `netbbs.selfupdate`'s update-check state already
uses) — purely for a future read-only SysOp status line (`_system_menu`,
alongside `[W]elcome`/`[U]pdate`/`[T]imestamp`/`[L]ink status`; letter `K`
for "bacKup", since `B` is already every submenu's universal `[B]ack`), not
required for restore itself.

`restore_backup(*, source, db_path, identity_dir)` reverses the five copies
-- **superseded by §13.10's staged/validated workflow (issue #75)**: the
original mechanism restored each artifact in place, sequentially, with no
validation before the first live path was overwritten and no recoverable
state if interrupted partway. §13.10 replaces the restore side of this
mechanism; `create_backup` and the artifact table above are unchanged.

Restoration always resumes the same node identity; there is still no
supported way to run an old and a restored instance simultaneously -- a
second instance of the same identity already running on a *different*
machine remains an accepted, documented operator responsibility (§13.10's
own PID-file check only ever covers *this* machine).

**Explicitly deferred, not part of this slice**: encrypting backup
contents at rest (identity material is already unencrypted-by-default on a
live node — see §4.5 — and this tool preserves whatever it finds rather
than changing that policy); off-site/remote transport of a completed backup
directory; retention/rotation of old backups; and any form of automatic
scheduling. All are operator/cron responsibilities this tool deliberately
does not take on, the same boundary `files.gc`'s SysOp-triggered-only
design already draws for blob garbage collection.

### 13.5 Bounded remote influence

Every remotely influenced queue, mailbox, retry set, retained-event collection,
transfer, relay store, and bandwidth consumer needs:

- an explicit limit;
- defined backpressure/rejection behavior;
- retry and terminal-failure policy;
- SysOp-visible state;
- safe defaults.

Security state and unread user data must not be silently discarded.

### 13.6 Operational control surface

Issue #60 remains the authority for the incomplete production operating model,
including:

- generic persistent outbound-work items, retry, backoff, dead-letter,
  replay, and cancellation (§13.7 specifies this — `netbbs.link.work_items`,
  implemented, wired into `netbbs.link.mail`/`netbbs.link.sync`, and
  surfaced as an `[O]utbox` SysOp screen);
- sync-lag and historical/trend peer-health visibility (a read-only current-
  state view — peer count/mode, dial-reliability score, last contact, relay
  activity, board/event counters, and relay-mailbox size — is available in
  the SysOp menu's `[L]ink status` screen; per-seed health has nothing to
  show yet, since no per-seed success/failure tracking exists);
- disk, event, mailbox, relay, and bandwidth quotas (§13.9 — peer-count,
  events-per-request, carried-board-count, received-post-size, request-
  body-size, and request-rate quotas, implemented. Event-retention/purging
  and node-wide disk quota are explicitly deferred out of that slice — see
  §13.9's own reasoning);
- integrity checks and crash recovery (§13.11 — a startup `PRAGMA integrity_
  check`, plus confirming migration/incoming-upload/work-item crash safety
  already held by construction — implemented);
- bounded diagnostic log retention without content logging (§13.11 — a new
  `link_diagnostic_log` table and `[D]iagnostic log` SysOp screen, warning-
  level-and-above only, age/row-bounded — implemented);
- protocol/database upgrade and rollback compatibility (§13.11 — the
  database half already done via `netbbs.selfupdate`; the wire-protocol
  half, `netbbs_protocol` version-checked on receipt for the first time,
  implemented);
- graceful drain of Link work during shutdown (§13.11 — `run_link_sync`
  finishes its current pass before stopping, including waking early from
  its own idle interval sleep rather than waiting it out, bounded by the
  existing `graceful_delay_seconds`, falling back to today's hard cancel
  only past that bound — implemented);
- disaster recovery drills exercising a restore under realistic conditions
  (§13.4 specifies the backup/restore mechanism itself — `netbbs.backup`,
  implemented; §13.10 replaces its original restore mechanism with a
  staged, validated, interruption-recoverable one and proves it against
  corrupt/truncated backups, missing components, and mid-switch
  interruption — issue #75, implemented, including a documented drill at
  `docs/NetBBS-disaster-recovery-drill.md`).

An externally operated persistent Link node should not be considered production
ready before these controls exist and have been exercised.

### 13.7 Outbound work items and retry (issue #60's second operational slice)

**Scope decision, made here rather than assumed**: this does *not* uniformly
cover every retry-shaped mechanism in the Link subsystem — only the two that
actually share the same shape. Auditing what exists today:

| Mechanism | Current behavior | Fits a work-item model? |
|---|---|---|
| Board/identity event gossip (`netbbs.link.sync`) | Every node-owned event is unconditionally re-pushed to every seed, every fixed-interval pass, forever — no attempt counter, no per-peer state at all. Safe and cheap only because the receiving side's own dedup (`link_events`) makes redundant delivery free. | **No.** There is no terminal "gave up" state that makes sense — a node's own content should be gossiped for as long as the node exists. Forcing this into a per-target attempt/backoff/dead-letter model would be inventing a failure mode (and per-peer tracking overhead) this mechanism deliberately has never needed. |
| Relay selection/consent maintenance (`_maintain_relay_selection`) | Continuously re-evaluated every pass against an evolving reliability score (`netbbs.link.reliability`), not a single item that must eventually resolve once. | **No.** This is ongoing re-optimization among many candidates, not "keep trying this one specific thing until it succeeds or we give up." It already has its own retry-like model (score-driven re-ranking); wrapping it in a second, differently-shaped abstraction would just be two competing retry policies for the same decision. |
| Link mail delivery (`mail_messages.link_delivery_status`) | Every `'pending'` row is re-pushed to its recipient every sync pass, forever, with **no cap** — the schema already reserves an unused `'expired'` status value for exactly this gap (round 93), never produced by any code path today. | **Yes.** A specific payload to a specific fingerprint that must eventually be confirmed or abandoned — the canonical case. |
| Link mail acknowledgement delivery (`link_mail_acknowledgements.sent_at IS NULL`) | Identical shape and identical gap: re-pushed every pass forever, no cap, no dead-letter. | **Yes.** Same reasoning as mail delivery. |

So `netbbs.link.work_items` is scoped to Link mail delivery and Link mail
acknowledgement delivery only — the two mechanisms that are both (a) a
specific payload addressed to a specific fingerprint, and (b) currently
missing exactly the retry/backoff/dead-letter/inspection issue #60 asks for.
Gossip and relay maintenance keep their existing, already-fit-for-purpose
models unchanged.

**A second scope narrowing, discovered while designing this**: a *work
item* resolving successfully means "the payload was successfully pushed to
the recipient's transport (or deposited at a relay)" — never "the recipient
confirmed receipt." That confirmation, for mail specifically, is a separate,
higher-level thing: `apply_link_message_accepted`/`apply_link_message_
bounced` already handle it, driven by a genuine signed event coming back,
completely unrelated to whether the push itself succeeded. Conflating the
two was a real risk in an earlier draft of this design — a work item is
**"pushed"** or **"dead_lettered"**/**"cancelled"**, never **"delivered"**;
`mail_messages.link_delivery_status` keeps its own independent
`'pending'`/`'delivered'`/`'bounced'` vocabulary, driven by accepted/bounced
events exactly as today. The one integration point is one-directional: when
a `link_mail_delivery` work item dead-letters or is cancelled (the payload
could never even be successfully pushed, or a SysOp gave up on it
manually), the caller — not `netbbs.link.work_items` itself, which stays
completely kind-agnostic — sets `mail_messages.link_delivery_status =
'expired'`, finally giving that reserved value a real producer. A
successfully **pushed** work item changes nothing on `mail_messages`: it
still waits for accepted/bounced exactly as it does today, except the sync
loop stops wastefully re-pushing bytes that already arrived once — a real
efficiency fix, not just new capability.

**Schema** (`link_work_items`, matching this project's established Link-table
conventions — `TEXT NOT NULL` ISO timestamps, a `status` CHECK-constraint
enum, a partial index on the still-pending predicate):

```sql
CREATE TABLE link_work_items (
    id                  INTEGER PRIMARY KEY,
    kind                TEXT NOT NULL,  -- 'link_mail_delivery' | 'link_mail_ack'
    reference_id        TEXT NOT NULL,  -- mail_messages.link_event_content_id, or the ack row's own id
    target_fingerprint  TEXT NOT NULL,
    status              TEXT NOT NULL
                        CHECK (status IN ('pending', 'retrying', 'pushed', 'dead_lettered', 'cancelled')),
    attempts            INTEGER NOT NULL DEFAULT 0,
    next_attempt_at     TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    last_attempt_at     TEXT,
    last_error          TEXT,
    resolved_at         TEXT,
    UNIQUE(kind, reference_id, target_fingerprint)
);

CREATE INDEX idx_link_work_items_due
    ON link_work_items(next_attempt_at) WHERE status IN ('pending', 'retrying');
```

`reference_id` is a pointer, not a payload copy — `netbbs.link.work_items`
never stores or looks at the actual signed event bytes; the caller (mail
delivery/ack-push code in `sync.py`) already has those via the referenced
row and is the only thing that knows how to actually attempt the push.

**State machine**: `pending` (never attempted) → `retrying` (attempted at
least once, not yet resolved) → one of `pushed` / `dead_lettered` /
`cancelled` (terminal). `enqueue_work_item` is idempotent on
`(kind, reference_id, target_fingerprint)` — creating a mail message or an
acknowledgement always enqueues its work item at that same moment, so
there's no separate "did we remember to schedule this" step to forget.

**Backoff and dead-letter thresholds** (product judgment, not derived from
anything load-bearing — adjustable later): each failed attempt schedules
the next one at `min(300s * 2^attempts, 6h)` (starting at the sync loop's
own default interval, since backing off faster than the loop even runs is
meaningless, doubling from there, capped at six hours so a long-unreachable
target still gets retried a few times a day rather than trailing off to
nothing). Dead-lettered once `attempts >= 10` **or** `now - created_at >= 5
days`, whichever comes first — the attempts cap is what actually fires in
the common case (already ~29 hours of real spacing by the tenth attempt);
the age cap is a safety net for a node that was itself offline for a
stretch and so accumulated attempts slower than wall-clock time would
suggest.

**Mechanism**: no new background loop. `netbbs.link.sync.run_link_sync`'s
existing fixed-interval loop already iterates several independently-loaded
"pending work" lists every pass (seeds, pending mail, pending
acknowledgements, relay candidates) — `_push_pending_link_mail`'s and the
acknowledgement-pushing code's own `load_pending_link_mail`/
`load_pending_link_mail_acknowledgements` calls are replaced by
`load_due_work_items(kind=...)` (status in `pending`/`retrying` *and*
`next_attempt_at <= now`), and each attempt's outcome is recorded via
`record_success`/`record_failure` instead of silently falling through to
"try again next pass regardless." Everything else about the loop — lane
dispatch, per-item try/except-log-and-continue tolerance, the fixed sleep
— is unchanged.

**SysOp surface**: `list_work_items` (filterable by status/kind) plus
`replay_work_item`/`cancel_work_item` (both audit-logged via
`record_action`, matching every other SysOp-triggered mutation in this
codebase) — a new admin-menu screen, most naturally alongside `[L]ink
status` under `System`, listing dead-lettered/retrying items with a picker
to inspect one and replay or cancel it. `replay_work_item` resets a
`dead_lettered`/`cancelled` item to `pending` with `attempts = 0` and, for
`link_mail_delivery`, is the one place the caller undoes the
`mail_messages.link_delivery_status = 'expired'` side effect back to
`'pending'` — symmetric with how dead-lettering set it.

**Explicitly deferred, not part of this slice**: retention/purge of old
`dead_lettered`/`cancelled`/`pushed` rows (this table will otherwise grow
without bound — a real gap, but a generic "how long do resolved audit-
shaped rows live" question this project doesn't have an established answer
to yet, not specific to work items); applying this abstraction to any
future work kind beyond the two named here without first checking it
actually fits the shape (see the scope-decision table above — the fit
matters more than the count of kinds). The quotas/integrity-check/
log-retention/upgrade-rollback/graceful-shutdown bullets in §13.6, separate
pieces of issue #60, were open when this slice was written and have since
been closed by §13.9/§13.11.

Implemented: `netbbs.link.work_items` (schema, backoff/dead-letter,
replay/cancel, all audit-logged); `netbbs.link.mail.compose_link_message`/
`_queue_acknowledgement` enqueue a work item in the same transaction as
the row it tracks; `netbbs.link.sync._push_pending_link_mail` now attempts
only currently-due work items instead of unconditionally resending every
pending row every pass, and already skips a push entirely once a message
has resolved through some other path (a genuine accepted/bounced event, or
an earlier dead-letter); a new `[O]utbox` SysOp screen (`System` submenu,
gated on Link being configured, same as `[L]ink status`) lists
retrying/dead-lettered items and lets a SysOp replay or cancel one.
Verified end to end via `tests/test_link_sync.py`'s existing real-socket
sync tests (unchanged, still passing against the refactored push loop)
plus new dedicated tests for the state machine, the mail/ack integration,
and the SysOp screen.

### 13.8 Session lockdown, drain, and shutdown

Three related but distinct SysOp `[N]ode`-menu controls over who can
connect and who stays connected, each answering a different question:

| Control | Question it answers | New logins | Already-connected sessions | Reversible | Ends the node process |
|---|---|---|---|---|---|
| `[S]hutdown` | "Take this node down" | Blocked for everyone, no bypass | Warned, then all disconnected (immediately, or after a grace period) | No | Yes |
| `[M]aintenance mode` | "Stop admitting ordinary users for now" | Blocked for non-SysOps; a SysOp can still log in | Untouched | Yes — toggle again | No |
| `[D]rain` | "Clear ordinary users off, right now" | Unaffected (not this control's job) | Non-SysOps warned, then disconnected after an operator-chosen delay; SysOps (including the issuer) untouched | N/A — one-shot action | No |

`[S]hutdown`'s own sequence (round 51) already had the right order in
code — lock out new logins and warn everyone immediately, then disconnect
once the delay elapses — but its confirmation prompt's wording had drifted
out of sync with that, describing disconnect happening *before* the
lockout; fixed to match the actual, unchanged behavior.

**`[M]aintenance mode`** (`netbbs.net.maintenance.MaintenanceMode.
enable_lockdown`/`disable_lockdown`/`is_lockdown_active`) is a second,
independent flag on the same class that already holds shutdown's
`activate`/`is_active` — deliberately not the same flag, and deliberately
checked at a different point in the connection lifecycle: shutdown's gate
fires before login even begins (nothing is known yet about who's
connecting, so no bypass is possible or desired — the whole node is going
away regardless); lockdown is checked *after* credentials verify
(`netbbs.net.login_flow.run_authenticated_session`), specifically so a
SysOp can still reach the menu that turns it back off. A SysOp who logs in
while it's active sees a `(Maintenance mode is ON.)` notice appended to
their welcome line; a non-SysOp sees `LOCKDOWN_MESSAGE` and is disconnected
before ever reaching the main menu. Turning lockdown on does nothing to
sessions already connected — that is `[D]rain`'s job, a deliberately
separate action, not an implied side effect.

**`[D]rain`** (`netbbs.net.shutdown.run_drain_sequence`) borrows
`run_shutdown_sequence`'s warn-then-disconnect shape but never touches
`maintenance`/`shutdown_event` — the node keeps running throughout.
`ActiveSessionRegistry.broadcast_to_all`/`disconnect_all` both gained an
`exclude_sysops` parameter (backed by a new `is_sysop` flag recorded on
each session's entry at `mark_authenticated` time) so a SysOp, including
whoever issued the drain, is never warned or disconnected by it — the
whole point is staying connected to keep managing the node while ordinary
users clear out for a change that needs a reconnect to take effect.

**The intended workflow** (Thiesi's own framing): turn on `[M]aintenance
mode` first — if nobody else is online, or existing sessions don't need
disturbing, that alone is enough. If someone connected already needs to be
moved along, follow up with `[D]rain`. The two are composable, not
coupled: `[D]rain` never enables lockdown itself, and enabling lockdown
never triggers a drain — each is a deliberate, separate SysOp decision,
matching how follow/membership/node-carry are kept independent elsewhere
in this design (§6.6) rather than one silently implying another.

### 13.9 Quotas: closing the remaining bounded-remote-influence gaps (issue #60's third operational slice)

**Audit before design, same discipline §13.7 used for work items.** §13.5
already states the general rule (every remotely influenced resource needs an
explicit limit, defined rejection behavior, and SysOp visibility); this
section is the concrete audit of where that rule is and isn't met yet, and
the scope decision for what this slice actually closes.

**Already bounded, no change needed here** — peer-list entries per request
(`_MAX_PEER_LIST_ENTRIES_PER_REQUEST = 100`), unverified candidate
descriptors (`_MAX_CANDIDATE_DESCRIPTORS = 500`, new-fingerprint admission
capped but refreshing an already-tracked candidate is always allowed),
relay-serving slots (`max_relay_clients`, decline-not-error), relay mailbox
envelopes per recipient (`MAX_MAILBOX_ENVELOPES_PER_RECIPIENT = 50`, HTTP 507
on overflow, no eviction), Link mail delivery/acknowledgement retry
(§13.7's backoff-then-dead-letter), local mailbox size (`MAX_MAIL_PER_
RECIPIENT`, evict-oldest-read/refuse-if-all-unread — already applied to
incoming Link mail too, bouncing rather than silently dropping), and Zmodem
upload size (`max_upload_bytes`, checked against both claimed and actual
running size).

**Gaps this slice closes** — every currently-uncapped *admission* point in
the Link protocol, plus the two content-validation gaps that most directly
let one peer impose unbounded cost on another node:

| Gap | Fix | Enforcement idiom |
|---|---|---|
| `LinkNode.peers`/`link_peers` — any node that completes a hello becomes a permanent peer, no cap (mirror-image gap to candidate descriptors, which *are* capped) | New `LinkConfig.max_peers` (default 1000 — generous relative to §14's declared small-network scale, but no longer infinite) | Same shape as candidate descriptors: admitting a genuinely *new* fingerprint past the cap is refused; a hello from an *already-known* peer (key rotation, descriptor refresh) is always accepted regardless of the count. Implemented as `handle_hello`'s own optional `max_peers` keyword (`None` default, unbounded, preserving every prior caller) rather than `handle_relay_consent_request`'s "caller decides" split — that idiom exists specifically for relay-consent's in-band `accepted=False` reply shape, which `handle_hello` has no equivalent of; refusing a hello is a whole-request failure either way, the same shape peer-list's own internal cap already uses. Only threaded into the *inbound* path (`LinkServer._handle_hello`/`_handle_relay_mailbox_pickup`) — `dial_hello` (outbound) is left unbounded by this cap, already indirectly bounded by `_MAX_CANDIDATE_DESCRIPTORS` plus the operator's own small configured seed list. |
| `handle_events` batch size — no per-request cap, unlike peer-list's own `_MAX_PEER_LIST_ENTRIES_PER_REQUEST` from the same design round | New `_MAX_EVENTS_PER_REQUEST = 200` beside the existing constant in `protocol.py` | Reject the whole batch with `LinkProtocolError`, identical to the peer-list precedent — a genuine sync backlog still drains over several passes rather than one unbounded request. |
| `board_post`/`board_post_edit` content size — zero validation on receive, unlike locally created posts (`netbbs.boards.posts.MAX_SUBJECT_BYTES`/`MAX_BODY_BYTES`) | Apply the same two constants inside `handle_events`'s `board_post`/`board_post_edit` branches | `LinkProtocolError`, matching every other malformed-event rejection already in that method. |
| Carried-board count — `materialize_carried_board` turns any verified `board_genesis` into a local `Board` row unconditionally | New `LinkConfig.max_carried_boards` | The `board_genesis` event is still verified, accepted, and gossiped on past this node (dedup and chain integrity for *other* nodes must not depend on this node's own local storage choices) — only *materializing* a local, browsable `Board` row is refused once the cap is hit. This is the exact shape §9.3 already specifies for a local exclusion: represented honestly as "not carried on this node," never a silent, indistinguishable drop. |
| Link HTTP request body size — neither `LinkServer` nor `WebServer` sets `client_max_size`, so both silently inherit aiohttp's implicit 1 MiB default | Set `client_max_size` explicitly on `LinkServer`'s `web.Application()`, sized to comfortably fit `_MAX_EVENTS_PER_REQUEST` worth of events (2 MiB) | Turns an accidental library default into a deliberate, documented value; aiohttp's own 413 response is unchanged (not worth reshaping into a `LinkProtocolError` payload for a request that was rejected before any handler ran). |
| Link HTTP request rate — no throttling on any Link route at all, including the two unauthenticated ones (`/hello`, `/peers`) | New `netbbs.net.throttle.LinkRequestThrottle` (a small public wrapper around the existing `_KeyedTokenBuckets` machinery `LoginThrottle` already uses internally), keyed by source address, applied via an aiohttp middleware on every route -- constructed once in `netbbs.__main__` from three new flat `LinkConfig` fields (`request_rate_capacity`/`request_rate_refill_per_minute`/`request_rate_max_tracked_sources`, not a nested sub-dataclass) and passed into `LinkServer`, the same "build once, node-lifetime, threaded into the one real server" shape `_build_throttle` already uses for `LoginThrottle` | Exceeding it returns a plain HTTP 429, no signed payload needed (an unauthenticated-route response can't be signed meaningfully anyway). `None` throttle (every caller predating this) is a middleware no-op, not a hard requirement. |

**Explicitly deferred, not part of this slice** — following §13.7's own
precedent of naming what's excluded and why, rather than silently narrowing
scope:

- **`link_events` retention/purging.** The two places this gap already exists
  in code (`storage/migrations.py`, `link/store.py`) both name the same
  blocker themselves: purging has to reckon with `handle_events`'s own
  chain-idempotency self-heal logic for `key_transition`/`board_post_edit`/
  lifecycle events first, or a purge could resurrect a fork-detection false
  positive for an event this node legitimately already integrated. That is
  its own design pass, not a quota default.
- **Node-wide disk quota on blob storage** — the literal "disk" word in issue
  #60's own bullet, and still the single largest true gap (no `shutil.disk_
  usage`, no running byte counter, no code anywhere aware of aggregate
  storage consumption). Needs genuinely new disk-usage-tracking machinery
  this codebase doesn't have yet, not a threshold check against an existing
  number — sized as its own future slice rather than folded in here.
- **`link_work_items` terminal-row retention.** Locally driven growth (a
  node's own composed mail history), not remote-influenced admission — pairs
  more naturally with issue #60's separate "log retention" bullet than with
  quotas, and every individual item is already bounded by dead-lettering.
- **Zmodem transfer-rate (time) limiting.** The existing byte-size cap plus
  `ThrottleConfig`'s connection/session limits already bound the worst case
  tolerably; true transfer-rate instrumentation isn't worth building until a
  real problem is observed.

**SysOp visibility.** `[L]ink status` gains a peer-count line showing
`current/max_peers` (matching the existing `relaying_for`-slots-in-use
display precedent) and a carried-boards `current/max_carried_boards` line;
rate-limit rejections are logged the same way `LoginThrottle` rejections
already are, not surfaced as a separate screen in this slice.

Implemented.

### 13.10 Staged, validated restore (issue #75)

**Problem, confirmed by reading the code, not assumed from the issue alone**:
the original `restore_backup` copies each of the five artifacts (§13.4)
straight into its live path, sequentially, in place -- `shutil.copy2`/
`copytree` directly onto `db_path`/`identity_dir`/etc., with the previous
live directory `shutil.rmtree`d immediately before the replacement copy
starts. Nothing about the backup is checked before the first live path is
touched (the manifest's own fields today are metadata only -- no
checksums), and an interruption mid-copy leaves neither the old state (already
half-deleted) nor a complete new one -- exactly the "restore intended to
recover a node can make it less recoverable" failure the issue describes.
The write-lock probe (`_require_not_in_use`) also only ever catches a
transaction genuinely in flight at that instant, not an idle-but-running
node holding no lock between transactions -- its own docstring already
said so.

**Mechanism**: validate everything first, stage a full copy, then switch
staged artifacts into their live paths with atomic renames -- never restore
by copying directly onto a live path again.

**1. Manifest gains per-artifact checksums.** `create_backup` now writes
`manifest["checksums"] = {relative_path: sha256_hex}` for every file it
captures *outside* the content-addressed blob tree: the database snapshot,
each of the four identity files, the SSH host key, and the welcome banner.
The blob tree needs no manifest entry at all -- `netbbs.files.storage`
already lays every blob out at `root/{sha256[:2]}/{sha256}`, so a blob's own
path *is* its claimed hash; restore verifies the tree by recomputing each
blob's hash and checking it against its own filename, catching truncation/
corruption with no extra bookkeeping and no manifest growth as a node's
file area grows.

**2. Full validation before any live path is touched.** A new
`_validate_backup_source(source) -> Manifest`, called first, unconditionally:
manifest exists and parses; every checksummed file listed is present and its
hash matches; the database snapshot passes `PRAGMA integrity_check` *and*
opens cleanly as a real `netbbs.storage.database.Database` (this reuses,
rather than reimplements, that class's own existing "refuse a schema newer
than this build supports" guard from `_apply_migrations` -- restoring a
backup taken by a newer NetBBS version onto an older install was already a
real risk this makes checked, not just checked-if-someone-remembers); the
identity directory, if present, actually loads via `netbbs.link.node_
identity.NodeIdentity.load` (a genuine functional check -- chain-to-key
consistency and all -- not just "the files exist"). Any failure raises
`BackupError` with the specific problem before a single live byte moves.

**3. Node-liveness check gains a PID file, kept as a second layer alongside
the existing lock probe, not a replacement for it.** `netbbs.__main__`
writes its own PID to `db_path.parent / f"{db_path.stem}.pid"` once
started, removed in the same `finally` that already closes the database on
every exit path (SIGTERM, SIGINT, and startup failure alike). Restore reads
this file if present and checks the PID is still alive with a portable,
best-effort liveness check (`os.kill(pid, 0)` on POSIX; a `tasklist` shell-
out on Windows for local dev/test convenience -- the deployment target is
NetBSD, where the POSIX path is what actually matters) -- refuses if alive,
catching the idle-but-running case the lock probe alone could not. A PID
file present but pointing at a dead process is treated as a stale leftover
from an unclean exit (warn, proceed) rather than a hard refusal -- the same
"an operator responsibility, not a load-bearing distributed lock" framing
this section already applies to the cross-machine case.

**4. Stage before touching anything live.** Every artifact is copied (with
its checksum reverified against the fresh copy, catching corruption
introduced by the staging copy itself, not just the original backup) into
`db_path.parent / f".netbbs-restore-staging-{token}"` -- a sibling directory
on the same filesystem as the live targets, which is what makes step 5's
renames atomic rather than a second copy.

**5. Switch via rename, not copy, with a non-silent marker for the gap
between the first and last rename.** Before switching anything, restore
writes a small state file (`db_path.parent / ".netbbs-restore-state.json"`)
naming the staging directory, a per-token rollback directory, and which
artifacts remain to switch. For each of the five targets in turn: rename
the current live artifact (if any) into `db_path.parent /
f".netbbs-restore-rollback-{token}"` under its own name, then rename the
staged artifact into the live path, updating the state file after each
completed step. If any single rename fails, everything already switched is
renamed back from the rollback directory (best-effort, since the renames
already succeeded once and are switching back onto paths that still exist)
before re-raising -- recovering the previous generation automatically in
the common case. The state file is removed only once every artifact has
switched (success) or every switched artifact has been rolled back
(recovered failure); if the process is killed outright mid-switch rather
than raising a catchable exception, the state file survives as the "clearly
identified... not a silent mixture" record the acceptance criteria asks
for, and a subsequent `restore` invocation refuses to start a new one over
an unresolved marker rather than compounding the mess.

**6. The rollback generation is not auto-deleted on success.** A completed
restore leaves `.netbbs-restore-rollback-{token}` on disk holding the
*previous* live state, not silently discarded -- matching this project's
"never silently discard state a human might still need" stance (§13.5) and
`netbbs.selfupdate`'s own "kept on disk, rotated out" precedent for a
superseded release directory. The CLI prints its path; cleanup is an
explicit operator/cron action (retention/rotation stays out of scope here,
same as §13.4's own already-deferred list), not automatic.

**Disaster-recovery drill.** Documented at `docs/NetBBS-disaster-recovery-
drill.md`: stop the node; corrupt or truncate a real backup and confirm
restore refuses before touching anything live; interrupt the restore
process mid-switch and confirm the previous generation is intact or the
state file clearly names what to do; complete a real restore and confirm
identity continuity (same fingerprint), every configured transport still
authenticates, previously created local content is still browsable, and
Link resumes gossiping with its peers on restart. Proven functionally,
live, against a real running/killed/restarted node process during
implementation -- corruption refusal (checksum mismatch caught before any
live byte moved, confirmed via before/after hashing), the PID-file check
against a genuinely running process, a stale PID file from a hard-killed
process correctly tolerated rather than blocking restore, and a full
restore-then-restart cycle with identity/content verified. Running the
drill specifically on NetBSD hardware remains the one piece this design
enables but does not itself execute from a non-NetBSD development
environment.

Implemented.

### 13.11 Closing issue #60: integrity, diagnostics, protocol compatibility, graceful Link drain

Four remaining, previously-open bullets from §13.6 — audited individually
below, same discipline §13.7/§13.9 already used, each narrower in places
than its one-line issue wording once actually read against the code.

**1. Startup integrity check and crash recovery.** Confirmed by grep: `PRAGMA
integrity_check` exists nowhere in this codebase except inside issue #75's
own backup-validation path — an ordinary node startup opens the database
with no corruption check at all, so a corrupted file (disk failure, an
interrupted non-WAL filesystem operation, bit rot) surfaces only later, the
first time some unlucky query happens to touch the damaged page, as a raw,
confusing `sqlite3.DatabaseError` rather than a clear diagnosis at the one
point an operator can still act on it before real damage compounds.
`netbbs.__main__.run()` now runs `PRAGMA integrity_check` immediately after
`Database(config.db_path)` opens, wrapped into the same `StartupError`
message shape that already handles "wrong build/version" — refusing to
serve traffic against known corruption, matching round 56's own "refuses to
start with zero SysOps" precedent for a startup condition worth failing
loudly on rather than limping past. **Deliberately not folded into
`Database.__init__` itself** — a full-database scan on *every* `Database()`
construction would tax every admin script and the entire test suite (2500+
constructions) for a check only the one long-lived node process actually
needs once, at its own startup; `netbbs.__main__` calls it explicitly, once,
itself.

Crash recovery beyond that single check turns out to already exist,
confirmed by reading rather than assumed: `_apply_migrations` commits a
migration's schema change and its `user_version` bump in the *same*
transaction, so a crash mid-migration simply leaves `user_version`
unadvanced — the next startup resumes migrating from the correct point,
never re-applies a partial migration, never needs new code. `purge_incoming_
staging` already treats every leftover `.incoming` file as crash debris from
a previous run and removes it before any listener starts. `netbbs.link.
work_items` are DB-row-backed with their own retry/backoff already, so a
crash mid-processing just leaves an item `pending`/`retrying`, picked up
normally next pass. This slice adds one regression test proving the
migration-crash-safety claim directly (kill a `Database()` open partway
through applying migrations, confirm a fresh open resumes and completes
correctly) rather than leaving it as an untested assertion.

**2. Bounded Link diagnostic log, metadata only.** No Link operational log
exists today beyond whatever `logging.basicConfig(level=logging.INFO)`
sends to stderr — ephemeral, unbounded (retention is entirely the process
supervisor's problem), and gone the moment a terminal's scrollback rotates
or the service manager's own log rotation fires. A SysOp investigating "why
did sync with peer X stop working three days ago" has nothing durable to
look at. Deliberately **not** a general application-logging overhaul — the
existing `moderation_log` table is already this project's precedent for a
structured, DB-backed log, and the new one is explicitly its bounded,
non-permanent counterpart: a `link_diagnostic_log` table (`id`, `level`,
`logger_name`, `message`, `created_at`), populated by a small `logging.
Handler` subclass attached to the `netbbs.link` logger namespace at startup
(catching every existing `_logger.warning`/`.error` call already scattered
across `netbbs.link.sync`/`.transport`/`.seedlist` via ordinary logger
propagation — no per-call-site instrumentation needed) at `WARNING` level
and above only; routine `INFO`-level chatter stays stderr-only, ephemeral,
exactly as today. Audited every existing call site this handler will now
capture (§13.9's own audit-before-design habit, applied here to *existing*
log statements rather than a new feature): every one is already about
protocol/dial/sync *events* — a URL, a fingerprint, an exception message —
never a Link message's decrypted body, a board post's content, or any other
user-authored payload. "Metadata only, never content" is therefore a
property of which fourteen call sites happen to exist today, not a new
filter this handler has to enforce — worth re-checking whenever a future
Link module adds a new `_logger` call inside this namespace.

Both `LinkConfig.diagnostic_log_max_age_days` (default 30) and
`diagnostic_log_max_rows` (default 5,000) bound it — the handler prunes
against both on every write, cheap at this log's realistic warning-only
volume. Browsable via a new `[D]iagnostic log` SysOp screen under `[S]ystem`
(alongside `[L]ink status`/`[O]utbox`/`[R]epair carried posts`, same
`link_context is not None`-gated visibility), the same paginated-picker
shape `[O]utbox` already uses.

**3. Link wire-protocol version compatibility.** A real, confirmed gap, not
a hypothetical: every canonical event envelope already carries `netbbs_
protocol` (`build_envelope`, `NETBBS_PROTOCOL_VERSION = 1`, round 27) — but
grep confirms nothing anywhere ever reads it back on receipt. A future
protocol revision bumping this field would today be silently ignored by
`handle_hello`/`handle_events`, which would then either crash on an
unfamiliar payload shape with a confusing low-level error, or — worse —
successfully parse a subset of fields that happen to still match and
silently misinterpret the rest. `netbbs.link.protocol` gains one shared
check, applied once per envelope at the single point `handle_events`
already extracts `object_type` before dispatch (covering all nine event
types from one call site, not nine), and separately against the hello
bundle's own embedded transitions/descriptor envelopes in `handle_hello` —
rejecting a `netbbs_protocol` that doesn't exactly equal this build's own
`NETBBS_PROTOCOL_VERSION` with a clear `LinkProtocolError` naming both
versions, never a raw parse failure. "Exactly equal," not a supported range
— there is no forward/backward-compatibility promise to honor yet, since
version 1 is the only version that has ever existed; the point of this
slice is having a real, tested gate *before* a version 2 ever needs one, not
guessing at compatibility rules for a wire change nobody has designed.

The **database** half of "protocol/database upgrade and rollback
compatibility" turns out to already be done, confirmed by reading `netbbs.
selfupdate`'s own module docstring rather than assumed: round 82/95/96
already snapshot the database before applying an update's migration and
roll back to that snapshot if the newly started version fails to come up
cleanly. That same docstring is explicit about the boundary this slice
closes: *"It knows nothing about NetBBS Link protocol/schema compatibility
-- that's explicitly deferred to whenever Phase 3 needs it."* Now is that
moment; the wire-protocol check above is the answer, kept as its own
concern in `netbbs.link.protocol` rather than folded into `netbbs.
selfupdate`, which stays exactly as protocol-agnostic as its own docstring
already declares.

**4. Graceful drain of Link work during shutdown.** Today, `netbbs.__main__`
tears down `link_sync_task` with a bare `.cancel()` the instant shutdown
begins — no grace period at all, the one asymmetry with ordinary user-
session shutdown, which already warns and waits before disconnecting
anyone. A `SIGTERM` landing squarely mid-pass, mid-HTTP-call, aborts that
specific request against whatever peer is on the other end with no chance
to complete — a real (if narrow) asymmetry between how this project treats
its own users and how it treats the peers it talks to. `run_link_sync`
gains an optional `stop_event: asyncio.Event | None`, checked once at the
top of the outer loop (before starting a new pass, not mid-pass —
deliberately simple: passes are normally sub-second, so the value of
checking more granularly inside one is marginal against the complexity of
doing so) so a currently in-flight pass, including whatever HTTP call it's
in the middle of, is always allowed to finish naturally. Shutdown sets the
event, then `asyncio.wait_for`s the task against the existing
`ShutdownConfig.graceful_delay_seconds` (60s default) — reusing the one
"how long is a graceful shutdown allowed to take" operator-facing number
rather than adding a second, Link-specific timer to reason about — falling
back to the pre-existing hard `.cancel()` only if that bound is exceeded (a
pass stuck on an unreachable seed's own connect timeout, say), never
removing the fallback, only making it the last resort instead of the
first.

The loop's own trailing `await asyncio.sleep(interval_seconds)` turned out
to need the same treatment, contrary to this bullet's original assumption
above that an immediate cancel there is harmless — harmless to *correctness*,
yes, but not to shutdown *latency*: `sync_interval_seconds` defaults to
300s, routinely dwarfing `graceful_delay_seconds` itself, and the node
spends most of its time asleep between passes, not mid-pass. Leaving that
sleep uninterrupted would have meant an ordinary shutdown — almost always
landing during the sleep, not during a pass — silently waited out however
much of the interval remained, then hard-cancelled anyway once
`graceful_delay_seconds` ran out regardless, delivering neither a graceful
finish nor a prompt exit. `stop_event`-provided callers now wait on `stop_
event.wait()` bounded by `asyncio.wait_for(..., timeout=interval_seconds)`
in place of the plain sleep, waking immediately once shutdown signals
rather than waiting out the full interval; an idle sleep has no in-flight
work to protect, so cutting it short costs nothing the way interrupting a
live HTTP call would. Callers that don't pass a `stop_event` (`None`, the
default) still get the original unconditional `asyncio.sleep`, unchanged.

**Explicitly out of scope for this bullet**: `seed_refresh_task` (fetches
this project's own trusted release-hosting infrastructure, not a peer's
Link endpoint — an abrupt cut has no peer-visible consequence and already
retries on its own next-scheduled, forgiving 24h cadence) and `daybreak_
task`/`update_check_task` (neither talks to a Link peer at all). Extending
graceful draining to those would be solving a problem none of them actually
have.

**Closes issue #60.** Every acceptance criterion that issue names is now
either implemented (this slice; §13.4/§13.7/§13.9/§13.10 before it) or an
explicitly deferred, separately-tracked follow-up with its own stated
reasoning (node-wide disk quota and event-retention/purging, §13.9;
per-seed historical/trend health visibility, §13.6) — not a silently
abandoned acceptance criterion.

Implemented.

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
slice per currently implemented Link product surface, each covering
restart-between-stages and duplicate-delivery. A future Link vertical
slice (linked channels, remote files, tier-2 messages) is not complete
until it adds or extends a scenario in that file, the same way it is
not complete without unit tests for its own protocol logic.

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

## 15. Roadmap and phase boundaries

### Phase 1 — Foundation — complete

- modular runtime and SQLite storage;
- node/user identity foundations;
- password and keypair login;
- Telnet, SSH, and web transports;
- ANSI rendering and input plumbing;
- level/permission foundations;
- local boards, file areas, and chat;
- local blocklist foundation.

### Phase 2 — Complete standalone BBS — complete

- local moderation and approval workflows;
- maintenance/expiry;
- user directory, profiles, and finger-style lookup;
- channel visibility, invitations, membership, and moderation;
- local private chat, presence, aliases, and completion;
- SysOp administration and node controls;
- TUI/screen-buffer foundations;
- ANSI and prose editors.

### Post-Phase-2 local additions — substantially complete

- local Communities and Community-scoped authority;
- identity attestation and gates;
- local asynchronous mail;
- self-update foundations and scheduled checks;
- registration-mode and account-lifecycle refinements.

### Phase 3 — Link connectivity and asynchronous services — active

Implemented or substantially working:

- root/operational node-key lifecycle;
- canonical event bytes and signed transition events;
- authenticated hello and endpoint descriptors;
- real HTTP+JSON transport and node startup integration;
- persistent peer and event state;
- foreground/background database lanes;
- configured seeds, live seed refresh, peer-list exchange, and candidate
  fallback;
- deterministic multi-node fault harness;
- linked-board genesis, posts, self-authored edits, and origin transfer/
  orphan/fork behavior; local materialization both of the board shell and of
  received posts/edits (§9.3, issue #73, closed);
- tier-1 Link messages with accepted/bounced delivery state;
- reliability scoring, relay consent, automatic relay selection, and bounded
  relay mailboxes for outgoing-only recipients;
- issue #60's operational controls and recovery model: backup/restore
  (§13.4, §13.10, issue #75, closed), outbound work items/retry/dead-letter
  for Link mail (§13.7), bounded quotas (§13.9), and startup integrity
  checking, diagnostic log retention, protocol/database upgrade
  compatibility, and graceful Link drain on shutdown (§13.11) — issue #60 is
  closed.

Still required for Phase 3 completeness:

- inventory/pull-based catch-up and efficient synchronization;
- correctness-preserving event/dedup retention;
- linked channels and channel lifecycle;
- remaining linked-board governance, closure, moderator edits, and tombstones;
- remote file catalogue and on-demand chunks;
- broader real-world multi-node deployment validation (issue #83).

### Phase 3 stabilization gate (issue #84)

Phase 3 already contains enough working federation behavior that later
roadmap phases could plausibly begin opportunistically. They must not.
Substantial *implementation* work on Phase 4 (trust/reputation), Phase 5
(real-time Link chat), Phase 6 (advanced governance/Link Communities), or
Phase 7 (doors) is deferred until this gate is met, unless a specific
later-phase task is required to unblock or validate Phase 3 itself. Small
preparatory design work for a later phase — for example, drafting the issue
#55 or #63 threat models — is not blocked by this gate; committing
engineering effort to *building* a later phase is.

The gate is met when all of the following hold:

- every currently implemented Link product vertical (linked boards, Link
  mail) has at least one end-to-end regression test that exercises the real
  sender/receiver/acknowledgement or sender/receiver/materialization
  boundary across a restart, not only isolated unit coverage (issue #80);
- offline/missed-event catch-up exists and demonstrably converges after a
  partition, not only live delivery during an already-connected pass;
- retained event/dedup state has a correctness-preserving retention policy:
  purging the fast dedup cache must not make an old control event
  re-applicable, nor let suppressed or deleted content reappear;
- issue #60's operational controls have been *rehearsed*, not only
  implemented: backup/restore and an upgrade/rollback have each been
  exercised against a real running node at least once beyond their original
  implementation test;
- a sustained real-world multi-node dogfood deployment (issue #83) has run
  long enough to exercise repeated sync cycles, at least one restart, and at
  least one planned partition/recovery, with findings converted into issues
  or worklog invariants rather than left as a diary;
- the README, this design document, and the worklog agree on Phase 3's
  actual boundary, and a newcomer can install and run a node from a
  documented path (issues #76, #82);
- known protocol/interoperability correctness issues (issue #70, and issue
  #71's independent-implementation proof) are either closed or explicitly
  deferred here with a stated compatibility story.

Meeting this gate does not imply public federation. Phase 4 remains the
public-readiness security gate regardless of Phase 3 stabilization, and
completing this gate does not by itself authorize starting Phase 4
implementation — Phase 4 additionally requires the issue #55 threat model.

### Phase 4 — Trust, reputation, and public readiness

- formal threat model from issue #55;
- node/user reputation;
- probation and vouching;
- objective evidence and subjective moderation separation;
- local quarantine decisions and recovery;
- remote attestation trust.

No public/untrusted federation claim precedes this phase.

### Phase 5 — Real-time Link chat

- Noise transport using node transport keys;
- Link-wide typed chat events, presence, and discovery;
- multiple simultaneous channel memberships and unread/background delivery;
- Link-wide live private chat, distinct from asynchronous Link messages;
- decide whether and how trusted recent scrollback is offered to joining nodes.

### Phase 6 — Advanced Link governance and Link Communities

- linked channels and signed membership/topic governance;
- Link-blanket moderator grants and authorized moderation events;
- advanced creation, closure, and lifecycle surfaces;
- Link Communities and signed Community membership/carry changes;
- curated governance audit board and live activity feed.

### Phase 7 — Doors and legacy compatibility

- design and prove the native door sandbox and versioned capability API;
- UI-only threading refinements;
- classic DOS door compatibility through the same constrained capability
  boundary.

Issue #63 must be resolved before door implementation begins.

---

## 16. Open design decisions

GitHub issues are authoritative and may evolve beyond this summary.

### Issue #11 — canonical Link format

§7.2/§7.3/§7.4 now state the complete rule: canonical byte encoding (sorted
keys, recursive NFC, compact separators), the safe-integer bound, duplicate-
key wire rejection, omitted-versus-null field semantics, mandatory
object-type domain separation, event-identity distinctness (a nonce for
immutable creation events; `previous_event_id` chains, never `created_at`,
for per-object chains), `(home_node_fingerprint, local_user_id)` as a
node-vouched author's globally-scoped identity, and golden test vectors
(`tests/fixtures/link_canonical_vectors.json`).

Still open: this specification and its vectors exist only as this
codebase's own Python implementation plus one fixture file. No independent,
non-Python implementation has yet exercised the vectors to prove real
cross-language interoperability, and the rule is not yet published as an
external protocol document outside this repository. Closing that gap is
implementation/publication follow-up, not a further design decision.

### Issue #56 — unread, follows, activity, and search

§6.6 now states the complete design: cursor-based read/unread state for
boards, file areas, and channels (mail already had this); replies/mentions
derived from existing `parent_post_id`/message-body fields with no new
schema; a follow/favourite table independent of channel membership and node
carry; a `[N]ew scan` activity surface covering every accessible resource,
not only followed ones, with a direct jump to the first unread item; local
FTS5-backed search scoped to this node's own carried content, explicitly
never broadcast over Link; and a zero-backfill migration story (existing
users' read cursors start empty; first post-upgrade visit sets the
baseline).

Implemented: the read-cursor table (`netbbs.activity`), the follow table, and
the `[N]ew scan` main-menu screen, wired into board/file-area viewing and
channel scrollback replay. Verified against a real Telnet session, not just
scripted tests.

Also implemented: local FTS5-backed search (`netbbs.search`) over board
posts, files, and channel scrollback, synced from every write path, gated by
the exact same visibility rules browsing already enforces, and surfaced as a
new `[F]ind` main-menu entry that jumps straight to a selected hit. FTS5
availability, this round's stated blocker, was resolved by tracing pkgsrc's
actual build chain rather than empirical access to a NetBSD box: `lang/
python312` buildlinks against `databases/sqlite3`, whose own Makefile passes
`--fts5` unconditionally, so the target Python build should always have it —
and a build that doesn't fails the migration loudly rather than silently
disabling search.

Issue #56 is fully implemented; all four §6.6 subsections have shipped.

### Issue #72 — node-local arrival order for unread state — closed

§6.6's "Node-local arrival order for carried content" subsection now
states the complete design: `user_read_cursors.last_seen_arrival_id`,
sourced from `posts`/`files`' own rowid rather than authored
`created_at`, with existing cursors backfilled on upgrade. The one
accepted, documented scope boundary: jump-to-first-unread still uses
the `created_at`-based cursor and may not navigate precisely to an
out-of-order arrival, even though unread counting now correctly flags
it.

### Issue #78 — decompose LinkNode protocol state — closed

The engineering record's "LinkNode internal state organization" entry
(§9) is the design pass this issue asked for: which of `LinkNode`'s
eleven flat fields belong together (`PeerDirectory`, `BoardEventState`,
`BoardLifecycleState`, `RelayState`), and which stay directly on the
façade (`identity`, `known_event_ids`, `events`, as the shared
substrate every object type uses). Every external consumer
(`netbbs.link.store`/`.sync`/`.transport`/`.relay_selection`,
`netbbs.net.admin_flow`, and their tests) still reads the old flat
attribute names unchanged, via backward-compatible properties over the
same live dicts -- zero test changes, zero wire/serialized-shape
changes. A future state family (inventory/pull, linked-channel
lifecycle) should follow the same shape: its own small dataclass with
narrow methods, not a further flat field.

### Issue #80 — end-to-end regression tests for cross-subsystem Link orchestration — closed

§14.1's "Cross-subsystem end-to-end scenarios" subsection now states
the complete design: `tests/test_link_end_to_end.py`, a real-transport,
real-domain-read-path vertical slice per implemented Link product
surface (linked boards, Link mail), each covering restart-between-
stages and duplicate delivery; the linked-boards and Link-mail
verticals each have one. The mail vertical also covers a dead-letter ->
replay -> real-redelivery cycle end to end. Confirmed the consolidated
mail scenario (and its restart variant) would fail on the pre-fix
issue #69 implementation by temporarily reverting the fix and observing
both fail, then restoring it. Future Link vertical slices extend this
file before being considered complete.

### Issue #74 — FTS index integrity checks and rebuild tooling — closed

§6.6's "Integrity checking and rebuild" subsection now states the
complete design: `netbbs.search.check_index_integrity`/`rebuild_indexes`,
a standalone `python -m netbbs.search check|rebuild` command, and the
explicit decision that startup does not run this check automatically
(unlike `Database.check_integrity`). Reports drift by id only, never
indexed content, for all three FTS tables.

### Issue #60 — production operations — closed

Implemented across four slices, all merged: backup/restore (§13.4,
`netbbs.backup`, verified against a real running node including a
create-wipe-restore round trip and the live-lock restore refusal); outbound
work items/retry/dead-letter (§13.7, `netbbs.link.work_items`, scoped to
Link mail delivery and acknowledgement delivery specifically — not gossip or
relay maintenance, which don't fit the same shape — wired into
`netbbs.link.mail`/`netbbs.link.sync` and surfaced as an `[O]utbox` SysOp
screen); bounded quotas (§13.9); and startup integrity checking, diagnostic
log retention, protocol/database upgrade compatibility, and graceful Link
drain on shutdown (§13.11). Staged/validated restore (§13.10) shipped
separately as issue #75.

Rehearsing these controls against a real long-running node (not just their
original implementation tests) is tracked by the Phase 3 stabilization gate
above, not by this issue.

### Issue #55 — trust and quarantine

Define the Phase-4 threat model, evidence types, independence and Sybil rules,
signal rate limits, quarantine thresholds, reversibility, and local policy
semantics.

### Issue #63 — door isolation

Define process/jail/container boundaries, filesystem/network access, resource
limits, terminal mediation, session capability API, audit, crash cleanup, and
DOS adapter behavior.

**Candidate approach, not yet committed — exposing Link to doors.** Rather than
giving doors raw Link access, mediate it through the session capability API,
sized per interaction latency:

- real-time move exchange rides the future Phase 5 real-time Link chat
  channel;
- turn submission for asynchronous games (chess, TradeWars-style turns) maps
  onto linked-board events on a shared game board, reusing existing
  carry-materialization rather than new plumbing;
- point-to-point in-game mail maps onto tier-1 `link_message`;
- federated high-score lists fit neither primitive cleanly — they are shared,
  mergeable state with concurrent writers, so they need an explicit
  conflict-resolution rule (e.g. monotonic max per player) before they can
  ride on either.

To keep door-facing boards/channels invisible to ordinary users without new
schema, consider gating them with an elevated minimum user level (e.g. 245)
rather than a new visibility flag, since minimum-level is already a resource
gate (§5.1) and sits safely below `SYSOP_LEVEL = 255`. This only works if:

- door processes write under their own capability-scoped service identity
  minted by the session capability API, not the player's own account level;
- board/channel listing queries honor the minimum-level gate, not just entry,
  so gated resources don't appear in listings for users below the threshold;
- the level band used for infrastructure resources (e.g. 240–254) is a named
  constant, so a future SysOp level-preset feature cannot hand that range to a
  real user by accident.

### Deliberately deferred without active issue

- social/M-of-N node-root recovery;
- multiple simultaneous personal user keys/devices;
- true client-side Link-mail encryption;
- Link-chat initial scrollback policy;
- schema fingerprinting beyond SQLite `user_version`;
- Community defaults as mandatory floors/ceilings;
- cross-network gateway adapters such as FTN/FidoNet.

A deferred topic becomes normative only after an explicit design decision. Do
not infer commitment from its appearance in this list.

---

## 17. Maintaining this document

Add or change text here only when it affects:

- product semantics;
- protocol or compatibility guarantees;
- authority and trust boundaries;
- persisted data meaning;
- long-lived user or SysOp behavior;
- roadmap dependency or phase scope.

Keep one current answer per topic. Replace superseded text instead of appending
correction paragraphs. Preserve only rationale which prevents a plausible but
harmful alternative from being chosen again.

Do not add:

- numbered decision rounds;
- implementation walkthroughs;
- changed-file or test lists;
- passing-test totals;
- debugging transcripts;
- transient “next up” status;
- stale issue-resolution commentary.

Use issues for unresolved work, commit/PR descriptions for change narratives,
the engineering record for durable implementation constraints, and Git history
for archaeology.

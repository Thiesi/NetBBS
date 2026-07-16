# NetBBS — Design Document (v0.2, current architecture)

Second attempt at a modern, TCP/IP-native BBS system. First attempt got far
(multi-user chat, file areas, message boards) but required a rewrite once
mesh networking ("NetBBS Link") entered scope — this attempt builds NetBBS Link in
as a foundational principle from day one instead of retrofitting it.

Status: **CONFIRMED and substantially built.** Architecture signed off
by Thiesi across the review rounds below; Phases 1–2 of the 7-phase
roadmap (§15) are complete, Phase 3 has not started. This document
holds standing design decisions only — for the full round-by-round
implementation and bugfix history, see
[docs/NetBBS-worklog.md](NetBBS-worklog.md).

---

## 1. Naming

- **NetBBS** — the project itself, named for its primary target OS.
- **NetBBS Link** — the ad-hoc mesh network connecting NetBBS nodes.
  Formal name used throughout this document and all user-facing text;
  "the Link" is expected and fine as informal shorthand in speech and
  casual conversation, the same way "FidoNet" got called just "Fido" —
  but the written/official form stays "NetBBS Link" everywhere it's
  introduced or referenced formally.
- **Linked boards** — message boards distributed/synced across NetBBS Link.
- **Link messages** — personal messages routed across NetBBS Link.
- **Board vs. Area (strict terminology, borrowed from the first attempt):**
  "Board" always means *message* board. "Area" always means *file* area.
  Never use "board" for files. So: **linked file areas**, not "linked file
  boards."

## 2. Philosophy / non-negotiables carried over from the first attempt

- NetBBS Link is foundational, not bolted on. Every feature should be designed
  with "could/should this work across NetBBS Link?" in mind, even if the
  Link-wide version ships later than the local version.
- **No node can unilaterally silence another node network-wide.** This was
  the core design principle of the first attempt, and the thing that broke
  under real-world testing (a single node's operator went rogue with no
  mechanism for the rest of NetBBS Link to react). The trust/reputation system
  in §6 exists specifically to fix that failure mode without abandoning the
  underlying principle.

## 3. Target platform & stack

- **Primary deployment target:** NetBSD (via pkgsrc).
- **Language/runtime:** Python (asyncio). Rationale: cleanest NetBSD story
  via pkgsrc, easiest to maintain solo, and BBS-scale concurrency (dozens–
  low hundreds of connections) is well within asyncio's comfort zone. Go and
  Rust were considered; Rust's tier-3 NetBSD support was judged too much
  toolchain friction for a solo-maintained project, Go was viable but Python
  was preferred.
- **Storage:** SQLite (WAL mode), per node. **Confirmed.** No separate DB
  server process needed; matches BIHshare precedent.
- **Code structure: proper modular package, not a monolithic single file.**
  The first attempt deliberately stayed a single Python script, which made
  sense for *them* — they started with a small, mostly-local scope and only
  backed into NetBBS Link and its scale much later, by which point splitting
  thousands of LOC was a bigger lift than living with the monolith. We don't
  have that excuse: full scope (including NetBBS Link) is deliberately being
  designed in from day one, so starting monolithic now would mean choosing
  to inherit their retrofit problem on purpose. "Easy to deploy" doesn't
  require "single file" — a pkgsrc package or a couple of rc.d-managed
  processes remains trivially simple for a solo SysOp regardless of how many
  modules are inside it.

## 4. Connectivity & rendering

- **Connection methods (all three, v1):** Telnet, SSH, and a web-based
  terminal emulator (xterm.js).
- **Screen rendering:** Hybrid — plain ANSI/VT100 menus with reflow for
  general use, full server-side TUI treatment reserved for specific
  heavier screens (e.g. file browser). Must degrade gracefully above
  40x24 minimum.

## 5. Identity

- **Cryptographic keypairs** — **mandatory for nodes**, **optional for
  users** (see the key-lifecycle model below, round 89 — this asymmetry
  is deliberate, not an oversight: node keys underwrite network-wide
  provenance, user keys don't). Not hierarchical (no FidoNet-style
  zone:net/node addressing), since routing happens dynamically based on
  live Link membership rather than a fixed topology.
- **Addressing:** Matrix-federation-style human-facing addresses
  (`user@node-fingerprint`), but using a pubkey fingerprint instead of a DNS
  domain for the node part. This avoids DNS as a single point of
  failure/censorship (domains can be seized; a pubkey can't be revoked by
  anyone but its owner) while keeping addresses legible.
- **User login/auth:** both supported — traditional password auth as the
  **default, zero-cryptography-exposure path**, and keypair-based
  (passwordless) auth available as an **opt-in** for users who already
  have a keypair/agent set up for other reasons (SSH, etc.) and want to
  reuse it. See round 89 for why this is the recommended default rather
  than a secondary option.

**Key lifecycle (normative, round 89 — resolves issue #51's core design
shape and the target-audience question that made it hard to answer;
exact transition-record wire format remains #11/Phase 3 protocol work).**
Driven by a simple observation: NetBBS's node operators and its ordinary
users are different audiences with very different cryptographic
competence and very different stakes if a key is mishandled. Treating
them identically — the mistake the original round 87 draft of this
section made — either over-burdens ordinary users or under-protects
nodes. Three tiers, matched to those different stakes:

- **Password-only users (the default, and expected to remain the vast
  majority): no personal keypair at all.** Exactly the BBS login
  experience that's existed since dial-up — a username and a password,
  nothing to generate, back up, rotate, or lose. On NetBBS Link, such a
  user is represented as an **opaque, node-vouched local ID** (per
  issue #11's own suggestion for password-only-author identity): their
  content carries their *home node's* signature, not a signature of
  their own. Key rotation, revocation, and recovery are consequently not
  concepts that apply to this tier at all — they're entirely the node's
  problem (see the node tier, below), invisible to the user, the same
  way nobody browsing HTTPS has ever had to think about a server's
  certificate rotation.
- **Opt-in personal user keypair, for users who already run their own
  key/agent for other reasons and want passwordless login: a single
  key, no root/operational split.** The stakes don't justify more
  structure — a compromised personal key costs one person their own
  session and reputation, not the network's provenance chain. No
  bespoke recovery mechanism is built for this tier either: losing the
  key just demotes that identity back to new-arrival status under §6's
  existing probation/vouching model, exactly like any newcomer — reusing
  a mechanism that already exists for a different reason rather than
  inventing key-recovery infrastructure.
- **Node keys (mandatory, SysOp-operated): a root key plus two
  purpose-specific operational keys — signing and transport.** A node
  key underwrites every post, board, and moderator grant it originates,
  so the stakes here are the ones that justify real structure: a
  long-lived **root key**, whose fingerprint is the node's stable
  identity for as long as the node exists, delegates to **operational
  keys** via signed transition records — one record either authorizes a
  new operational key (rotation) or marks one revoked (compromise
  response), and any node can verify a signature by walking the
  transition chain back to the root, so historical signatures stay
  verifiable across rotations. Splitting **signing** (content/events)
  from **transport** (the Noise static key, §11) directly answers the
  reuse risk issue #51 flagged, without going further than two keys —
  nothing today needs per-device keys or a separate recovery key, and
  both are cheap to add later via the same transition-record mechanism
  if a real need appears. **Root-key loss or compromise has no
  cryptographic recovery** — stated plainly rather than engineered
  around, the same no-free-lunch stance already taken for decentralized
  identity in §6; a future social/M-of-N recovery scheme is a flagged
  extension point (same treatment as §6's jurisdiction-bound authority
  keys), not designed now because nothing requires it yet.
  **UX, deliberately unceremonious:** root and operational keys both
  auto-generate silently at first node bootstrap — no manual key
  ceremony to get running. Rotation is a single guided admin-menu/CLI
  action that handles the transition-record bookkeeping for the SysOp.
  Root-key custody folds into #60's ordinary backup/restore story
  (back it up like anything else critical) rather than requiring
  separate offline/HSM handling — a SysOp who wants stronger custody
  can pursue it, but nothing in NetBBS assumes they will, matching the
  actual competence floor of a hobbyist self-hoster rather than a
  professional PKI operator.

**Explicitly deferred, not decided now:** the exact transition-record
wire/signature format (#11 owns this, as part of the canonical event
envelope, once Phase 3's semantic model is being written); multi-device/
multiple-keys-per-user for the opt-in user tier; and social/M-of-N root-
key recovery for nodes. None of these are needed for the shape above to
be sound — they're refinements to add later if a real need for them
appears, following the same transition-record mechanism rather than a
new one.

**Account lifecycle (normative section, added round 87 — consolidates
standing behavior that was previously only recoverable by reading
sign-off notes in order; resolves issue #62's point on this).** This
section covers *account*-level lifecycle only — level, disable/delete,
registration. The key-lifecycle model above (round 89) now covers most
of issue #51's design shape for the underlying **cryptographic keys**;
what remains genuinely open there is the exact wire format (#11) and the
deferred refinements just listed.

- **Creation — a single tri-state `registration_mode` (round 96,
  superseding round 87's binary description; implemented round 100):
  `open` | `approval_required` | `closed`.** Matches three real, common BBS operating postures rather
  than an arbitrary flag: **(a) public** — self-registration active
  immediately (`open`, the default, preserving today's behavior); **(b)
  closed-but-open-to-signups** — self-registration creates a
  `pending_approval` account, unable to log in on any auth path until a
  SysOp approves it (`approval_required`, round 76's existing behavior,
  unchanged); **(c) private** — no public registration surface at all,
  every account SysOp-created (`closed`, new — the genuine gap round 87
  correctly flagged as *currently* true of the code rather than a design
  goal). A single enum rather than two independent booleans, since one
  combination of the old shape (registration disabled + require approval)
  would have been meaningless — same reasoning as round 84's nullable-
  vs-explicit-zero fix. In `closed` mode, the registration option is
  **hidden at the login prompt entirely**, not offered and left to fail
  at the end — matching the conditional-visibility pattern already used
  for menu options with nothing behind them (round 84). This axis is
  independent of §6's probation/reputation system: `registration_mode`
  governs whether an account can log in **at all**; §6 governs what an
  *already-active* account is currently allowed to **do** — a type (a)
  public system's brand-new users are still in §6's read-only probation,
  unaffected by registration mode.
- **Levels:** a single `level` integer drives all gating (§13), with
  `SYSOP_LEVEL = 255` reserved as the unambiguous top of the range —
  not a separate role flag/table, so it composes with the same
  `meets_level`/`require_level` checks used everywhere else.
- **Promote/demote, soft-disable/enable, hard delete:** all SysOp
  actions (round 57), sharing one lockout guard that refuses any action
  which would leave the node with zero *usable* SysOps. "Usable"
  (corrected round 87 follow-up, restating the exact invariant fixed
  under issue #44) means all three: level ≥ `SYSOP_LEVEL`, not disabled,
  and not still `pending_approval` — a pending account can hold a
  SysOp-level row (promoted before approval, or created directly against
  the database) but every login path refuses it regardless of level, so
  it must not count toward the guard any more than a disabled account
  does. Self-delete/self-disable is allowed, gated by the same guard;
  promoting a still-pending account straight to SysOp level is refused
  outright (approve first) rather than allowed to silently satisfy the
  guard while remaining unable to log in.
- **Hard delete's `ON DELETE` behavior** is explicit per table (round 57):
  authorship/uploader columns `SET NULL` (denormalized display labels
  already survive account removal); administrative data (moderator
  grants, channel membership/invitations, preferences, blocklist entries)
  `CASCADE`s; `moderation_log`'s actor/target columns `SET NULL`, since an
  audit trail should outlive the account it names — this is also the full
  extent of "retained data after deletion."
- **Disable/delete revocation is enforced live, not just at next login**
  (round 73): a per-session background watcher polls every 5 seconds and
  forcibly disconnects a session the moment its account goes inactive,
  regardless of which screen/loop it's currently in — plus zero-latency
  in-loop checks at the main menu and chat's send loop as defense in
  depth.
- **Username mutability:** usernames are immutable post-creation — no
  rename path exists anywhere in the codebase. **Node/account migration**
  (moving an existing account's identity to a different node) is
  undesigned; it's Phase-3-or-later work, entangled with #51's
  cryptographic key-lifecycle question rather than a plain account-level
  concern.

## 6. Trust & reputation (the core fix for what broke last time)

Root cause of the first attempt's failure: fully egalitarian nodes, zero
reputation system, no mechanism to react when a node's operator went rogue
during testing.

- **Local, web-of-trust model** — no global reputation ledger, no
  network-wide vote. Each node computes its own trust view from (a) direct
  observation of a peer's behavior and (b) optionally, signals relayed from
  *other nodes it already trusts* (PGP-web-of-trust-style). This avoids
  reintroducing centralized power in a different shape (a "majority of
  nodes bans you" is still the problem the project set out to avoid).
- **Dual-layer reputation:** node-level reputation as a baseline, with
  individual user-level reputation on top of that.
- **New node/user probation:** starts read-only. Graduates to posting
  (boards/Link messages) and eventually Link chat via a **hybrid model** —
  time-based graduation by default, significantly accelerated by vouching
  from an already-established node/user.
- **Abuse handling:** local blocklists are the hard mechanism (each node
  decides who *it* stops relaying to/from — no network-wide effect from a
  unilateral decision), with shareable reputation/trust signals as a soft,
  web-of-trust layer on top.
- **Emergency quarantine (fast reaction at scale, without a master node).**
  Gradual reputation decay is too slow to react to active abuse once the
  Link has dozens/hundreds of nodes. The fix is *not* a privileged node that
  can act unilaterally — that recreates the exact centralization the first
  attempt's Master Node concept represented, which is our best working
  theory for what was actually abused during their rogue-SysOp test.
  Instead: a node enters a heavily rate-limited, time-boxed, reversible
  quarantine once a *threshold* of independent, already-established nodes
  each sign and broadcast their own "seeing bad behavior from X" flag.
  Flags propagate with priority over normal Link traffic for fast
  network-wide awareness. The threshold scales with the flagging nodes'
  own reputation weight, so a handful of colluding low-reputation/sockpuppet
  nodes can't manufacture a quarantine against a legitimate one. Quarantine
  is a circuit breaker that buys time for normal local-blocklist/reputation
  mechanisms to catch up — not a silent permanent expulsion.
- **Known extension point: jurisdiction-bound authority keys.** Some future
  node operator may face a legal requirement to be able to remove content/
  nodes immediately, no independent multi-node consensus required. This is
  retrofittable *without* changing the base protocol: a node operator can
  locally configure a specific authority's public key to carry an
  outsized flag-weight — high enough that one flag from that key alone
  crosses their own node's quarantine threshold. This is opt-in per node,
  not protocol-level, so it only takes effect for operators who choose (or
  are legally compelled) to honor that key; it cannot force removal on
  nodes outside that authority's actual jurisdiction, since no design can
  promise that while remaining genuinely decentralized (true of Tor,
  BitTorrent, and every other real decentralized network, not a NetBBS-
  specific limitation). Not implemented now — nothing requires it before an
  operator actually needs it — but noted here so this reasoning doesn't need
  to be rediscovered later.

## 7. Message propagation (boards & Link messages)

- **Content-addressed DAG.** Every message gets a unique ID derived from a
  hash of its content plus the ID(s) of whatever it's replying to/following
  — same underlying idea as Git objects or Usenet/NNTP propagation.
- **Deduplicated flood-fill gossip.** Nodes exchange "here's what message
  IDs I have" with peers; missing messages get pulled. Content-addressing
  means a message is never stored or re-relayed twice no matter how many
  paths it took to arrive.
- **Ordering:** causal via DAG parent pointers, timestamp as tiebreaker,
  **content-ID as a final deterministic tiebreaker** for genuinely
  concurrent siblings sharing both a parent and a timestamp (round 90) —
  timestamps can collide or be lightly spoofed, the hash can't. **No
  CRDT/vector-clock conflict resolution is needed for *immutable content
  objects*** (narrowed, round 90 — see the reopened issue #11 discussion
  that prompted this), since content-addressed IDs mean nodes can never
  disagree about what a given message *is* — only about which ones
  they've seen yet. **Mutable/state-changing objects** (post edits,
  moderator grants, board/channel metadata, key transitions) use a
  different, non-CRDT mechanism instead — see "Canonical event model,"
  below — rather than falling under this same blanket claim.
- **Signing:** DAG parent links + message signatures (via the keypair
  identity system) make forged history very hard to inject.
- **Store-and-forward:** full support for nodes offline for days/weeks,
  FidoNet-style. A returning node just resumes gossip and catches up.
- **Deduplication: persistent seen-event table, not Bloom filters** —
  adopted from the first attempt's design (never fully implemented by them,
  but the reasoning holds and their file-chunk detail is worth keeping
  outright). A false positive in a Bloom filter would mean silently losing
  a real post/file chunk/message — unacceptable for a BBS, so correctness
  wins over memory efficiency. Each node maintains a small persistent table
  of already-processed event IDs (our content hash serves as this ID
  directly, unlike their composite `<origin>:<type>:<seq>` scheme, since
  content-addressing already guarantees global uniqueness). Flow: verify
  signature → check table → drop silently if seen, else record + process +
  relay onward. Old entries purged after a retention window. File transfers
  get their own two-level scheme: a chunk ID (transfer ID + chunk number)
  for per-chunk dedup, and a separate transfer-level ID so a completed file
  is never double-imported.

### Canonical event model (semantic layer, round 90 — resolves the semantic-specification half of issue #11)

Stage 1 of #11's staged resolution (round 87): the *semantic* protocol
model, settled before any wire-visible persistence or endpoint is
implemented. Stage 2 (exact canonical bytes — Unicode normalization form,
numeric/duplicate-key/unknown-field handling, golden vectors) remains
explicitly deferred to whenever real Phase 3 event-store/endpoint code is
being written, per round 27's original list and Thiesi's own staged-gate
framing — not resolved here.

**One unifying rule covers every event type that isn't a bare immutable
content-creation event:** it's an append to a per-object **event chain**,
headed by a current-state pointer, and every new event must reference
what it extends (the ID of the event/state it supersedes). Post edits,
tombstones/closures (§13), moderator grants/revocations (§13), and round
89's key-transition records all already fit this shape without any new
mechanism — this round just names it once instead of re-deriving it per
feature.

- **Envelope:** `{netbbs_protocol, object_type, payload}` (round 27),
  with one addition — **the hash/signature always covers the whole
  envelope, never just `payload`.** `object_type` therefore acts as a
  mandatory domain separator: a signature valid for one event type can
  never be replayed as valid for a different one, even if payload bytes
  happened to coincide.
- **Author reference** is a tagged union matching round 89's three
  identity tiers: `node_vouched_user` (home-node fingerprint + opaque
  local ID, for password-only users), `user_key` (a personal keypair
  fingerprint, opt-in tier), or `node` (the node's own signing key, for
  node-authored events like board creation or moderator grants).
  Verifying a signature resolves to a key fingerprint, then — for
  node-controlled keys — walks round 89's transition chain back to root.
- **Two event classes, not one:**
  - **Immutable content-creation events** (a board post, a file
    descriptor announcement) — content-addressed, causally ordered per
    above, nothing to project beyond "does it exist."
  - **Mutable per-object event chains** (board/channel metadata, post
    edit/tombstone history, moderator roster, key transitions,
    Community membership) — "effective state" is the fold over the
    chain from genesis to the current head. A node determines whether
    an incoming event is new (extends the current head), already known
    (is an ancestor of it — safe no-op), or a genuine fork (rare,
    handled the same way a DAG fork already is) — never by "have I seen
    this ID before."
- **Replay safety no longer depends on the dedup table — this is the
  key correction from the reopened issue #11 discussion.** Three
  previously-conflated things are now explicit and separate:
  1. **Transport-level dedup** (§7's existing seen-event table, purged
     after a retention window) is a pure performance optimization —
     avoids reprocessing/re-relaying an event already handled. It is
     *not* a safety mechanism.
  2. **Projection-level idempotency** (the event-chain/head-pointer
     structure above) is the actual, permanent safety mechanism: a
     replayed state-changing event is a no-op if it's already an
     ancestor of the object's current head, regardless of whether the
     dedup table still remembers ever seeing it — same shape as `git
     push` of a commit already in history.
  3. **Tombstones and local storage pruning stay purely local, as
     already stated for board/channel closure in §13.** A tombstone is
     just another chain entry, not a deletion of history; pruning bytes
     locally never revives suppressed/moderated content network-wide,
     since any node that still holds the chain can re-serve it,
     tombstone included.
- **Distinguishing two identical posting actions** (same content, same
  parent, submitted twice — which would otherwise hash identically and
  look like a dedup hit): every event carries a **random nonce field**,
  confirmed with Thiesi over an author-local monotonic sequence counter
  — no persistent per-identity counter to maintain, which would have
  been awkward for round 89's opt-in user tier once multi-device support
  exists (deferred, but not worth designing around before it's real).
- **Compatibility/version negotiation:** `netbbs_protocol` bumps only for
  backward-incompatible wire changes; purely additive changes (new
  optional fields, new event types) don't need one. Peers exchange a
  supported-version range at handshake (§12). **Unknown event types, or
  events at a higher protocol version than a node supports, are stored
  and relayed opaquely — never locally projected or displayed** —
  confirmed with Thiesi as a deliberate security-relevant default:
  matches how Git already handles a ref it doesn't understand, and it's
  safe here specifically because a node that can't parse an event can't
  act on it or show it to a user either, so nothing bypasses local
  moderation/quarantine by virtue of being unrecognized. Unknown fields
  inside a known, understood event type are preserved verbatim in the
  signed bytes (never stripped or re-serialized, which would break the
  signature) but ignored for projection.

**Explicitly still deferred to stage 2 (#11's exact-canonicalization
half) or later:** Unicode normalization form, numeric type/range rules,
duplicate-key and absent-vs-null field handling, and signed golden test
vectors — round 27's original list, unresolved by design until real
Phase 3 event-store/endpoint code is actually being written, per
Thiesi's own staged-gate framing (round 87).

### Personal mail: local inbox/outbox, and Link messages (round 93 — resolves issue #52)

Fifth piece of Phase 3 design work, and the first of the two
feature-specific gates named in round 88's dependency matrix. Two
layers, deliberately separated: **local asynchronous personal mail**
(genuinely local, no Link dependency, placed in the same "after Phase 2,
before Phase 3" addendum slot as Communities/self-update/identity-
attestation — see §15) as the prerequisite domain, with **Link
messages** as a Phase 3 extension of it rather than a separate feature
invented from scratch.

**Local mail is a new, persistent domain — deliberately not the same
mechanism as `/msg`.** `/msg` (§8, round 46) stays exactly what it is:
ephemeral, online-only, session-addressed, with no fallback to
persistence (round 32's own explicit prohibition). Local mail is the
opposite shape on purpose: one message per row (sender, recipient,
subject, body), `read_at` (nullable — unread/read), independent
`sender_deleted_at`/`recipient_deleted_at` (each side manages their own
view of the same message; the row hard-deletes once both are set — no
shared-content reason to keep it around the way a board post sometimes
needs re-fetching for someone else). **Quota:** a cap on stored
(non-deleted) messages per recipient; over the cap, the oldest
**already-read** message is dropped to make room (same drop-oldest
precedent as `ChatHub`'s queues and `MessageMailbox`'s own per-session
cap); if the inbox is entirely unread and full, the new message is
**bounced back to the sender** rather than silently destroying something
unread — deterministic, matching issue #52's own acceptance criterion,
and the same "never silently drop something a user hasn't seen yet"
principle already applied to `/msg`'s own bounded queues. Read receipts
are explicitly **not** built — optional, privacy-sensitive, nothing
requires them for this to be a complete feature.

**Link messages extend the same mailbox, and routing turns out to be
simpler than boards' because a Link message has exactly one intended
recipient node — not "everyone carrying this board."**
- **Recipient discovery is free**: addresses already encode the home
  node (`user@node-fingerprint`, §5), so there's no discovery protocol
  to design — parse the address. Node/account migration (moving to a
  different home node) remains explicitly undesigned, per round 87's
  §5 note — that's #51's job; until it exists, a message to a migrated
  user's old address simply bounces as unknown-recipient, an honest
  failure mode rather than a silent one.
- **Direct point-to-point store-and-forward, not flood-fill gossip.**
  The sending node retries delivery directly to the one specific
  recipient node using the existing HTTP+JSON transport (§11), backing
  off until delivered or expired — no multi-hop routing, so no loop-
  prevention machinery is needed either. **Confirmed with Thiesi: no
  dedicated relay/opaque-envelope-storage mechanism for Link messages**
  — the original issue raised this as an open question, but it's punted
  entirely to #58 (WAN reachability): if a relay/rendezvous mechanism
  ever exists at the transport layer, Link messages benefit
  automatically without a separate design; nothing about a 1:1 message
  needs its own relay story.
- **Duplicate delivery is already handled** — a Link message is itself a
  signed event under round 90's model, so it gets the same seen-event
  dedup for free; no new mechanism.
- **Separate signed events for acceptance, delivery failure/bounce, and
  expiry** (issue #52's own recommendation), never conflating raw
  transport receipt with actual user-level delivery: a transport ACK
  only means the bytes arrived; a separate delivery-acceptance event
  means the recipient's node placed it in that user's mailbox; a bounce
  event (mailbox full, blocked sender, unknown recipient) is a distinct,
  explicit signed rejection rather than silence, so the sender gets a
  specific reason rather than an ambiguous timeout. Node-level blocking
  reuses the existing local blocklist (§6) — a blocked peer's Link
  messages are refused at the transport boundary, no new mechanism.
- **Confidentiality is honestly tiered, matching round 89's identity
  model rather than promising one uniform guarantee — confirmed with
  Thiesi rather than assumed.** A recipient with an opt-in personal
  keypair (round 89 tier 2) gets true end-to-end encryption: unreadable
  even by their own home node. A password-only recipient (tier 1, the
  expected majority) has no personal key to encrypt to at all — the
  message can only be encrypted to their home node's key, meaning that
  node's operator can technically read it, exactly as they already
  could technically see any of that account's other local data. This is
  **best-effort E2E, disclosed plainly, rather than excluding tier-1
  users from Link messages to preserve a guarantee they never had
  anyway** — the alternative (Link messages require a personal keypair)
  would exclude the tier round 89 established as the expected default
  majority from a core messaging feature, to protect against an
  exposure that already exists for all their other local data. Which
  tier applies to a given recipient should be surfaced to the sender at
  compose time where knowable, not silently assumed.
- **Metadata visible to any future transport intermediary** (should #58
  ever introduce one) is limited to what's needed for routing: recipient
  node fingerprint, coarse expiry, size — never subject/body, which stay
  encrypted per the tier above.

**Explicitly still open:** the exact schema/table names and command
surface (menu placement, etc.) are implementation detail for whenever
this is actually built, not fixed here. Node/account migration (#51) and
any relay mechanism (#58) remain separately tracked, not solved by this
round.

## 8. Real-time chat

Explicitly **not** routed through the DAG/store-and-forward system — real-time chat needs low latency, while boards and Link messages value reliable asynchronous delivery.

### Chat event model

Chat traffic consists of typed events rather than formatted text. Initial event types are:

- `message` — ordinary channel message.
- `action` — `/me`, rendered IRC-style (`* user waves`).
- `private` — online-only `/msg` traffic, visually distinct from channel traffic.
- `join` / `leave` — presence events.
- `nick` — transparent display-alias changes.
- `system` — server-generated notices.

Using typed events avoids reparsing display text when features later extend across NetBBS Link.

### Identity

Chat may display an optional user alias (`/nick`), but aliases are presentation metadata only. Every rendering keeps the authenticated canonical identity visible in plain text. Moderation, permissions, blocking, reputation and message addressing continue to use canonical identity.

### Presence and discovery

Phase 2 introduces local node-wide away status (`/away`) shared across all active sessions, plus local `/who`, `/whois`, `/names`, and `/list` commands. `/whois` reuses the user-directory/vCard system and must respect profile privacy and hidden-channel visibility. `/names` is a compact channel roster; `/who` is the more detailed presence view.

Phase 5 extends presence and discovery across NetBBS Link with per-node and per-user privacy controls.

### Private conversation

`/msg <user> <text>` sends a one-off, online-only private message. `/private <user>` enters a temporary private-conversation mode layered on top of `/msg`; ordinary input is sent privately to that recipient until `/close` returns to normal channel input. `/query` is accepted only as an IRC-compatibility alias.

Live private messages remain ephemeral and do not fall back to asynchronous Link messages. A user-facing `/notice` command is deliberately omitted: human private communication uses `/msg`/`/private`, while server and service notifications use typed `system` events.

### Channels and commands

Phase 2 keeps one active channel per session. `/join <channel>` switches from the current channel to another visible/authorized channel, and `/leave` returns to channel selection or the main menu. `/list` exposes visible channels without joining them. `/topic` displays the current topic; changing it is privilege-gated.

Multiple simultaneous channel memberships are deferred until Phase 5, when the richer presence, background-traffic, unread-state, and Link-wide chat machinery exists to justify the added complexity.

### Completion

Slash-command tab completion is Phase 2 scope and belongs in the shared character-input layer so Telnet, SSH, and web sessions behave consistently. Completion is case-insensitive, context-aware, and permission-aware.

Username completion is also Phase 2 scope, but candidate sources are command-specific and visibility-aware: online visible users for `/msg` and `/private`, visible directory entries for `/whois`, and invite-eligible users for `/invite`. Completion always targets canonical usernames, not display aliases. Link-wide identity completion waits until Phase 5 has remote presence/directory data.

### Persistence

Channel traffic retains bounded channel scrollback. Private `/msg` conversations are intentionally ephemeral and are not written to channel scrollback. Asynchronous Link messages remain a separate store-and-forward feature introduced in Phase 3.

### Link extension

Phase 5 carries typed chat events (`message`, `action`, `private`, presence and alias changes) over the authenticated Noise transport. Live `/msg` and `/private` remain distinct from asynchronous Link messages and do not silently fall back to store-and-forward delivery.

## 9. File areas

- **Node-local**, not replicated/synced across NetBBS Link.
- **Discoverable and downloadable on-demand** from remote nodes — no full
  Link-wide replication (avoids the bandwidth/storage blowup of syncing
  potentially large files everywhere). Remote-discoverable areas are
  referred to as **linked file areas** per the terminology rule in §1.

## 10. Door games

- **v1:** modern native API only, designed against NetBBS directly.
- **Later phase:** classic DOS door compatibility (DOOR.SYS/DORINFO1.DEF-
  style, via DOSBox/dosemu) to unlock the large existing library of legacy
  door games.

## 11. Node-to-node transport security

**Confirmed: split by traffic shape, not one protocol for everything.**

- **Store-and-forward Link traffic** (boards, Link messages, file-area
  catalogue/descriptor events): **HTTP+JSON**, carrying payloads
  authenticated via signatures from the existing keypair identity system
  (§5) rather than a shared-secret HMAC model. The first attempt's
  experience showed HTTP+JSON causes essentially no firewall/NAT friction
  on modern infrastructure, and it's a natural fit for async, queued,
  request/response-shaped traffic. Signatures replace their bootstrap-
  secret/per-peer-HMAC scheme, removing the shared-secret handshake
  entirely — the thing you authenticate is the identity you already have,
  same reasoning as originally applied to Noise.
  **Transport family vs. propagation policy, clarified round 87 (see
  round 87 sign-off note):** file *chunks/contents* use this same
  HTTP+JSON-signed service family for transport, but are **not** part of
  §7's DAG/gossip flood-fill — they're fetched on demand from a source
  node and are deduplicated/resumable, matching §9's "node-local,
  discoverable and downloadable on-demand" model. Only the file-area
  *catalogue/descriptor* events (what exists, where, roughly how big)
  participate in ordinary Link discovery/gossip the way board posts do;
  the bytes themselves do not. Sharing a transport family does not imply
  sharing a gossip/replication policy. (The eventual chunk-transfer wire
  format may reasonably use signed JSON metadata plus a raw bounded HTTP
  body rather than base64-encoding large chunks into JSON — left for
  Phase 3 protocol work, per #11.)
- **Real-time chat** (§8): **Noise Protocol Framework**, using the node's
  dedicated **transport key** (§5, round 89) as its Noise static key for
  mutual authentication — **not** the same key used for content/event
  signing. Originally specified as key reuse; split into two
  purpose-specific operational keys under one root identity in round 89,
  directly resolving issue #51's flagged risk of mixing a signing key
  and a Noise DH key rather than leaving it unresolved. A persistent,
  low-latency encrypted stream is the right shape here, unlike the
  store-and-forward path.

The first attempt reportedly considered this same real-time/async split
conceptually but never implemented it, due to developer resource
constraints rather than a design objection.

## 12. WAN rendezvous

New nodes bootstrap onto NetBBS Link via a **fixed/hardcoded seed node list**
(classic, simple approach — accepted trade-off of mild central dependency
at the bootstrap stage only; once connected, a node operates as a full
peer).

### WAN reachability, NAT, and seed trust boundaries (round 95 — resolves issue #58)

**Two deployment modes**, matching what's actually achievable for the
declared audience (§14: hobbyist/residential self-hosters, not
datacenter operators): **full peers** (stable address+port, accept
inbound connections) and **outgoing-only nodes** (never accept inbound —
the common NAT/residential case — only ever connect out).

- **Endpoint advertisement**: every node signs and periodically
  re-advertises its own reachability — a list of (protocol, address,
  port) tuples for a full peer, or an explicit "outgoing-only" marker —
  self-authenticated by its own key (§5), so nothing else needs to vouch
  for it. Multiple simultaneous addresses (including IPv4/IPv6) are
  supported; peers try them in order.
- **Seed compromise cannot impersonate peers** — not a new mechanism,
  just a property already true by construction: identity is keypair-
  based and independent of network location, so a malicious seed can lie
  about *where* to find a node, but connecting to the wrong address just
  fails the handshake (an impostor can't produce the real signing key).
- **Seed introduction does not imply trust.** A seed only ever supplies
  reachability information; it grants no trust/reputation standing under
  §6, regardless of how a peer was discovered.
- **Multiple independent seeds, operator-overridable.** Once connected, a
  node learns and remembers further peers directly via signed peer-list
  exchange, so it isn't perpetually dependent on the seed list — the
  resilience path when every configured seed is unavailable.
- **Duplicate simultaneous connections** (both sides dial each other at
  once): resolved by a deterministic tiebreak comparing fingerprints —
  the lower one stays the dialer, the other accepts inbound and drops its
  own outbound attempt.

**Automatic relay selection for outgoing-only nodes (round 95).** A
sender can never dial an outgoing-only recipient directly, which round
93 explicitly left for this section to resolve. Rather than requiring
any manual configuration, an outgoing-only node selects its own relays
automatically, reusing mechanisms this document already has rather than
inventing new ones:

- **Reliability scoring reuses §6's existing local-reputation
  mechanism, not a new metric.** An outgoing-only node is already dialing
  its peers on a schedule; every attempt is a free direct-observation
  sample of that peer's reliability. True hop-count/shortest-path
  topology awareness was considered and rejected as disproportionate
  complexity for this project's declared scale (§14) — "is this
  candidate a reachable full peer, and how reliable have I personally
  found it" is a cheap, sufficient proxy. Second-hand reliability claims
  learned via peer-list exchange are treated as a weak prior worth
  *trying*, not trusting outright, matching §6's own "direct observation
  first, relayed signals second" shape — a new node has no trusted peers
  yet to weight relayed signals from, so it leans on direct observation,
  which it starts accumulating immediately upon joining.
- **Selection and consent**: a node picks a small redundant set (3) of
  candidate relays — reachable full peers, ranked by observed
  reliability — and requests relay consent from each, itself a signed
  exchange over the existing transport. A candidate accepts or declines
  per its own local relay-acceptance policy (below).
- **Publication**: accepted relays are named in the requesting node's own
  signed endpoint descriptor, so any sender resolving that address
  automatically learns where to deliver — no sender-side configuration
  either.
- **Self-healing**: the same reliability-observation loop that selected a
  relay keeps watching it; a relay whose reliability drops (including one
  silently dropping traffic instead of relaying) is automatically
  replaced and the descriptor republished. No human interaction required
  after initial bootstrap, matching Thiesi's stated goal that a new
  SysOp can bring a node online and have it eventually use every Link
  feature unassisted.
- **A relay only ever custodies opaque, already-encrypted envelopes**
  (round 93's confidentiality tiers apply unchanged) — it can observe
  who's talking to whom and roughly how much, never content. Stated
  plainly as an honest limitation, not hidden.
- **Relay-serving defaults to *on*, with a conservative resource cap
  (bounded storage/bandwidth, bounded number of nodes relayed for at
  once) and an easy opt-out — confirmed with Thiesi over defaulting
  off.** An opt-in-only default would leave a young or small Link without
  enough relays for outgoing-only nodes to reliably reach anyone,
  defeating the zero-touch goal; a SysOp who doesn't want to spend any
  resources on it can switch it off in node config, the same shape as
  the existing ANSI welcome-banner toggle (round 63) — autonomy
  preserved via opt-out, automation preserved via the default.

### Live seed-list refresh via the self-update channel (round 97)

Prompted by Thiesi asking, after self-update's first implementation
pass (round 96) landed, whether that same mechanism could address a
gap in the seed-bootstrap model above: the seed list is fixed/hardcoded
at ship time, so every node installed at time T stays anchored to
whatever seeds existed then, forever, unless a SysOp manually
intervenes — the "operator-overridable, learn-more-peers-once-connected"
resilience round 95 already built helps *after* first contact, but does
nothing for the list a brand-new node starts from.

**Decided: fetch a small, independently-updated seed list over the same
HTTPS/GitHub channel self-update already uses, as a supplement to —
never a replacement for — the operator-configured and shipped-fallback
seeds.** Reasoning:

- **Reuses an already-accepted trust boundary for a strictly lower-stakes
  payload, not a new one.** Round 82 already accepted HTTPS + the GitHub
  API as self-update's entire trust boundary specifically because it's
  needed to deliver *code* — the highest-stakes payload a network-facing
  server can receive. A seed list is a strictly lower-stakes payload than
  that: the actual damage a hostile list can do is already bounded by
  decisions made elsewhere in this document — seed introduction never
  implies trust (this section, above), and seed compromise can't
  impersonate peers, since identity is keypair-based and independent of
  network location. A malicious list can get a node to *attempt*
  connections to attacker-controlled addresses; it cannot force trust,
  fake a real peer's identity, or exempt a new node from §6's low-trust
  probation regardless of who introduced it.
- **Named residual risk, not hidden — an eclipse attack during the
  bootstrap window.** If the channel were compromised at exactly the
  moment a brand-new node's candidate pool were 100% attacker-supplied,
  that node's early view of the Link could be selectively distorted
  before it builds any peer relationships of its own. This risk already
  exists today with a hardcoded list compromised at ship time; this
  mechanism doesn't create the risk, it widens the window it could occur
  in, by turning a one-time bootstrap artifact into an ongoing
  dependency on the same channel. Recorded here explicitly, matching
  round 82's own style of naming accepted trade-offs plainly rather than
  letting them ride as an unexamined side effect.
- **Fetched via a well-known raw file path, decoupled from the software
  release cycle — confirmed with Thiesi over two alternatives.**
  Rejected piggybacking the seed list onto release assets (ties seed-
  list freshness to the software release cadence, the wrong coupling —
  a seed going offline shouldn't have to wait for the next NetBBS
  release to be dropped from the list) and a separate parallel "release
  train" reusing `check_latest_release`'s exact machinery (more code
  reuse, but stretches what a GitHub release is conceptually for).
  Fetching a plain file (e.g. `seeds.json`) from a fixed repo path via
  GitHub's raw-content delivery lets the seed list update on its own
  cadence, independent of versioned software releases.
- **Supplements, never replaces, the existing seed sources.** Priority
  order: operator-configured seeds first (explicit intent always wins),
  then the software-shipped fallback list (used if the live fetch fails
  — no network yet, GitHub unreachable, or a deliberately air-gapped
  test node), with the live-fetched list layered in as a freshness
  improvement on top, not a dependency the "every configured seed
  unavailable" resilience path (round 95) now requires to function.
- **No new trigger-point machinery.** Refreshed at the same three points
  self-update already checks at (startup/manual/daily-background, §17)
  — most valuable for a brand-new node with no learned peers yet, but
  available to any node, since there's no harm in an established node
  also refreshing its candidate pool if its own learned peers have gone
  stale.

**Phase placement:** part of Phase 3 (WAN reachability, issue #58) — the
fetch/parse logic itself has no Link-protocol dependency and could be
built as a small, self-contained piece (mirroring `check_latest_release`'s
shape) whenever that's useful, but it has nothing to plug into until
Phase 3's actual peer-connection code exists.

## 13. Permissions & Moderation

Covers intra-node user/board/channel permissions — a distinct layer from
§6, which governs inter-node Link trust/abuse. Substantially informed by a
feature request from the first attempt's architect to their lead designer,
relayed by Thiesi; adapted to fit our architecture rather than copied
as-is.

**Design principle, stated explicitly because it echoes §2's core lesson:**
level/permission gating must be first-class plumbing in the menu/command
dispatch layer from Phase 1 onward, not retrofitted per-feature later —
even though most gated features (boards, chat, moderators) don't exist
until later phases. Same reasoning as building NetBBS Link in from day one:
retrofitting cross-cutting infrastructure onto already-built features is
what caused the original rewrite.

**Board & file area permissions:**
- Separate read/write access (unlike chat, where access is binary — doesn't
  make sense to split read/write on a synchronous medium).
- Per-board/per-area moderator roles: read, write, edit, delete, approve —
  settable individually or combined, per board, per moderator. Moderators
  need not be SysOps.
- "Moderated" boards/areas: posts/uploads require designated-moderator
  approval before becoming visible.
- Moderator edits are flagged inline in the edited post itself.
- All moderation actions are logged.
- Boards, file areas, and channels alike support minimum-age gating and
  real-name-verification gating (§18), symmetric with level-gating
  rather than chat-only — added round 85, alongside the discovery that
  the age-gating claim below had never actually been implemented.
- `min_read_level`, `min_write_level`, and the new `min_age` are all
  **nullable**, not `NOT NULL DEFAULT 0` — corrected round 84. `NULL`
  means "no explicit value, inherit the containing Community's default
  (§16) if any, else system default 0/no-gate"; an explicit value,
  including an explicit `0`, always wins outright. Existing resources
  keep whatever they currently have stored on migration (no retroactive
  nulling) — a SysOp clears a field explicitly (typing `inherit` in the
  edit screen) to opt an existing resource into a Community's default.

**Chat permissions & moderation:**
- Channels support minimum-age gating (see §18 — round 85 discovered
  this line previously described a mechanism, citing §5, that was never
  actually implemented anywhere; §18 now specifies the real one:
  self-attested birthdate, verifiable via the identity-attestation
  system, computed fresh at check time).
- Channels support minimum user-level gating, optionally combined with
  individual per-user access grants that bypass the level requirement —
  and optionally hidden entirely (not just inaccessible) from users who
  don't meet the requirement.
- `mute`/`ban`/`unmute`/`unban` commands. Bare numeric argument = minutes;
  no argument = indefinite; suffix alters unit: `s`/`m`/`h`/`d`/`w`/`y`
  (seconds/minutes/hours/days/weeks/years). All actions logged and echoed
  in-channel for transparency.
- Chat moderators (non-SysOp) can `kick`/`mute`/`ban` within their scope.

**Channel membership and invitations:**
- New `manage_members` permission, deliberately separate from `edit`: membership management is authorization, not metadata editing.
- Covers sending/revoking invitations, viewing members, granting/removing persistent channel access, and configuring whether ordinary members may invite.
- Invite-only channels use an invitation-plus-acceptance workflow; an invitation alone never creates membership.
- Channel visibility and join policy are independent axes: listed vs. hidden, and open-to-eligible vs. members-only. `hidden + open` is permitted but documented as obscurity rather than access control.
- Local invitations may be delivered immediately to online users and retained as pending invitations for offline users, with configurable expiry. Membership persists until revoked unless explicitly configured otherwise.
- Default invitation policy is moderators/SysOp only; channels may opt into ordinary members being allowed to invite.
- Linked-channel membership is Phase 6 scope: invitations, acceptances, grants, removals, and revocations become signed governance events.
- Access-restricted Linked channels are not described as end-to-end confidential from participating node operators. True encrypted group confidentiality requires a separate future design covering group keys, rotation, history access, and compromised members.

**Moderator scope tiers** (four levels, each reusing the same underlying
permission primitives — a "global" moderator is just "moderator of every
object in a category," not a different mechanism):
1. **Per-object** — authority over one specific board/area/channel.
2. **Community-blanket** — authority over every board/area/channel
   currently or later assigned to one specific Community (§16).
   Blanket/automatic over that Community's membership, the same way
   local-blanket (below) is blanket/automatic over the whole node's
   local-only resources — not a new automatic-power-grant pattern, an
   application of the existing one at a narrower scope. Added round 83,
   specced for local Communities only; extending it to Link Communities
   is Phase 6 scope, wired up against real signed grants the same way
   Link-blanket already is for Linked boards/channels.
3. **Local-blanket** — authority over every *local-only* board/area/channel
   on a given node (i.e., content not carried on NetBBS Link).
4. **Link-blanket ("global")** — authority over every Link-participating
   board/area/channel that node carries.

**Identity-verification authority is a separate trust domain, not a
fifth moderator tier.** `can_verify_identity` (§18) is a plain per-user
boolean, SysOp-grantable, with no scope tiers of its own — verifying
someone's real-world age or name isn't authority over a specific
board/area/channel, it's a fact-finding role about the person, so it
doesn't fit the per-object/Community/local/Link-blanket shape above at
all. Deliberately kept independent of content moderation: a SysOp can
grant both to the same person, but neither implies the other, same
reasoning as "global does not imply local," below.

**Global does not imply local, by design (opinion, confirmed with
Thiesi).** Local-only content is a single-SysOp trust domain; Link-wide
moderator authority is a separate, multi-party trust domain governed by the
mechanism below. Merging them automatically increases blast radius (a
compromised global-mod identity would also inherit local keys) without a
corresponding need, and it violates the same "no automatic power grants"
principle already applied in §6. A SysOp wanting one person to hold both
grants both explicitly.

**Privilege separation, SysOp vs. global moderator (revised round 87 to
resolve a standing self-contradiction — see round 87 sign-off note):**
SysOp remains root — the only role that can grant/revoke *any* moderator
tier or change node configuration. **Originating** a Linked board/channel
(below) is deliberately carved out as a narrower capability: a global
board/channel moderator may also initiate creation, but this is
**initiating creation, not the same thing as being the signed origin
authority.** The distinction that matters:
- The **node identity** (§5) signs the genesis/announcement event and is
  the cryptographic *origin authority* of record — same identity either
  way, whether a SysOp or a global moderator triggered the creation.
- The **initiating human actor** is recorded separately, for audit —
  who actually asked for this board/channel to be created is knowable
  without conflating it with node-level origin authority.
- Creating a Linked board/channel gives the initiator ordinary/per-object
  moderation rights over the new object (matching the grant-authority
  model below), but explicitly **not** the ability to appoint further
  blanket moderators, alter node configuration, or govern pre-existing
  resources they didn't create. Those remain SysOp-only, full stop.

This prevents a compromised or bad-acting moderator identity from
escalating or self-perpetuating — directly informed by the Master Node
lesson in §2/§6 — while still letting a global moderator do the narrow,
useful thing (spin up a new Linked board/channel for their area) without
waiting on the SysOp for every one.

**Moderation authority on Linked boards/channels — deferred, scoped for
later.** Ships **local-only first** (Phase 2, alongside local moderator
tooling per §15); Linked-board/channel moderation is explicitly scoped out
as a Phase 2/3-boundary sub-problem once §7's Link core actually exists to
build on. Design direction already settled so it doesn't need to be
rediscovered later: a moderator grant for a Linked board is a **signed
event that propagates as part of that board's own DAG history** (§7),
issued by the node that **originated** the board (same trust logic as a
repo owner adding collaborators). A moderator *edit* to a Linked post
can't mutate the immutable original — it's a new, signed event that
references and amends it, verified against the granting event before other
nodes trust it. Other nodes accept this because choosing to sync a Linked
board already means trusting its provenance chain — same trust decision
already being made for every post on it, not a new category of trust.

**Board/channel lifecycle — creation, deletion, and maintenance
(confirmed):**

- **Creation:** global board/channel moderators and the SysOp can
  *initiate* creation of new Linked boards/channels — the node identity
  becomes the signed *origin* (see the privilege-separation note above
  for why "who initiated it" and "what the origin authority is" are kept
  distinct), which is a narrow, self-contained power, not a blanket
  administrative one. Creation propagates as a **signed announcement**,
  not a forced action: other nodes decide whether to carry the new board
  (see default-carry policy, below), rather than having it appear on
  their system without their node's consent.
- **Deletion:** a board/channel's origin can mark it **closed/archived**
  (a signed event; no new posts accepted) — but this cannot force other
  nodes to purge data they've already stored. Real deletion of stored
  content remains a purely local, per-node decision (see maintenance,
  below). Rationale: unrecoverable data loss triggered by another node's
  action is the same shape of problem as the Master Node — a small set of
  privileged users able to act on infrastructure they don't own — even
  though the stakes here are lower than that original failure.
- **Origin succession, transfer, and orphan/fork policy (round 94 —
  resolves issue #53's remaining scope).** Since origin authority for a
  Linked resource *is* its originating node's identity, most of
  succession is already solved by round 89's key-lifecycle model applied
  here rather than invented fresh: **routine rotation and compromise-
  with-root-intact** need nothing new — verification already walks the
  transition chain, and a revoked key's forged events already fail.
  **Voluntary origin transfer** (a SysOp deliberately handing a
  board/channel to a different node) is modeled as just another entry in
  the resource's own event chain (round 90's general shape), requiring
  **mutual consent** — the old origin signs the handoff, the new origin
  signs acceptance, before any other node honors it — directly reusing
  the existing "an invitation alone never creates membership" pattern
  above for channel invitations, not a new trust shape. **Root-key loss
  or compromise has no cryptographic recovery**, symmetric with round
  89's own stance on node/user keys: the resource becomes **orphaned** —
  it keeps existing exactly as last known (still content-addressed,
  still re-fetchable), but accepts no further origin-authorized events.
  **Orphan recognition and fork handling are kept purely local, not a
  network-wide protocol state — confirmed with Thiesi over a lightweight
  opt-in signal piggybacking on §6's quarantine-flag machinery.** There
  is no cryptographic proof that an origin is gone versus merely
  offline, so — consistent with §6's core principle that no single
  node's observation gets an automatic network-wide effect — each node
  independently judges and decides for itself. A fork is simply a new
  resource with a new origin, optionally carrying a non-authoritative
  `forked_from` pointer for discoverability; each node locally decides
  whether to carry the frozen original, the fork, both, or neither,
  exactly like today's default-carry-with-visible-opt-out.
- **Default-carry policy for Link participation:** joining NetBBS Link
  **carries every Linked board/channel by default** — this gives "same
  content available on any node" as the **default availability/behavior**,
  with zero configuration, for the overwhelming majority of SysOps who'll
  never want to deviate from it. A SysOp retains the ability to **explicitly exclude**
  a specific board/channel on their own node (local legal exposure, topic
  preference, irrelevance to their community, etc.) — and that exclusion
  is **visible**, shown as "not carried on this node" rather than silently
  absent, so it stays an honest, discoverable local decision instead of
  quietly fragmenting the network. Deliberately not a hard mandatory-carry
  rule with no exceptions: that would conflict with §6's core principle
  that no one else dictates what a node stores or serves.
- **Maintenance/expiry:** every board/area has a configurable maximum post
  age (default: retain indefinitely). Expiry follows the first attempt's
  **active → expired → deleted** state machine with a **grace period**
  between expired and deleted, rather than immediate deletion at max-age —
  cheap insurance against an overly aggressive age setting. Moderators can
  **exempt** specific posts from expiry and **pin** posts to the top of a
  board/area. Expiry/deletion remains a purely local decision even for
  Linked boards — content-addressing means a pruned post is simply
  re-fetchable via the DAG from another node if anyone later needs it, no
  network-wide coordination required.
- **Pin/exempt permission mapping:** both fold under the existing `edit`
  permission rather than becoming a sixth permission type — pinning/
  exempting is conceptually a metadata edit, not new machinery. **Known,
  accepted coupling, not an oversight:** this means edit rights and pin/
  exempt rights can't currently be granted independently (e.g. no
  "curator who pins without editing wording" role). Cheap to split into
  its own permission bit later if that separation turns out to matter in
  practice; not worth the complexity preemptively.
- Users control which profile fields are public via their preferences menu.
- vCard: short free-text bio, sensible line cap (six lines, matching the
  first attempt's figure — reasonable default, easy to reconsider later).
- vCard visibility independently toggleable.
- New feature: table-style user directory listing public info.
- `finger`-style lookup of a user's vCard, accessible from the directory,
  main menu, and chat.

## 14. Deployment scale assumptions

- Primary use case: **single node**, Thiesi as sole sysop/dev.
- Other interested parties are not expected to do meaningful multi-node
  testing.
- Multi-node testing will happen via **local virtualization** (spinning up
  a second node in a VM), not a live multi-party Link, at least initially.
- Practical implication: the architecture must be correct for multi-node
  operation, but near-term testing/validation targets one-or-two-node
  scenarios rather than large-scale Link behavior (partition handling under
  real-world latency, large-N gossip overhead, etc.) — those can be
  revisited once/if NetBBS Link actually grows.
- **Empirical calibration from the first attempt** (their lead developer,
  relayed via Thiesi): single-node interactive load — 20–100 concurrent
  users reasonable on modest hardware with asyncio+SQLite, 100–250 possible
  with care, 250+ needs real testing/refactoring. Link scale — 2–10 nodes
  easy, 10–25 realistic with careful queue/retry handling, 25–50 possible
  but needs dedupe/backoff/batching/monitoring, 50+ explicitly beyond their
  original design assumptions. They never actually tested past ~15
  concurrent Link nodes (no lag observed at that scale, but unverified
  beyond it). Relevant to the §6 authority-key discussion: "hundreds of
  nodes" is untested territory for anyone, not a gap specific to us.
  Predicted first real bottleneck at scale: SQLite write contention under
  simultaneous heavy chat fanout + Link sync writes — not CPU. Worth
  monitoring once we're past Phase 2, not a blocker now.

### Database execution model (round 91 — resolves issue #57)

Third piece of Phase 3 design work. Round 30 diagnosed the problem
(exactly one synchronous `sqlite3.Connection`, shared for the node's
whole lifetime, blocking the entire event loop per query — see
`netbbs.storage.database.Database`'s own docstring) and deliberately
deferred choosing a fix. This round chooses one, scoped to what Phase
3's continuous background Link activity (peer inventory exchange, event
verification/ingestion, retry/outbox processing) actually needs, not a
full rewrite.

**Two dedicated single-worker-thread lanes, not a full actor rewrite and
not a full reader-connection pool.** A **foreground lane** (interactive
Telnet/SSH/web session work — everything that exists today) and a
**background lane** (reserved for Phase 3's continuous Link activity),
each a `ThreadPoolExecutor(max_workers=1)` owning its own SQLite
connection (WAL) against the same database file:

- **Existing business-logic functions are unchanged.** Only their call
  sites move, from a direct synchronous call to `await
  loop.run_in_executor(lane, existing_function, *args)`. Every function's
  transaction ownership stays exactly where it already correctly is —
  this was the deciding factor over option 1 taken literally (a typed-
  message actor, which would require rewriting every one of these
  functions' call contracts) and over option 3 in full (a real
  N-connection reader pool, see below for why that's deferred rather
  than built now).
- **Cross-lane write serialization is SQLite's own job.** Two lanes
  writing concurrently just hit SQLite's existing single-writer lock,
  retried via the already-configured `busy_timeout` (round 30) — no new
  application-level locking to build.
- **Cancellation is safe by a property of the mechanism, not an added
  check.** If a session disconnects mid-call, the underlying worker
  thread keeps running to completion regardless of the awaiting
  coroutine's cancellation — Python doesn't abort a thread mid-flight.
  A transaction therefore always finishes (commit or rollback), never
  left half-abandoned; the caller just doesn't get to see the result.
- **Backpressure:** a bounded semaphore per lane, not the executor's
  default unbounded queue — new submissions past a configurable depth
  block/reject rather than growing without limit. Exact numeric limits
  and observability surfacing are #60's job (operational model); this
  round only establishes that a bound exists and is enforced.
- **The standalone admin CLI (`python -m netbbs.admin`) is unaffected** —
  it's already a separate OS process/connection, already covered by WAL
  + `busy_timeout`.

**Explicitly deferred: a full N-reader-connection pool for genuine read
parallelism (option 3 in full).** Confirmed with Thiesi after estimating
the actual cost, not assumed cheap without checking:
- The mechanical pool code is small (~100–150 lines). The real cost is
  auditing every business-logic function — and everything it transitively
  calls — for hidden write side effects, since a function can look
  read-only and still write through a nested helper. Concretely found
  during this round's own analysis: `netbbs.auth.users.
  authenticate_password` reads a user row and looks pure, but calls
  `_finish_password_login` → `_touch_last_login`, which writes
  `last_login_at` — a mechanical "contains SELECT" scan would have
  misclassified it as safe to route to a reader connection. That's the
  actual shape of the migration cost, not the boilerplate, and per this
  project's own track record (round 30's dict-mutation bug, the `goto`
  bug, the password-masking bug — all only caught by actually running
  tests, per this project's established working style) this kind of
  audit should be expected to surface real bugs on the first pass, not
  complete cleanly.
- **Two different bottleneck thresholds, kept separate rather than
  answered with one number:** the **foreground/interactive** lane has
  comfortable headroom well past this section's declared scale — BBS
  usage is human-paced and bursty, so aggregate query rate even at
  "low hundreds of users" is a small fraction of one thread's capacity;
  estimated headroom into the thousands of low-activity concurrent
  sessions before queuing delay would be user-visible. The
  **background/Link** lane's threshold is a genuinely different,
  currently unmeasurable axis — it depends on aggregate incoming event
  rate from however many peers a node carries, against SQLite's own
  single-writer throughput ceiling (a rough, unverified estimate:
  hundreds to low thousands of small writes/sec on ordinary hardware).
  Deferring the reader pool doesn't make this axis worse either way — a
  saturated background lane queues its own work without affecting the
  foreground lane regardless of whether a reader pool exists.
- The real threshold is for #59's deterministic multi-node harness to
  establish empirically, once it can generate synthetic Link traffic
  against real code — not to guess further now. This matches #57's own
  acceptance criteria calling for benchmarks under concurrent interactive
  and Link-sync load.
- Cheap to add a third+ lane later, using the exact same mechanism, if a
  specific hot path is ever shown to need it.

### Operational model for a Phase 3 Link node (round 95 — resolves issue #60)

Mostly consolidates precedent already set elsewhere in this document
rather than inventing new ground.

- **Backup ordering, made safe by construction**: always snapshot the
  database — via SQLite's own online-backup API, not a raw file copy
  against WAL — *before* copying the blob directory. That ordering can
  only ever produce "blobs slightly ahead of what the DB references"
  (harmless: an unreferenced extra file), never the reverse (a DB
  pointing at a missing blob). The root identity key backup is already
  folded into this same set, per round 89.
- **Restoration is the same identity resuming, not migration** — it
  recreates the same node from an earlier point in time, not a new one;
  node/account migration (a *different* node taking over an identity)
  remains #51's separately-tracked open question. The real operational
  risk is running two instances of the same identity simultaneously
  (old and restored); documented as an operator responsibility rather
  than built-in detection for now — a cheap future addition would be
  noticing Link traffic signed by your own key that you didn't send, as
  a diagnostic signal, but nothing requires it yet.
- **A concrete gap this round closes in round 82's self-update**: round
  82 scoped its rollback-on-failed-start as "protocol-agnostic plumbing
  only," before Phase 3 schema concerns existed. If an update bundles a
  schema migration and then fails, rolling back to the old binary
  without also rolling back the schema would leave code that can't read
  its own database. **Fixed here**: self-update must snapshot the
  database (this round's backup mechanism) *before* applying any
  migration, so a failed upgrade restores binary and schema together —
  not assumed already covered by round 82, which predates this.
- **Quotas and retry/dead-letter share one generic abstraction**: a
  single "outbound work item" shape (pending → retrying → delivered |
  dead-lettered) covers gossip retries, round 93's Link-message
  delivery, and round 95's own relay/reconnection attempts uniformly —
  inspectable and manually replayable/cancelable by a SysOp, extending
  existing admin tooling rather than a new subsystem. The same
  reject-with-a-clear-signal-not-silent-drop principle already used for
  round 93's mailbox quota applies consistently to peer/bandwidth/disk
  quotas too.
- **Crash recovery**: an explicit `PRAGMA integrity_check` gate at
  startup, failing loudly — matching round 56's "refuses to start with
  zero SysOps" precedent — rather than silently running against a
  corrupt database.
- **Graceful shutdown** extends round 51's SIGTERM/SIGINT precedent to
  the background Link lane (round 91): stop accepting new work, let
  in-flight operations finish up to a bounded timeout, then force-close
  what remains.
- **Log retention/privacy**: Link-specific operational logs (sync
  attempts, peer/relay connection events) get their own bounded
  retention policy, separate from `moderation_log`'s permanent audit
  trail, since they're diagnostic rather than accountability records —
  and log metadata only (peer fingerprint, event ID, outcome), never
  message content, matching round 93's own minimum-metadata principle.
- **Peer/relay health observability** extends existing SysOp admin
  tooling (round 59's node admin menu) rather than introducing a new
  surface: per-peer last-successful-sync time, backoff state, aggregate
  queue depths from the work-item abstraction above, and blob-storage
  growth trend.

## 15. Feature scope & phasing

Thiesi has delegated release-scope decisions — all listed features are
wanted eventually, prioritizing shipping working increments over cramming
one release full of unfinished features. Restructured from an original
4-phase draft after a scope review (see round 6 sign-off notes below) found
two phases had grown too large and one risky ordering issue. **Current
7-phase breakdown, confirmed:**

**Phase 1 — Foundation (single node, no live Link yet)**
- Keypair identity system (node + user)
- Telnet / SSH / web (xterm.js) connectivity
- ANSI rendering + transport-independent character-mode input (the
  "TUI half" — screen-buffer diffing for heavy cursor-addressable
  screens — is Phase 2 scope; see round 26 sign-off note)
- Password + keypair auth
- SQLite storage layer
- **Permission/level-gating plumbing in the menu/command dispatch layer**
  (§13) — built early even though most gated features ship later, per §13's
  explicit anti-retrofit rationale
- Local message boards (not yet linked)
- Local file areas
- Local real-time chat (single-node), including **bounded, disk-backed
  scrollback per channel** (last N messages, count-based not time-based
  — predictable storage/scrollback length regardless of how chatty a
  given channel is) — revisits round 10's "no persistence" decision
  specifically to solve chat looking empty after a node restart, per
  round 19's discussion. Deliberately scoped to *this* problem only —
  see Phase 5 for the separate, harder "new Link node catch-up" question
  this does not attempt to answer.
- Basic local blocklist (moderation stub, pre-dates full reputation system)

**Phase 2 — Local permissions & moderation**
No Link dependency — delivers a genuinely complete standalone BBS before
any Link work begins, matching Thiesi's actual primary deployment target.
- **Local-only board/file-area/chat moderation** (§13): per-object and
  local-blanket moderator tiers, read/write/edit/delete/approve
  permissions, moderated-board approval flow, mute/ban/unmute/unban
  command set
- **Maintenance/expiry system** (§13): configurable max post age,
  active → expired → deleted state machine with grace period,
  pin/exempt (under `edit` permission)
- **User directory & vCard/finger system** (§13)
- **Local chat command set and conversation UX:** `/private` (with `/query` compatibility alias), `/close`, `/who`, `/whois`, `/join`, `/leave`, `/names`, `/list`, and local `/topic`; one active channel per session in this phase.
- **Command and identity completion:** permission-aware slash-command tab completion plus visibility-aware canonical-username completion for commands that address users.
- **Invite-only/hidden local channels:** independent visibility and join-policy controls, pending invitations with expiry, explicit acceptance, and a new `manage_members` permission.
- SysOp admin tools (user/board/node management, beyond blocklists)
- ANSI art support for login/welcome screens
- **Local real-time private messages (`/msg`)**: online-only, node-wide private chat between currently connected users. Private messages are visually distinct from channel traffic, are not written to channel scrollback, and remain intentionally separate from asynchronous Link messages (Phase 3).
- **Per-user chat timestamp preference**: persistent user preference controlling timestamps for live chat, scrollback replay, join/leave notices, actions, and private messages.
- **Chat action events (`/me`)**: first-class action events stored distinctly from ordinary chat messages and rendered in IRC style.
- **Local away state (`/away`)**: node-wide away status with optional message, shared across all active sessions, surfaced through presence and private-message feedback.
- **Transparent chat display aliases (`/nick`)**: optional persistent aliases that never replace canonical identity. Chat always renders both alias and canonical username; moderation, permissions, blocking, and addressing continue to use canonical identity.
- The TUI half of the rendering framework — a transport-independent
  screen-buffer/diff abstraction (moved from Phase 1; see round 26
  sign-off note) — plus the fullscreen editor (see editor implementation
  notes, below) that's the actual reason it's needed. Built together
  deliberately: a screen-buffer abstraction designed without a real
  heavy-screen consumer to validate its API against is more likely to
  need reworking than one built alongside its first real use case.

**Phase 3 — Link connectivity & sync core.** Explicitly
**private/experimental federation** (round 87, resolving issue #61) —
Phase 4's trust/reputation/quarantine system is the public-readiness gate;
nothing in Phase 3 is meant to be exposed to untrusted/public peers before
Phase 4 exists. Internally sequenced as **protocol foundation first, then
user-facing async services**, even though this is one phase number, not
two — see the dependency gates below (also tracked, in more detail, on
each named issue rather than duplicated here):

**Dependency matrix (refined round 88, per follow-up review of round
87's gate wording — issue #61), replacing an earlier, coarser
"settle before any wire-visible implementation" formulation that bundled
tiers which don't actually depend on each other:**
- *Before wire schemas, IDs, signatures, or durable authority are
  frozen:* the semantic portion of the canonical event spec — event
  taxonomy, projection rules, replay/compatibility semantics (issue #11);
  and the node/user key-lifecycle model (issue #51).
- *Before continuous sync, ingestion, retry queues, or other background
  Link work is implemented:* the non-blocking DB/background-work
  execution model (issue #57 — design chosen round 91, §14: two
  single-worker lanes, foreground and background; implementation itself
  still pending), plus a *minimal* deterministic test harness (issue #59
  — built round 92: `tests/link_harness.py`, node spawning + fake clock +
  scripted transport, verified with 6 passing tests) and the
  fault-injection seams it needs — not the
  harness's full end state; that grows in lockstep with later features,
  per the next bullet, rather than needing to be complete up front.
- *Before the first end-to-end Linked feature is treated as complete:*
  issue #59's harness expanded to cover at least 3 nodes, duplicate/
  reordered delivery, restart, partition, and convergence.
- *Before deployment beyond a controlled local/private harness:*
  WAN/NAT/seed trust boundaries (issue #58 — design chosen round 95,
  §12's new subsection, including automatic relay selection for
  outgoing-only nodes) **and** the operational model — quotas,
  retry/dead-letter visibility, crash recovery, backup/restore
  (issue #60 — design chosen round 95, §14's new subsection).
  Implementation of both still pending. A prototype need not wait for
  every dashboard or disaster-recovery detail, but an externally
  operated persistent Link node should not precede these.
- *Feature-specific gates — settle before the specific Phase 3 feature
  they block, not before Phase 3 as a whole:* local async mail is a
  prerequisite before **Link messages** specifically (issue #52 — design
  chosen round 93, §7's "Personal mail" subsection; implementation still
  pending); the minimum signed lifecycle/succession model before
  **Linked resource creation, carry, and closure** specifically
  (issue #53 — succession/orphan/fork policy chosen round 94, §13's
  board/channel lifecycle bullets; implementation still pending).
- Seed-node bootstrapping
- Node-to-node transport: **HTTP+JSON with keypair signatures** (§11) —
  *not* Noise, which is reserved for Phase 5's real-time chat only
- Content-addressed DAG message format + flood-fill gossip sync
- Persistent seen-event dedup table + file-chunk transfer ID scheme (§7)
- Store-and-forward for offline nodes
- Linked boards (distribution across NetBBS Link) — any **structural**
  message-threading/revision semantics that affect event IDs or
  propagation must be settled here, before Linked boards ship, not left
  for Phase 7 (see Phase 7's note, below)
- Link messages (cross-Link PMs)
- Remote file-area catalogue discovery and on-demand chunk transfer
  (moved forward from Phase 5, round 87 resolving issue #61 — this is
  asynchronous HTTP-service traffic like the rest of Phase 3, not
  real-time-chat-transport traffic, so it belongs with its actual
  transport family rather than waiting on Phase 5's Noise work it
  doesn't depend on)
- Interim abuse defense: the local blocklist mechanism from Phase 1,
  extended to remote nodes/traffic — acceptable given near-term testing is
  single/VM-node scale (§14), not a live public rollout. Full reputation
  system arrives in Phase 4, deliberately not co-developed with sync
  mechanics.

**Phase 4 — Link trust & reputation**
Isolated as its own phase specifically because it's the hardest,
least-precedented part of the whole design — built and tested against
already-working Phase 3 sync mechanics rather than developed alongside
them. **Explicitly the public-federation-readiness gate** (round 87,
resolving issue #61): Phase 3 is private/experimental by design (see
that phase's note), and this phase — not any point within Phase 3 — is
what makes exposing a node to untrusted/public peers a reasonable thing
to do. The precise definitions this needs (what "established,"
"independent," and "reputation weight" mean; the objective-abuse-vs-
subjective-moderation split; quarantine as a local, explainable
circuit-breaker rather than an authoritative network-wide state) remain
open design work, tracked in issue #55.
- Full trust/reputation system: local web-of-trust, dual-layer
  (node + user) reputation, hybrid time+vouching probation
- Emergency quarantine mechanism (§6)
- Jurisdiction-bound authority key extension point remains unimplemented
  but documented (§6) — no operator needs it yet

**Phase 5 — Real-time Link chat**
Deliberately sequenced *after* Phase 4, not before: shipping live
Link-wide chat before trust/reputation/quarantine exists would mean chat
abuse has zero defense — too close to recreating the original incident's
risk profile.
- Noise Protocol Framework transport + mutual auth (§11), used only here
- Real-time Link-wide chat (separate low-latency path per §8)
- Link-wide `/private`, `/who`, `/whois`, `/names`, `/list`, and identity completion where remote presence/directory visibility permits.
- Multiple simultaneous channel memberships, with active-channel selection, background delivery, and unread-state handling.
- Who's-online (local + Link-wide)
- Link-wide extension of `/msg` over the real-time Noise transport for currently-online recipients only; asynchronous Link messages remain a separate store-and-forward mechanism.
- Link-wide propagation of `/me`, `/away`, and transparent display aliases as typed presence/chat events.
- (Remote file-area discovery/download **moved to Phase 3**, round 87 —
  see that phase's entry; it's asynchronous HTTP-service traffic, not
  real-time chat, so it doesn't actually depend on this phase's Noise
  transport.)
- **Open question, deliberately deferred to whenever this phase actually
  starts:** should a newly-joining Link node be fed recent scrollback
  from peers (e.g. the last 50–200 messages per channel) so Link-wide
  chat doesn't look empty on first join? Distinct from — and harder
  than — Phase 1's local, single-node scrollback (round 19): propagated
  scrollback crosses trust boundaries between nodes, raising questions
  Phase 1's version doesn't (does it need signing/provenance like DAG
  events do? how does it interact with the Phase 6 Link activity feed?).
  Not decided now; flagged so the question doesn't need rediscovering.

**Phase 6 — Linked governance & lifecycle**
The most structurally novel part of the whole design — nothing like it
existed in the first attempt. Isolated here specifically because everything
it depends on (trust system, chat) is already proven by this point.
**Scope boundary vs. Phase 3, clarified round 87 (issue #53):** the
*minimum* signed genesis/carry/closure machinery needed for a Linked
resource to exist without inventing temporary authority rules is a
Phase 3 gate (see that phase's note) — what stays here is the *advanced*
delegated governance below (Link-blanket moderator grants, membership
governance, the governance log/activity feed). Origin succession,
transfer, and orphan/fork policy — the other half of issue #53 — was
designed round 94 (§13's board/channel lifecycle bullets, built on issue
#51's key-transition primitives) and doesn't wait on this phase either;
implementation of both halves is still pending.
- **Link-blanket ("global") moderator tier and Linked board/channel
  moderation** (§13): signed grant/edit events, verified against the
  granting event
- **Global-moderator board/channel creation & closure** (§13): signed
  announcement/opt-in-carry model, default-carry-with-visible-opt-out
  policy
- **Linked-channel membership governance:** signed invitations, acceptances, grants, removals, and revocations under `manage_members`; access-restricted but not represented as end-to-end confidential from participating node operators.
- **Linked-channel topic changes:** signed metadata events authorized against the applicable moderator grant.
- **Link governance log (board) + Link activity feed (channel)** — a
  capstone deliverable, deliberately placed last in this phase since it
  depends on everything else already existing (Link chat transport from
  Phase 5; quarantine flags from Phase 4; board/channel creation, closure,
  and moderator grants from earlier in this phase). Two complementary
  views over the same underlying governance/activity data, not two
  separate features:
  - A **Linked board** (uses existing board infrastructure, no new
    mechanism — just a convention of posting governance events as
    content) serving as the **curated, persistent audit trail**:
    board/channel creation and closure, moderator grants, quarantine
    flags. Worth reading days later, not just in the moment.
  - A **Linked, Link-wide chat channel** (uses existing Phase 1 chat
    infrastructure, extended by Phase 5's Link-wide transport) serving as
    a **live, ephemeral "tail -f" feed** of Link activity — including
    automatic/mechanical events with no human intervention, not just the
    curated governance actions the board tracks. Genuinely disposable;
    nobody's expected to have read every line.
  - **Access control on both defaults to the same restricted audience**
    (SysOps, optionally global moderators, configurable) — deliberately
    not more permissive for the channel just because it's "only"
    operational noise, since that noise plausibly includes
    governance-sensitive content like quarantine flags firing.
  - **Open questions, deliberately left for whenever this is actually
    built** rather than guessed at now: whether the channel should offer
    a small amount of scrollback on join (ordinary chat channels don't,
    but a pure "nothing until the next event fires" experience may be
    worth avoiding here specifically); and exactly which mechanical
    events are "interesting" enough to include vs. too granular — not
    decidable without real Link traffic to calibrate against.

**Phase 7 — Door games & legacy compatibility**
- Door game native API — trust boundary/sandbox and API versioning are
  still fully open design questions, tracked in issue #63; must be
  designed and proven before this phase's implementation begins
- Message board threading refinements — **UI-only by this point**
  (round 87, resolving issue #61): any *structural* threading/revision
  semantics that would affect event IDs or propagation were required to
  land back in Phase 3, before Linked boards shipped, specifically so
  nothing structural was left this late
- Classic DOS door compatibility (legacy game support) — per issue #63's
  recommended direction, this should be a later adapter that receives the
  same constrained session-capability set as the native API, not direct
  node access; sequenced after the native API/sandbox has been proven,
  not developed alongside it

**Editor implementation notes (relevant for Phase 2's fullscreen editor):**
first attempt's dual-editor approach (robust line editor as universal
fallback + nano-like fullscreen editor as a per-user-preference convenience
layer, not the only path) is worth keeping as-is. Their hard-won lessons,
worth revisiting at implementation time: cursor keys arrive as escape
sequences and vary by client; insert/overwrite mode touches every
printable-character code path; ANSI formatting makes visual width diverge
from stored string length; line-list buffers make search/replace awkward;
flicker-free redraw over telnet is genuinely fiddly. Their caution against
building this "too cleverly inside the monolith" doesn't apply to us given
§3's modular-package decision — but keeping syntax highlighting/spell-check
as optional, separately-loadable modules (rather than baked into a core
editor module) is still good advice regardless of overall project
structure.

**Communities (topic-oriented navigation layer):** local Communities
fully specced this round — data model, permission inheritance
(including a new Community-blanket moderator tier in §13), navigation,
migration, and phase placement (after Phase 2, before Phase 3) are all
confirmed. See §16 for the full design. Link Communities remain Phase 6
scope, layered on top later.

**Self-update mechanism:** local, single-node feature with no Link
dependency — scoped for after Phase 2 (standalone BBS feature-complete)
and before Phase 3 (Link connectivity) begins, so the update channel
already exists by the time Phase 3 might need to ship a protocol-
version bump. See §17 for the full design.

**Identity attestation (age & real-name verification):** local
mechanism fully specced — self-attested birthdate, SysOp-delegated
verification, and gating for boards/channels/areas/Communities all land
in the same after-Phase-2/before-Phase-3 window as Communities and
self-update. Link propagation of attestations is explicitly deferred
past that, gated on Phase 4 (trust/reputation) rather than Phase 3. See
§18 for the full design.

**Local asynchronous personal mail:** genuinely local, no Link
dependency — lands in the same after-Phase-2/before-Phase-3 addendum
window as Communities, self-update, and identity attestation, so it
exists as a settled prerequisite domain before Phase 3's Link messages
extend it. See §7's "Personal mail" subsection (round 93) for the full
design, including the Link-message extension itself, which stays Phase
3 scope as originally listed.

---

## 16. Communities (topic-oriented navigation layer)

Status: **local Communities confirmed and fully specced; Link
Communities directionally specced (round 86)** — behavior and reuse of
existing patterns are settled, exact wire/signed-event schema deferred
until Phase 3's DAG substrate actually exists, matching how §13 already
treats Linked-board moderation. See round 71 for the original
directional discussion, round 83 for local Communities' full spec, and
round 86 for Link Communities.

**The idea:** users navigate NetBBS by topic first, not by resource
type first. Instead of a main menu offering "[M]essage Boards /
[C]hat / [F]ile areas" as the primary split, users enter a
**Community** — e.g. "Vintage Computing," "Politics," "Climate
Change" — and find whichever of boards, a chat channel, and a file
area are relevant to that subject, all in one place. A Community is
not required to contain every resource type: Climate Change might be
boards-only (no software to share), Politics might be boards+chat (no
files), Vintage Computing might have all three.

**What stays the same:** `netbbs.boards`, `netbbs.chat`, and the
file-area package remain exactly what they are today — independent,
separately-packaged resource types with their own behavior (boards
stay asynchronous, chat stays live, file areas stay repositories).
Communities do not replace or absorb them. A Community is a new
**coordination/container object above the resource packages**: a shared
parent for navigation, and an optional inheritance point for permissions/
moderation defaults and presentation (branding, visibility) that
individual boards/channels/areas can still override. (**Wording fix,
round 87:** earlier drafts called this "thin," which undersold it once it
started carrying real moderator-grant scope and Link carry-decision
authority, below — it's intentionally not a unified content type, but it
was never actually thin.)

**Naming:** local ones are **Communities**; ones distributed across
NetBBS Link are **Link Communities** — not "Linked Communities." **Correction,
round 87:** this was previously (mis)described as evidence of a blanket
"Link X" convention applying to every noun — it doesn't. The actual,
now-confirmed convention (round 87 sign-off note, resolving issue #62):
"Link" prefixes *named features* (**Link message**, **Link Community**,
**NetBBS Link** itself, Link-wide chat/presence as a scope description),
while an ordinary resource merely *participating* in the Link keeps the
adjective form — **linked board**, **linked channel**, **linked file
area** (§1) — which is deliberately not renamed despite the superficial
inconsistency this reads as at first glance.

**Why this fits NetBBS specifically, beyond the general UX argument:**
it lines up with the node-operator autonomy principle already
established elsewhere in this doc (§2, §14) — just as a user
selectively enters the Community whose topic interests them, a SysOp
can selectively decide which Link Communities to carry on their node,
and in turn receives everything the Link has to offer about that
topic. This is the same "default-carry-with-visible-opt-out" shape
already documented for Phase 6's Linked board/channel creation (§15).

**Data model: zero-or-one, confirmed.** A board/channel/file-area has at
most one Community (`community_id`, nullable FK), never several. "No
Community" is a real, distinct, common state — not a fallback synthetic
Community — since Community rows carry real semantics (carry/moderate/
brand-able things), and every other Link-participating object in this
design is meant to be exactly that. Rejected: mandatory-with-a-default-
"Uncategorized"-Community (simpler schema, but makes Uncategorized a
fictional Community rather than a real topic) and many-to-many (most
accurate to real topic overlap, but turns permission inheritance into
an unresolved conflict-resolution problem — which Community's defaults
win when two disagree? — and complicates the Link-carry decision: carry
a resource if *any* of its Communities are opted in, or *all*? No clean
answer, and nothing forced the question to be answered now).

**Categories (round 18) are unchanged and sit *below* Community, not
replaced by it.** A Community's board/channel list is exactly round
18's existing two-level category picker (`board_categories`/
`channel_categories`), pre-filtered to that Community's `community_id`
— Community is a new outer layer, the same relationship it already has
to boards/chat/files themselves per "what stays the same," above.

**Permission inheritance — two different mechanics, matching the two
different kinds of data §13 already has:**
- **Scalar defaults** (level-gates, age-gates, name-verification
  requirement, presentation/branding/visibility): same resolution-order
  pattern already used for display-timestamp config (round 8/9) — a
  resource's own explicit value wins if set, else its Community's
  default if it belongs to one, else the hardcoded system default
  (0/none). A resource's override always wins outright; a Community is
  not a floor/ceiling a child can't loosen past (nothing else in §13's
  model enforces that shape either; still flagged, not decided, as a
  possible later addition). **Correction, round 84:** this only works
  if "unset" is actually distinguishable from "explicitly set to the
  default" — `min_read_level`/`min_write_level` shipped as `NOT NULL
  DEFAULT 0`, meaning every existing resource already has an explicit
  stored `0`, so a Community default could never actually apply to any
  pre-existing resource under the wording above. Fixed: these, plus the
  new `min_age` and `name_requirement` fields (§18), are all nullable —
  `NULL` means inherit, an explicit value (including `0`) always wins.
  Existing resources keep their current explicit values unchanged on
  migration; a SysOp opts one into inheriting by explicitly clearing it
  (`inherit` in the edit screen). Community itself gains
  `default_min_read_level`, `default_min_write_level`, `default_min_age`,
  and `default_name_requirement`, cascading identically.
- **Moderator grant authority — new Community-blanket tier, added to
  §13's moderator scope tiers** (now four levels, between per-object and
  local-blanket). Blanket/automatic over a Community's membership,
  present and future — the same shape local-blanket already has over
  the whole node, not a new automatic-power-grant pattern.

**Main-menu navigation — replaces the resource-type-first split, per
round 84.** `[M]essage Boards`/`[C]hat`/`[F]ile areas` are removed as
top-level main-menu entries, replaced by `[E]nter a Community`,
`[U]ncategorized`, and `[J]ump to...`. Both `[E]nter a Community` and
`[U]ncategorized` are **conditionally visible** — hidden when there are
zero Communities, or zero uncategorized resources, respectively — the
same conditional-visibility pattern the main menu already uses for
`[I]nvitations`. On a freshly upgraded node with no Communities created
yet, the main menu reduces to `[U]ncategorized  [J]ump to...` plus the
untouched non-Community-shaped entries, functionally identical to
today's flat menu — the concrete UI consequence of "migration is a
non-event," below.

**Three entry points, one shared sub-menu shape.** `[E]nter a
Community` (via `pick_item` over Communities), `[U]ncategorized`, and
`[J]ump to...` all lead to the same sub-menu: `[M]essage Boards
[C]hat [F]ile areas [B]ack`, offering only the resource types that
actually have a matching item. **Caught during design: `[B]oards`
collides with `[B]ack`** — resolved by reusing the *original*
`[M]/[C]/[F]` letters one level in, rather than inventing new ones, so
existing muscle memory isn't fully lost, just relocated one screen
deeper. Each entry point differs only in what filter it applies to the
existing `_browse_boards_in_category`-style functions (which gain a new
`community_id: int | None` parameter threaded through the existing
category recursion — `stable_id_of`/category-negation trick unchanged):
Community → `community_id = X`; Uncategorized → `community_id IS NULL`;
Jump → no community filter at all (today's full list, categories
intact). Headers give orientation: `"{Community name} — message
boards"`, `"Uncategorized — message boards"`, and the unchanged
`"Available message boards"` for Jump.

**Categories (round 18) stay schema-unchanged; leak prevention happens
at the query layer.** Since Community and category are two independent
nullable FKs on a resource, nothing stops the same category from
holding resources in different Communities, which would leak another
Community's resources into what looks like a Community-scoped category
view. Resolved without touching `board_categories`/`channel_categories`
at all: both the end-user browse path and the admin-side assignment
picker (below) apply a community-scoped *existence filter* — "only
show/offer categories currently used by ≥1 resource in this Community"
— so cross-Community category reuse stays possible in principle but is
never surfaced by the UI, and in practice never happens.

**Jump-shortcuts — deliberately scoped to one resource type per use,
not a unified cross-type search, confirmed round 84.** `[J]ump to...`
reuses round 18's existing `search`/`goto` commands (stable IDs already
decoupled from sort/filter order, round 18 point 2) against an
unfiltered list, but asks resource type first via the same shared
sub-menu above. A single search spanning boards+channels+areas at once
would need a new multi-type stable-ID scheme (today's category-vs-
resource negation trick only disambiguates two ID spaces per type); the
per-type version is a strict subset of that and doesn't foreclose
building it later, so it's the version shipped now.

**Resources that were never Community-shaped to begin with** — private
mail, the user directory, admin menu, profile/preferences — are
untouched by any of this; they keep their existing main-menu placement.

**Admin-side management, mirroring existing SysOp tooling rather than
inventing new patterns — added round 84.**
- New content-menu entry **`[O]Communities`** (the next free letter in
  "Communities" after Categories claims "C," same disambiguation rule
  already used for `[H]annels`), with its own `[C]reate [L]ist [B]ack`
  submenu and `[E]dit [D]elete [B]ack` detail screen, mirroring
  `_board_menu`/`_board_detail_screen` exactly — no "pending posts"
  equivalent, a Community holds no content of its own.
- **Create stays lean, Edit carries the rest** — same split boards
  already use. Create: `"Name: "` → `"Description (optional): "` → land
  on the detail screen. Edit adds the cascading scalar defaults
  (`"Default minimum read level [0]: "`, `"...write level [0]: "`,
  `"Default minimum age [0]: "`, `"Require verified real name?
  [none/verified/verified+displayed]: "` — see §18) plus `"Hidden? [y/N
  or current]: "`, reusing §13's existing listed/hidden visibility
  language. A richer branding concept (e.g. an ANSI banner shown on
  entry, reusing whatever `[W]elcome banner` already does) is
  deliberately not designed this round — Description is the only
  presentation field for now.
- **Community assignment mirrors `_pick_optional_category` exactly** —
  a new `_pick_optional_community` helper, invoked from board/channel/
  area create *and* edit screens, prompted **before** the existing
  category prompt (Community is the outer layer, chosen first):
  `"Assign a Community? [y/N]: "` → `pick_item` over Communities.
- **Community-blanket grants (§13) extend the existing `X`/`Y`/`Z`
  blanket keys rather than adding new ones.** After picking "blanket
  across all boards `[X]`" (or areas `[Y]`/channels `[Z]`), one new
  follow-up: `"Scope this blanket grant to one Community instead of the
  whole node? [y/N]: "` → if yes, `pick_item` over Communities. Reuses
  the exact same per-type preset vocabulary local-blanket already has
  rather than inventing a parallel one — Community-blanket is
  local-blanket narrowed to one Community's membership, not a
  structurally new kind of grant. Mechanically: one new nullable
  `community_id` column on the existing grants table, alongside the
  existing nullable `object_id`; the authority check gains one more
  fallback (per-object → Community-blanket → local-blanket). Revoke
  mirrors the grant flow's shape automatically.
- **Deletion** reverts every referencing resource to `community_id =
  NULL` (Uncategorized — the reverse of assignment, consistent with
  "migration is a non-event," below) and revokes any Community-blanket
  grants scoped to it outright, rather than leaving them dangling.
  Confirmation shows the blast radius before committing: `"This
  Community has N board(s)/channel(s)/area(s) and M moderator grant(s).
  Deleting will un-categorize its resources and revoke those grants.
  Continue? [y/N]: "`.

**Migration path: a non-event, by construction.** Because `community_id`
is nullable, every existing board/channel/area on an upgraded node
defaults to `NULL` — functionally "Uncategorized" — with no forced
categorization pass and no data loss risk. "Create Community" and
"assign resource to Community" are new SysOp admin actions (extending
Phase 2's existing admin tooling), not a migration wizard.

**Link Communities — directional design, round 86 (behavior settled;
exact wire/event schema deferred to Phase 3, matching §13's existing
treatment of Linked-board moderation).**
- **Not a separate object type.** A Community becomes Link-participating
  the moment its origin node announces it via a signed event — the same
  mechanism §13 already specifies for Linked board/channel creation,
  applied unchanged. No separate `link_communities` table; Link-
  participation is a property layered onto the same `communities` row,
  exactly how a board doesn't become a structurally different thing by
  being carried on the Link.
- **Identity: content-addressed and origin-scoped, same as §7's
  existing scheme** — no new naming/collision concept needed. Two nodes
  independently creating a Community both named "Vintage Computing"
  simply coexist as two distinct Link Communities that happen to share
  a display name, the same non-collision property boards and messages
  already have under content-addressing.
- **Promotion, not just from-scratch creation.** An existing local
  Community gets a new admin action (`[L]ink`, alongside `[E]dit
  [D]elete`) that triggers the signed announcement, turning an
  already-built local Community Link-participating without migrating it
  to a different object — handles the realistic case (a local Community
  grows, the SysOp later decides to open it to the Link) rather than
  only supporting ground-up Link Community creation.
- **Carry decisions compose with the existing per-resource opt-out,
  plus a new bulk convenience.** Carrying a Link Community carries its
  current and future boards/channels/areas by default (§15's existing
  default-carry-with-visible-opt-out shape); a SysOp keeps the same
  per-board exclusion ability already available today, plus a new
  Community-level bulk exclude ("don't carry this whole Link
  Community") as a single action — a convenient grouping unit is the
  whole point of a Community.
- **Moderator grants need no new design.** Community-blanket→Link-
  blanket is §13's existing Linked-board-moderator-grant mechanism
  (signed DAG event from the origin node, verified against the granting
  event by receivers), applied to a Community instead of a board —
  already specified, not new.
- **Cascading scalar defaults: origin sets the recommendation, carrying
  node's local override always wins** — same node-sovereignty principle
  already governing every other piece of Linked content (expiry,
  exclusion, moderation boundaries; §6, §13). A Link Community's
  `default_min_age` etc. propagates as a suggested starting point; a
  carrying SysOp can override it locally on their own carried copy
  without the origin node's permission.
- **The Phase 4 dependency for age/name-gating is narrower than §18
  originally implied.** A carrying node enforcing its *own* local
  attestation data against its *own* local users accessing a carried
  Link Community's age/name-gated content works from Phase 3 onward —
  no Phase 4 needed. Phase 4 (trust/reputation) is only required for the
  narrower case of one node trusting *another* node's attestation about
  a shared/roaming identity, not the common case for a locally-hosted
  user base. §18 corrected accordingly.
- **Discovery needs no new mechanism.** Link Community announcements are
  just another event kind in Phase 6's already-designed governance log
  (curated audit trail) and activity feed (live tail), per §15 Phase 6.

**Phase placement: local Communities land after Phase 2, before Phase
3 — confirmed, no renumbering.** Same treatment as §17's self-update
mechanism: an addendum pointer in §15 rather than inserting a new
numbered phase, since the existing 7-phase numbering is referenced by
number throughout this document. **Link Communities remain Phase 6
scope for actual implementation** — the signed-event/DAG governance
machinery Linked board/channel creation already needs doesn't exist
before then — but their behavior is now directionally specced (round
86, above), not just named. The Community-blanket moderator tier is
specced and built now for local Communities only; extending it to Link
Communities needs no new design, only Phase 6's real signed grants to
run against — the mechanism itself (§13's Linked-board-moderator-grant
model) already covers it.

---

## 17. Self-update mechanism (SysOp-facing)

Local, single-node feature — no NetBBS Link dependency, though its
practical motivation is making future Link protocol changes easy to
roll out once Phase 3+ exists. Scoped for implementation after Phase 2
(the standalone BBS is feature-complete) and before Phase 3 begins; see
§15's phase list and round 82 sign-off note for the placement reasoning.

**Implementation status (round 96, first addendum-backlog item
built):** version comparison, GitHub release checking, tarball download/
extraction (with a path-traversal guard), database snapshot/restore
(round 95's DB-safety-net addition), and the pending/confirm/rollback
state machine are implemented and tested (`netbbs.selfupdate`, 24
tests). The admin-menu `[U]pdate` screen currently supports **checking
only** — it reports whether a newer release exists and records the
outcome, but does not download/apply/restart. The actual apply
orchestration described below (graceful drain, re-exec, rollback-on-
failed-start, the startup/daily-background trigger points) is **not
yet wired up** — a deliberate scope cut for this pass, not an
oversight: real GitHub network access and real process replacement
(`os.execv`) can't be safely exercised end-to-end from this sandboxed
environment, matching this project's existing, already-accepted
limitation for SSH/Zmodem/browser-rendering verification. See the
round 96 worklog entry for the full implementation writeup.

**Update source:** GitHub Releases on the project's public repo, queried
via the GitHub API (not a raw branch/tag pull), so a release is an
explicit, versioned unit rather than "whatever HEAD happens to be."

**Three trigger points, three different apply behaviors** — chosen
because "SysOp is live-serving connections" is not the same situation
as "process hasn't started yet":

- **Startup check.** Runs before the node binds Telnet/SSH/web ports.
  If a newer release is found, it's downloaded and applied immediately
  (nothing to drain — no sessions exist yet), then the node boots
  straight into the new code.
- **Manual, admin-menu-triggered.** SysOp explicitly requests a check;
  if a newer release exists, confirms before applying. Apply triggers
  the same graceful-drain-then-restart sequence as the background case,
  below, since the SysOp may already have live sessions open.
- **Daily automatic background check.** Runs once every 24h while the
  node is live. On finding a newer release: stop accepting new
  connections, show currently-connected sessions a countdown notice
  before restart, then apply and restart. Auto-apply is the default,
  consistent with "as seamless as possible" being the stated goal — not
  merely a notification, which would put the actual upgrade back on the
  SysOp remembering to act.

**Apply mechanism:** download the release, replace the on-disk source
tree, then re-exec the running process in place (`os.execv`-style) to
pick up the new code — no separate supervisor/watchdog process
introduced. Rejected alternative: an external supervisor that restarts
the main process on a specific exit code (the pattern several other
self-hosted daemons use) — more moving parts than justified here, since
self-exec accomplishes the same restart-into-new-code outcome without a
second long-running component to build, deploy, and keep correct.

**Rollback:** the previous release's source tree is kept on disk
(rotated out, not deleted, when a new one is applied). If the newly
applied version fails to start cleanly, the node automatically reverts
to the previous tree, and the SysOp is notified either way (successful
update, or failed-and-rolled-back). This matters more given the trust
model below, not less — see round 82 sign-off note.

**Trust model — deliberately simple for now, confirmed with Thiesi:**
HTTPS + the GitHub API is the entire trust boundary; there is no
release-signing scheme layered on top, even though the project already
has keypair/signing infrastructure available (§5, §11) that could
provide one. An explicit, discussed tradeoff, not an oversight — see
round 82 sign-off note for the reasoning and what would need to change
to harden this later.

**pkgsrc — deliberately not special-cased, confirmed with Thiesi.** No
pkgsrc packaging exists yet (§3 names NetBSD/pkgsrc as the primary
target, but no PLIST has been built). The self-updater applies
unconditionally regardless of install method for now; if/when a pkgsrc
package is actually built, it will need to reconcile with this
mechanism at that time rather than the reverse — see round 82 sign-off
note.

**Off switch:** a `node_config` key (reusing the generic node-wide
key-value store from round 8's sign-off note) disables the daily
automatic background check. The startup check and manual admin-menu
check remain available regardless — a SysOp who wants to stay pinned to
a specific version can still see what's available without it being
force-installed.

**Deliberately not designed here: Link protocol-version awareness.**
The actual motivating case — making future NetBBS Link protocol changes
easy to roll out — needs the updater to eventually understand
version/compatibility semantics that don't exist yet, since Phase 3+
(where the Link protocol itself is built) hasn't started. This section
scopes the updater as protocol-agnostic plumbing only: it can fetch and
apply a new release. Teaching it (or the Link handshake) about protocol
compatibility is Phase 3-or-later work, deferred rather than guessed at
now.

---

## 18. Identity attestation (age & real-name verification)

Status: **local mechanism confirmed and specced** (round 85); **core
implemented round 101** (`netbbs.attestation`, 34 tests); **UI wiring
and boards enforcement implemented round 102** — the `[V]erify`
main-menu screen, profile-edit additions (display name/location/
birthdate + visibility), the `can_verify_identity` admin toggle, and
full age/name-gating enforcement + anti-forgery display wired into
message boards specifically, as the reference implementation. **Still
open**: the identical wiring for chat channels and file areas — same
schema (already migrated for all three resource types), same
enforcement shape, just not yet repeated for those two — see the round
102 worklog entry for the exact accounting. Link propagation of
attestations is explicitly out of scope for now — see "Phase
placement," below, for why it's gated on Phase 4 specifically, not
Phase 3.

**Why this exists, in Thiesi's own framing:** NetBBS can't itself
define who counts as a minor or what counts as age- or identity-
restricted content — those are jurisdiction-specific policy questions.
Rather than NetBBS guessing at a global answer, this delegates the
judgment call to whoever's actually accountable to local law and
actually knows their own community: the SysOp (or someone the SysOp
trusts to do it). NetBBS's job is to give that judgment a real
cryptographic mechanism to act on, reusing the identity/signing
infrastructure §5/§11 already established rather than inventing a
parallel trust system.

**New voluntary user fields**, alongside the already-existing username
(required) and password/keypair (required, one or the other):
`birthdate` (full date, not just year — see below for why),
`display_name`, `location` (deliberately coarse: free text, no
structured city/region/country fields forcing precision). All three are
nullable, self-reported, and get the same independently-toggleable
public/private visibility every existing profile field already has.
`display_name` is a new, directory/vCard-level field — distinct from
the existing chat-only `/nick` alias (round 41 deliberately kept
`/nick` out of the directory; this doesn't change that).

**Age is computed fresh at check time from a stored birthdate, never
stored as a derived "current age."** Same computed-at-read-time
philosophy already established for display timestamps (round 8/9),
applied here because storing a derived age is actively wrong, not just
imprecise: a value like "attested age: 17" is frozen the instant it's
written and never re-evaluates, so a verified 17-year-old would never
be recognized as correctly-18 without someone manually re-attesting —
worse for someone born early in the year, who'd wait up to a year
longer than necessary. A stored birthdate with real date-math computed
on every check is self-correcting forever, with zero further action
from anyone. Year-only birth data was considered and rejected for the
same reason: `current_year − birth_year` is systematically biased
toward *overestimating* age for anyone whose birthday hasn't yet
happened in the current year — exactly the wrong direction for a safety
gate.

**`meets_age(user, min_age)`:** `min_age` unset/0 → always passes (no
gate). Otherwise: prefer a verified attested birthdate if one exists
(below), else fall back to the self-reported `birthdate`, else **fail
closed** — an age gate that treats "unknown" as "assume old enough"
defeats its own purpose. This is the one place age-gating and
level-gating genuinely differ in shape, not just in name: level-gating
defaults permissive (`min_level` unset/0 = no gate, and a user with no
special level still has a real, always-present `user_level`); age-
gating's *resource* side defaults permissive the same way, but its
*user* side fails closed when data is simply missing.

**Attestation mechanism.** A `user_attestations` record — not just a
flag on the user row, since it needs real provenance and a signature to
be worth anything: `(subject_user, attribute: 'age' | 'name',
attested_value, verifier_fingerprint, signature, created_at,
link_visible)`. `attested_value` is an actual determined value (an
attested birthdate, or an attested real name) supplied by the verifier
from whatever real-world verification they used — a local meetup, ID
shown in person, whatever the SysOp judges sufficient for their
jurisdiction and community — never a threshold-specific pass/fail
against one particular resource's gate, so a single attestation stays
valid and reusable against any future gate, however strict. Signing
reuses round 7's existing node-vouching fallback exactly, applied to a
new kind of claim rather than a new mechanism: a verifier with a
personal keypair signs the attestation themselves; a password-only
verifier has their node sign on their behalf. For `attribute = 'age'`,
an attestation's `attested_value` always takes precedence over
self-reported `birthdate` in `meets_age` when both exist — verified is
strictly more trustworthy than self-reported by construction.

**`can_verify_identity` — a new, narrow, SysOp-grantable permission,
deliberately independent of §13's four moderator tiers** (see §13's own
note on this). A plain per-user boolean, no scope tiers — verifying a
person's real-world age or name isn't authority over a specific
board/area/channel, it's a fact-finding role about the person, and can
reasonably be granted to someone with no other moderator role at all
(e.g. a trusted local meetup organizer). Verifiers get a new,
conditionally-visible main-menu entry, **`[V]erify`** (same conditional
pattern as `[I]nvitations`/`[A]dmin`, hidden for everyone else): pick a
user, see their self-reported values (visible to the verifier
regardless of the subject's own visibility toggle — verifying is a
privileged trust action, not browsing the public directory), enter the
attested value, confirm, sign, store.

**Real name vs. display name: never overwritten, always coexist.**
Attestation never touches `display_name`. This mirrors §8's existing
`/nick` principle exactly — an alias/display name is presentation
metadata the user chose for themselves; canonical/verified identity is
layered alongside it, never replacing it. Beyond consistency, this
matters practically: a user might keep a pseudonymous display name for
ordinary use while still wanting to pass a real-name gate for one
specific resource (legal-liability accountability, a professional-
networking Community). Auto-overwriting the moment a verifier checks a
box would silently strip that choice away as a side effect of what's
supposed to be a narrow gate-passing mechanism.

**`name_requirement`: a three-state field
(`none`/`verified`/`verified_and_displayed`), not two independent
booleans.** "Displayed but not verified" isn't a coherent state, so the
field's own shape rules it out rather than needing separate validation.
`verified` covers a SysOp needing to be *able* to identify someone
(legal compulsion, escalated moderation) without broadcasting it;
`verified_and_displayed` covers a community where mutual visible
accountability is the actual point. Unlike age-gating, **there is no
self-report fallback for name-gating** — `display_name` never satisfies
`min_name_verification_required`, since the entire point is a verified
identity; an unverified self-report satisfying it would defeat the
feature outright.

**Display scope: the specific resource only, never BBS-wide.** A
Community/board/channel/area requiring `verified_and_displayed` shows
the real name only within its own rendering, never elsewhere on the
node — same reasoning as not overwriting `display_name`: one resource's
disclosure requirement shouldn't leak into contexts that never asked
for it. A user who doesn't want that tradeoff in a given resource
simply doesn't use it, the same choice age-gating already offers.

**Display formatting: primary slot is always self-chosen, real name is
always parenthetical, never leaves an empty lead.** Format (final form,
incorporating round 99's anti-forgery marker below):
`"{display_name or username} (={attested real name}=)"` — e.g. `SysOp
from Hell! (=Claude Code=)`, with the whole `(=...=)` unit rendered in
`VERIFIED_COLOR` on a color-capable client. If `display_name` is unset,
the primary slot falls back to `username`, not to nothing: `Thiesi
(=Claude Code=)`. Reversing the order (real name first) was considered
and rejected — `username`/`display_name` are both self-chosen by the
user, the real name never is, so leading with it would misrepresent how
someone wants to be known, the same shape of harm as misgendering.
`username` is guaranteed present (one of only two required signup
fields), so this fallback chain never bottoms out in a blank/orphaned
parenthetical.

**Anti-forgery mechanism — superseded from round 98's plain character ban
to a color-plus-marker scheme, round 99.** Round 98 first caught the
underlying problem (an unrestricted `display_name` can forge the entire
attestation display — a user with no verification at all could set
`display_name` to `Alice (Robert Smith)` and render *identically* to a
genuinely verified `Alice` whose real name is `Robert Smith`) and fixed
it by rejecting `(`/`)` from `display_name` entirely, mirroring round
53's `/nick` marker rejection. Round 99 replaces that with a stronger,
less restrictive mechanism:

- **The entire `(attested real name)` unit — delimiters included — is
  rendered in a new, dedicated `VERIFIED_COLOR`** (following the exact
  precedent of `NICK_COLOR`, round 53), applied at the render layer
  directly to the trusted `attested_value` from the `user_attestations`
  record — never derived from or combined with unsanitized user text.
  This is a **rendering-layer guarantee, not a text-pattern guarantee**,
  and strictly stronger than round 98's approach: round 29's existing
  terminal-sanitization boundary already strips embedded ANSI/escape
  sequences out of user-supplied fields like `display_name` before
  anything renders, so there is no way for a user's own text to acquire
  `VERIFIED_COLOR` — they can type all the parentheses they want, but
  never make their own substring render in that color. **`display_name`
  no longer needs to reject `(`/`)` at all** — round 98's restriction is
  lifted; ordinary display names (e.g. `Alex (they/them)`) work again.
- **A reserved marker inside the parenthetical, for when color doesn't
  survive rendering** (logs, transcripts, and — the deciding factor,
  not merely "rare" — screen readers, which have no way to perceive
  color at all, ever, making a color-only signal a permanent equity gap
  for exactly the population least able to work around it). Format:
  `"{display_name or username} (={attested real name}=)"` — e.g. `SysOp
  from Hell! (=Claude Code=)`. The `=` marker is deliberately distinct
  from `/nick`'s own `~` (the two rendering contexts rarely coincide,
  per round 53's own note that `/who`/`/whois`/`/names` use the plain
  `nick|username` form without markers, but keeping them visually
  distinct costs nothing and avoids any ambiguity where they might).
  **`display_name` rejects literal `=` at write time**, the same
  mechanism round 53 already built for `/nick`'s `~` and round 98
  applied to `(`/`)` — narrower than round 98's restriction (a
  genuinely rare character in real display names, unlike parentheses),
  while closing the exact same gap even in a color-stripped view: a
  spoofed `display_name` can produce `Alice (Robert Smith)` but can
  never produce the `=`-wrapped inner form, since `=` is rejected before
  it could ever reach storage.
- **Chosen over color-only (no marker, no restriction at all) —
  confirmed with Thiesi, weighing accessibility over implementation
  cost.** Thiesi had no strong preference either way and asked for a
  judgment call; decided in favor of the marker specifically because the
  people most affected by a color-stripped view (screen-reader users)
  have no alternative way to perceive the distinction, and the
  implementation cost is low — it reuses round 53's exact validation
  pattern for a new field rather than building anything new. Applied
  unconditionally, on every node, regardless of whether real-name-
  gating/attestation is enabled there, matching round 98's own reasoning
  for why this shouldn't be a rule that only activates once the feature
  is switched on.

**A separate, general "verified" badge** — just the boolean fact of
verification, not the attested value itself — may be shown on a user's
own profile, gated by the same existing per-field visibility toggle
everything else uses, entirely independent of any specific resource's
`name_requirement`. The user controls whether even the fact of
verification is public.

**Phase placement: local mechanism ships in the same before-Phase-3
window as Communities and self-update; Link propagation is explicitly
gated on Phase 4, not Phase 3 — and that's a different reason than Link
Communities' Phase 6 gate.** Local attestation (verifying your own
node's users, gating your own node's resources) is fully self-contained
and has no Link dependency at all. A *remote* node honoring *your*
attestation needs two things that don't exist before Phase 4
specifically: Phase 3's signed-event sync to carry the attestation, and
— the actual blocker — Phase 4's trust/reputation system to give a
remote node any basis for deciding whether to honor an attestation from
a verifier it doesn't know. Without that, a remote gate would be
trusting the signature's authenticity while having no way to judge the
signer's credibility — exactly the "trust the signature, not the
signer" gap §6's web-of-trust design exists to close. So: `link_visible`
defaults to `false` on every attestation, and even once Phase 4 exists,
propagation additionally requires the *subject user's* own separate
opt-in consent — it's their sensitive data, and the existing profile-
privacy philosophy (§13) already gives users that kind of control over
everything else.

**Clarified round 86, narrower than the paragraph above originally
read:** this Phase 4 gate is specifically about a remote node trusting
*another* node's attestation of a user it doesn't manage. It does not
block a carrying node from enforcing a Link Community's cascaded
age/name-gating default (§16) against its *own* local users using its
*own* local attestation data — that works the moment Phase 3's carry/
sync exists, no trust/reputation system required, since no cross-node
attestation trust decision is involved.

---

The sign-off notes below capture standing design decisions only —
reasoning, alternatives considered and rejected, and things
deliberately left open. As of this round of cleanup, the full
round-by-round implementation and bugfix history (debugging journeys,
"N tests passing" confirmations, code-diff-heavy walkthroughs) has been
moved out to [docs/NetBBS-worklog.md](NetBBS-worklog.md), which is a
companion, not a replacement — consult it for the complete unabridged
record of a given round, but it is historical record, not standing
design rationale.

## Sign-off notes (2026-07-08)

1. SQLite as storage layer — **confirmed**.
2. Noise Protocol Framework for node-to-node encryption — **confirmed**.
3. Phase breakdown — **confirmed as-is**. Explicitly validated rationale:
   the first attempt's core mistake was carving out NetBBS Link's design only
   after every other feature already existed, forcing significant
   conceptual rework across the board. Discussing NetBBS Link fully before
   Phase 1 (even though Phase 1 itself ships without a live Link) avoids
   repeating that mistake — every Phase 1 feature is being built with the
   Link already in mind, not bolted on later.

## Sign-off notes, round 2 (post first-attempt-docs review)

Prompted by reading the first attempt's Developer Manual and Onboarding
Guide (both supplied by the original dev team).

1. **Master Node theory confirmed by Thiesi** as the likely actual
   mechanism behind the rogue-SysOp incident — the first attempt's
   "Master Node" concept (authenticated network-wide administrative
   commands honored Link-wide) was a de facto centralized point of control
   sitting on top of an otherwise egalitarian mesh. Our design has no
   equivalent construct anywhere; confirmed as correct by construction, not
   by later patch.
2. **Emergency quarantine mechanism — added to §6.** Addresses "does local
   web-of-trust hold up at scale (dozens/hundreds of nodes)?" without
   reintroducing a master node. See §6 for full mechanism.
3. **Jurisdiction-bound authority key — added to §6 as a documented,
   deliberately-not-yet-implemented extension point.** Answers "can a
   'super SysOp' capability be added later if legally required?" Answer:
   yes, opt-in per node, without a protocol rearchitecture — but it cannot
   guarantee removal from nodes outside that authority's jurisdiction that
   never opted in, since no genuinely decentralized network can promise
   that.
4. **Monolithic single-file design — rejected, reversing earlier hedging.**
   Confirmed with Thiesi: the first attempt's monolith made sense for their
   trajectory (small scope initially, Link scope arrived late, thousands of
   LOC already existed by the time it would have mattered to split them).
   We're deliberately not repeating that trajectory, so there's no reason to
   inherit the same constraint. Going with a modular package from day one;
   see §3.
5. **Board/Area terminology — adopted** from the first attempt's docs; see
   §1 and §9.
6. **Bootstrapping hen-and-egg concern — no further input needed from the
   first-attempt team.** Their manual-peering requirement (a human on both
   ends before any node can join) was their actual pain point; our
   seed-node auto-bootstrap (§12) already avoids it by design.

## Sign-off notes, round 3 (answers from the first-attempt team)

1. **Master Node theory: confirmed as fact, not just plausible theory.**
   Root cause chain, per Thiesi (who ran the original test): flooding/spam
   → Master Node implemented as the response → Master Node itself got
   compromised. This was a controlled test explicitly designed to surface
   exactly this kind of flaw; no real-world harm occurred. Strengthens
   confidence in the quarantine mechanism (§6) as the right fix, since it
   solves the same problem (need to react fast to abuse) without the
   single-point-of-authority that got exploited.
2. **Transport (§11) — confirmed.** Split by traffic shape: HTTP+JSON with
   keypair signatures for store-and-forward Link traffic (boards/Link
   messages/file chunks), Noise Protocol Framework reserved for real-time
   chat. The first attempt reportedly considered this same split
   conceptually but didn't implement it due to developer resource
   constraints, not a design objection.
3. Dedup mechanism — see §7, now updated with persistent seen-event table
   + file-chunk transfer ID scheme, adopted from their (conceptual, not
   fully implemented) design.
4. Scale calibration — see §14, now updated with their empirical numbers.
5. Editor implementation lessons — see §15, now placed in Phase 2 after
   the round 6 phasing restructure.

## Sign-off notes, round 4 (permissions & moderation)

Prompted by a feature request from the first attempt's architect to their
lead designer (age/level-gated channels, read/write board permissions,
per-board moderators, mute/ban duration syntax, chat moderator tiers,
user directory/vCard/finger), relayed by Thiesi, plus follow-up discussion
on moderator scope tiers.

1. New **§13 Permissions & Moderation** added in full — covers board/area
   read-write split and moderator roles, chat age/level gating and
   mute/ban/kick commands, the three-tier moderator scope model
   (per-object / local-blanket / Link-blanket), the SysOp/global-moderator
   privilege boundary, and the user directory/vCard/finger system.
2. **Confirmed with Thiesi:** permission/level-gating plumbing belongs in
   Phase 1, not retrofitted later — same anti-retrofit reasoning as the
   Link itself. Now reflected in §15's Phase 1 bullet list.
3. **Confirmed with Thiesi:** ship local-only board/chat moderation first
   (originally Phase 3, now Phase 2 post-restructure — see round 6); scope
   out Linked-board/channel moderation properly once §7's Link core is
   mature, rather than designing it in the abstract now (now Phase 6).
4. **"Global implies local" — rejected, by design.** Thiesi's recollection
   of the first attempt didn't clearly confirm or contradict this, so the
   decision was made on merit rather than precedent: merging the two tiers
   increases blast radius without a corresponding need, and violates the
   same "no automatic power grants" principle from §6/§2. Explicit
   dual-grant required instead.
5. **SysOp vs. global-moderator privilege boundary defined:** SysOp alone
   can grant/revoke any moderator tier, change node config, and originate
   boards/channels. Global moderators are strictly content-scoped — cannot
   appoint moderators or touch configuration. Directly informed by the
   Master Node lesson (§2/§6): moderator authority must not be able to
   self-perpetuate or escalate.
6. **Linked-board moderation mechanism specified (design only, not yet
   implemented):** a moderator grant is a signed DAG event issued by the
   board's originating node; a moderator edit to an immutable Linked post
   is a new signed event that references and amends the original, verified
   against the granting event by every receiving node.

## Sign-off notes, round 5 (board/channel lifecycle)

Prompted by two more requests found in the architect/designer exchange:
global-moderator board/channel creation-deletion, and maintenance/expiry.

1. **Board/channel creation — adopted with a modification.** Global
   moderators/SysOp can create Linked boards/channels (origin-based grant
   authority, consistent with §13's existing model) — but creation
   propagates as a signed announcement, not a forced action on other
   nodes.
2. **Board/channel deletion — adopted with a modification.** Origin can
   mark closed/archived (signed event, no new posts), but cannot force
   deletion of already-stored content on other nodes. Real deletion stays
   local, tied to the maintenance system.
3. **Mandatory full-carry rule — rejected in favor of default-carry with
   visible opt-out.** Thiesi was open to a hard mandatory-carry rule
   ("joining NetBBS Link means carrying everything"); settled instead on
   carry-all-by-default with an explicit, visible per-board exclusion
   option, to stay consistent with §6's node-sovereignty principle while
   still delivering the "same content everywhere" property for the vast
   majority of cases.
4. **Maintenance/expiry system — adopted**, closely matching the first
   attempt's actual implementation: configurable max post age (default
   indefinite), active → expired → deleted state machine with a grace
   period, moderator-controlled pin/exempt. Expiry remains a local decision
   even for Linked boards, enabled by content-addressing (a pruned post
   stays re-fetchable from other nodes).
5. **Pin/exempt permission — confirmed to fold under `edit`**, with the
   coupling (can't grant pin/exempt independently of edit rights) explicitly
   logged as a known, accepted limitation rather than an oversight.

## Sign-off notes, round 6 (phasing scope review)

Prompted by Thiesi explicitly requesting a feasibility review of the
original 4-phase breakdown after several rounds of additions.

1. **Bug found and fixed:** old Phase 2 still referenced "Noise-encrypted
   node-to-node channel," stale since §11's later transport split (HTTP+
   JSON+signatures for store-and-forward, Noise reserved for real-time
   chat only). Corrected in the new Phase 3.
2. **Restructured from 4 phases to 7.** Two phases had grown too large to
   be a single unit of work (old Phase 2 bundled Link sync mechanics with
   the much harder trust/reputation system; old Phase 3 bundled real-time
   chat with the entire §13 permissions/moderation system). Full new
   breakdown in §15 above.
3. **Local permissions/moderation moved earlier** (now Phase 2, was
   bundled into old Phase 3) — has no Link dependency, and delivers a
   complete standalone BBS matching Thiesi's actual primary deployment
   target before any Link work begins.
4. **Trust/reputation resequenced before real-time chat** (Phase 4 before
   Phase 5, reversing the old implicit ordering where chat and trust were
   both loosely "Phase 3"). Rationale: shipping live Link-wide chat before
   quarantine/reputation exists would leave chat abuse undefended,
   uncomfortably close to the original incident's risk profile.
5. **Linked governance/lifecycle isolated as its own phase** (Phase 6,
   split out from old Phase 4) rather than grouped with door games/legacy
   compatibility — it's the most structurally novel part of the design and
   depends on both trust (Phase 4) and chat (Phase 5) already being proven.
6. **Fullscreen editor placement moved** from a loose "likely Phase 3"
   note to a firm home in Phase 2, since post/PM editing is exercised
   heavily once local moderation and boards are both fully functional.

## Sign-off notes, round 7 (implementation: boards)

Prompted by starting actual implementation of local message boards
(§15 Phase 1), which surfaced a real design fork not previously discussed.

1. **Post/board IDs are content-addressed starting in Phase 1**, per §7,
   even though no actual Link networking or signing exists yet — computed
   now specifically so a board's ID scheme never needs migrating when it
   later becomes Linked. Implemented in `netbbs.boards.content_id`: a
   deterministic BLAKE2b hash (32 bytes, hex-encoded) over
   sorted-key-JSON-canonicalized content, distinct from the shorter
   base32 identity fingerprints (§5), which are optimized for being
   human-typable rather than for network-scale collision resistance.
2. **Node-vouching for password-only users' posts — confirmed.** §5
   allows password-only accounts with no keypair, but §7/§11's signing
   model implicitly assumed every author has one. Resolved: the *node*
   (which already has its own keypair identity for §11 transport auth)
   signs/vouches for posts from its local password-only users when
   relaying to the Link, rather than requiring every user to hold a
   personal keypair just to post. Accepted tradeoff: a password-only
   user's posts are attributable to "some user on this node," not
   personally non-repudiable the way a keypair holder's are. Actual
   signing/vouching is Phase 3 scope (needs node identity loaded at
   runtime, not yet wired up); Phase 1 only needed the schema to be
   ready for it, via the nullable `author_fingerprint` column.
3. **Board permission model boundary, flagged rather than asked:** Phase
   1 boards get a simple coarse `min_read_level`/`min_write_level`
   (reusing the Phase 1 level-gating plumbing from §13). The richer §13
   moderator model (named read/write/edit/delete/approve grants,
   moderated-board approval) remains Phase 2 scope, layering on top of
   this rather than replacing it. Presented as an assumption rather than
   a full stop-and-ask, since the cost of being wrong is low — the
   column is additive, not something Phase 2 would need to remove.

## Sign-off notes, round 8 (display formatting)

*(Condensed — full round including the strftime validation bug writeup is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

1. **Storage vs. display timestamps formally split.** `utc_now_iso()`
   (microsecond-precision, for storage/content-ID hashing) is untouched;
   a new `format_for_display()` handles what a user actually sees, and
   never includes sub-second precision regardless of configuration.
2. **Configurability level, confirmed with Thiesi:** node-wide default
   (SysOp-configurable) now; per-user preference later, once a user
   preferences system exists. `format_for_display()`'s resolution order
   (future per-user override > node config > hardcoded default) is built
   in now specifically so adding per-user preferences later needs no
   changes to this function.
3. **New `netbbs.config` module**: a generic node-wide key-value store
   backed by a new `node_config` table, not a single hardcoded setting.
4. **European-style default** (`%d.%m.%Y %H:%M`, 24-hour clock) per
   Thiesi's preference, fully overridable.
5. **Validation approach: upfront allowlist of directive characters,
   not a runtime `try/except` around `strftime`** — verified directly
   that glibc's `strftime` does not reliably raise on an unknown
   directive, so invalid formats are now rejected at set-time rather
   than silently discovered later at display time.

## Sign-off notes, round 9 (timezone conversion)

Prompted by Thiesi correctly noting that a configurable format string
alone doesn't produce correct *local* time — format and timezone are
independent axes, and only the first had been built.

1. **Timezone conversion added**, same architecture as the display
   format work: `DISPLAY_TIMEZONE_CONFIG_KEY` in `node_config`, resolved
   through the same priority order (future per-user override > node
   config > hardcoded default), with the actual UTC-to-target-zone
   conversion now happening in `format_for_display` before `strftime`
   runs (previously it only reshaped the string, never converted the
   instant).
2. **Default is UTC, not any assumed locale** — deliberately
   unopinionated; node operators are expected to set this explicitly via
   `set_display_timezone`.
3. **Validation approach differs from the format-string case, and that
   difference was verified, not assumed:** `zoneinfo.ZoneInfo`'s failure
   modes are well-defined Python-level logic (does a matching tzdata file
   exist), unlike `strftime`'s platform-dependent C-library delegation —
   confirmed directly, including that it independently guards against a
   path-traversal-shaped key. `try/except` is a reliable validation
   mechanism here, in deliberate contrast to round 8's finding that it
   wasn't for format strings.
4. **Open item, not yet confirmed:** whether NetBSD's base system ships
   IANA tzdata that Python's `zoneinfo` can find. Verified working in a
   Linux sandbox only. `tzdata` is listed as an optional dependency
   fallback in `pyproject.toml`; if `is_valid_timezone("Europe/Berlin")`
   returns `False` on the actual NetBSD deployment, that's the fix.
5. **Housekeeping:** README's License section removed and
   `pyproject.toml`'s license field set to BSD-2-Clause, per Thiesi
   licensing the repo directly on GitHub. No LICENSE file added from this
   side, deliberately, to avoid duplicating/conflicting with whatever
   GitHub's own license picker already added.

## Sign-off notes, round 10 (implementation: local real-time chat)

*(Condensed — full round including the concurrency-bug writeup and testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

1. **New architectural piece: `netbbs.chat.hub.ChatHub`.** A per-node,
   in-memory, queue-per-participant broadcast hub with two concurrent
   asyncio tasks per chat session (one reading input, one draining the
   queue) — the first feature requiring a session to *receive* a
   message while idle.
2. **Channels mirror boards' content-addressing** (§7) for the same
   forward-compatibility reason, but with a single `min_level` rather
   than a read/write pair — chat access has no meaningful read/write
   split, confirmed explicitly during the earlier permissions design
   discussion.
3. **Chat messages are not persisted.** Ephemeral by design for Phase 1;
   revisit if local chat history/scrollback turns out to be wanted
   later (it was — see round 19/20).
4. **Known, deliberate UX limitation, not a bug:** because Telnet stays
   in the client's default line-editing mode at this point, an incoming
   chat message can land on screen while a user is mid-typing their own
   line, interleaving with it — the same behavior classic line-mode
   chat tools have always had. Properly fixing this needs the
   character-mode/redraw machinery the rendering framework is meant to
   provide (delivered in round 14).
5. **Main menu introduced** (`[B]oards [C]hat [Q]uit`), replacing the
   previous purely linear flow, since there are now genuinely two
   independent things to route between.

## Sign-off notes, round 11 (Link governance log + activity feed)

Discussion only — nothing implemented yet, deliberately, since almost
none of this feature's prerequisites exist before Phase 6. Captured now
so the reasoning doesn't need to be rediscovered when Phase 6 actually
starts.

1. **Feature proposed by Thiesi:** visibility into Link-wide
   administrative/control activity (new Linked boards/channels being
   created, etc.), restricted to SysOps and optionally global moderators.
2. **Refined from a single mechanism into two complementary ones, per
   Thiesi:** a persistent board (curated audit trail — human-relevant
   governance actions, worth reading later) plus an ephemeral chat
   channel (live "tail -f" feed — includes automatic/mechanical Link
   activity too, genuinely disposable). This resolves an initial concern
   Claude raised about the single-mechanism version becoming too noisy at
   large Link scale, without needing any future mitigation — the firehose
   simply has an ephemeral home nobody's obligated to read, from day one.
3. **Neither needs new infrastructure.** Both reuse existing
   board/channel/chat mechanisms as-is; the "feature" is a convention
   (governance and Link-activity events get posted as content by
   automated system actions, not typed by a human) layered on top,
   consistent with how boards/channels were always meant to extend to
   Link participation.
4. **Access control: same restricted audience on both**, deliberately —
   not more permissive for the channel just because it's framed as
   "noise," since that noise plausibly includes governance-sensitive
   content (e.g. quarantine flags firing).
5. **Phase placement: Phase 6, as a capstone deliverable, not earlier.**
   Every real content source this feature depends on — Link-wide chat
   transport (Phase 5), quarantine flags (Phase 4), board/channel
   creation/closure and moderator grants (Phase 6 itself) — only exists
   by Phase 6. Building it earlier would mean designing against events
   and APIs that don't exist yet, the exact retrofit risk this whole
   design process exists to avoid.
6. **Two open questions deliberately left unresolved** for whenever
   Phase 6 actually starts, rather than guessed at now: whether the
   channel should have a small amount of join-time scrollback (ordinary
   chat channels don't), and exactly which mechanical events are
   "interesting" enough to include — neither is answerable without real
   Link traffic to calibrate against.

## Sign-off notes, round 12 (implementation: local blocklist)

*(Condensed — full round including schema-verification detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

1. **New `netbbs.moderation` package**, the natural home for the richer
   §13 moderation model (mute/ban/kick, board moderator roles) once
   that's built in Phase 2 — started now with just the blocklist.
2. **Entries key on fingerprint when possible, local user ID otherwise**
   — mirrors the same keypair-vs-password-only duality already handled
   in `posts.author_fingerprint` and `users.fingerprint`.
3. **Blocklist enforcement lives in the login flow, not inside
   `netbbs.auth`.** Authentication ("are these credentials correct")
   and this kind of authorization ("is this account allowed to
   proceed") are different concerns — same layering principle already
   applied to keep `netbbs.permissions` separate from `netbbs.auth`.
4. **Edge case identified and handled defensively:** a user blocked
   while password-only (by local user ID) could theoretically later
   gain a keypair and no longer show as blocked under a naive
   fingerprint-only check — `is_blocked` checks both fields whenever a
   fingerprint is present, closing the gap now rather than leaving it.

## Sign-off notes, round 13 (implementation: ANSI rendering framework)

*(Condensed — full round including protocol-verification detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

1. **Scoped to the "ANSI half" only, per discussion:** color/cursor
   helpers and text reflow, both built now since they benefit every
   existing screen immediately. The "TUI half" (character-mode input,
   screen-buffer diffing) remains deliberately deferred until a real
   heavy screen needs it — confirmed with Thiesi rather than assumed.
   **Superseded in part by round 14:** character-mode input specifically
   was pulled forward after real testing surfaced client-side
   line-editing problems. Screen-buffer diffing for full heavy-screen
   TUI rendering remains deferred until round 64.
2. **256-color/extended ANSI**, confirmed with Thiesi over classic
   16-color — richer, at the accepted cost of some old/dumb clients not
   rendering it correctly. No 16-color fallback/downgrade path built.
3. **Terminal width: NAWS negotiation with an 80-column fallback**,
   confirmed with Thiesi. Implemented in `netbbs.net.telnet` (the one
   piece of the rendering framework that's inherently transport-specific,
   hence not living in `netbbs.rendering`).
4. **Line-ending convention fixed at the transport boundary
   (`Session.write()`), not by changing the transport-agnostic
   `reflow()`** — line-ending convention is a transport concern, not a
   text-utility one.

## Sign-off notes, round 14 (color consistency + character-mode input)

*(Condensed — full round including extensive testing/bug-fix detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

**Color consistency:**
1. New `netbbs.rendering.theme` — the actual palette in one place,
   replacing local color constants that had started drifting
   independently.
2. New `netbbs.rendering.menu.menu_key()` — highlights the actual valid
   keystroke in a menu option (e.g. the `B` in `[B]oards`).

**Character-mode input — a genuine architectural reversal, not an
incremental addition:**
3. **Root cause explained to Thiesi:** both symptoms (Backspace not
   working, `^M` instead of a newline) traced to the Phase-1-era
   decision to stay in the client's default line-editing mode. Asked
   whether to keep deferring character-mode input or pull it forward
   now — Thiesi chose to pull it forward, reversing round 13's earlier
   deferral.
4. **Scope confirmed with Thiesi before implementation:** whole-session
   character mode (not mixed per-prompt), Backspace/Delete-only editing,
   no arrow-key/cursor-movement support — full cursor-addressable
   editing remains out of scope, arguably fullscreen-editor territory
   (delivered in round 47). Password masking changed to `*` per
   character, now purely a local rendering decision since the server
   controls all echo persistently from connection start.
5. **`netbbs.net.telnet` rewritten**: `IAC WILL ECHO` sent once,
   persistently, at connection start rather than toggled per-read;
   `read_line()` replaced entirely, reading one byte at a time via a
   new `_read_byte()` primitive that centralizes all IAC/negotiation
   handling.
6. **A correctness detail identified during design, not discovered as a
   bug later:** multi-byte UTF-8 characters need explicit
   continuation-byte handling once reading byte-by-byte — a naive
   per-byte decode would have corrupted every non-ASCII character.

## Sign-off notes, round 15 (self-color in chat + immediate menu keys)

*(Condensed — full round including concurrency-verification and bug-fix detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

1. **Own chat messages now visually distinct.** New `SELF_COLOR`,
   distinct from `ACCENT_COLOR` (used for everyone else's names). The
   sender receives a self-colored copy directly; a separately-formatted,
   accent-colored copy is broadcast to everyone else (sender now
   excluded from that broadcast).
2. **New `Session.read_key()`** — reads one character and returns
   immediately, no Enter required, added to the `Session` ABC.
   Deliberately scoped to genuine single-choice menu selections only;
   free-text prompts correctly stay on `read_line`.
3. **A real, deliberate behavior loss, flagged rather than silently
   dropped:** the main menu previously accepted the full word
   ("boards") as an alternative to the single letter. Immediate
   single-key dispatch can't support that — acting on the first
   keystroke means there's no way to know whether more characters are
   about to follow.

## Sign-off notes, round 16 (shared paginated list picker)

*(Condensed — full round including the goto/stable-index bug writeup is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

1. **Two proposals evaluated, neither adopted as-is.** Pure two-digit
   paginated numbering solved "don't make me type" but not "jump to
   item #769" without paging through everything first. Tab completion
   doesn't solve long-range jumps either, and was flagged as
   inconsistent with single-key navigation elsewhere.
2. **Synthesis landed on and confirmed with Thiesi:** always-exactly-
   2-digit page-relative selection (for browsing) + a search command
   (filters by substring, auto-selects on a unique match) + a goto
   command (jumps directly to an absolute index). Page size adapts to
   the session's actual negotiated terminal height (NAWS), confirmed
   with Thiesi over a fixed size.
3. **Built once as `netbbs.net.picker.pick_item()`, shared across
   boards, chat channels, and (once built) file areas** — same
   underlying problem in all three, not reimplemented per feature.
4. **A stable, always-displayed absolute index — `(#N)` — carried
   alongside page-relative selection**, so `goto` always resolves
   against the original list regardless of active search filtering, and
   is actually discoverable on screen.

## Sign-off notes, round 18 (sort order, stable-ID goto, pinning, categories)

*(Condensed — full round including bug-fix and testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

1. **Creation-order sorting rejected as a real user-facing default** —
   a pure implementation convenience never actually chosen *for* the
   user. Three sort orders now supported for boards: **activity** (most
   recent post, default), **alphabetical**, and **volume** (total post
   count — a deliberately different signal from activity). Channels
   support activity (in-memory) and alphabetical; no volume sort, since
   channel messages aren't persisted. Per-user sort preference remains
   deferred, pending a user-preferences system.
2. **A genuine technical tension identified and resolved: configurable
   sort order breaks `goto`'s whole premise.** Resolved by decoupling
   entirely: `pick_item` now takes a `stable_id_of` callable supplying
   each item's permanent identity, fully independent of whatever order
   the caller's list happens to be sorted in for browsing. Confirmed
   with Thiesi over the alternative (accepting `goto` as only stable
   within one sort choice).
3. **Favorites — designed for, not built.** A per-user favorites list is
   just another list through the same picker with the same stable IDs,
   once user-scoped storage exists (deferred).
4. **Channel "activity" tracked in-memory (`ChatHub.last_activity`), not
   in the database** — a persisted "last activity" column would need a
   database write on every single chat message, working against the
   ephemeral-by-design reasoning for chat.
5. **Pinning added for both boards and channels** — a boolean column,
   always sorting first regardless of chosen order. Direct parallel to
   post-pinning already designed in §13.
6. **Two-level categories added for both boards and channels**, capped
   there deliberately (checked with Thiesi explicitly: arbitrary depth
   wasn't wanted). Separate `board_categories`/`channel_categories`
   tables rather than one shared polymorphic table, consistent with
   boards and channels already being fully independent subsystems.
7. **Category vs. board/channel raw-ID collisions**, since both start at
   1, are resolved by negating category IDs for picker purposes only;
   board/channel IDs stay unchanged.
8. **Browsing is now recursive, capped naturally at two levels**: pick a
   category to drill in, or a board/channel to open it directly. A level
   with no categories falls back to the exact flat picker experience
   from before categories existed.

## Sign-off notes, round 19 (chat scrollback — discussion only, not implemented)

Prompted by Thiesi asking for a genuine opinion on the first attempt's
short-term chat persistence, explicitly not requesting implementation.

1. **Split into two distinct problems, not one decision:** (a) chat
   looking empty after a *local* node restart — a real, low-risk, purely
   local UX problem — versus (b) a newly-joining *Link* node needing
   catch-up scrollback from peers — a much bigger question entangled
   with trust/provenance and Phase 5/6 machinery that doesn't exist yet.
   Treating these as one question risked either over-scoping Phase 1 or
   under-thinking the harder Link version.
2. **Confirmed with Thiesi: solve (a) only, defer (b) explicitly** to
   whenever Phase 5 actually starts, rather than attempt to resolve it
   now — added as an open question there (§15) instead of a decision.
3. **This is round 10's "no persistence" decision being revisited, not
   reversed** — that note explicitly said "revisit if local chat
   history/scrollback turns out to be wanted later." This is that
   revisit.
4. **Design landed on: disk-backed, bounded by message count (not time
   window)** — count gives predictable storage size and predictable
   scrollback length regardless of a channel's chat volume, unlike a
   time window (huge buffer for an active channel, nearly empty for a
   quiet one). Added to Phase 1's scope (§15).
5. **A privacy-posture point raised and worth carrying forward, not
   silently absorbed:** even short/bounded persistence is a different
   promise to users than pure ephemeral chat — someone typing something
   impulsively today reasonably expects it to vanish on disconnect under
   the current design. Worth a visible note to users once this is
   actually built (e.g. in a channel-join message), not just an internal
   implementation detail.

## Sign-off notes, round 20 (chat scrollback — implemented)

*(Condensed — full round including a test-correctness fix unrelated to this round's design is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Implements round 19's design.

1. **Retention limit: 100 events per channel, node-wide, confirmed with
   Thiesi.** Configurable via `node_config`, same pattern as
   `netbbs.timeutil`'s display format/timezone settings.
2. **Real design addition beyond round 19, raised by Thiesi during
   review: join/leave presence events are persisted and replayed
   alongside chat messages, not just message text.** Without this, a
   replayed message from someone who has since left the channel carries
   no indication of that. A `kind` discriminator column on one
   `channel_messages` table carries this, rather than two separate
   tables, since both share identical channel/ordering/trimming
   semantics.
3. **Storage is structural, not pre-rendered ANSI** — the same
   storage/display separation `netbbs.boards.boards.list_boards`
   already keeps; means a future theme change, or a non-ANSI client,
   needs no data migration.
4. **Replayed messages are never shown in `SELF_COLOR`** — that color is
   a live-typing affordance, which doesn't carry meaning when reading
   back history that may have originated from a different session.
5. **Round 19's privacy note implemented as literal bracketing text**
   around the replay, rather than a one-line disclaimer.
6. **Trim-on-insert, not a background job** — consistent with there
   being no background-job machinery anywhere else in Phase 1; revisit
   only if per-insert trimming cost ever actually shows up as a real
   bottleneck.

## Sign-off notes, round 21 (file area core — implemented; upload/download transfer deferred)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

1. **Schema mirrors `netbbs.boards` closely** — content-addressed IDs
   (§7), separate read/write level-gating (§13). Strict §1 terminology
   followed: "area," never "board," anywhere in code, tables, or
   messages.
2. **Categories, pinning, and sort order built in from the start**,
   unlike boards/channels, which got this shape retrofitted in round 18
   after shipping without it — confirmed as the right call given this
   project's own anti-retrofit principle.
3. **A real design gap found and resolved through two rounds of
   back-and-forth:** the first proposed transfer mechanism (a
   length-prefixed raw-byte protocol over Telnet) assumed a
   NetBBS-aware client, which a generic Telnet client can't provide.
   Presented as a three-way fork (companion CLI tool / real Zmodem /
   defer transfer entirely) — **confirmed: real Zmodem support**,
   authentic to the BBS tradition, but explicitly scoped as its own
   separate task given its size and correctness risk (delivered in
   round 24).
4. **File area core ships now without live upload/download**, the same
   bootstrap sequencing boards and channels already went through.
5. **Storage: filesystem, not SQLite blobs** — content-addressed by
   sha256, sharded two hex characters deep (the same pattern git's own
   object store uses). A deliberate side effect, not the primary
   motivation: two uploads with byte-identical content share one stored
   blob.
6. **A file's content-addressed `file_id` is computed from its sha256
   *and* upload metadata (area, filename, uploader, timestamp) — not a
   pure content hash.** Two uploads of byte-identical content are still
   distinct events.
7. **`upload_file` takes a complete file as one in-memory `bytes`
   buffer, not a stream** — appropriate at this project's stated scale
   (§14). Revisit once real Zmodem transfer streams incrementally.

## Sign-off notes, round 22 (SSH & web connectivity — discussion only, not implemented)

The last two open pieces of Phase 1 connectivity (§15), researched and
proposed for sign-off. Neither is implemented yet — this is the design,
following the same "discuss and confirm before code" pattern round 19
used for chat scrollback.

1. **SSH library: confirmed `asyncssh`.** Asyncio-native (fits this
   project directly, unlike thread-based `paramiko`), actively
   maintained, and already packaged in NetBSD pkgsrc (updated to 2.24.0
   as of 2026-07-01) — installable as a binary via `pkgin`.
2. **A real trade-off against §2's PyNaCl-over-`cryptography` decision,
   surfaced and accepted rather than silently overridden:** every viable
   Python SSH library (asyncssh, paramiko, Twisted Conch) depends on
   `cryptography`, which needs a Rust toolchain to build from source —
   the exact friction PyNaCl was chosen to avoid. No pure-libsodium SSH
   implementation exists, or realistically could: SSH's algorithm set
   (RSA/ECDSA/DH key exchange, not just Ed25519) goes beyond what
   libsodium covers. Accepted because NetBSD pkgsrc already builds
   `py-cryptography` as a binary package — installing via `pkgin` needs
   no local Rust toolchain, a materially different risk than "you must
   compile this yourself." Scoped narrowly: PyNaCl stays canonical for
   node identity/signing (§5/§7); `cryptography` is accepted as an
   SSH-connectivity-specific dependency, not a reversal of the original
   decision.
3. **A genuine bonus, not the primary motivation: SSH public-key auth
   can finally exercise the already-implemented Ed25519 challenge-
   response login path.** `netbbs.net.login_flow._login`'s docstring has
   flagged since Phase 1's early Telnet work that keypair login is
   "fully implemented in the auth module already" but unreachable
   because "a plain Telnet client has no way to sign a challenge... that
   path needs a NetBBS-aware client." Standard SSH public-key auth *is*
   exactly that challenge-response, with zero custom client software
   needed — any real SSH client exercises it. Both password auth
   (mapping straight onto the existing `authenticate_password`) and
   Ed25519 pubkey auth (matching a connecting client's key against the
   same `public_key` already stored on the user's account) are in scope
   from day one, given how little incremental cost the second path adds
   once the first exists.
4. **New `Session` implementation, no changes needed elsewhere** — this
   is exactly the abstraction boundary `netbbs.net.session.Session` was
   designed for (its own docstring: "Telnet, SSH, and a web-based
   terminal emulator... are all supported connection methods, landing on
   this one interface"). An `SSHSession` implements `write`/`read_line`/
   `read_key`/`close` against `asyncssh`'s process I/O; the whole
   login/menu/boards/chat/file-area layer needs no awareness that a
   connection arrived over SSH rather than Telnet.
5. **One item explicitly left to verify empirically once implementation
   starts, not decided now:** whether `asyncssh`'s PTY channel hands
   over raw, unechoed bytes the way Telnet's character-mode negotiation
   does, or whether the connecting client's own terminal remains in
   charge of local echo/line-editing by default (requiring an explicit
   terminal-mode request to match Telnet's behavior). Round 19-style
   "confirm before code" caution doesn't extend to guessing protocol
   behavior that's simply checkable once the library is in hand.
6. **Web: `aiohttp`, serving both the static terminal page and the
   websocket endpoint from one process** — also NetBSD pkgsrc-packaged.
   No second library/process needed just to host a static HTML/JS page
   alongside the websocket route.
7. **Web wire protocol: structured JSON messages
   (`{"type":"key","data":"a"}` / `{"type":"resize","cols":...,
   "rows":...}` from browser; `{"type":"output","data":"..."}` to
   browser), not raw byte passthrough via xterm.js's `addon-attach`.**
   Confirmed over the alternative after weighing both: raw passthrough
   would reuse `TelnetSession`'s existing character-mode byte parsing
   (backspace, CR/LF, UTF-8 continuation, escape-sequence discarding)
   almost as-is, but that parsing exists specifically to compensate for
   *raw terminal byte* ambiguity — a problem a browser's `keydown` event
   has already resolved before anything reaches the websocket. Structured
   messages also make terminal resize a first-class message instead of a
   bolted-on side channel `addon-attach` has no native way to carry.
   `WebSession` implements `Session` directly against these messages, no
   byte-level parsing at all.
8. **xterm.js is vendored into the repo as static assets
   (`src/netbbs/web/static/`), not loaded from a CDN.** Consistent with
   this project's self-hosted, NetBSD-friendly posture (the same
   reasoning behind avoiding a runtime Rust dependency for crypto) — no
   external network dependency at connect time, works on an offline/
   airgapped node. A small custom JS shim (also vendored, not npm-built
   at install time) speaks the structured-message protocol from point 7.
9. **Sequencing: SSH before web.** SSH reuses more of what already
   exists (raw byte I/O, the existing character-mode parsing patterns);
   web additionally needs a new static-asset story and a genuinely new
   wire protocol. Building the smaller, more-precedented piece first.

## Sign-off notes, round 23 (SSH connectivity — implemented)

*(Condensed — full round including extensive verification detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Implements round 22's SSH design.

1. **A real architectural finding, not anticipated in round 22: Telnet's
   character-mode line/key-reading logic needed to move to a new shared
   module, `netbbs.net.char_input`, rather than being duplicated in
   `netbbs.net.ssh`.** Once `asyncssh`'s own client-visible line editor
   is disabled, it hands over exactly the same kind of raw, un-echoed
   byte stream Telnet does, needing the same backspace/UTF-8/
   escape-sequence handling. Extracted behind a small `ByteSource`
   protocol each transport implements against its own primitives.
2. **`asyncssh` configuration confirmed empirically, not just from
   docs:** `line_editor=False` (server-wide) plus `encoding=None`
   (binary mode) reproduces Telnet's character-mode contract exactly.
   Binary mode specifically avoids `asyncssh` decoding UTF-8 one raw
   byte at a time, which would corrupt multi-byte characters.
3. **Terminal resize delivered as an exception (`asyncssh.
   TerminalSizeChanged`) raised out of `stdin.read()`,** not a callback
   — handled the same transport-level-action-with-no-data way Telnet's
   NAWS subnegotiation already is.
4. **A new auth entry point, `authorize_public_key`, added alongside
   the existing `authenticate_keypair`, not reusing it.**
   `authenticate_keypair` expects a caller-generated challenge and
   signature, built for a hypothetical NetBBS-aware client that doesn't
   exist. SSH's own protocol already proves private-key possession
   before validation is ever called; calling `authenticate_keypair`
   there would demand a second, redundant signature.
5. **A dedicated SSH host key, not the node's `netbbs.identity`
   keypair** — generated on first use and persisted alongside the DB
   file. Reusing the node's Link identity keypair as its SSH host key
   was considered and rejected: it would tie two independent concerns
   together for no real benefit.
6. **`asyncssh` (and therefore `cryptography`) kept as a separate `ssh`
   extra in `pyproject.toml`, not a core dependency** — confirming
   round 22 point 2's scoping. A Telnet-only deployment never needs to
   install `cryptography`/Rust's build chain at all; the node logs a
   one-line note and continues Telnet-only if `asyncssh` isn't
   importable.

## Sign-off notes, round 24 (real Zmodem support — implemented)

*(Condensed — full round including the desync-bug writeup and testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Implements the file-transfer piece deferred out of round 21.

1. **Build-vs-buy checked before writing anything, per Thiesi's
   question.** Three existing options surveyed: `modem` (abandoned since
   2011, Python-2-era); `trzsz` (not actually ZMODEM, a different
   proprietary protocol incompatible with real Zmodem terminals, which
   would have defeated the entire point); `xmodem` (XMODEM only, not
   ZMODEM). None viable; confirmed writing this from scratch.
2. **Scope, confirmed with Thiesi before writing the state machine:**
   CRC-16 only (not CRC-32); no resume/crash-recovery; no batch mode;
   **no retry/timeout resync state machine** — classic ZMODEM's retries
   exist for a noisy serial line, a failure mode that essentially
   doesn't happen over Telnet/SSH's TCP transport. A CRC mismatch or
   malformed frame raises `ZmodemError` and aborts immediately rather
   than attempting recovery.
3. **One deliberate, narrow exception to "no timeouts," added during
   implementation:** every point waiting on the peer's *next expected
   response* (not mid-transfer bulk data) is bounded by a 15-second
   timeout, so a terminal that simply doesn't support Zmodem doesn't
   hang the whole session forever.
4. **`Session` gained two new abstract methods, `read_byte`/
   `write_raw`**, formalizing raw byte I/O both `TelnetSession` and
   `SSHSession` already had pieces for — Telnet's `write_raw`
   IAC-doubles literal 0xFF bytes, since ZMODEM's own framing can
   genuinely produce them; SSH's needs no escaping, already 8-bit clean.
5. **What was honestly flagged, not glossed over:** genuine
   interoperability with an actual external Zmodem-capable terminal
   client (SyncTERM, lrzsz) hasn't been verified — no such client is
   available in this sandboxed dev environment. Worth a direct test
   from Thiesi's own machine or NetBSD target.

## Sign-off notes, round 25 (web/xterm.js connectivity — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Implements round 22's web design, the last open piece of Phase 1
connectivity.

1. **`netbbs.net.web`: `aiohttp`-based, mirroring `TelnetServer`/
   `SSHServer`'s shape exactly** — confirms the `Session` abstraction
   boundary actually holds across a third, structurally very different
   transport (request/response HTTP plus a push-based websocket).
2. **`WebSession` does *not* reuse `netbbs.net.char_input`'s byte-level
   `ByteSource` protocol** — confirmed deliberate, not an oversight.
   That abstraction reconstructs UTF-8 characters from a raw byte
   stream one byte at a time; a browser's `onData` event already hands
   over complete, decoded Unicode characters (and can hand over several
   at once), unlike Telnet/SSH's strictly one-byte-at-a-time delivery.
3. **File transfer is explicitly not available over this transport** —
   real Zmodem interop depends on the *terminal client* auto-detecting
   and driving the protocol, a capability of native terminal emulators,
   not a JS widget in a browser tab. `WebSession.read_byte`/`write_raw`
   raise `NotImplementedError`; the user sees a clear "not available"
   message rather than a crashed session.
4. **xterm.js vendored into the repo as static assets, not loaded from
   a CDN.** Consistent with this project's self-hosted, NetBSD-friendly
   posture — no external network dependency at connect time. `aiohttp`
   is its own optional `web` extra in `pyproject.toml`.
5. **What could not be verified: actual browser rendering and
   interaction** — no browser-automation tool is available in this
   sandboxed environment. Worth a direct check from Thiesi (or a future
   session with browser tooling) before considering this fully done.

## Sign-off notes, round 26 (Phase 1 scope: TUI framework rescoped to Phase 2)

Prompted by a post-Phase-1 external audit finding a real contradiction:
§15 listed "Hybrid ANSI/TUI rendering framework" as Phase 1 scope, but
round 13 had already confirmed with Thiesi that the TUI half (screen-
buffer diffing for heavy cursor-addressable screens) would be deferred
until a real heavy screen needed it — meaning Phase 1 was being
declared complete while one of its own listed deliverables was still
documented as unimplemented, an internal inconsistency rather than a
new decision.

1. **Formally rescoped, not just re-explained:** §15's Phase 1 bullet
   now reads "ANSI rendering + transport-independent character-mode
   input," with the TUI half moved to Phase 2's bullet list, alongside
   the fullscreen editor — the actual first real consumer for a
   screen-buffer/diff abstraction. This was a genuine two-way fork
   (build the minimal TUI foundation now with no consumer to validate
   it against, vs. formally move it) — confirmed with Thiesi rather
   than assumed, per this project's design-before-code convention.
   Rescoping was chosen: designing a screen-buffer/diff API in a
   vacuum, months before the fullscreen editor that's supposed to
   exercise it, risked getting the abstraction wrong and needing a
   rework anyway once Phase 2's actual requirements were known.
2. **No code changed.** This is a documentation/scope-alignment fix
   only — `netbbs.rendering` already only ever implemented the ANSI
   half (round 13); README and `netbbs.net.login_flow`'s docstring
   already described the same ANSI-only reality, just without §15
   agreeing with them. Updated both to stop calling the ANSI-only
   framework "hybrid ANSI/TUI" now that the name would otherwise imply
   a still-Phase-1 deliverable.
3. **Phase 1 is now genuinely, not just declaratively, feature-complete**
   — every bullet remaining under Phase 1 in §15 has shipped code and
   tests behind it as of round 25.

## Sign-off notes, round 27 (NetBBS Link canonical event format — placeholder only)

An external audit flagged that content IDs (round 7: hashing sorted
compact JSON) are meant to survive local boards moving to NetBBS Link
unchanged, but the canonical wire format they'll eventually hash isn't
actually specified yet — Unicode normalization, allowed value types,
duplicate-key/unknown-field handling, versioning, event-type domain
separation, and stable identity for password-only authors (username
alone isn't globally unique without an origin node) are all still
open. Explicitly a Phase 3 concern (§15) — this round records the
open questions and a provisional shape, not final answers.

1. **Deliberately not finalized now, confirmed with Thiesi rather than
   assumed:** a two-way fork existed between writing the full formal
   spec (with golden test vectors) immediately, or recording a
   lightweight placeholder and deferring the real answers to when
   Phase 3 work begins. The placeholder was chosen — freezing detailed
   canonicalization semantics (e.g. nonce vs. author-local monotonic
   sequence for distinguishing two identical posting actions) months
   before there's a second implementation to test interop against
   risks guessing wrong and needing a migration anyway, the exact
   outcome content-addressing was originally adopted to avoid. No code
   changes in this round — `netbbs.boards.content_id`'s current hash
   remains a Phase-1-only, single-node, non-wire value; nothing today
   depends on it surviving unchanged into Link.
2. **Provisional envelope shape, for Phase 3 design work to start
   from, not to implement against yet:**
   ```json
   {
     "netbbs_protocol": 1,
     "object_type": "board_post",
     "payload": { ... }
   }
   ```
   `netbbs_protocol` makes versioning mandatory from the first byte
   rather than inferred; `object_type` makes event-type domain
   separation a required envelope field rather than caller convention
   (directly answering the audit's "mandatory, not caller convention"
   criterion) — both decided now since they're cheap, low-regret
   structural choices independent of the harder open questions below.
3. **Open questions deliberately left open, to resolve when Phase 3
   design work actually begins:** canonical JSON serialization rules
   (key ordering, Unicode normalization form, whether floats are
   forbidden, duplicate-key handling); absent-field vs. explicit-`null`
   semantics; whether a nonce or an author-local monotonic sequence
   distinguishes two visually-identical posting actions; the exact
   shape of a node-vouched opaque local ID + origin-node fingerprint
   for password-only authors (vs. keypair authors, who already have a
   natural global identity — see §5); deterministic tie-break ordering
   beyond `(created_at, post_id)`, which Phase 1 already uses locally
   (round 7) but which hasn't been confirmed as the Link-wide rule.
4. **Explicit non-goal for this round:** no golden test vectors,
   since there is no second implementation yet for them to protect
   interop against — premature this far ahead of Phase 3.

## Sign-off notes, round 28 (node configuration, secure defaults, login throttling — implemented)

*(Condensed — full round including extensive verification/bug-fix detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Three audit issues bundled deliberately into one round, since #1's
"insecure listeners require explicit opt-in" and #15's "runtime
behavior driven by validated configuration" are the same underlying
config model, and #3's throttling policy needed to be *part of* that
config model from the start.

1. **New `netbbs.net.nodeconfig` module — an optional TOML file plus
   CLI overrides (CLI wins), validated before use.** Python 3.11's
   stdlib `tomllib` was used rather than adding a dependency. Unknown
   config-file sections/keys are a hard error, not silently ignored.
2. **Secure defaults, resolving issue #1:** SSH defaults *enabled*;
   Telnet and the plain-HTTP web transport both default *disabled*,
   and even when explicitly enabled without an operator-chosen host,
   default to `127.0.0.1` rather than every interface. **No TLS support
   was built directly into the web transport** — confirmed as the
   deliberately smaller-scope option: a TLS-terminating reverse proxy in
   front of a loopback-bound instance is the documented, supported
   path, avoiding ongoing certificate-handling maintenance surface this
   project doesn't need to own when every mainstream reverse proxy
   already solves it well.
3. **Cross-connection login throttling, resolving issue #3 — new
   `netbbs.net.throttle.LoginThrottle`:** three independent token-
   bucket budgets (per-source-address, per-username, node-wide global),
   node-lifetime shared state that reconnecting does not reset, plus a
   separate concurrent-unauthenticated-session cap. Token buckets over
   hard lockouts, per the issue's own explicit preference — a bucket
   run dry simply refills, so there's no persistent locked-out state to
   weaponize. All-or-nothing consumption across the three budgets via a
   non-consuming `peek`. Per-key buckets are capped via LRU eviction —
   **an honest, explicitly accepted limitation**: an attacker who also
   rotates *both* source and username defeats per-key throttling by
   construction, which is exactly why the global budget layer exists as
   a backstop that doesn't depend on key identity at all.
4. **The expensive-verification budget check happens *before*
   `authenticate_password_async` runs, not after** — a throttled
   attempt never pays Argon2's real cost.
5. **Idle timeout and overall login deadline are two genuinely
   different mechanisms:** each individual prompt read is wrapped in
   its own timeout that resets on activity; separately, the *whole*
   login attempt loop is wrapped in one overall deadline.
6. **SSH gets a deliberately narrower slice of this throttling story,
   not the full treatment — a scope boundary, not an oversight:** SSH
   *does* consult the same shared `LoginThrottle` budgets, but the
   idle-timeout/login-deadline/concurrent-session-cap machinery is not
   reimplemented for SSH — `asyncssh` already owns that connection's
   handshake lifecycle via its own documented `login_timeout` option;
   reimplementing it independently would mean two competing timeout
   mechanisms racing on the same connection for no benefit.
7. **`netbbs.__main__` rewritten around one testable `run(config, *,
   shutdown_event)` coroutine, resolving issue #15:** the injectable
   `shutdown_event` was a deliberate design choice so the whole
   coordinated-shutdown path is unit-testable without a real subprocess
   or real OS signals.
8. **Zero listeners started is also a hard, clear failure**, not a
   silently-idle process.

## Sign-off notes, round 29 (terminal rendering sanitization — implemented)

*(Condensed — full round including extensive verification detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Addresses that usernames, board/channel/file-area names and
descriptions, post subjects/bodies, chat messages, and filenames all
ultimately reach an ANSI-capable terminal, with nothing previously
stopping one of those values from smuggling an arbitrary CSI/OSC/DCS
sequence into a user's terminal.

1. **New `netbbs.rendering.sanitize.sanitize_text()` — the one
   documented sanitizer every terminal-visible untrusted string now
   passes through, immediately before interpolation.** Two genuine
   either/or forks confirmed with Thiesi rather than picked
   unilaterally: **silent removal, not visible-marker replacement**, for
   stripped characters — simpler, no unpredictable length change; and
   **only the 9 well-documented bidi embedding/override/isolate
   controls** (the "Trojan Source" set), not the entire Unicode "Format"
   category, since the broader category also contains characters with
   legitimate uses in real multilingual/emoji text and no reordering
   capability of their own.
2. **Removes every Unicode "Control" (Cc) character** — since all
   CSI/OSC/DCS/APC sequences require an ESC byte to introduce them,
   removing ESC alone is sufficient. Tab is always kept; newline is
   kept only when the caller opts in (post bodies — genuinely
   multi-line content). Carriage return is **always** stripped
   regardless, unlike `\n`, since it isn't touched by CRLF
   normalization and would reach the wire as a raw cursor move.
3. **Sanitizes on output, not on storage — the exact split the issue
   asked for.** Nothing written to the database is ever touched;
   `sanitize_text()` is called only at the point a value is about to be
   interpolated into something written to a `Session`. A moderator, or
   a future Link re-transmission, still sees the original content.
4. **Distinguishing trusted NetBBS-generated ANSI markup from untrusted
   content is structural, not a property `sanitize_text` itself
   tracks:** callers sanitize only the untrusted piece, at the point of
   interpolation, before handing it to `colored()` — never the whole
   already-composed line.
5. **Centralized in `netbbs.net.picker.pick_item()` for board/channel/
   file-area listings** — sanitized inside the shared picker itself,
   once, rather than requiring every current and future caller to
   remember it individually.
6. **Explicit non-goals for this round**, matching the issue's own
   framing as "Medium now; High once remote Link content is accepted"
   — nothing here is Link-specific, since no Link code exists yet.
   Also explicitly not attempted: broader Unicode confusable-character
   detection (homoglyph spoofing of usernames/board names) and
   moderation-side content filtering — both real, separate concerns,
   worth their own design pass if/when they matter.

## Sign-off notes, round 30 (board post pagination + honest concurrency docs — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Addresses that `list_posts()` fetched a board's *entire* history on
every visit, and the `Database` class's own docstring overclaimed what
WAL mode buys given this project's actual single-connection
architecture.

1. **Default view flipped to newest-first, a genuine UX fork confirmed
   with Thiesi rather than assumed:** opening a board now jumps to its
   most recent activity, not its oldest post.
2. **`netbbs.boards.posts.list_posts_page()` replaces `list_posts()`
   entirely** — not added alongside it, since keeping an unbounded
   query function sitting in the module would just be a footgun for
   some future caller to reach for instead of the bounded one.
3. **Cursor-based (keyset) pagination, not `OFFSET`/`LIMIT`** —
   deliberate: stable under concurrent inserts (a new post arriving
   between two page loads can't shift already-seen posts or duplicate
   one across pages), and avoids an ever-growing `OFFSET` scan cost
   when paging deep into an old board's history. Ordering is
   `(created_at, post_id)` ascending, `post_id` breaking ties
   deterministically since `created_at` alone is not a total order.
4. **`has_older`/`has_newer` are computed with their own small indexed
   `EXISTS` queries against the fetched page's actual boundary posts,
   not inferred from which fetch mode was used** — an earlier, simpler
   "infer from mode" design was rejected once it became clear it
   doesn't hold in every case (e.g. the cursor passed in was already
   the newest post available).
5. **New composite index** on `(board_id, created_at, post_id)`; the old
   single-column index was deliberately *not* dropped in the same
   migration — dropping a shipped index is a separate, non-urgent
   cleanup with no user-visible benefit at this project's declared
   scale (§14).
6. **`Database`'s docstring corrected:** round 2's original claim that
   WAL lets "concurrent asyncio readers and writers... not block each
   other more than necessary" is true of WAL in general but not of
   *this* architecture — exactly one synchronous `sqlite3.Connection`
   blocks the *entire event loop* per query, not just the calling
   coroutine. Concurrent sessions are not concurrent database access;
   they're serialized by Python's own cooperative scheduling. WAL is
   still worth keeping for the one place genuinely independent
   connections against the same file exist today: an admin/dev script
   run against a live node's database file.
7. **`PRAGMA busy_timeout = 5000` added** for that same separate-process
   scenario, so a momentary overlap surfaces as a wait rather than a raw
   `OperationalError`.
8. **The larger architectural question — "consider a bounded
   connection pool, database actor, or off-loop execution for
   expensive queries" — is deliberately not attempted this round.**
   Documented honestly as a real, current limitation rather than
   silently ignored; worth its own dedicated design round if/when the
   declared scale (§14) is actually being approached.
9. **Interactive navigation**: `[O]lder`/`[N]ewer`/`[R]ecent`/`[B]ack`,
   only offering the options that currently apply.
10. **Explicit non-goals:** first-unread state (no read-tracking exists
    to build it on). File area listings were flagged as having the same
    unbounded-fetch shape, left as a known gap for a future round
    (addressed in round 31). Chat scrollback does *not* have this
    problem — it's already bounded by round 19/20's trim-on-insert
    retention cap.

## Sign-off notes, round 31 (file-area pagination — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Round 30 explicitly flagged `netbbs.files.entries.list_files` as having
the same unbounded-fetch shape `list_posts` used to.

1. **`list_files_page()` deliberately mirrors `list_posts_page()`'s
   design byte for byte**, not a fresh design pass: same cursor-based
   pagination, same `(created_at, file_id)` ordering with `file_id` as
   the deterministic tie-breaker, same `has_older`/`has_newer`
   computed via their own indexed `EXISTS` checks. `list_files`
   replaced entirely, same reasoning as `list_posts`'s replacement.
2. **New composite index** on `(area_id, created_at, file_id)`, same
   reasoning as round 30's posts index including leaving the old
   single-column index in place.
3. **`_show_area` mirrors `_show_board`'s pagination *semantics*
   exactly**, but reads its navigation choice via `read_line()`, not
   `read_key()`, since it also has to accept free-text multi-character
   commands (`/download <filename>`, `/upload`) in the same prompt.
4. **A real correctness issue pagination would otherwise have silently
   introduced, found and fixed proactively rather than shipped as a
   regression:** once browsing is paginated, `/download <filename>`
   could no longer match against the *entire* area's listing, only the
   current page in memory. Fixed with a new `get_file_by_name(db, area,
   filename)` doing its own direct, indexed lookup against the whole
   area regardless of what's currently paged — pagination bounds what's
   fetched for *browsing*, not what can be *referenced by name*.
   Preserves the old scan's oldest-match tie-breaking behavior.

## Sign-off notes, round 32 (chat interaction commands and identity presentation — discussion only, not implemented)

Prompted by reviewing the Phase 1 local-chat implementation and identifying the interaction features needed to make chat feel complete before Phase 2 moderation work begins.

1. **Local `/msg` adopted for Phase 2 as an online-only, node-wide real-time private-message mechanism.** The recipient must currently be online; delivery goes to every active session belonging to that canonical account. Private messages are rendered distinctly from channel traffic and are not written into channel scrollback.

2. **Live `/msg` remains separate from asynchronous Link messages.** NetBBS must never silently turn a failed or offline `/msg` into a Phase 3 store-and-forward Link message. The two features have different delivery guarantees and user expectations, so fallback between them would be misleading. Phase 5 extends live `/msg` across NetBBS Link only for currently online recipients.

3. **Per-user chat timestamps confirmed for Phase 2.** Timestamp display is a persistent user preference, defaulting to off, available through `/timestamps on`, `/timestamps off`, `/timestamps toggle`, and the preferences interface. It applies consistently to live messages, replayed scrollback, join/leave events, `/me` actions, and private messages. Formatting uses the existing per-user/node display-timezone and display-format system rather than creating chat-specific formatting rules.

4. **`/me` adopted as a typed action event.** Actions are stored and transported as a distinct event type rather than encoded as specially formatted ordinary text. Local actions are retained in bounded channel scrollback and follow the user's timestamp preference. Phase 5 later carries the same typed event across NetBBS Link.

5. **Local `/away` state adopted for Phase 2.** `/away [message]` sets an optional node-wide away status for the authenticated account; `/away` without an argument clears it. The state is shared across all active sessions and clears only when the account's final session disconnects. It is visible through local presence views and private-message feedback, but is not written into channel scrollback or broadcast as a channel event by default.

6. **Sending a message does not automatically clear away state.** Users may intentionally remain marked away while briefly responding. When an away user sends chat traffic, NetBBS should remind them that their away status is still active rather than changing it silently.

7. **Transparent persistent chat aliases adopted through `/nick`.** An alias is presentation metadata, not identity. Every chat rendering must keep the canonical authenticated username plainly visible, using a form such as `<DeepParse|thiesi>`. Moderation, permissions, blocking, reputation, auditing, and addressing always operate on canonical identity.

8. **Aliases are non-unique but may not exactly match another account's canonical username.** This preserves freedom of presentation without allowing an alias to impersonate an authenticated local identity. Alias input must be length-limited, sanitized, and safe for terminal rendering.

9. **Commands address canonical identities.** `/msg` and later Link-wide addressing primarily use canonical usernames or full Link addresses. An alias may be accepted only when it resolves uniquely, and the resulting canonical identity must be shown before or during delivery so ambiguity cannot remain hidden.

10. **Nickname changes are typed chat events.** Alias changes are retained in local channel scrollback so subsequent messages remain understandable. Phase 5 extends them across the Link while retaining canonical user and node identity, including the node fingerprint where needed.

## Sign-off notes, round 33 (chat commands, presence and invitations — discussion only, not implemented)

Prompted by a second post-Phase-1 chat-design round covering IRC-style command affordances, channel discovery, private-conversation mode, and invite-only channels.

1. **`/private`, not `/query`, is the primary sustained private-conversation command.** `/msg` remains the one-off direct-message command. `/private <user>` enters a temporary conversation mode layered on top of `/msg`, ordinary input is sent privately to that recipient, and `/close` returns to normal channel input. `/query` remains available only as an IRC-compatibility alias. No additional delivery or storage mechanism is introduced.

2. **No user-facing `/notice` command will be implemented.** Human private communication is fully covered by `/msg` and `/private`, while server-generated and service-generated notifications use typed `system` events. Reproducing IRC's notice distinction would duplicate behavior without adding a meaningful NetBBS capability.

3. **One active channel per session is confirmed for Phase 2.** `/join <channel>` switches the session from its current channel to the requested visible and authorized channel. `/leave` exits the active channel and returns to channel selection or the main menu. Simultaneous multi-channel membership is deferred until Phase 5, when background delivery, unread state, active-channel selection, and richer Link-wide presence already justify the additional machinery.

4. **Local `/who`, `/whois`, `/names`, `/list`, and `/topic` are confirmed for Phase 2.** `/whois` reuses the user-directory and vCard system and must honor profile privacy and hidden-channel visibility. `/names` provides a compact roster for a channel, while `/who` provides a more detailed presence view. `/list` exposes only channels visible to the requesting user.

5. **Topic viewing and topic modification are distinct operations.** Any user allowed to see a channel may view its topic. Changing a local topic requires the existing `edit` permission and is recorded in moderation or metadata history with setter identity and timestamp. Changing a Linked-channel topic becomes a signed authorized metadata event in Phase 6.

6. **Slash-command tab completion is confirmed for Phase 2.** It belongs in the shared character-input layer so Telnet, SSH, and web sessions behave consistently. Completion is case-insensitive, context-aware, and permission-aware; commands unavailable to the current user are not suggested.

7. **Username completion is also Phase 2 scope, with visibility-aware candidate sources.** `/msg` and `/private` complete visible online users; `/whois` completes visible directory entries; `/invite` completes users eligible to be invited. Completion targets canonical usernames rather than aliases. Link-wide identity completion waits until Phase 5 provides suitable remote presence and directory information.

8. **Invite-only channels are adopted with a new `manage_members` permission.** Membership administration is an authorization operation, not a metadata edit, so overloading `edit` was rejected. `manage_members` covers sending and revoking invitations, reviewing membership, granting or removing persistent access, and configuring whether ordinary members may invite others.

9. **Channel visibility and join policy are independent controls.** A channel may be listed or hidden, and independently open to all eligible users or restricted to explicit members. A hidden but otherwise open channel is permitted, but is treated as obscurity rather than meaningful access control.

10. **Invitation-plus-acceptance is confirmed for both local and Linked channels.** Sending an invitation creates a pending invitation only; it does not create membership. The invitee must explicitly accept before access is granted. Invitations may expire, while accepted membership persists until revoked unless a separate expiry policy is deliberately configured.

11. **The default invitation policy is moderators and SysOp only.** A channel may explicitly allow ordinary members to invite others, but this is opt-in rather than inherited automatically.

12. **Linked-channel membership governance belongs in Phase 6.** Invitations, acceptances, membership grants, removals, and revocations become signed events verified against the inviter's or moderator's authority. This extends the existing signed-governance model rather than creating a parallel trust mechanism.

13. **Access restriction is not end-to-end confidentiality.** Invite-only Linked channels may restrict authorized membership, but participating node operators can still observe or retain relayed content. True confidential group chat would require a separate design covering group-key distribution, key rotation, history access, membership changes, compromised members, and multi-session key handling. It is deliberately not implied by the word “private.”

## Sign-off notes, round 34 (moderator/permission grant model — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Track 1 of the foundation-first Phase 2 sequencing plan — the §13
moderator/permission model everything else in Phase 2 depends on.

1. **Bitmask (`IntFlag`) representation, not a junction table.** A
   single integer `permissions` column per grant row, chosen because it
   matches §13's own phrasing directly — "settable individually or
   combined" — and avoids a join table for what's a small, closed set
   per object type.
2. **Two permission enums, not one shared set.** `BoardPermission`
   (`READ/WRITE/EDIT/DELETE/APPROVE`) is shared by boards and file
   areas; `ChannelPermission` (`EDIT/MODERATE/MANAGE_MEMBERS`) is
   deliberately smaller and different — chat access itself has no
   read/write split, and `MODERATE` bundles kick/mute/ban into one bit
   rather than three separately-grantable ones, since §13 describes
   them as one bundled capability.
3. **Local-blanket grants (`object_id IS NULL`) are all-or-nothing —
   no partial-exception carve-outs.** Nothing in §13 asks for this, and
   it would turn every permission check into a compound lookup. Cheap
   to add later if a real case surfaces.
4. **Link-blanket ("global") is deliberately not modeled yet.** Only
   per-object and local-blanket grants exist in the schema today — the
   third tier is unreachable until Phase 6's Link-wide moderation
   exists; its shape is left to be decided then.
5. **A generic `moderation_log` audit table was built now**, ahead of
   most of its consumers — designed to also carry mute/ban/kick and
   moderated-board approval once those later tracks land, rather than
   have each track invent its own logging. Free-text `action`/`detail`
   columns rather than a `CHECK`-constrained action allowlist, so later
   action types don't require editing an already-shipped migration.
6. **Grants are additive, revokes are subtractive, both by bit.**
   Chosen so callers never need to fetch-then-recompute-then-write the
   existing mask themselves.
7. **New module homes: `netbbs.moderation.roles` and `netbbs.moderation.
   log`, not `netbbs.permissions`.** `netbbs.permissions.levels` is
   left untouched — still pure `user_level` gating; the richer model is
   a different, richer permission model layering on top, not a
   replacement.
8. **Deliberately not wired into any existing call site this round.**
   `boards/posts.py`, `files/entries.py`, and chat's `ChatHub`/
   `chat_flow.py` still only call `require_level`/`meets_level` — this
   round ships the grant model as standalone plumbing with no consumer,
   the same precedent `netbbs.permissions.levels` itself set.

## Sign-off notes, round 35 (moderated-board approval + post maintenance/expiry — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Track 2 of the foundation-first Phase 2 sequencing plan, built on
round 34's grants. **Scope: posts + boards only** — files/file areas
deliberately deferred to round 36 rather than doubling this round's
diff.

1. **Expiry applies only to `'approved'` content.** A `'pending'`
   post's age doesn't count toward expiry — a stale unreviewed post is
   a moderation-queue hygiene problem, not an expiry one.
2. **`'expired'` content stays individually reachable** — as a
   reply-parent, and (once files land) via `/download <filename>` —
   even though it's delisted from normal paginated browsing. Only
   `'pending'` is a genuine access gate; the grace period is real
   insurance against an overly aggressive age setting, not a second
   soft-block.
3. **The moderation queue is a separate, simple function
   (`list_pending_posts`)**, not a mode bolted onto the existing
   cursor-paginated `list_posts_page` — keeps round 30's pagination
   logic untouched.
4. **No separate `'rejected'` status.** `delete_post` (requires
   `BoardPermission.DELETE`) is also how a pending post gets rejected —
   avoids inventing a state nothing asked for, logging `action="reject"`
   vs. `"delete"` depending on the post's status at the moment of
   deletion.
5. **Expiry mechanism: lazy sweep-on-access, not a background job.**
   Runs at the top of `list_posts_page` (the natural "someone is
   looking at this board" trigger) — there is no scheduled/
   background-task mechanism anywhere in this codebase. Not logged to
   `moderation_log`, which is for explicit human moderation decisions,
   not mechanical time-based housekeeping.
6. **Grace period is a single node-wide default**, not a per-board
   column — nothing asks for per-board control over it, unlike max post
   age, which genuinely is per-board.
7. **Post-level `pinned` and `exempt_from_expiry` are independent
   flags**, both gated by the existing `BoardPermission.EDIT` bit
   (matching the already-settled §13 pin/exempt-under-`edit` note).
   `Post.pinned` is a distinct concept from `Board.pinned`.
8. **Pinned posts do not reorder `list_posts_page`'s feed — caught
   during implementation, not fully resolved in the original plan.**
   Sorting pinned posts first would have broken keyset pagination's
   stability guarantee. Resolved with a separate, small, non-paginated
   `list_pinned_posts` function instead.

## Sign-off notes, round 36 (moderated-area approval + file maintenance/expiry — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

The files/file-areas mirror of round 35, deferred there explicitly —
structurally identical (same status/pinned/exempt columns, same
lazy sweep-on-access mechanism, same function set, gated by the same
`BoardPermission` bits shared between boards and file areas).

1. **`get_file_by_name` needed its own pending-visibility check, which
   `get_post` doesn't.** Posts have exactly one unbounded lookup path
   (`get_post`, by content-addressed ID, effectively unreachable in
   practice); files have a *second* unbounded path, `get_file_by_name`
   (round 31), a real practical route to a pending file. Gained an
   optional `requesting_user` parameter: a `'pending'` match is only
   returned to its own uploader or an `APPROVE`-holder; an `'expired'`
   match is always returned, consistent with round 35's "expired is
   delisted, not access-blocked."
2. **`delete_file` only removes the database row, not the underlying
   bytes in `netbbs.files.storage`.** Storage-level garbage collection
   of orphaned content-addressed blobs is out of scope for this round —
   a real gap this mirror pass introduces that round 35 has no
   equivalent of.

## Sign-off notes, round 37 (chat mute/ban/kick — implemented)

*(Condensed — full round including a schema-constraint bug writeup and testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Track 3 of the foundation-first Phase 2 sequencing plan, built on
round 34's `ChannelPermission.MODERATE` grant bit.

1. **The real architectural question this round had to answer:** there
   was no mechanism by which one live session could force another to
   disconnect. Mute doesn't need this (enforced lazily at the muted
   user's own next send attempt); kick and ban do.
2. **`ChatHub` gained exactly two primitives**, staying inside its
   existing opaque `participant_id` string abstraction rather than
   teaching it `chat_flow.py`'s own convention: `participant_ids(...)`
   (a snapshot) and `async send_to(...)` (deliver to one specific
   queue).
3. **A small `_KickNotice` object, not a string, forces the exit.**
   `receive_loop` checks for it and **returns** instead of looping,
   picked up by the existing `FIRST_COMPLETED` wait the same way
   `/quit` finishing `send_loop` already is. A kicked/banned user's own
   disconnect is thus indistinguishable from an ordinary leave to
   everyone else, by design — the moderation action itself gets its own
   separate transparency notice.
4. **One unified `channel_restrictions` table for mute and ban**,
   discriminated by `kind`, not two tables — structurally identical.
   `UNIQUE(channel_id, user_id, kind)` makes re-muting/re-banning an
   upsert rather than accumulating rows.
5. **No cleanup sweep for expired mute/ban rows** — unlike round
   35/36's board/file expiry, a stale expired restriction causes no
   problem sitting there; checked lazily at query time.
6. **`parse_duration` matches §13 exactly**: no argument = indefinite;
   bare number = minutes; `s/m/h/d/w/y` suffix = that unit.
7. **Command shape: `/mute <user> [duration] [reason...]`** — the first
   token after the username is tried as a duration; if it fails, the
   entire remainder is the reason and duration defaults to indefinite.
   Explicitly flagged as reconsiderable rather than fully settled.
8. **`kick_user` (in `netbbs.chat.moderation`) persists no state at
   all** — only the permission check and audit trail. Actually removing
   a live session is `chat_flow.py`'s job; `netbbs.chat.moderation`
   knows nothing about `ChatHub` or live sessions by design, matching
   how `netbbs.boards.posts` doesn't know about `net.chat_flow`.
9. **New module `chat/moderation.py`, not `netbbs.moderation`** — same
   precedent as `approve_post`/`delete_post` living in `boards/posts.py`:
   feature-specific moderation actions live with the feature.

## Sign-off notes, round 38 (user directory & vCard/finger — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Track 4 of the foundation-first Phase 2 sequencing plan.

1. **This is also the first per-user (not node-wide) settings storage
   this codebase would have.** Built as a generic, reusable mechanism
   now (`user_preferences(user_id, key, value)`, mirroring
   `netbbs.config` exactly) rather than a narrow vCard-only table,
   specifically so a later per-user chat timestamp preference doesn't
   need to invent its own storage.
2. **vCard fields: bio only**, matching what §13 concretely names — not
   inventing fields it doesn't ask for. Cheap to extend later: each
   additional field is just another preference key, no schema change.
3. **Bio visibility defaults to hidden**, not shown, until the owner
   explicitly opts in — matches this project's consistent
   privacy-safe-by-default posture elsewhere.
4. **New `netbbs.directory` module**, not nested in `auth` or
   `moderation` — a distinct concern from both. `get_vcard` always
   shows the bio to its own owner regardless of visibility.
5. **`auth.users.list_users`** is deliberately not paginated, unlike
   `list_posts_page`/`list_files_page` — total registered users is
   naturally bounded at this project's declared scale (§14).
6. **Full vertical slice**: main menu gained `[D]irectory` and
   `[P]rofile`; `net.chat_flow` gained a `/finger <user>` command —
   satisfies §13's "accessible from... chat" explicitly.
7. **Confirmed directly: there is no multi-line text-input mechanism
   anywhere in this codebase** — even a board post's `body` is
   collected via one single `read_line()` call today. Bio entry
   therefore loops over `read_line` up to 6 times, a blank line ending
   it early.

## Sign-off notes, round 39 (chat command dispatch infrastructure — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Track 5a of the Phase 2 sequencing plan — the dispatch mechanism the
rest of Track 5 needs first.

1. **A small `ChatCommandContext` dataclass** replaces what used to be
   a different ad hoc positional-argument list per handler.
2. **Handler contract: `async def handler(ctx, args) -> bool | None`.**
   A truthy return means "exit the chat loop after this command."
   Chosen over a control-flow exception for quitting: an explicit
   return value reads more plainly here than exception-driven flow
   would for what is, after all, the ordinary way to leave.
3. **Explicit dict registry (`_COMMANDS`), not a decorator-registration
   pattern** — matches this codebase's existing preference for
   explicit, greppable structures over registration magic.
4. **Any line starting with `/` is now always treated as a command
   attempt** — looked up in `_COMMANDS`, "Unknown command" shown if not
   found, nothing broadcast. Previously an unrecognized `/x` line fell
   through to being sent as ordinary chat text — standard behavior for
   slash-command chat systems generally.
5. **The existing mute check deliberately stays exactly where it
   was** — gating only the plain-message fallthrough after dispatch,
   not `_dispatch_command` itself, so a muted moderator can still
   unmute themselves.
6. **`/quit`/`/leave` keep their exact current meaning this round**
   (both fully exit the chat loop) — deliberately not redesigning
   `/leave` yet (that's a later track's scope), flagged explicitly so
   it doesn't get silently redecided piecemeal.

## Sign-off notes, round 40 (/me action events — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

The first slice of Track 5b (presence/identity). `/nick` and `/away`
were deliberately not attempted in this same round — both cross into
larger, not-yet-built machinery (rendering-wide changes; a new
session-lifecycle tracking mechanism), genuinely larger design surfaces
than `/me`, which is small and mechanical.

1. **New `channel_messages.kind` value: `'action'`.** Deliberately
   widened only for what this round needs, not speculatively for
   `/nick`'s not-yet-designed event kind too — consistent with this
   project's "don't build for hypothetical future requirements" stance.
2. **`/me <action>` renders identically for the actor and everyone
   else** (`* alice waves`) — unlike an ordinary chat message, there's
   no "my own words" distinction worth making for a shared
   narrative-style action.

## Sign-off notes, round 41 (/nick transparent display aliases — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

The second slice of Track 5b, per design doc round 32, points 7-10.

1. **New module `netbbs.chat.nick`**, not folded into
   `netbbs.directory` — round 32 discusses `/nick` as chat
   presentation, a different concern from the user-directory/vCard
   feature even though both sit on the same generic
   `netbbs.user_preferences` store underneath.
2. **Validation**: a 32-character length cap, and a case-insensitive
   check against every other account's canonical username (round 32:
   "may not exactly match another account's canonical username" —
   checked case-insensitively for actual anti-impersonation effect).
   Setting your own username as your own nick is explicitly allowed.
3. **`/nick off` clears the alias; `/nick` alone shows the current
   one.** Chosen over accepting a bare blank line as "clear" — an
   explicit reserved word avoids ambiguity.
4. **Nickname changes are their own scrollback event kind (`'nick'`)**,
   per round 32 point 10.
5. **Live rendering shows the *current* alias, computed fresh at each
   point of use — not cached from channel-join time** — so a nick
   change is reflected immediately in the very next message. Scrollback
   replay also shows the current alias, not whatever was set at the
   original moment.
6. **Moderation notices and command targeting deliberately stay
   canonical-only, unchanged** — matches round 32 point 7 explicitly
   ("moderation, permissions, blocking, reputation, auditing, and
   addressing always operate on canonical identity").
7. **Resolving a *nick* to a canonical identity for command targeting
   is explicitly out of scope for this round** — that's addressing/
   completion scope for a later track.

## Sign-off notes, round 42 (/away node-wide presence — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

The third and final slice of Track 5b, per design doc round 32,
points 5-6.

1. **New `PresenceRegistry` class, separate from `ChatHub`** —
   `ChatHub` tracks per-*channel* participants; this tracks
   per-*account* state that has nothing to do with which channel, or
   whether the account is in a channel at all. One instance per node.
2. **The real plumbing question this round had to answer**: round
   32's "clears only when the account's final session disconnects"
   means "session" is a *login connection*, not a chat-channel visit.
   Solved by threading `PresenceRegistry` through `handle_session` (the
   actual per-connection entry point), with `leave()` in a `finally` so
   an exception can't leak an "online forever" session count.
3. **`/away [message]` sets; `/away` alone clears** — matching §13's
   literal wording exactly, not a toggle.
4. **Not written to scrollback or broadcast** — round 32 explicitly
   scopes away-status visibility to "local presence views and
   private-message feedback," neither of which exists yet at this
   point.
5. **Sending a message while away reminds, doesn't clear** (round 32,
   point 6).
6. **Per-user chat timestamp preference (round 32, point 3) — found to
   need no new work this round.** `format_for_display` already had
   `override_format`/`override_timezone` parameters reserved for
   exactly this; wiring a real per-user value through was left as a
   small follow-up (delivered in round 62).

## Sign-off notes, round 43 (discovery commands — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Track 5c: `/who`, `/names`, `/list`, `/whois`, per design doc rounds
32/33. No new storage needed — purely new views over existing state.

1. **`/names`/`/who` both operate on the current channel's roster**,
   via a helper parsing `ChatHub`'s opaque participant-id strings back
   into deduplicated canonical usernames. `/names` is one
   comma-separated line (alias-aware); `/who` is one line per person
   with an away indicator — matching round 32/33's "compact roster" vs.
   "more detailed presence view" framing exactly.
2. **`/list` is a flat, sorted text dump** (pinned-first-then-
   alphabetical), not the interactive category-nested `pick_item`
   picker — a quick reference from inside chat, not a second navigation
   UI competing with the main menu.
3. **`/whois` reuses `get_vcard` (Track 4) via a helper shared with
   `/finger`** — works for offline/never-online accounts too, since
   it's a directory lookup, not an online-only one.
4. **A new helper answers "which channels is X currently in"** by
   iterating every channel the *requesting* user can see and checking
   each roster — this *is* round 32/33's "must respect... hidden-
   channel visibility" requirement for `/whois`, applied consistently
   now even though no channel is actually hidden yet, so nothing needs
   revisiting once hidden channels land.

## Sign-off notes, round 44 (menu/navigation UX consistency — implemented)

*(Condensed — full round including flaky-test and other bug-fix detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Prompted by Thiesi testing the current build and raising several real
UX inconsistencies.

1. **"Boards" → "Message Boards" everywhere user-facing**, matching
   "File Areas"' full-name convention. The main menu's `[B]oards`
   hotkey itself is unchanged — rendered as `Message [B]oards` rather
   than switching the highlighted letter, to avoid quietly changing a
   keybinding nobody asked to change as a side effect of a label
   rename.
2. **`[Q]uit` → `[B]ack`, scoped to `netbbs.net.picker.pick_item` only —
   both the label and the actual keystroke, confirmed with Thiesi after
   inventorying every place "quit"/"back"/"Enter" appears in the UI.**
   Chat's typed `/quit` command and the main menu's `[L]ogoff` both
   genuinely end something and were confirmed to stay as-is.
3. **No redraw and no error message on an invalid single-keystroke menu
   choice — a silent bell (`\a`) instead.** A holdover from the
   pre-round-15 line-mode menu, no longer meaningful once dispatch is
   immediate: reprinting an entire menu/page just because one stray key
   didn't match anything added nothing. A sub-prompt a user
   deliberately typed into (`pick_item`'s `search`/`goto`) still gets
   its own specific text response on failure — a direct answer to a
   specific question, unlike a stray top-level keystroke. (This
   principle was later tightened further in round 52.)

## Sign-off notes, round 45 (Phase 2 Track 5d: channel switching — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Implements the plan agreed with Thiesi for Track 5d (`/join`, `/leave`
redefined, `/topic`).

1. **`CommandHandler`'s return type widened from a bare `bool` to a
   small tagged union, `ChatAction`** (`_Quit | _ToPicker | _SwitchTo`)
   — a direct, mechanical extension of round 39's own "explicit return
   contract, not exceptions" choice.
2. **`_chat_loop` itself needed no internal concept of "switching
   channels"** — `browse_channels` became a small outer loop reacting
   to whatever `ChatAction` is returned: `_SwitchTo(channel)` loops
   straight back into `_chat_loop` with the new channel, naturally
   re-running the existing leave-then-join sequence with no
   special-casing needed.
3. **`/leave` stops aliasing `/quit`'s handler and gets its own**,
   returning to the channel picker — a deliberate divergence from
   round 39's "both map to the same handler," flagged there as a
   placeholder specifically pending this track.
4. **`/join <channel>`'s handler resolves and validates, but never
   touches `hub`/the database itself** — all the actual joining happens
   for free via `_chat_loop`'s existing entry sequence once
   `browse_channels`' loop calls it again with the new channel.
5. **`/topic`: new nullable `channels.topic` column**, distinct from
   the existing `description` (a creation-time listing blurb, never
   moderator-edited). Gated by `ChannelPermission.EDIT` — already
   reserved for exactly this since round 34. Deliberately **not**
   persisted into scrollback, unlike `/nick`'s explicit scrollback
   requirement — round 33 point 5 only asks for moderation-log history.

## Sign-off notes, round 46 (Phase 2 Track 5e: private messaging — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Implements the plan agreed with Thiesi for Track 5e (`/msg`,
`/private`/`/query`, `/close`).

1. **Delivery: mailbox + next-prompt, confirmed with Thiesi over full
   session-wide live interrupt delivery.** Round 32 requires `/msg` to
   reach "every active session belonging to that canonical account,"
   but only a session actually inside `_chat_loop` has any live
   receive mechanism today — true interrupt delivery to *any* screen
   would mean threading a persistent receive-task through the whole
   session, a much bigger change than this track's scope. Resolved as
   two paths: a recipient with a live participant right now gets pushed
   instantly via `ChatHub`; otherwise the message queues in a new
   `netbbs.chat.mailbox.MessageMailbox` and is shown at the recipient's
   next natural prompt.
2. **Exactly one flush point needed: the top of `_main_menu`'s loop** —
   every screen already passes through there before its next redraw.
3. **`/msg`/`/private` both check `presence.is_online(...)` at send
   time and refuse outright if not online** (round 32 point 1) — no
   queuing for a genuinely offline user, only for the
   online-but-not-reachable-right-now gap the mailbox exists for. Never
   written to scrollback or the moderation log.
4. **`/private <user>` layers on `/msg` via new `ChatAction` variants**
   consumed entirely within `send_loop`, never propagating further —
   other slash-commands still dispatch normally while in private mode,
   matching round 39's existing "leading `/` is always a command
   attempt" rule.
5. **`/query` is registered as a plain alias for `_handle_private`**
   (round 33 point 1: "accepted only as an IRC-compatibility alias") —
   later removed entirely (round 54).
6. **A real edge case identified and handled, not left implicit:** if
   the private-conversation target goes offline *during* an active
   conversation, the next line sent re-checks presence, clears the
   target, and tells the user — rather than silently queuing into a
   mailbox for someone no longer reachable at all.

## Sign-off notes, round 47 (Phase 2 Track 5f: command history & cursor-addressable line editing — implemented)

*(Condensed — full round including bug-fix and testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Implements the track Thiesi added mid-session after finding retyping
long chat commands genuinely painful. Confirmed scope up front: history
*and* full cursor editing, not history alone. Explicitly **not** the
deferred "TUI half" (round 13/26's screen-buffer-diffing scope) — a
single, non-wrapping input line is a bounded reprint-and-reposition
problem, closer to a shell's readline than to a screen editor.

1. **Two new transport-agnostic primitives added to
   `netbbs.net.char_input`**, reused directly by both the byte-oriented
   Telnet/SSH path and the already-decoded-character Web path:
   `move_cursor` and `redraw_tail` — one redraw operation covers every
   edit shape (mid-line insert, backspace, forward-delete, full-line
   history recall).
2. **Escape-sequence handling changes from discard-only to
   parse-and-act, for a specific, still-bounded set of sequences**
   (arrows, Home/End, Delete, Insert). Anything not recognized is still
   discarded as a complete unit exactly as before — existing
   bounded-length/time safety properties unchanged.
3. **The line buffer became cursor-aware**, replacing the old
   append/pop-at-the-end-only model.
4. **`InputHistory`**: bounded to a fixed `max_entries` (default 50 —
   the same bounded-not-unbounded posture as chat scrollback's cap and
   the picker's page cap), in-memory only, no persistence, same
   ephemeral posture as chat itself.
5. **Owned per connected session, not per node and not per channel** —
   recall works across a `/join` channel switch within one connection,
   but each new connection starts with empty history.
6. **Masked reads (`echo=False`, i.e. password entry) deliberately
   keep the old simple append/pop behavior** — no cursor movement, no
   history recall, nothing that would echo password characters back
   via redraw.
7. **`netbbs.net.web.WebSession` gets a full parallel implementation**,
   not a shared one — consistent with round 25's already-accepted
   deliberate non-sharing between the byte-oriented and
   character-oriented transports, though the three pure,
   no-bytes-dependency primitives are reused directly.

## Sign-off notes, round 49 (Phase 2 Track 5g: slash-command + username tab completion — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Implements Track 5g — Tab completion for chat's slash-commands and
username arguments, plus wiring the same mechanism into the picker's
`"Search: "` prompt.

1. **`apply_tab_completion` added to `netbbs.net.char_input`**, reused
   directly by `WebSession` rather than re-deriving the same redraw
   arithmetic a second time. Deliberately generic: the function has no
   idea what a "command" or "username" is, only the generic notion of
   "word." Zero candidates does nothing; one candidate replaces the
   word plus a trailing space; multiple candidates extend to their
   longest shared prefix (bash-style) and list every candidate below.
2. **Deliberate simplification: no caller-side prompt label is
   redrawn alongside a multi-candidate list** — `read_line` has no idea
   a prompt string like `"Choice: "` even exists.
3. **The BBS-specific completer lives in `netbbs.net.chat_flow`, built
   fresh on every `send_loop` iteration** — always reflects the actor's
   *current* moderator status rather than a snapshot taken once at
   channel entry. Command-name completion is filtered through a
   separate `_COMMAND_VISIBILITY` predicate dict — **this is purely a
   suggestion filter, not an authorization check** — the handlers
   themselves remain the sole source of truth for what's actually
   allowed to run.
4. **Picker addendum, folded in as agreed:** `pick_item`'s `"Search: "`
   prompt gets a completer built fresh each time from the *current*
   filtered set, not the caller's full original list. **One real scope
   boundary found and deliberately handled:** the generic
   word-boundary logic only ever replaces the *last* whitespace-
   delimited word, which could corrupt a multi-word picker candidate
   name — sidestepped by returning no candidates at all once the query
   already contains a space, rather than redefining the picker's own
   search matching to prefix-only (a separate, larger,
   round-16-reversing question, explicitly out of scope here).

## Sign-off notes, round 50 (Phase 2 Track 5h: invite-only/hidden channels, manage_members — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Implements Track 5h (design doc §8/round 33 points 8/9/11) — the last
of Tracks 5d-5h.

1. **Schema: two independent axes plus an opt-in, all defaulting off.**
   `channels.hidden`/`channels.members_only`/`channels.allow_member_
   invites` — an existing channel's behavior is unchanged unless a
   moderator explicitly opts in. New `channel_members`/
   `channel_invitations` tables, deliberately their own tables, not
   folded into `moderator_grants` — membership is access/visibility
   eligibility, not a permission bit.
2. **New `netbbs.chat.membership` module**, distinct from `netbbs.chat.
   channels` (CRUD/topic) and `netbbs.chat.moderation` (mute/ban/kick)
   for the same reason those two are already separate.
3. **No `/accept` command.** Accepting an invitation is just
   successfully `/join`-ing the channel — reuses Track 5d's existing
   "look up, check authorization, switch" flow instead of inventing
   parallel command surface.
4. **Command surface, all gated by `MANAGE_MEMBERS` except `/invite`
   (its own OR-condition predicate) and `/members` (ungated — reviewing
   your own channel's roster is different from administering it).**
5. **Visibility consolidated into one shared helper**, replacing three
   separately-duplicated `meets_level`-only comprehensions. A `hidden`
   channel is excluded from listings unless the user is already a
   member, holds a pending invitation, or holds *any* moderator grant
   on it. "Hidden + open is obscurity, not access control" (round 33
   point 9): a `members_only`-but-not-`hidden` channel still appears in
   every listing.

## Sign-off notes, round 51 (deliberate node shutdown: SIGTERM=graceful, SIGINT=immediate — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Prompted by Thiesi testing real shutdown behavior directly: sending the
node process a signal while users were connected acknowledged the
request but didn't visibly finish until the last connection closed on
its own, with no warning ever shown to anyone still connected.

1. **Root cause identified by direct code reading, not assumed.** The
   thing actually reaching in-progress connections was `asyncio.run
   (main())`'s own *implicit* end-of-process cleanup — opaque,
   undocumented, and not something to keep depending on. Replaced
   outright with a deliberate, awaited sequence.
2. **Two new small node-wide pieces:** `netbbs.net.session_registry.
   ActiveSessionRegistry` (structurally identical to `PresenceRegistry`)
   adds `broadcast_to_all`/`disconnect_all`; `netbbs.net.maintenance.
   MaintenanceMode` is a single plain flag, checked at the very top of
   `handle_session` before throttling even runs.
3. **Registration covers a connection from the moment it arrives, not
   just once authenticated** — deliberately different scope from
   `presence`, which only ever knows about accounts.
4. **New `ShutdownConfig`/`[shutdown]`**: one field,
   `graceful_delay_seconds` (default 60 — Thiesi's own "a minute or 90
   seconds"). No configurable message text — scope stayed to the one
   thing actually asked for.
5. **SIGTERM = graceful, SIGINT = immediate — the conventional mapping,
   confirmed with Thiesi after an initial proposal had it backwards.**
   The graceful path activates maintenance mode, broadcasts a warning,
   then sleeps the configured delay before disconnecting everyone;
   `shutdown_event` is set last, so the final cleanup only runs once
   every session is confirmed gone.
6. **Known, accepted limitation, not solved here:** the broadcast text
   can interleave oddly with whatever a user is mid-typing — the same
   already-documented limitation ordinary chat messages have always
   had.

## Sign-off notes, round 52 (invalid-keystroke: bell only, genuinely nothing else — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Revises round 48. Thiesi tested that round's fix directly and judged
the reprinted `Choice: ` prompt after the bell to add no value — the
prompt was already on screen. Settles round 44's ambiguity the other
way: **nothing** beyond the bell, for a genuinely invalid/inapplicable
action.

1. **Structural blocker identified and fixed at its root, not patched
   per-symptom:** all four affected loops printed `Choice: `
   unconditionally at the *top* of their loop, every iteration — the
   actual reason "no reprint on invalid input" was structurally
   impossible before this round. Fixed by moving the prompt print out
   of the loop entirely, folded into whatever function already redraws
   real content.
2. **The two-bucket distinction from round 48 survives, clarified, not
   abandoned:** a *specific answer to a specific question* (the
   picker's "No matches."/"Not a number."/"Out of range." sub-prompt
   responses) still gets its own text and a freshly printed prompt
   afterward. A bare invalid/inapplicable keystroke gets neither.

## Sign-off notes, round 53 (`/nick` display: chat-stream marker+color, lists keep both forms — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Reverses round 32 point 7 / round 41's original "every chat rendering
must keep the canonical username plainly visible" mandate, specifically
and only for the live conversational stream — Thiesi tested `nick|
username` shown on every single line of live chat and judged it
cluttered in practice. Explicitly flagged by Thiesi as provisional,
easy to revisit once seen live.

1. **Two rendering forms, deliberately, not one changed in place.**
   `netbbs.chat.nick.display_label` (`nick|username`, unchanged) stays
   the form `/who`/`/whois`/`/names` use — directory-style listings
   still show canonical identity alongside presentation. New
   `chat_stream_label` (nick-only, marked and colored, or plain
   `username` if unset) is used everywhere in the live conversational
   stream instead. Moderation notices already never called either
   function (canonical-only, round 32 point 7) — untouched.
2. **Marker character: `~` (tildes wrapping the nick), ASCII-safe** —
   guaranteed to render identically on a CP437-only classic BBS client,
   and not already used elsewhere in this codebase's chat rendering
   conventions (unlike `*`, already overloaded for `/me` actions).
   `/nick` now rejects the marker character from submitted nick
   content.
3. **New `NICK_COLOR`** in `netbbs.rendering.theme`, following the
   existing `SELF_COLOR`/`ACCENT_COLOR`/`MUTED_COLOR` precedent.
4. **Sanitize-before-color, not after — verified as the only safe
   order.** `chat_stream_label` sanitizes the raw nick/username
   *before* wrapping it in `colored()`, never the reverse — running
   `sanitize_text` on an already-colored string would risk stripping
   this function's own legitimate SGR codes right alongside any
   genuinely hostile content.

## Sign-off notes, round 54 (`/query` removed entirely — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Reverses round 33 point 1's original "`/query` accepted only as an
IRC-compatibility alias" decision. Thiesi confirmed it was a bare alias
with no behavior of its own, then asked for it gone outright — it was
the only command in the entire surface with two names, existing purely
for a compatibility convention nobody had actually asked to keep. A
future shell-alias-style user-defined-command-name feature was
explicitly floated and explicitly deferred as out of scope for now.

1. **Removed from every place it was registered, not just the dispatch
   table** — including Tab completion's online-user command-prefix
   list, a second registration point that would have silently kept
   suggesting a now-dead command if missed.

## Sign-off notes, round 55 (`/help` overhaul + `/?` alias — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Thiesi found the pre-existing `/help` "mostly useless." Asked, when
prompted on whether to centralize command metadata: "Centralize it, and
yes to permission-aware and /help <command>."

1. **`_COMMAND_INFO: dict[str, tuple[str, str]]`** — new single source
   of truth for every command's syntax and one-line description.
   Replaces scattered, independently-worded `"Usage: /command ..."`
   literals with a single helper generated from the same table, so
   usage text and `/help` can no longer drift out of sync with each
   other.
2. **Bare `/help`** lists every command visible to the caller, reusing
   `_COMMAND_VISIBILITY` — the exact same predicate dict Tab completion
   already applies, so the list a user sees matches what `/` + Tab
   would offer them.
3. **`/help <command>`** looks up detail directly, **regardless of the
   caller's own visibility** for that command — consistent with Track
   5g's established framing that `_COMMAND_VISIBILITY` is a suggestion
   filter, not an authorization check.
4. **`/?` alias**: mapped to the *same* handler — deliberately not the
   same shape as round 54's `/query` removal. The distinction: `/query`
   was two *names* for a command that already had one, existing purely
   as an unused compatibility convention; `/?` is a genuinely distinct,
   commonly-expected terse trigger being added on request, for a
   command whose only other name is a full word.

## Sign-off notes, round 56 (SysOp foundation: SYSOP_LEVEL, dual-purpose admin tool, bootstrap — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

First slice of §15's "SysOp admin tools" line — scoped, on Thiesi's
confirmation, to the foundation plus user management only; node
management and board/channel management are explicitly deferred.

1. **`SYSOP_LEVEL = 255`** — a level, not a separate flag/table, so it
   composes with the existing `meets_level`/`require_level` gating
   everywhere else already uses levels. Thiesi's own choice of 255 over
   a lower round number, to make it visually unmistakable as the top of
   the range.
2. **Dual-purpose admin tool — one shared implementation, two entry
   points.** A gated `[A]dmin` option on the main menu inside an
   authenticated BBS session, and a standalone local CLI tool run as
   `python -m netbbs.admin` (mirroring how the node itself runs).
   Both call the exact same `admin_menu(session, db, user)` — no
   command logic is duplicated between them.
3. **`LocalCLISession`**, modeled directly on `TelnetSession`,
   delegates all echo/backspace/UTF-8/history/Tab-completion logic to
   `netbbs.net.char_input` — the one genuinely new, platform-specific
   piece (raw/cbreak terminal mode) is isolated in its own small
   module.
4. **CLI auth: no credential check, ever, when run locally.** Local
   shell/filesystem access to the database file is already the real
   trust boundary — the same reasoning `sudo` relies on. This resolved
   a genuine design gap: a password prompt would permanently lock out a
   pubkey-only SysOp, since there is no local equivalent of SSH's own
   handshake proof of key possession. Instead the CLI only determines
   *which* SysOp to attribute actions to, for the audit log — an
   earlier idea to default to the local OS shell username was
   explicitly rejected, since BBS usernames have no required
   relationship to OS account names.
5. **Bootstrap.** If zero active SysOp accounts exist, the CLI walks
   through the same create-account prompts the admin menu's own create
   screen uses and creates the first `SYSOP_LEVEL` account,
   self-attributing its own audit-log entry (a genuine
   chicken-and-egg resolution).
6. **The network-facing server refuses to start at all with zero
   SysOps.** A node with no SysOp could never be administered once
   running over the network — no one could create a second account,
   disable a rogue one, or recover from any mistake — so this fails
   loudly at startup rather than running in a permanently-stuck state.

## Sign-off notes, round 57 (SysOp user management: create/promote/demote/disable/delete — implemented)

*(Condensed — full round including a real cascade-migration bug writeup and testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Built on round 56's foundation. The five actions confirmed during
discussion: create (SysOp-only for now — public self-registration is a
separate, deferred feature), promote/demote, soft-disable/enable, and
hard delete.

1. **Schema: `users.disabled_at`** — a nullable ISO timestamp, checked
   at all three auth entry points with the same generic failure a
   wrong credential produces, per `AuthError`'s existing
   anti-enumeration docstring.
2. **Schema: real `ON DELETE` behavior for hard delete**, across all
   nine referencing tables in one migration. Two shapes: `posts.
   author_user_id`/`files.uploader_user_id` go `SET NULL` (both tables
   already carry a denormalized author/uploader label so display
   survives account removal); administrative data (moderator grants,
   channel membership/restrictions/invitations, preferences, blocklist)
   goes `CASCADE`; `moderation_log`'s actor/target columns go `SET
   NULL`, since an audit trail should survive the account it names.
3. **`netbbs/auth/users.py`: `UserManagementError`, `count_sysops`,
   `set_user_level`, `set_user_disabled`, `delete_user`.** All three
   mutating functions share one lockout guard: refuses an action that
   would leave the node with zero *active* SysOps, where "active"
   deliberately excludes already-disabled accounts — an interpretation
   call beyond the literal spec, made explicit here rather than left
   implicit. Self-delete/self-disable is allowed, gated only by that
   same guard, confirmed with Thiesi.
4. **`netbbs/identity/keys.py`: `parse_verify_key`.** Accepts either
   this project's own base64 raw-key form or a standard OpenSSH
   public-key line — the two forms a SysOp realistically has on hand
   when creating a pubkey account.
5. **Delete requires re-typing the exact username to confirm** — the
   first destructive-confirmation prompt of its kind in this codebase,
   given the choice to support hard delete alongside soft-disable and
   its irreversibility.

## Sign-off notes, round 59 (node management: [N]ode admin menu — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Second slice of §15's "SysOp admin tools" line: exposing round 51's
shutdown/session-registry machinery as an in-session command.

1. **Standalone-CLI scope, confirmed with Thiesi: in-session only.**
   List-sessions/force-disconnect/trigger-shutdown all need live
   in-memory state that only exists inside the running server process
   — `python -m netbbs.admin` is a separate OS process with no access
   to it. Building real IPC to bridge that was discussed and explicitly
   rejected — not enough use case beyond this one feature to justify a
   remote-control layer for a solo-SysOp target. Also confirmed out of
   scope: no standalone reversible maintenance-mode toggle.
2. **A self-cancellation hazard, designed around up front rather than
   discovered the hard way** — directly informed by round 58: if the
   shutdown-trigger command awaited `disconnect_all()` inline from
   within the very session issuing the command, that session's own
   task would be cancelling itself while being one of the tasks its own
   `gather()` call is waiting on. Resolved architecturally: the
   `[S]hutdown` screen fires the sequence as an independent background
   task, never awaited inline, exactly matching how the existing
   signal-handler path already does it. A parallel, narrower hazard for
   single-target self-disconnect is resolved with a simple UI-level
   guard instead ("use Logoff instead").
3. **`login_flow.py` threading, backward-compatible by construction.**
   Existing required params are unchanged (zero churn for existing test
   call sites); two new *optional* params were added instead. The
   standalone CLI bypasses this whole chain, so `node_controls` simply
   stays `None` there — the signal `admin_menu` needs to hide the
   `[N]ode` option, achieved for free by *which* caller invokes it.

## Sign-off notes, round 60 (board & file-area management: [M]anage boards/areas admin menu — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Third and final planned slice of §15's "SysOp admin tools" line, scoped
to boards and file areas — channels explicitly deferred to round 61.

1. **No schema migration for board/area/category deletion — a design
   correction made mid-implementation, not originally planned.** The
   first attempt followed round 57's user-deletion migration pattern
   directly, but testing found that SQLite's `DROP TABLE` (under
   `foreign_keys = ON`) applies its own SET-NULL/cascade-delete
   fallback to *any* row in another table still referencing it,
   **regardless of that column's actual declared `ON DELETE`
   behavior** — a real, silent side effect discovered because boards/
   areas (unlike round 57's `users`) are simultaneously a child and a
   parent within the same migration. A fully correct fix exists (strip
   every cross-table FK, rebuild everything, re-add FKs in
   topologically-ordered passes) but roughly doubles the migration's
   size; simpler and just as correct: no schema change at all —
   application-level cascade/cleanup with explicit statements in the
   right order.
2. **A genuinely cross-cutting decision, confirmed with Thiesi over the
   narrower alternative**: `has_permission` now short-circuits to
   `True` for any `SYSOP_LEVEL` caller, with zero grant rows required —
   input validation still runs first regardless of caller identity.
   This is not scoped to board/area moderation specifically: it applies
   retroactively to *every* existing consumer of `has_permission`
   (chat's mute/ban/kick, membership-admin gating, Tab-completion's
   visibility). Deliberately *not* extended to functions that answer
   "what grants actually exist" for admin displays, which must stay
   literal.
3. **New library functions**: `update_board`/`update_file_area`
   (full-state replacement, not partial/PATCH) and `delete_board`/
   `delete_file_area` (explicit application-level cascade per point 1).
   `create_board`/`create_file_area` gained audit-log entries now that
   they're finally reachable through a real command with a known actor.
4. **Moderator grant/revoke via named presets** ("Full moderator" =
   EDIT+DELETE+APPROVE, or "Approver only") rather than raw per-flag
   toggles. Revoke removes a target's *entire* existing grant on the
   chosen scope in one action, not a partial per-flag revoke, matching
   the enable/disable screen's existing "one clean toggle" precedent.

## Sign-off notes, round 61 (channel management: [H]annels admin menu — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Closes out the "SysOp admin tools" line from §15 — the piece round 60
explicitly deferred.

1. **Channels needed a narrower slice of round 60's work than boards/
   areas did, for two structural reasons:** channels have no
   moderated-content/approval workflow (chat messages aren't even
   persisted beyond bounded scrollback, so there's no `posts`/`files`
   "pending" queue equivalent to build a UI for); and membership admin
   was already fully exposed via existing `/invite`/`/kick`/`/mute`/
   `/ban`/`/members` commands, gated by the same `ChannelPermission`
   grants this round's admin UI now also assigns.
2. **`update_channel`/`delete_channel`** mirror round 60's
   board/area functions field-for-field, with the same
   application-level cascade for the same DROP-TABLE-cascade-hazard
   reason. `topic` is deliberately *not* part of `update_channel`'s
   full-state replace — it stays gated by `set_topic`'s own `EDIT`
   check and audit trail (round 33), not folded into this SysOp-only
   action.
3. **Moderator grant/revoke presets are deliberately different for
   channels than boards/areas**, since `ChannelPermission` has no
   `READ`/`WRITE`/`APPROVE` bits to begin with (round 34's original
   reasoning): "Full moderator" = `EDIT|MODERATE|MANAGE_MEMBERS`,
   "Moderator only" = `MODERATE`.

## Sign-off notes, round 62 (per-user chat timestamp preference: `/timestamps` — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Closes the follow-up round 42 explicitly left open — identified as the
clear low-effort candidate among Phase 2's remaining gaps (ANSI-art
login screens and the TUI/editor pair both need real design decisions
or are much larger pieces).

1. **New module `netbbs/chat/timestamps.py`**, wrapping
   `netbbs.user_preferences` the same way `netbbs.timeutil` wraps
   `netbbs.config` for the node-wide equivalent — deliberately kept in
   `netbbs.chat`, not `netbbs.timeutil`, since it also does ANSI
   coloring, matching `chat_stream_label`'s precedent of a small
   chat-specific helper combining a lookup with sanitizing/coloring.
2. **A real architectural wrinkle:** a genuinely per-user toggle can't
   be satisfied by rendering a broadcast string once at send time,
   since `ChatHub.broadcast` pushes one shared string to every
   recipient's queue but whether *that* recipient wants a timestamp is
   only knowable at receive time. Resolved by loosening the broadcast
   type hint and introducing a small envelope carrying the raw text
   plus a raw timestamp; `receive_loop` (the one place that already
   knows both "which session" and "which account") is the single place
   that turns an envelope into a final string, against its own
   session's user's preference.
3. **Scope held to round 32 point 3's literal wording** (live messages,
   replayed scrollback, join/leave, `/me`, private messages) rather
   than extended to every chat event kind — `/topic`/`/nick` change
   notices and moderation notices are deliberately not timestamped
   live, since extending coverage there would have been scope creep.
   Scrollback replay is the one exception applied uniformly regardless
   of original event kind.
4. **`/timestamps on|off|toggle`**, no bare-invocation toggle (unlike
   `/away`) — this preference has no natural meaning for a bare
   invocation, and the design doc's own wording already specifies
   exactly these three subcommands.

## Sign-off notes, round 63 (welcome banner: ANSI art login screen, Round A of a three-part skinning initiative — implemented)

*(Condensed — full round including a real test-regression writeup is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

1. **Phase 2's last two line items reframed as related, not
   independent — Thiesi's own insight, not a decision made
   unilaterally mid-implementation.** He wants a SysOp to manage as much
   as possible *through the BBS itself*, including an eventual in-BBS
   WYSIWYG ANSI art viewer/editor, and wants the whole BBS eventually
   skinnable beyond just the login screen — directly echoing round 26's
   own reasoning for pairing a TUI abstraction with a real consumer.
   Agreed sequencing: **Round A** (this round) ships *display* of a
   pre-made ANSI file at login, no TUI dependency; **Round B** (future)
   builds the TUI screen-buffer abstraction + a WYSIWYG in-BBS ANSI
   editor, designed against two real consumers (the ANSI editor and the
   originally-planned prose editor); **Round C** (future) generalizes
   Round A's display mechanism beyond the login screen.
2. **CP437-fallback decoding, not a required-UTF-8 or raw-bytes
   scheme.** Tries UTF-8 first, falls back to the stdlib `cp437` codec
   (no new dependency) on failure — `cp437` is a total function over
   all 256 byte values, so decoding cannot fail once the fallback is
   reached. Real scene-authored `.ans` files are raw CP437 and will
   almost always fail strict UTF-8 decoding, making the heuristic
   reliable. This content is trusted, SysOp-authored, and deliberately
   bypasses `sanitize_text` entirely — same trust tier as `colored()`
   output, documented explicitly so a future reader doesn't "fix" this
   into destroying real art.
3. **Filesystem-only storage, no in-BBS upload in this round** — a
   single well-known file colocated with the database (mirroring the
   existing SSH-host-key pattern), not a `node_config` TEXT column or
   the content-addressed file-area scheme. The enable/disable flag
   lives separately in `node_config` so a SysOp can revert without
   deleting their prepared file. Every failure mode falls back to the
   original hardcoded banner silently to the connecting user, but is
   logged server-side at WARNING.
4. **Deferred to Round B/C, deliberately, not oversights**: no caching;
   size enforcement stays a login-time fallback rather than an
   in-editor error, since there's no "save" path to enforce it at yet;
   the config key and file path are named specifically for the login
   banner, not a generic "skin" concept — guessing a generalized shape
   now, before Round C's real requirements exist, would repeat the
   mistake round 26 already flagged for the TUI abstraction itself.

## Sign-off notes, round 64 (TUI screen-buffer core + WYSIWYG ANSI editor, Round B1 of the skinning initiative — implemented)

*(Condensed — full round including extensive bug-fix and testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

1. **Round B split into B1/B2, confirmed with Thiesi rather than
   assumed** — round 63 scoped "Round B" as the screen-buffer core plus
   *both* eventual editors together; given this is, by round 26's own
   framing, the largest remaining piece of Phase 2, it was split:
   **Round B1** (this round) ships the screen-buffer core, structured
   key events, and a basic WYSIWYG ANSI editor wired into Round A's
   welcome-banner screen; **Round B2** (future) reuses B1's foundation
   for the prose editor. Also confirmed: periodic **autosave** to a
   draft file, and a **cursor + type + color-picker** first-version
   scope, with undo/redo, block copy/fill/select, and line/box-drawing
   tools confirmed as a real, planned *later* phase, not abandoned.
2. **`netbbs.rendering.screen_buffer`**: `Cell`/`ScreenBuffer`/
   `diff_ansi`/`full_render_ansi`, pure and I/O-free, built entirely on
   existing ANSI primitives. Both diff functions group consecutive
   same-style cells on a row into one `colored()` call — a real saving
   confirmed relevant on the web transport, which has no server-side
   write batching.
3. **A gap round 63 didn't anticipate**: `decode_ansi_bytes` only ever
   does byte decoding, never interprets embedded cursor/color escape
   sequences (Round A only needed to *display* a banner via a real
   terminal emulator). *Editing* an existing file needs the server side
   to actually know what's in each cell — filled by a new minimal,
   best-effort ANSI interpreter, `netbbs.rendering.ansi_parse.
   parse_ansi_into_buffer`, scoped the same honest way this project's
   Zmodem implementation already is.
4. **Structured key events — the actual foundation a screen editor
   needs, which nothing in this codebase provided before this round.**
   A new `Session.read_editor_key() -> EditorKey` surfaces arrows,
   Home/End, Page Up/Down, and a real standalone Escape press as
   first-class events. `WebSession` keeps its own independently-
   maintained escape decoder (confirmed pre-existing, not shared with
   `char_input`'s) and translates its own events to the same type at
   the boundary — a known, accepted duplication rather than an
   unscoped refactor to unify two already-working transports' decoders.
5. **`netbbs.net.ansi_editor.edit_ansi_art` is deliberately generic**
   — knows nothing about "welcome banner" specifically, returning bytes
   for the caller to persist wherever it wants, so Round B2/Round C can
   reuse it unchanged later. Fixed 80x24 canvas; a 16-color palette
   (not the full 256-color range `colored()` supports elsewhere — real
   scene ANSI art overwhelmingly targets the classic 16, and it keeps
   the color picker a single unpaginated screen); a genuine independent
   `asyncio.create_task` autosave loop that survives the interactive
   session dying, by design; draft recovery offered on entry.
6. **A real design correction made during this round's own testing,
   not assumed correct from the plan text**: the plan described the
   glyph picker as persistent "brush" state, but a real keyboard has no
   key that ever sends a glyph character directly — resolved by having
   glyph selection paint immediately, exactly as if that glyph had been
   typed, removing the dead persistent-brush concept entirely. Ordinary
   typing continues to place the literal typed character, colored per
   the current fg/bg (which *do* remain persistent state, genuinely
   different from the glyph case).

## Sign-off notes, round 65 (login username case-sensitivity bug — fixed)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

1. **The bug**: every login lookup compared usernames under SQLite's
   default BINARY collation, so login only succeeded with the exact
   registered case, and the `UNIQUE` constraint was likewise
   case-sensitive, letting two case-variant accounts (e.g. `"Thiesi"`
   and `"thiesi"`) coexist as a trap for both login and registration.
2. **Fix scoped to avoid this project's biggest available foot-gun**:
   `users` is the referenced *parent* of nine tables' foreign keys
   (several `CASCADE`/`SET NULL` since the SysOp hard-delete round),
   and SQLite's `DROP TABLE` performs an implicit `DELETE FROM` first
   whenever `foreign_keys = ON` — rebuilding `users` itself via the
   usual drop/rename table-rebuild pattern would have cascade-wiped
   every user's moderator grants, channel membership, preferences, and
   blocklist entries as a side effect of fixing a login bug.
   Deliberately avoided in favor of a plain `CREATE UNIQUE INDEX ...
   COLLATE NOCASE` — no table rebuild, no drop of the parent, closing
   both the login-lookup and registration-uniqueness halves of the bug
   in one migration.
3. **Left as-is, deliberately**: any case-variant duplicate usernames
   already present in a database from before this migration are not
   auto-merged — the migration fails loudly rather than silently
   picking a winner between two existing accounts. Acceptable at this
   project's current single-sysop-node stage; a real migration-time
   merge tool would be speculative scope for a scenario that hasn't
   occurred.

## Sign-off notes, round 69 (nano keybindings; post editing; prose editor round B2 — implemented)

*(Condensed — full round including extensive testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

Three related pieces of work, confirmed with Thiesi across several
rounds of discussion before any of it was written: (1) nano-style
keybindings, shared between the round B1 ANSI editor and a new prose
editor; (2) the post-editing data model this needed answered first,
since it collided with content-addressing; (3) the fullscreen prose
editor itself (design doc round 63/64's "Round B2").

1. **Nano keybindings** applied retroactively to the round B1 ANSI
   editor and shared with the new prose editor (Ctrl+X quit, Ctrl+O
   save, Ctrl+T glyph picker — freeing bindings that collided with
   nano's own conventions). A bare Esc press is now a genuine no-op,
   matching nano's own Meta-combo-prefix behavior rather than "exit."
2. **Post editing — the core tension**: `posts.post_id` is a content
   hash computed from the body itself (§7). An in-place `UPDATE` on
   edit would leave a row's own `post_id` silently mismatched against
   its current content, and would orphan any existing reply's fixed
   `parent_post_id` reference. Resolved the way §13 already described
   for *moderator* edits on Linked boards ("a new, signed event that
   references and amends" the original) — generalized here to local
   self-edits too, confirmed with Thiesi as the "do it properly" option
   over two others (in-place mutation; deferring editing entirely).
3. **Schema**: `posts.root_post_id` (the original post's `post_id` for
   every edit of it) and `posts.edit_of_post_id` (the specific
   immediate predecessor, kept for a future edit-history view). Added
   via plain `ADD COLUMN`, not the usual drop/rebuild pattern — `posts`
   is a live parent of several tables' own foreign keys, and round 60
   already found the hard way that `DROP TABLE`'s cascade side effects
   would risk touching all of those relationships at once.
4. **`edit_post`** is allowed for the post's own original author with
   no grant needed (this project's first "you may act on it because you
   own it" concept for posts — everything else here is purely
   grant-based), or anyone holding `BoardPermission.EDIT`.
5. **`list_posts_page`/`list_pinned_posts` resolve each result to its
   *root* identity, not the row shown.** `post_id`/`created_at`/
   `pinned`/etc. always come from the *root* row; only `subject`/
   `body`/`status` are substituted from whichever row in the chain is
   newest-approved. This is what makes editing invisible to pagination:
   cursors and feed position never move on edit. On a moderated board,
   an edit re-enters moderation exactly like a new post — an edit must
   not be a way to bypass moderation.
6. **New `netbbs.rendering.prose_buffer`**: pure, I/O-free, mirroring
   `screen_buffer.py`'s own shape. Holds logical lines (exactly what
   was typed between real Enter presses, never auto-rewrapped) plus a
   cursor; wrapping only enters the picture in a separate `wrap_lines`
   function, using the same stdlib `textwrap` primitives `reflow()`
   already relies on — but deliberately not `reflow()` itself, which is
   wrong for something being actively edited a character at a time.
7. **`netbbs.net.prose_editor.edit_prose`** shares the same
   control-loop/autosave/quit-confirm shape as the ANSI editor; moves
   by *visual* (soft-wrapped) row, not logical line. Viewport size is
   the session's own negotiated terminal size (with a 40x10 floor), not
   a fixed canvas — a deliberate difference from the ANSI editor, where
   80x24 is the *content's* own fixed dimensions.
8. **Explicit V1 scope boundary, not silently dropped**: no cut/copy/
   paste, no search/replace, no syntax highlighting or spell-check —
   nothing this round's planning discussion settled required them;
   worth its own follow-up if actually wanted.
9. **New `netbbs.net.editor_preference`**: a per-user "compose with the
   fullscreen editor" opt-in, defaulting off, the same thin-wrapper
   shape `netbbs.chat.timestamps` already established. Surfaced on the
   Profile screen (confirmed with Thiesi over a chat-style slash
   command, since this preference has nothing to do with chat).
10. **Wired into composing a new board post and editing the bio** —
    the two real gaps identified during planning. **Not wired in this
    round, deliberately flagged rather than guessed at**: an `[E]dit`
    entry point on an *existing* post — the board-reading screen has no
    mechanism today for selecting one specific post from a page, and
    inventing a selection scheme without the same care round 16 gave
    the picker's own problem would be exactly the kind of unilateral
    mid-implementation design call this project's process exists to
    avoid (addressed in round 70). Also flagged, not fixed: a
    pre-existing, unrelated bug where deleting a post with a live reply
    already raises a foreign-key error on current `main` — the right
    fix is its own design decision, out of scope for a round about
    editing (addressed in round 70).

## Sign-off notes, round 70 (the two round 69 gaps closed out — implemented)

*(Condensed — full round including testing detail is in [docs/NetBBS-worklog.md](NetBBS-worklog.md).)*

1. **The pre-existing FK delete bug (round 69's flagged gap), fixed at
   the root round 60 already established for this exact class of
   problem.** The expiry sweep's hard-delete step now excludes any post
   still referenced by another live row (a reply's parent, an edit
   chain's root, or a later edit's predecessor) via a `NOT EXISTS`
   clause, rather than changing `parent_post_id`'s `ON DELETE`
   behavior — which would need the drop/rebuild pattern round 60
   already found risks cascading into *all* of a live parent table's
   other relationships at once, not just the one column being fixed. A
   referenced post simply stays `'expired'` indefinitely instead of
   being purged — already a valid, harmless state, not a new one.
2. **The edit-existing-post UI entry point (round 69's other flagged
   gap).** Posts on a board page are now numbered page-relative only
   (not a stable identity across page changes), with an `[E]dit` option
   shown only when at least one post on the current page is actually
   editable by the viewer, checked *before* prompting for any new
   content. Deliberately not the real picker — a board page is at most
   a handful of posts, too small to justify it, matching how the ANSI
   editor's own glyph/color pickers are the only place in this codebase
   that *does* reach for the real picker over inline numbering.
3. **A real UX regression caught and fixed before it shipped, not
   discovered after**: the first version simply re-fetched the *newest*
   page after every edit, silently bouncing the SysOp back to page one
   if they'd been browsing older history. Fixed by having `_show_board`
   track which cursor produced the page currently on screen
   (`page_anchor`) — editing a post never moves it in the feed to begin
   with (round 69 point 5), so the fix only needed to stop discarding
   navigation state, not anything in the pagination model itself.
4. **The plain (non-fullscreen-editor) body-entry path gained real
   edit support**: shows the current body as read-only context above
   the prompt when editing, and treats a bare Enter as "keep it
   unchanged," matching the fullscreen path's own pre-fill contract.

## Sign-off notes, round 71 (Communities / Link Communities — directional decision only, not specced)

**What was decided:** NetBBS will move toward topic-oriented
navigation as the primary way users move through the system, via a new
"Community" (local) / "Link Community" (federated) concept described
in §16 — without touching the existing boards/chat/file-area package
split, which stays exactly as designed.

**Why:** originated from Thiesi noticing that the traditional BBS
main-menu split (boards/files/chat as separate top-level destinations)
was substantially a consequence of 1980s–90s technological constraints
— sequential, non-overlapping session workflows dictated by real
hardware/bandwidth limits — rather than something intrinsic to what a
BBS is. Modern terminal clients don't have those constraints (a file
transfer, a chat window, and composing a board post can all coexist),
so the traditional split no longer reflects how people actually think
about using the system; they arrive wanting to engage with a
*subject*, not a *medium*. The concept was refined through discussion
with people outside the two-person Thiesi/Claude design process, one
of whom produced the clearest summary of the rationale, which is
reflected in §16's framing.

**Alternatives considered:**
- *Do nothing / keep the traditional split* — rejected as the status
  quo, but explicitly not because it's wrong on its own terms;
  FidoNet-era BBSes and the first attempt both used it successfully.
  Rejected because it stops matching how *this* generation of users
  actually approaches a subject-first internet, and because nothing
  about NetBBS's terminal-based nature actually requires it (that
  constraint died with synchronous single-tasking dial-up sessions,
  not with terminals themselves).
- *Replace boards/chat/files with a single unified "Community"
  resource type* — rejected. Boards, chat, and file areas have
  genuinely different delivery semantics (asynchronous vs. live vs.
  repository) that the existing package split (§3) correctly keeps
  separate; collapsing them into one entity would be a much larger,
  riskier rewrite for no real benefit, since the actual user-facing
  want is *co-location*, not *merging*.
- *"Linked Communities" instead of "Link Communities"* — rejected for
  naming consistency; the project has already established "Link X"
  (Link messages, Link boards, Link chat) as its standing modifier for
  anything distributed over NetBBS Link, and there's no reason to
  introduce a second, inconsistent pattern for this one feature.

**Explicitly deferred, not decided now** (see §16 for the full list):
data model (one Community per resource vs. many), permission-
inheritance mechanics, the "uncategorized" bucket for cross-cutting
resources like private mail, jump-shortcut navigation design,
migration path, and phase placement. This entry exists so the
direction is recorded and the reasoning doesn't need rediscovering —
not as a green light to start building any of it.

## Sign-off notes, round 73 (two architecture forks from a code-review follow-up: session revocation watcher, streaming Zmodem uploads)

A code-review follow-up (see the round 73 worklog entry for the full
fix narrative) surfaced two questions that were genuine forks, not
contained bugfixes, and were put to Thiesi before implementation
rather than picked unilaterally.

**Decision 1 — account revocation: one background watcher per
session, not a check at every loop boundary.**

**What was decided:** cross-process account disable/delete
revalidation (issue #29) is now enforced by a single background task
per authenticated session (`netbbs.net.login_flow.
_watch_for_account_revocation`), polling every 5 seconds and
forcibly disconnecting the session the moment the account comes back
inactive — regardless of which screen/loop the session is currently
in, including one sitting fully idle. The two existing in-loop checks
(main menu, chat's send loop) were kept alongside it, not replaced, as
zero-latency defense-in-depth for an actively typing session.

**Why:** the alternative — bolting an `account_still_active()` check
onto every long-running loop (board browsing/posting, file areas, the
profile screen, the whole admin menu tree) — was the smaller patch,
but its coverage is only ever as complete as the list of loops someone
remembered to touch, a list this exact bug had already grown past
twice (the original fix covered the main menu; the first reopening
added chat's send loop; the second reopening found four more). A
background watcher's coverage is structural, not enumerable — it
doesn't care what loop the session is in, or whether a future feature
adds a new one. It also closes a case no in-loop check ever could: a
session that's genuinely idle, producing no input for the check to
fire on. Thiesi chose this over the smaller patch specifically for
that "stop enumerating loops" property.

**Alternatives considered:**
- *Add the guard at every current loop boundary* — rejected as
  described above: correct today, but a maintenance trap that already
  bit this exact feature twice.
- *Awaiting the existing `ActiveSessionRegistry.disconnect_one()`
  directly from the watcher task* — this was the first implementation
  attempt, and it deadlocks: `disconnect_one` cancels the target
  session's task *and awaits its full unwind*; the target session's
  own cleanup (in `run_authenticated_session`'s `finally`) then tries
  to cancel and await the *watcher* task in turn, and the watcher task
  is still blocked inside `disconnect_one`'s own await at that exact
  moment — a mutual wait between the two tasks. `disconnect_one`'s own
  docstring already flags the analogous *self*-cancellation hazard,
  but this is a distinct, new *mutual* variant between two different
  tasks that didn't previously exist. Fixed with a new lighter-weight
  `ActiveSessionRegistry.cancel_one()` — schedules the cancellation
  without awaiting the target's unwind — rather than either accepting
  the deadlock or making the target session's own cleanup not wait for
  the watcher (which would risk exactly the "Task was destroyed but it
  is pending" warning round 51's `disconnect_all` design already went
  out of its way to avoid).

**Left as-is / deferred:** the 5-second poll interval is a fixed
module constant (`_REVOCATION_CHECK_INTERVAL_SECONDS`), not
node-configurable — judged an internal responsiveness/DB-query-
overhead tradeoff an operator doesn't need control over, unlike e.g.
invitation expiry. Revisit if a real deployment ever wants it tuned.

**Decision 2 — Zmodem uploads: stream to a temp file with incremental
SHA-256, not a bounded concurrency semaphore.**

**What was decided:** `netbbs.net.zmodem.receive_file` now streams
each received subpacket directly to a caller-supplied destination
path, hashing incrementally, instead of accumulating the complete
transfer in a `bytearray` and returning a second `bytes` copy on top
of that. `netbbs.files.storage` gained `new_incoming_temp_path`/
`move_temp_file_into_storage` as the streaming counterpart to the
existing bytes-based `store_bytes`; `netbbs.files.entries` gained
`upload_file_from_temp` alongside the existing `upload_file`.

**Why:** the per-transfer `max_bytes` ceiling (already in place from
the original round-72 fix) bounds any *one* upload, but says nothing
about several concurrent ones — a node accepting `N` simultaneous
uploads near that ceiling could transiently hold something like
`2×N×max_bytes` in memory (the accumulating buffer plus the returned
copy, per transfer), an availability gap at node scope even though
every individual transfer was already correctly bounded. Streaming
removes the whole-file in-memory requirement at its root, rather than
capping how many transfers can be in flight around a design that still
buffers each one fully.

**Alternatives considered:**
- *A bounded upload-concurrency semaphore (or a weighted aggregate
  byte budget), keeping the buffered design underneath* — the smaller
  patch, explicitly offered as the "acceptable hardening step" if the
  bigger rewrite wasn't wanted. Would need a new node-wide shared
  resource threaded through the session stack the same way
  `NodeControls`/`ChatHub` already are, plus a policy decision (how
  many concurrent uploads, per-account vs. node-wide) with no strong
  existing precedent to anchor it to. Not chosen — the streaming
  rewrite closes the actual problem (unbounded memory *per transfer*
  compounding across concurrency) rather than capping the compounding
  factor around a design that still has it.
- *Streaming without incremental hashing (hash after the fact, reading
  the temp file back)* — rejected as pointless: the whole motivation is
  avoiding a second full read of the content, and `hashlib.sha256()`
  already supports incremental `.update()` calls naturally, so there
  was no reason to hash any way other than as bytes already pass
  through on their way to disk.
- *Staging temp files in the platform temp directory
  (`tempfile.gettempdir()`)* — rejected specifically for this
  project's NetBSD deployment target: the final placement step
  (`move_temp_file_into_storage`) uses `os.replace()`, an atomic
  rename POSIX only guarantees within a single filesystem, and `/tmp`
  is commonly its own separate `tmpfs`/`mfs` mount there, distinct from
  wherever a node's data volume actually lives. Staging under a
  `.incoming` subdirectory *inside* `storage_root` itself instead
  guarantees the rename is always same-filesystem, regardless of
  platform temp-directory placement.

**Left as-is / deferred:** `upload_file` (the bytes-based path) was
kept, not removed — dev scripts (`scripts/create_test_file.py`) and
most existing tests already have the complete content in hand, and
forcing them through a temp-file dance would add ceremony with no
corresponding benefit for a caller that was never the availability
risk in the first place. `download_file` (the send side) was
deliberately left untouched — a download reads a size already bounded
by whatever's stored, not attacker-controlled growth the way an
upload's declared/actual size is, so it was never the same class of
problem.

## Sign-off notes, round 75 (two final-polish items deferred to last; a real self-registration gap identified)

Recorded directly from Thiesi, v2.0.0 having just shipped (Phase 1+2
complete plus two rounds of security hardening) — three scope items,
none implemented this round.

**1. Online contextual help — deferred until 100% feature-complete.**
A short, in-context description of what an option/keystroke actually
does, shown before a user commits to pressing it — not a full manual;
closer to a one-line-per-option hint than a help system in its own
right. Deliberately deferred, not scoped further now: Thiesi's own
reasoning is that workflows and behaviors need to stop moving first,
since writing accurate help text against a UI that's still changing
means rewriting it repeatedly for no benefit. Revisit once the feature
set is genuinely done, not before.

**2. Menu prettification — deferred until 100% feature-complete,
direction not yet chosen.** The current menu presentation (colored
bracketed hotkeys, one-line option lists) is functional but plain —
Thiesi's own words, "not a pleasant place to be." Real candidate
directions were named but explicitly not decided between: ANSI/TUI
"windows" in a DOS-application style (feasible now that round 64's
screen-buffer/diff TUI core exists), or simply cleaner, non-one-liner
menu layouts without a full windowing model. To be designed and
confirmed once actually reached — recorded here so the direction
question doesn't need rediscovering, not as a spec.

**3. Newly identified gap: no self-service account registration.**
Confirmed by inspection (grepping every call site of
`netbbs.auth.users.create_user`): there is no code path anywhere that
lets a new, unauthenticated Telnet/SSH/web connection create its own
account. Every existing route into `create_user` — the in-session
`[A]dmin` menu, the standalone `python -m netbbs.admin` CLI, and dev
bootstrap scripts — requires either an already-authenticated SysOp or
direct machine access. No phase (§15) ever specified self-registration
as in-scope, and Phase 1's "Password + keypair auth" bullet, in
hindsight, only ever meant authenticating an *existing* account.
Unlike items 1–2 above, this is **not** deferred — it's a genuinely
missing core feature for what a BBS is, flagged as the next real
implementation task after the in-progress chat status line, not a
someday item.

**Ordering, as given:** chat status line first (checked for blockers
before starting; see its own worklog entry once done), then
self-service registration, ASAP. Items 1–2 above sit at the very end
of the list, after everything else, including registration.

## Sign-off notes, round 76 (self-service registration: design decisions)

Implements round 75 item 3 (the identified self-registration gap) —
Telnet, web, and SSH all gain a way for a new, unauthenticated
connection to create its own account. Confirmed directly with Thiesi
before implementing: SSH support is required (not optional/deferred),
and whether a newly created account needs SysOp approval before it can
log in is a system-wide SysOp setting, defaulting to off (instant
activation), toggleable on for a stricter/private node. This note
records the design decisions made while implementing that; the
implementation narrative itself (files touched, tests written, final
suite count) lives in the worklog per this project's usual split.

**1. One reserved sentinel username (`new`) across all three
transports, not a separate command/menu option.** Telnet/web: typing
`new` at the ordinary `_login` username prompt (instead of a real
username) enters registration. SSH has no username prompt to hook —
its identity is proven during the protocol-level handshake itself —
so it reuses the exact same sentinel as the username the SSH client
connects with (`ssh new@host`), triggering a keyboard-interactive
(kbdint) exchange instead. One discoverable answer to "how do I sign
up" regardless of which transport a person reaches the node through,
rather than three different answers. `new` is rejected as a real,
registerable account name at the single username-grammar choke point
(`netbbs.auth.users._validate_username`), case-insensitively (matching
the existing `COLLATE NOCASE` uniqueness the rest of the username
system already uses) — so it can never be shadowed by a real account
and can't collide with the trigger.

**2. SSH's mechanism: keyboard-interactive (kbdint) auth, verified
against the actually-installed `asyncssh` package before committing to
the design, not assumed from memory.** `asyncssh.SSHServer` exposes
`kbdint_auth_supported()`/`get_kbdint_challenge()`/
`validate_kbdint_response()` — a genuine, standard SSH protocol
feature (RFC 4256; commonly used for OTP/2FA in real deployments), not
a workaround. It allows a multi-round, server-driven prompt sequence
(desired username, then password, then confirm password) entirely
within the SSH *authentication* phase, before any shell/session
exists. **Inherent SSH constraint, confirmed against the real
library, not a limitation of this implementation:** the "authenticated
identity" for an SSH connection is fixed to whatever username the
whole auth attempt used
(`SSHSession.__init__`'s `authenticated_username`, sourced from
asyncssh's own `get_extra_info("username")`) — so a connection that
authenticated *as* `new` cannot seamlessly become the freshly created
account within that same connection. Registration over SSH therefore
always ends by deliberately *failing* the auth attempt (after showing
a final "account created, reconnect as X" message) rather than
succeeding into a session — the client must reconnect using the new
username. This is presented to the user as an inherent property of the
SSH protocol, not a shortcoming of this feature.

A second, non-obvious SSH-specific fix, found only by actually running
a real `asyncssh` client/server pair against each other rather than
reasoning about the protocol in the abstract: asyncssh's client-side
auth loop re-offers a still-`kbdint_auth_supported()`-advertised method
again after every failed round, since nothing about that method's
availability changes between attempts by default — a client configured
to offer keyboard-interactive only (no password/public key, as a
registration-only connection naturally is) would otherwise retry the
entire username/password/confirm exchange forever within one
connection once the first attempt's deliberate final failure came
back, rather than cleanly disconnecting. Fixed by capping registration
to exactly one kbdint challenge per connection
(`_NetBBSSSHServer._registration_attempted`, consulted by both
`kbdint_auth_supported` and `get_kbdint_challenge`) — set the first
time *any* kbdint challenge is requested on a connection, regardless of
which username triggered it or whether it was ultimately the reserved
sentinel, so the same fix also protects an ordinary account's own
(normally password/pubkey) connection from the identical retry loop if
a client ever restricted itself to kbdint only against it.

Which auth methods a connecting client actually tries, and in what
order, is the client's own choice — kbdint is simply *offered*
alongside password/public-key whenever the connecting username is
`new`. Most OpenSSH clients try keyboard-interactive before password by
default, so `ssh new@host` reaches this flow with no extra flags; a
client explicitly configured to skip kbdint (e.g. `-o
PreferredAuthentications=password`) instead just sees an ordinary
failed login for that reserved name. Documented as a known,
client-negotiation-dependent caveat, not something the server side can
force — same "flagged, not blocking further work" treatment this
project already gives other real-client-behavior unknowns (see the
Phase 2 completion notes on unverified third-party SSH/Zmodem/browser
interop).

**3. `pending_approval`: a dedicated new `users` column, not a reuse of
`disabled_at`/`set_user_disabled`.** Considered reusing the existing
SysOp soft-disable mechanism (`disabled_at`) for "awaiting approval"
too, since both ultimately gate login the same way. Rejected:
`set_user_disabled` requires a `changed_by: User` actor and records a
moderation-log row with "who disabled this and why" semantics that
don't fit a system-generated "brand new, no human has looked at this
yet" state — there is no actor at account-creation time. Just as
importantly, a SysOp scanning the user-management screen needs to
tell "awaiting my first review" apart from "banned troublemaker" at a
glance; collapsing both into one `disabled_at` timestamp would erase
that distinction. A plain `ALTER TABLE ADD COLUMN` (not the
table-rebuild pattern several earlier migrations used), matching round
46's `posts.root_post_id` precedent — `users` is itself a live parent
of many other tables' foreign keys, and SQLite's `DROP TABLE`
(the rebuild pattern's first step) applies its own cascade/SET-NULL
side effects to every row still referencing the table regardless of
that row's own `ON DELETE` clause, a hazard already documented and
deliberately avoided the same way there.

**4. Approval policy: a `netbbs.config` node-wide toggle
(`require_registration_approval`), default off.** Directly per
Thiesi's own instruction — instant activation is the common case for a
small, low-risk community node; the stricter behavior is an explicit
opt-in for an operator running a private/invite-adjacent node. Same
shape as every other node-wide setting already in `netbbs.config`
(expiry grace period, max upload size, invitation expiry) — a single
key-value row, not a new mechanism.

**5. Self-registration is password-only, never keypair, on every
transport.** Telnet/web already had this limitation for ordinary login
(`_login`'s own docstring: a plain client has no way to sign a
challenge), so registration doesn't introduce anything new there. SSH
public-key auth proves possession of a private key as part of the SSH
protocol itself — but a client connecting *as* `new` to register is,
by definition, not yet authenticating as any real account, so there's
no natural place in the kbdint exchange to also capture and register a
public key without meaningfully complicating the flow for a benefit
nothing has asked for. An account can still gain a public key later,
added by a SysOp through the existing admin screen. Kept explicitly
minimal rather than speculatively supporting a keypair-registration
path no one requested.

**6. Registration attempts reuse the existing `LoginThrottle`
directly, not a new mechanism.** Telnet/web: `_register_new_account`
calls the same node-wide `LoginThrottle.allow_attempt`, keyed by the
*desired* username instead of an authenticating one, before the
expensive `create_user_async` (Argon2 hash) work runs — identical
reasoning to why `_login` checks it before
`authenticate_password_async`. SSH's kbdint flow does the same via
`_NetBBSSSHServer`'s own already-shared `throttle` reference. This is
literally the same `LoginThrottle` instance every login attempt on the
node already shares (one per node, constructed in `netbbs.__main__`),
not a second parallel budget — registration traffic is exactly the
kind of authentication-adjacent, hash-triggering load issue #3's
throttle already exists to bound.

**7. A failed/cancelled registration consumes one of `_login`'s
per-connection `max_attempts`, on Telnet/web.** Considered and
rejected a separate registration-attempt counter threaded through
`_login`'s return type — simpler to let `_register_new_account`
returning `None` just `continue` the existing attempt loop, the same
outcome a failed password attempt already produces. Deliberately not
over-engineered for a case with no reported real-world pressure yet.

**Left as explicitly out of scope for this round:** self-registered
accounts are always created at `user_level=0` (the existing
`create_user`/`create_user_async` default) — there is no path for
self-registration to request or receive an elevated level; that
remains exclusively a SysOp action via the admin screen, unchanged.
Approving a pending account (`netbbs.auth.users.approve_pending_user`)
reuses the existing `[L]ist users` → user-detail screen rather than a
second, parallel pending-accounts queue UI, matching how pending
posts/files are already reviewed through their own existing
board/area-detail screens rather than a dedicated inbox.

## Sign-off notes, round 77 (chat status line: bugfix, inverse video, expanded content, timestamp format)

Thiesi's feedback on the round-75 chat status line, four separable
pieces (per this project's own "flag and tackle one at a time"
convention) — a real bug, two quick approved changes, and one genuine
UX fork resolved via an `AskUserQuestion` mockup before implementing,
matching round 75's own DECSTBM precedent.

**1. Bugfix: leaving a channel didn't clear the screen.** Root cause:
`clear_screen()` was only ever called on chat *entry* (round 75) —
neither the channel picker (`pick_item`, reached via `/leave`) nor the
main menu (`_draw_main_menu`, reached via `/quit`) ever cleared it
themselves, so the last screenful of chat stayed visible until
unrelated output happened to overwrite it. Only `/join`'s
"switch channel" path looked clean, as an incidental side effect of
`_chat_loop` re-clearing on its own next entry, not because leaving was
actually handled. Fixed by clearing on exit too (`_chat_loop`'s
`finally` block), symmetric with entry, gated on the same
`status_line_enabled` condition.

**2. Inverse video.** Added `reverse: bool = False` to
`netbbs.rendering.ansi.colored()` (SGR 7), applied to the status line
in place of the previous muted-color/bold styling. Padded to the full
terminal width (`text.ljust(session.terminal_width)`) before coloring —
otherwise reverse video would only invert the characters themselves,
leaving a ragged partly-colored bar rather than one solid inverted row.

**3. Status line content, expanded — structure confirmed via
`AskUserQuestion` before implementing.** Thiesi asked for a lot more on
the bar at once (own username, nick, away count, topic, channel
type/visibility, own privileges) and separately asked whether that much
information could fit without wrapping oddly on narrow terminals. Three
structurally different answers were presented with concrete mockups:
single-line priority-truncated, a two-row reserved bar, or keeping the
bar minimal and moving the rest to an on-demand command. **Thiesi chose
single-line, priority-truncated** — matches the existing `truncate()`
call already in place (round 75) and this project's "must degrade
gracefully above a 40×24 minimum" requirement better than permanently
spending a second row of an already-scarce minimum-height terminal.
Final field order, most- to least-important (`truncate()` only ever
cuts from the right, so later fields are what silently disappear first
on a narrow screen): `#channel[type]` → `N online(M away)` → `"topic"`
(omitted entirely if unset) → `you:username(nick)[privileges][away/
muted]` → `HH:MM` clock. Compact codes throughout (`pub`/`hidden`/
`invite` for channel type; `mod`/`edit`/`members` for individual
`ChannelPermission` bits, collapsed to a single `sysop` label instead
of enumerating bits for a SysOp, since `has_permission` passes a SysOp
on every bit regardless of any real grant — listing them all out would
be misleading busywork, not information).

**Explicitly not implemented: "linked vs. local" channel.** Requested,
but there is no such distinction anywhere in the schema or code yet —
NetBBS Link is Phase 3, which per this document's own phase tracking
has not started. Every channel today is inherently local; adding a
`[local]` (or worse, hardcoding it) now would need revisiting the
moment Link actually exists, for a label that carries zero information
in the meantime. Flagged back to Thiesi rather than guessed at.

**4. Chat timestamp format: date dropped, time-only.** Applies to
`netbbs.chat.timestamps.format_with_preference` — the per-user
`/timestamps on|off` preference (round 32/62) now always renders
`override_format="%H:%M"` when on, not the node's full configured
date+time format. Same reasoning round 75 already applied to the
status line's own clock: chat is inherently a *now* context, so a
per-message date is static clutter for the overwhelming majority of a
session, not information. Applies uniformly to live messages *and*
scrollback replay — an old message showing only a time (no date) on
replay is an accepted trade-off (surrounding chat context, not the
exact date, is what actually signals "this was from a while ago"), not
worth a separate format for that one case.

**Discussed, not decided — recorded for whenever either is actually
scoped:**

- **Moving the input line next to the status line** (mirroring a
  Claude-Code-style fixed bottom bar): Thiesi asked for Claude's own
  opinion rather than a decision. Given, not yet acted on — a real
  layout change (the input prompt currently scrolls with ordinary chat
  content; pinning it means it needs its own reserved row(s) and
  cursor-position bookkeeping distinct from what `save_cursor`/
  `restore_cursor` currently do for the status line alone) large enough
  to deserve its own design round rather than folding into this one.
- **A per-channel "good morning" system message at local midnight**:
  raised as a "what do you think", specifically flagging the awkward
  Link case (ten nodes across ten time zones each announcing their own
  midnight Link-wide would be incoherent). Discussed, not decided —
  Thiesi's own framing already identifies the crux correctly: this is
  cleanly implementable *today* as a purely local, per-node, per-
  channel event (no dependency on anything Link-related), but doing it
  in a way that's still correct once Link exists needs an explicit
  scope boundary decided *before* writing code, not after — specifically
  whether such a message ever crosses a Link channel's node boundary at
  all (most defensible default: never — a "new day" notion tied to one
  node's local clock has no coherent meaning federated across nodes in
  different time zones), and if it must ever be *some* Link-relayed
  event, what that would even mean is genuinely Phase 3+ design work,
  not decidable now. Not scheduled.

## Sign-off notes, round 78 (local-only midnight "new day" chat announcement)

Thiesi asked to build round 77's discussed-not-decided midnight
announcement, explicitly confirmed local-only (matching the
recommendation in that note: a "new day" tied to one node's local
clock has no coherent meaning federated across nodes in different time
zones, so this never crosses a Link channel's node boundary — moot for
now since NetBBS Link is Phase 3 and hasn't started, but the boundary
is decided regardless of when Link ships).

**1. New module, `netbbs.net.daybreak`, not `netbbs.chat.daybreak`.**
The natural broadcast envelope to reuse is `netbbs.net.chat_flow`'s
existing `_TimestampedNotice` — the same one join/leave/chat messages
already use to reach `_chat_loop`'s `receive_loop`. `netbbs.chat` is
`chat_flow`'s own dependency, never the reverse, so a module under
`netbbs.chat` importing `_TimestampedNotice` back from `chat_flow`
would be a circular import. Living under `netbbs.net` instead (where
every other session/broadcast-orchestration module already lives —
`login_flow`, `admin_flow`, `ssh`, `telnet`, `web`) resolves that
cleanly, and is arguably the more accurate home anyway: this is a
scheduling/broadcast-orchestration concern built *on top of*
`netbbs.chat`'s domain primitives (`ChatHub`, `list_channels`,
`record_message`), not a new primitive belonging to that domain layer
itself.

**2. Persisted to scrollback, not broadcast-only.** Every other
system-generated chat event (join/leave/mute/ban/kick/nick) is both
broadcast live *and* recorded via `record_message`, so a replayed
scrollback reads coherently rather than looking like it happened in a
vacuum — the daybreak announcement follows the same convention rather
than being a special ephemeral-only case. Needed a new `channel_
messages.kind` value, `'daybreak'`, via the same CHECK-widening
table-rebuild migration pattern rounds 37/40/41 already established
(SQLite still has no `ALTER TABLE` for changing a CHECK constraint in
place). Deliberately a specific `'daybreak'` kind, not a generic
`'system'` bucket for "any future non-actor announcement" — matches
round 40's own stated convention of widening only for what's actually
needed now, not speculatively for a future use case that doesn't exist
yet. (Found and fixed in passing while touching this: the Python-side
`MessageKind` `Literal` had already drifted out of sync with the DB —
missing `"nick"`, which round 41 added to the schema but never back-
ported to the type hint. Fixed alongside adding `"daybreak"`, not
tracked as a separate round.)

**3. Only channels with a live participant get the announcement.**
Thiesi's own explicit ask ("channels which have at least one person
joined at midnight") — `ChatHub` has no direct "every channel with
someone in it" query, so this cross-references every existing channel
(`list_channels`) against `hub.participant_count(name) > 0` per name,
the same pattern `netbbs.net.chat_flow`'s own per-channel hub lookups
already use. A dormant channel with dozens of stale scrollback entries
never gets one, by design — nobody's there to read it, and it would
just be noise on the next actual visit.

**4. `netbbs.timeutil.get_node_timezone(db) -> ZoneInfo` factored out
as a small new public helper.** `format_for_display` already resolved
the node's configured timezone internally, but only as a step toward
formatting one specific already-known instant — nothing needed the
actual `ZoneInfo` object for date/time *arithmetic* until this round
(computing "when is the next local midnight" needs real timezone-aware
datetime math, not just a formatted string). Left `format_for_display`
itself untouched rather than restructuring its internals to share this
new helper — a few lines of harmless duplication versus touching
well-established, already-tested display-formatting code for a
refactor with no behavioral benefit.

**5. Scheduling: computed directly from the target date, not a fixed
24-hour sleep-and-repeat.** `_seconds_until_next_local_midnight`
constructs the *target* midnight datetime directly with the current
timezone's `tzinfo`, rather than adding a flat `timedelta(days=1)` to
"now" — the former stays correct across a DST transition
(`zoneinfo.ZoneInfo` resolves the correct UTC offset for the
constructed wall-clock date), the latter would silently drift by an
hour on a DST-transition day. `now`/`sleep` are both injectable
parameters on `run_daybreak_announcer`, matching
`netbbs.net.throttle.LoginThrottle`'s own `clock` injection precedent —
needed here specifically because this is the first node-lifetime
background task in the codebase with no existing precedent to test
against (confirmed by grepping every `asyncio.create_task` call site
in `src/netbbs`: every other one is per-session/per-connection, not a
standalone loop running for the whole node's lifetime).

**Left as explicitly out of scope:** no Link-relayed variant of this
event exists or is planned — see round 77's own note for why that's
the deliberate, permanent boundary, not a placeholder pending Phase 3.
No per-channel opt-out/config toggle was requested or added; every
channel with a participant present gets the announcement unconditionally.

## Sign-off notes, round 79 (pinned chat input row)

Thiesi asked to build round 77's other discussed-not-decided item: a
fixed input row near the status line, mirroring a Claude-Code-style
layout. Scoped in detail before implementation (per this project's own
"design before code" convention) because a real investigation surfaced
that the valuable version of this feature is materially bigger than it
looks — confirmed with Thiesi via `AskUserQuestion` before writing any
code, same as rounds 75/77's own UX forks.

**1. The real problem, found before scoping could even start.**
`_chat_loop` has always been the *only* screen in this codebase with
two concurrent tasks (`send_loop`/`receive_loop`) reading and writing
one session at once — confirmed by checking every other `asyncio.
create_task` call site in `src/netbbs/net`. That concurrency already
had an accepted, documented gap: an incoming message arriving mid-
keystroke could visually corrupt a user's in-progress typing, because
`netbbs.net.char_input._read_line_editable`'s `line`/`cursor` state is
a private local variable with no visibility outside that one call
frame, and nothing serializes `send_loop`'s per-keystroke writes
against `receive_loop`'s. Pinning the input row to a fixed screen
position *without* fixing this would just move the corruption to a
spot the eye expects to stay stable — worse, not better. So this round
is really "make concurrent chat I/O safe" with "give input a fixed
row" as the visible result, not the other way around.

**2. Chosen mechanism: a shared `live_buffer` + `lock`, not a full
merge of `send_loop`/`receive_loop` into one task.** Considered and
rejected the architecturally "purest" fix (one unified loop
multiplexing keystrokes and incoming broadcasts via `asyncio.wait`) as
the more invasive option. Instead: `netbbs.net.char_input.
LiveInputBuffer` is a small dataclass (`text`, `cursor`) that `read_line`
now keeps refreshed once per keystroke (in a `finally`, so it happens
regardless of which of several `continue`/`break` branches fired) —
readable from outside without exposing the whole edit loop. An
`asyncio.Lock`, threaded through the same `read_line`/`_read_line_editable`
call as a new keyword-only `lock` parameter, wraps each keystroke's
own writes as one atomic critical section; `receive_loop` (and
`send_loop`'s own post-command redraw) acquire the *same* lock before
touching the screen, so the two tasks' writes can never interleave.
Both parameters default to `None` — a complete no-op for every one of
the 40+ other `read_line()` call sites in the codebase, none of which
need this. `netbbs.net.web.WebSession`'s own separate `_read_line_editable`
reimplementation (round 25: a browser delivers decoded characters, not
raw bytes, so it can't share `char_input`'s byte-oriented reading)
needed the identical mirrored change, to keep the pinned row behaving
the same way over web as over Telnet/SSH.

**3. Layout, confirmed via `AskUserQuestion` mockups before
implementing:** input row *above* the status line (the status row's
own fixed target, `session.terminal_height`, is completely unchanged —
only the scroll region's upper boundary and the new input row are
additions), with a visible `"> "` prompt marker now that a full redraw
happens on every update anyway. Reserved rows went from 1 to 2;
`_STATUS_LINE_MIN_HEIGHT` (round 75) is renamed `_PINNED_UI_MIN_HEIGHT`
and raised from 2 to 3 — one combined gate for both pinned rows, not
two independent minimums, since there's no sensible state where one
exists without the other.

**4. Positioning is unconditional, not incrementally tracked.** Every
write into the scrolling content region (an incoming broadcast, or this
session's own command/message output) jumps straight to the scroll
region's bottom row before printing, rather than tracking how "full"
the region currently is — no such row-position bookkeeping exists
anywhere else in this codebase, and DECSTBM's own auto-scroll-at-the-
bottom-margin behavior makes it unnecessary: newest content always
lands adjacent to the pinned input box, which is the actually-expected
behavior once there's a fixed anchor to grow from (a deliberate,
arguably-improved change from the old "fills from the top" behavior
the status-line-only version had, not a regression — there was no
fixed anchor to be adjacent *to* before this round).

**5. A very long in-progress line is truncated, not horizontally
scrolled.** `_repaint_input_row` uses the existing `truncate()` helper
(same one the status line already uses) rather than building real
horizontal-scroll logic — an accepted simplification for what's
expected to be a rare case; the cursor is left at the end of the
truncated view rather than at its true position when that happens.

**6. Nested reads were checked for and confirmed absent.** Wrapping
`send_loop`'s entire per-command dispatch in the shared lock (so
`_repaint_status_line`, unchanged, correctly inherits that protection
without needing its own lock parameter — nesting an `asyncio.Lock`
acquire inside itself deadlocks, since it isn't reentrant) is only safe
because no chat command handler ever calls `session.read_line`/
`read_key` a second time from inside its own dispatch — verified
directly by grepping the whole file for a second `read_line`/`read_key`
call site before relying on it, not assumed.

**Left as explicitly out of scope:** true horizontal scrolling for an
oversized in-progress line (point 5); a fully merged single-task event
loop (point 2) — worth revisiting only if the lock-based approach turns
out to have some real problem in practice, none identified so far.

## Sign-off notes, round 80 (daybreak task failure policy — decided while fixing GitHub issue #48)

An independent reviewer's audit of round 78's daybreak announcer (see
`NetBBS-worklog.md` round 80 for the full five-issue fix writeup) found
that an unhandled exception in that node-lifetime background task went
unobserved and could later block shutdown cleanup — not itself a design
question, but fixing it required picking one of three genuinely
different policies for **what a background task's failure should mean
for the rest of the node**, which hadn't been decided anywhere before
now.

**Decided: graceful degrade — log the failure immediately, keep serving,
the feature stays dead for the rest of that node's uptime.** Two
alternatives were considered and rejected:

- **Fail-fast** (a background task's failure triggers full node
  shutdown) — rejected because the daybreak announcement is purely
  cosmetic and strictly local-only (round 77/78's own sign-off note);
  taking every listener down because a "good morning" message generator
  broke would be a wildly disproportionate blast radius for what a
  solo SysOp would actually want.
- **Auto-restarting supervisor** (catch, log, restart after a bounded
  delay) — rejected as more machinery than this ancillary a feature
  warrants: a real implementation needs its own backoff and eventual
  give-up policy for a repeatedly-crashing task, and nothing about this
  feature's importance justifies building that now, speculatively, for
  a failure mode with no evidence of actually recurring.

Graceful degrade fully addresses both real complaints the issue raised
— "silently dead" (fixed: a done-callback logs it, by name, the instant
it happens, not just eventually at shutdown) and "masks the real
shutdown reason" (fixed: the `finally` block's listener-stop/db-close
sequence can no longer be skipped by an ancillary task's exception) —
without inventing supervision machinery this codebase has no other
precedent for. Should another node-lifetime background task get added
later with a stronger claim to node-critical status, that one can
reasonably choose fail-fast instead — this decision is scoped to what
"a background task's failure means" *for tasks no more important than
this one*, not a blanket policy for every future one.

## Sign-off notes, round 82 (self-update mechanism)

Prompted by Thiesi requesting a SysOp-facing self-update feature ahead
of Phase 3, motivated by wanting future NetBBS Link protocol changes to
roll out to existing nodes with minimal friction.

1. New **§17 Self-update mechanism** added in full — three trigger
   points (startup/manual/daily-background) with different apply
   behavior, self-exec-based restart with no new supervisor component,
   rollback-on-failed-start, and a `node_config`-based off switch for
   the automatic path only.
2. **pkgsrc conflict — flagged, then deliberately deferred rather than
   designed around.** §3 names NetBSD/pkgsrc as the primary deployment
   target, and a self-updater that rewrites files pkgsrc believes it
   owns will desync pkgsrc's manifest from what's actually on disk,
   breaking future `pkgin` upgrades/deinstalls on that target. Raised
   directly with Thiesi, including the option of disabling self-update
   under a detected pkgsrc install; **Thiesi chose to ignore the
   conflict for now** and revisit it if/when a pkgsrc package is
   actually built, rather than design around a packaging path that
   doesn't exist yet. Recorded here so this isn't rediscovered as a
   surprise later — it's a known, accepted gap, not an oversight.
3. **Trust model: HTTPS + GitHub API only, no release signing —
   confirmed with Thiesi over the stronger alternative.** Raised
   directly as the higher-stakes of the two open questions: an
   auto-updater is the highest-leverage attack surface a network-facing
   server has, and this project's own §6 exists specifically because of
   a past incident where one privileged construct (the Master Node)
   became a single point of compromise with network-wide reach — an
   unsigned update channel risks recreating that same shape (one
   compromised GitHub account or MITM'd connection, pushing arbitrary
   code to every node) even though nothing here is Link-facing. The
   project already has keypair/signing infrastructure (§5, §11) that
   could underwrite a signed-release scheme. **Thiesi opted for the
   simpler HTTPS-only model anyway**, deliberately, as a considered
   tradeoff rather than an unconsidered gap. Automatic rollback on a
   failed start (§17) is the direct mitigation adopted in exchange —
   doesn't prevent a malicious release from running, but bounds how
   long a *broken* one stays live. Revisiting this with real signing
   remains straightforward later: the release pipeline and the node's
   verification step are both new, isolated code with no existing
   behavior depending on the absence of signatures.
4. **Restart mechanism: in-place self-exec, not an external supervisor
   process — confirmed.** Considered and rejected the watchdog-process
   pattern (main process restarted by a separate long-running
   supervisor on a specific exit code) as more machinery than justified
   for a solo-maintained project with no other supervisor-shaped
   component anywhere else in the design.
5. **Link protocol-version awareness — explicitly out of scope for this
   round.** The feature's real motivation (easing future Link protocol
   rollouts) can't be fully designed yet since Phase 3+ (where the Link
   protocol lives) hasn't started; §17 scopes the updater as
   protocol-agnostic plumbing only, leaving actual compatibility
   semantics to whichever future round builds the Link handshake.
6. **Phase placement: after Phase 2, before Phase 3 — confirmed.**
   Matches Thiesi's own framing ("towards the end of initial
   development... when we are feature complete"); also has the
   practical benefit of the update channel already existing by the time
   Phase 3 might need to ship a protocol-version bump. §15 updated with
   a pointer to §17, alongside the existing Communities pointer to §16.

## Sign-off notes, round 83 (Communities — full spec, following round 71's directional decision)

Prompted by Thiesi wanting Communities designed and landed before Phase
3 starts, so Link/trust/chat work isn't built against a data model that
still might change underneath it. Worked through as six genuinely
separable sub-questions, per §16's own "deliberately not decided yet"
list, rather than attempted in one pass.

1. **Data model: zero-or-one cardinality — confirmed over two
   alternatives.** A board/channel/file-area belongs to at most one
   Community. Rejected *exactly-one-with-a-default-bucket* (simpler, but
   makes "Uncategorized" a fictional Community rather than a real
   topic) and *many-to-many* (most accurate to real overlap, but has no
   clean answer for conflicting inherited defaults or for whether a
   multi-Community resource should be Link-carried on *any*-opted-in or
   *all*-opted-in). §16 updated with the full reasoning.
2. **Round 18's existing two-level category system stays exactly as
   designed — confirmed, not rediscovered as a conflict.** Communities
   sit as a new outer layer above categories, the same relationship
   they already have to boards/chat/files themselves; a Community's
   board list is round 18's picker pre-filtered to that Community. This
   resolved two of the six sub-questions for free: the uncategorized
   bucket is the same picker filtered to `community_id IS NULL`, and
   jump-shortcuts are the same `search`/`goto` machinery (round 18
   point 2's stable-ID/sort-order decoupling) pointed at an unfiltered,
   cross-Community list — both reuses of existing infrastructure, no
   new picker mechanism designed this round.
3. **Permission inheritance splits into two mechanics — confirmed.**
   Scalar defaults (level-gates, presentation) follow the existing
   override-then-fall-back resolution order from round 8/9's
   display-timestamp config, with a Community's default never enforced
   as a floor a child resource can't loosen past (flagged as a possible
   later addition, not designed now). Moderator grant authority gets a
   genuinely new **Community-blanket tier**, added to §13's moderator
   scope tiers (now four, was three) — confirmed with Thiesi as a
   natural extension of the existing "blanket = automatic over a
   category's membership" pattern rather than a new kind of grant.
4. **Migration path: confirmed as a non-event.** Direct consequence of
   decision 1 — a nullable `community_id` column means every existing
   board/channel/area defaults to `NULL` on upgrade, with no forced
   categorization pass. No migration wizard designed or needed.
5. **Phase placement: local Communities after Phase 2, before Phase 3
   — confirmed, no renumbering of the existing 7 phases.** Same
   addendum-pointer treatment as round 82's self-update mechanism,
   for the same reason (the 7-phase numbering is referenced by number
   throughout this document, so inserting a new numbered phase would
   invalidate those references for no real benefit).
6. **Link Communities explicitly separated out as Phase 6 scope, not
   this round's.** Federated Community creation, default-carry-with-
   visible-opt-out, and cross-node discovery depend on the same
   signed-event/DAG governance machinery Linked board/channel creation
   already uses, which doesn't exist before Phase 6. This round
   specced and phased *local* Communities only; the Community-blanket
   moderator tier (point 3) is likewise local-only for now, extended to
   Link Communities whenever Phase 6 wires up real signed grants for it.

**Deferred, not decided this round:** whether a Community-level scalar
default should be enforced as a hard floor/ceiling on its children
(point 3); a from-anywhere typed jump/search command beyond the
main-menu entry point (would touch the shared command-dispatch layer
more broadly than this round's scope). Both flagged in §16 so they
don't need rediscovering later.

## Sign-off notes, round 84 (Communities navigation UI, admin-side tooling, and a scalar-default bug fix)

Prompted by Thiesi wanting the uncategorized-bucket UI and admin-side
Community tooling detailed enough that the build phase can proceed
without design interruptions. Grounded in the real implementation
(`netbbs/net/picker.py`, `netbbs/net/login_flow.py`,
`netbbs/net/admin_flow.py`, `netbbs/boards/categories.py`) rather than
designed in the abstract, since Phase 1/2 code already exists here.

1. **Main-menu structure: `[M]/[C]/[F]` replaced, not kept alongside
   Community-first navigation — confirmed with Thiesi over two
   coexistence alternatives.** Matches §16's original round-71 pitch
   ("instead of a main menu offering `[M]essage Boards / [C]hat /
   [F]ile areas`...") literally rather than hedging it. `[E]nter a
   Community`, `[U]ncategorized`, and `[J]ump to...` take their place,
   both of the first two conditionally visible using the exact
   conditional-visibility pattern `[I]nvitations` already established
   in this codebase — new UI concept avoided by reuse, not invented.
2. **Bug caught before it shipped: `[B]oards` collides with `[B]ack`
   in the new per-entry-point sub-menu.** Fixed by reusing the original
   `[M]/[C]/[F]` letters one level deeper rather than picking new ones
   — a side benefit is that veteran muscle memory isn't fully lost, just
   relocated one screen in, softening the disruption from point 1.
3. **Categories (round 18) stay schema-unchanged — confirmed with
   Thiesi over adding a `community_id` column to them.** The
   alternative (making categories themselves Community-scoped) would
   have been a real change to "unchanged" categories for no clear
   benefit; a community-scoped *existence filter* at the query layer
   achieves the same leak-prevention property without touching
   `board_categories`/`channel_categories` at all.
4. **Jump-shortcuts scoped to one resource type per use, not a unified
   cross-type search — confirmed as a deliberate scope-reduction, not
   an oversight.** A true unified search needs a new multi-type
   stable-ID scheme beyond today's two-space (category vs. resource)
   negation trick; the per-type version is a strict, non-foreclosing
   subset, so it's what ships now.
5. **Admin-side Community management mirrors existing board/category
   admin patterns exactly, including a real letter-disambiguation
   precedent already in the codebase.** `[O]Communities` follows the
   same rule that produced `[H]annels` (next free letter once
   Categories claims "C"); create/edit split follows boards' own lean-
   create/rich-edit shape; Community assignment reuses
   `_pick_optional_category`'s exact structure via a new
   `_pick_optional_community` helper, ordered before category
   assignment since Community is the outer layer.
6. **Community-blanket moderator grants extend the existing
   local-blanket `X`/`Y`/`Z` keys with one optional follow-up question,
   rather than adding new scope letters or a parallel preset
   vocabulary — confirmed as the minimal-surface-area option.**
   Mechanically one new nullable `community_id` column on the existing
   grants table.
7. **Deletion cascade specified:** referencing resources revert to
   Uncategorized (the reverse of assignment), scoped Community-blanket
   grants are revoked outright rather than left dangling, and the
   confirmation prompt shows the blast radius (affected resource and
   grant counts) before committing.
8. **Bug found and fixed in round 83's own design, not just new
   scope:** `min_read_level`/`min_write_level` shipped as `NOT NULL
   DEFAULT 0`, which silently defeated round 83's own "resource's own
   explicit value wins if set, else Community default" resolution
   order — every existing resource already has an explicit stored `0`,
   so no pre-existing resource could ever actually inherit a Community
   default. Fixed by making these fields (and the new `min_age`/
   `name_requirement` from round 85) nullable, with `NULL` meaning
   "inherit" and an explicit value — including `0` — always winning.
   Existing resources keep their current explicit values unchanged on
   migration; opting one into inheritance requires a SysOp to
   deliberately clear it. Caught while designing round 85's age-gating
   resolution order, which forced the same NULL-vs-explicit-zero
   question to be answered precisely for a second field and exposed
   that round 83 had never actually answered it for the first.

## Sign-off notes, round 85 (age-gating, real-name-gating, and identity attestation)

Prompted by Thiesi asking whether Communities should support
age-gating alongside the already-designed level-gating.

1. **Discovered, not assumed: §13's existing "channels support
   minimum-age gating" claim was never implemented anywhere** — no age
   field on `users`, no `min_age` on `channels`, and its own citation
   ("§5's user model") doesn't define an age concept either. Verified
   directly against the actual schema/code before designing anything
   further, rather than building Community age-gating on top of a
   premise that turned out to be false. §13 corrected to point here.
2. **Scope: full symmetric build across users, boards, channels, areas,
   and Communities — confirmed with Thiesi over two narrower
   alternatives** (channels-only matching the original false claim's
   scope; deferring age-gating entirely). Chosen specifically so a
   Community's cascading default isn't a half-feature that only ever
   reaches some of its children.
3. **Birthdate, not birth-year — corrected after Thiesi identified a
   real correctness bug in the first proposal, not just an imprecision
   concern.** `current_year − birth_year` is systematically biased
   toward *overestimating* age for anyone whose birthday hasn't yet
   happened that year — the unsafe direction for a safety gate. Worse,
   storing any derived "current age" (self-reported or attested) freezes
   it at write time with nothing to ever re-evaluate it, so a verified
   17-year-old would never be recognized as 18 without manual
   re-attestation. Fixed by storing a full birthdate and computing age
   fresh via real date-math at every check, both self-reported and
   attested — self-correcting forever, same computed-at-read-time
   philosophy as round 8/9's timestamp handling, just not applied here
   the first time until Thiesi caught it.
4. **`meets_age` fails closed on missing user data, deliberately
   asymmetric with level-gating's permissive default.** A resource's
   own `min_age` still defaults to no-gate, same as `min_level` — but a
   user with no usable birthdate (self-reported or attested) does not
   pass a gate that is actually set, since treating "unknown" as "old
   enough" would defeat the gate's purpose.
5. **Identity attestation mechanism adopted from Thiesi's own proposal**
   — real-world age/name verification, performed by whatever means a
   SysOp-delegated verifier judges sufficient for their own jurisdiction
   and community, recorded as a signed claim rather than a bare flag.
   Explicitly framed by Thiesi as outsourcing a policy question NetBBS
   structurally can't answer globally (who is a minor, what counts as
   restricted content) to whoever is actually locally accountable.
   Signing reuses round 7's node-vouching fallback unchanged, applied to
   a new claim type rather than new infrastructure — a direct
   consequence of already having keypair identity (§5) and a
   Linked-board-grant-shaped signed-event precedent (§13) to build on.
6. **`can_verify_identity` is a new, narrow, SysOp-grantable permission
   independent of §13's four moderator tiers — confirmed with Thiesi
   over folding it into the existing moderator-grant flow.** A plain
   per-user boolean with no scope tiers, since verifying a real-world
   fact about a person isn't authority over any specific board/area/
   channel and can reasonably be granted to someone with no other
   moderator role at all. Gets its own conditionally-visible main-menu
   entry (`[V]erify`) rather than living inside the admin menu, since a
   granted verifier may not have admin access otherwise.
7. **Real-name-gating designed this round, not deferred — confirmed
   with Thiesi over noting it as a future extension only.** Small
   incremental addition given the attestation mechanism is already
   shared; modeled as a three-state `name_requirement`
   (`none`/`verified`/`verified_and_displayed`) rather than two
   independent booleans, so "displayed but not verified" is impossible
   by construction rather than requiring separate validation. Unlike
   age, there is no self-reported fallback — an unverified `display_name`
   never satisfies this gate, since the entire point is verification.
8. **Real name never overwrites display name; the two always coexist —
   resolved by direct analogy to §8's existing `/nick` principle**
   (alias is presentation metadata, canonical identity stays visible
   alongside it, never replaced by it). Raised by Thiesi as a concrete
   scenario (a user's chosen display name differing entirely from their
   verified real name) rather than an abstract concern.
9. **Display formatting: primary slot is always self-chosen, real name
   is always parenthetical — refined twice from Thiesi's own follow-up
   questions.** First pass (`display_name (real name)`) broke when
   `display_name` was unset, collapsing to an orphaned `(real name)`
   with nothing leading it. Reversing the order was considered and
   explicitly rejected by Thiesi: `display_name` is chosen, the real
   name isn't, so leading with the involuntary name would misrepresent
   how someone wants to be known — the same shape of harm as
   misgendering, in Thiesi's own framing. Resolved by falling the
   primary slot back to `username` (also self-chosen, just less
   expressive) instead of to nothing, since username is guaranteed
   present as one of only two required signup fields — never reorders,
   never collapses to an orphaned parenthetical.
10. **Link propagation of attestations deferred, and specifically gated
    on Phase 4, not Phase 3 — a different reason than Link Communities'
    Phase 6 gate, worth distinguishing rather than reflexively citing
    "Link phase" for every Link-adjacent deferral.** Phase 3's
    signed-event sync alone isn't sufficient — a remote node would be
    trusting an attestation's signature authenticity while having no
    basis to judge the signer's credibility, exactly the "trust the
    signature, not the signer" gap §6's web-of-trust system exists to
    close. Phase 4 (trust/reputation) is the actual prerequisite. Even
    once available, propagation additionally requires the *subject
    user's* own opt-in consent (`link_visible`, defaults `false`),
    consistent with existing profile-privacy philosophy (§13) rather
    than assuming Link-sharing sensitive verification data by default.
11. **New voluntary signup fields (`display_name`, `location`,
    `birthdate`) confirmed as distinct from the existing chat-only
    `/nick` alias** — round 41 deliberately kept `/nick` out of the
    directory; this round's `display_name` is a directory/vCard-level
    field and doesn't revisit or conflict with that separation.
    `location` deliberately stays free-text and coarse, no structured
    fields forcing city/region/country precision, on the same
    minimal-disclosure reasoning applied to attestation Link-visibility
    in point 10.

## Sign-off notes, round 86 (Link Communities — directional design)

Prompted by Thiesi wanting Link Communities designed properly, following
the same "before Phase 3, but scoped to what can actually be settled
without Phase 3's real substrate" pattern already applied to local
Communities, self-update, and identity attestation.

1. **Design depth: directional only, confirmed with Thiesi over two
   deeper alternatives** (a full implementation-ready spec including
   signed-event wire format; widening scope to design Phase 3's general
   signed-event substrate first). Chosen because no Phase 3 code exists
   at all (verified directly — no Link/DAG/gossip modules anywhere in
   the tree) and §7's DAG/gossip description remains conceptual, with no
   concrete event schema anywhere in the doc yet to build Community-
   specific mechanics on top of. Matches the exact level of detail §13
   already gives Linked-board moderation ("design direction already
   settled... actual implementation Phase 3 scope") rather than
   introducing a new, deeper standard just for Communities.
2. **Not a separate object type from local Communities — confirmed.** A
   Community becomes Link-participating via the same signed-announcement
   mechanism §13 already specifies for Linked board/channel creation,
   applied unchanged rather than designing a parallel mechanism. Direct
   consequence of round 83's decision to model Community's own
   origin/grant authority identically to a board's from the start.
3. **Identity: content-addressed and origin-scoped (§7's existing
   scheme), not a new naming system.** Avoids a global-namespace
   collision problem without inventing anything — two independently-
   created same-named Link Communities simply coexist, the same
   non-collision property boards and messages already have.
4. **New: promotion of an existing local Community, not just
   from-scratch creation.** A gap in the original framing — round 83's
   admin design only covered creating a Community, silent on an
   existing one later going Link-participating. New `[L]ink` admin
   action added to the Community detail screen, reusing the same
   signed-announcement mechanism as creation.
5. **Carry decisions: composes with the existing per-resource opt-out;
   adds a Community-level bulk exclude as a new convenience.** No
   change to the existing board/channel-level default-carry-with-
   visible-opt-out mechanism (§15) — a Link Community's carry decision
   is additive on top of it, not a replacement.
6. **Moderator grants: zero new design, confirmed.** Community-blanket→
   Link-blanket is §13's existing Linked-board-moderator-grant mechanism
   applied to a Community instead of a board — the mechanism was already
   fully specified in round 4/5, before Communities existed as a concept
   at all.
7. **Cascading scalar defaults: carrying node's local override always
   wins over the origin's default — confirmed, consistent with every
   other Linked-content sovereignty decision already made** (expiry
   remains local even for Linked boards, §13; a SysOp can exclude any
   specific board, §15). A Link Community's own cascaded defaults are a
   suggestion carried across the Link, never a mandate.
8. **Correction to round 85's own framing: the Phase 4 dependency for
   age/name-gating is narrower than originally stated.** §18 previously
   read as if any Link-Community-wide age/name-gating needed Phase 4.
   Actually only the cross-node case does (node A trusting node B's
   attestation about a user A doesn't manage) — a carrying node
   enforcing its own local attestation data against its own local users
   works from Phase 3 onward. §18's Phase placement section corrected to
   state this precisely rather than the broader, less accurate version.
9. **Discovery: no new mechanism, confirmed.** Link Community
   announcements are just another event kind in Phase 6's already-
   designed governance log/activity feed (§15 Phase 6) — this was
   already general enough to cover it without modification.

## Sign-off notes, round 87 (independent design-doc review, GitHub issues #11/#51-63 — resolutions and direct doc fixes)

An external reviewer audited a pre-round-82 snapshot of this document and
opened/reopened 14 GitHub issues (#11, #51–63). Claude assessed each
against the *current* document (post round-86), posted a per-issue status
comment distinguishing "already resolved," "partially resolved," and
"still fully open," and Thiesi replied on each with binding decisions.
This round records those decisions and makes the direct text fixes they
called for. Full comment threads are on the issues themselves; this note
is the durable summary.

**1. Canonical event format (#11) — staged-gate resolution, not full
resolution.** Round 27's placeholder deferral was confirmed reasonable
for Phases 1–2, not an undiscovered gap. Going forward, a three-stage
gate applies rather than an all-or-nothing freeze: (a) work that doesn't
depend on final event shape — most of #57's DB-execution-model design,
most of #58's WAN/NAT design — may proceed now; (b) the **semantic**
protocol model (event taxonomy, envelope fields, identity references,
immutable-object-vs-mutable-projection rules, replay/tombstone
semantics, version/capability negotiation) must be settled before any
wire-visible persistence or endpoint is implemented; (c) **exact**
canonicalization and signed golden vectors must be settled before any
real Phase 3 code emits an ID or signature. Three additional protocol-
state concerns surfaced during the reopened review and are now folded
into #11's scope for whenever that semantic-specification work happens:
deterministic effective-state/projection rules distinct from immutable
content-object identity; replay-safety after local dedup-table/retention
cleanup, distinguished from moderation tombstones and storage pruning;
and peer capability/version negotiation with defined unknown-event/
downgrade behavior. #11 stays open as a gate before wire-visible
implementation; it does not block the orthogonal Phase 3 prep in #57/#58.

**2. Identity/authority/operations cluster (#51, #53, #60) — one
coherent design item, explicit dependency order.** Confirmed: #51 is the
normative root (stable identity, which operational keys it authorizes,
rotation/revocation/recovery, historical-signature verification survives
rotation); #53 applies those primitives to Link-resource authority
(ownership transfer, succession, orphaning, forks — must not invent a
second key-transition model); #60 operationalizes the result (backup,
restore, disaster recovery preserving the identity model rather than
defining it accidentally through whichever files happen to get copied).
Concretely: a root/recovery key normally kept offline, replaceable
online signing/transport keys, transition records living in event
history, and a restore procedure that cannot clone one identity into two
simultaneously-active nodes. To be designed together (one round or a
tightly cross-referenced set), not as three independent passes. None of
#51/#53/#60 is resolved by this round — the ordering and shared
constraints are.

**3. Link resource lifecycle scope (#53) — narrowed, not closed.**
Agreed §13 already supplies a defensible conceptual answer for genesis/
origin/carry/closure (see round 5 onward) — the issue was overclaiming
"undesigned" for that part. #53's real remaining scope: which minimum
signed lifecycle machinery moves from Phase 6 into Phase 3 so a Linked
resource can exist without inventing temporary authority rules (now
stated directly in §15's Phase 3 and Phase 6 entries); origin succession/
voluntary-transfer/total-loss/compromise/orphan/fork policy, built on
#51's key-transition primitives once those exist; reconciling the
original carry-every-Linked-resource default with the newer Link-
Community carry model (§16 round 86 already establishes Link-Community
carry as additive, not a replacement — cross-referenced from #53 rather
than re-litigated); and explicitly deferring exact event-envelope/
projection mechanics to #11 rather than duplicating them. Suggested
retitle, not yet applied to the tracker: "Place minimum Link resource
lifecycle in Phase 3 and define origin succession/forks."

**4. Communities domain model (#54) — superseded, recommend closing.**
Rounds 83/84/86 resolved cardinality (zero-or-one), permission/
moderation inheritance (nullable-scalar-defaults-plus-Community-blanket-
tier), personal/system surfaces staying outside Communities, migration-
by-nullability, and phase placement (local before Phase 3, Link wire
details in Phase 6). One deliberate divergence from the issue's original
recommendation, confirmed as intentional rather than an oversight: a
child resource may explicitly *loosen* a Community default rather than
the Community acting as a mandatory floor — coherent for a node-
sovereign system where the carrying SysOp remains final policy
authority, and already described in §16 as an intentional override, not
silently. The one genuinely live point — a per-user follow/favourite
relationship distinct from resource membership and node carry — is
folded into #56 rather than kept open here.

**5. Unread/follow/activity state (#56) — kept open, absorbs #54's
remaining point, four concepts kept explicitly distinct.** None of the
following may silently imply another: **follow/favourite** (a user
preference about what gets surfaced prominently), **read marker**
(consumption state for a specific ordered stream/resource), **membership/
access** (authorization), **node carry** (SysOp-level federation/storage
policy). Following a Community must not grant access; membership must
not force follow/notification state; a user following something must not
cause the node to carry a Link Community. Phase 6's Link activity feed
may remain deliberately non-unread-tracked (a live "tail -f," not every
surface needs the same UX) — #56's job is to define which surfaces
participate in the new-activity model and which explicitly opt out, not
to force uniformity.

**6. Roadmap dependency gates (#61) — addendum-slot pattern kept, three
gate classes adopted, doc updated directly.** Confirmed the existing
"after Phase 2, before Phase 3" addendum-slot pattern (already used for
Communities/self-update/identity-attestation) is a fine mechanism —
Thiesi does not want the established phase numbering fought over for its
own sake; the actual concern was dependency *order*, not the `3A`/`3B`
labels. §15 now states directly: Phase 3 is explicitly
**private/experimental federation**, Phase 4 is the explicit **public-
federation-readiness gate**; Phase 3's protocol-foundation gate (#59, #57,
#11's semantic portion, #51) is distinguished from its feature-specific
gates (#52 before Link messages, #53 before Linked-resource lifecycle,
#58 before real deployment); remote file-area discovery/download moved
from Phase 5 into Phase 3 (it's async HTTP-service traffic, not
real-time-chat traffic, so it never actually depended on Phase 5's Noise
transport); Phase 7's threading-refinement bullet is now marked
explicitly UI-only, with the structural requirement (any ID/propagation-
affecting threading semantics must land in Phase 3, before Linked boards
ship) stated in Phase 3's own entry instead. #56 (unread/follow/
activity) is confirmed as important product work that does *not* block
starting DAG/transport implementation, provided event ordering gives it
stable markers to build on later — it may proceed in parallel rather
than gating Phase 3 entry.

**7. Document-normalization fixes (#62) — six confirmed contradictions
fixed directly in this round; the multi-document split proposal
deferred, not adopted.** Fixed in place this round:
- Title no longer claims "draft for review" while the status line calls
  the architecture confirmed and substantially built (now "v0.2, current
  architecture").
- §13's SysOp-vs-global-moderator origination contradiction resolved by
  distinguishing **initiating creation** (global moderators and the
  SysOp) from being the **signed origin authority** (always the node
  identity) — see §13's revised privilege-separation note. The initiating
  human actor is recorded for audit; initiating creation does not grant
  the ability to appoint further blanket moderators, alter node
  configuration, or govern pre-existing resources.
- "Linked boards/channels" vs. "Link boards/chat" terminology drift
  resolved in favor of the document's actual, overwhelming usage (15+
  occurrences), not the round-71/§16 claim: "Link" prefixes *named
  features* (Link message, Link Community, NetBBS Link itself, Link-wide
  chat/presence as a scope description); an ordinary resource merely
  *participating* in the Link keeps the adjective form (linked board,
  linked channel, linked file area, §1). Round 71's original text is left
  as historical record per this project's sign-off-note convention
  (corrected forward, not rewritten); §16 now carries an explicit
  correction note pointing here.
- "Same content available on any node" reworded from "guarantee
  automatically" to "default availability/behavior" in §13 — agreed not
  a substantive contradiction (the opt-out was already explained in the
  same breath) but a needlessly absolute word for something with a
  documented exception.
- File-chunk transport clarified in §11: chunks/contents share the
  HTTP+JSON-signed transport family with boards/messages but are **not**
  part of §7's DAG/gossip flood-fill — only file-area catalogue/
  descriptor events participate in Link gossip, matching §9's on-demand
  model. Sharing a transport family does not imply sharing a gossip
  policy; the exact chunk wire format remains #11/Phase 3 scope.
- §16's "thin" wording replaced with "coordination/container object above
  the resource packages" — the behavior was already correct (not a
  unified content type), "thin" was simply the wrong adjective once
  Community-blanket moderator scope and Link carry-decision authority
  existed.
- Account lifecycle promoted from scattered sign-off-note-only status
  into a new normative §5 subsection, cross-referencing #51 for the
  cryptographic-key lifecycle that remains genuinely open (deliberately
  kept separate: account state is settled, key-lifecycle semantics are
  not).

**Deferred, not adopted:** the five-way document split (architecture /
ADRs / worklog / open-decisions register / protocol spec) proposed
alongside #62. Thiesi's stance: reassess after the direct fixes above,
since the worklog split already happened once for a similar reason and a
five-document shape may be finer-grained than a solo-maintained project
needs. If a split does happen later, the one piece Thiesi already expects
to want regardless: a standalone **normative Link protocol specification**
once Phase 3 begins — exact wire/state-machine rules don't belong mixed
into chronological sign-off prose the way they currently would. Until
then: current architecture doc (this file, with its existing sign-off-
note history), worklog, and issues/comments as the interim open-
decisions register.

**8. Door-game isolation (#63) — confirmed still fully open, no
objection.** §15's Phase 7 entry now states directly that the native
API's trust boundary/sandbox and versioning must be designed and proven
before Phase 7 implementation begins, and that DOS-door compatibility
should be a later adapter receiving the same constrained session-
capability set as the native API, not direct node access — matching
#63's recommended direction. No design work landed this round; this is a
sequencing note only.

**Not touched by this round, confirmed still fully open with no
disagreement registered:** #52 (local async mail / Link message
delivery — a real gap, the existing `/msg` feature is a different,
deliberately ephemeral thing), #55 (Link trust/quarantine threat model —
correctly sequenced to Phase 4, still undefined), #57 (non-blocking DB
execution model — the document already self-diagnosed this in round 30;
still no chosen design), #58 (WAN/NAT/seed-trust — still four lines in
§12, no design work done), #59 (deterministic multi-node test harness —
doesn't exist, correctly gated before Phase 3 substance).

## Sign-off notes, round 88 (follow-up review of round 87 — two factual corrections, one gate-wording refinement)

Thiesi reviewed commit `9edbd1d` against the actual codebase and posted
follow-up replies on #11, #61, and #62. Two were genuine factual errors
in round 87's new text, caught by checking the claims against
`netbbs.auth.users` rather than taking the sign-off note's own wording at
face value — exactly the kind of check this project's sign-off notes are
supposed to survive. Both are fixed directly in §5 this round, not left
as known errors.

1. **§5's registration bullet was wrong — confirmed against
   `netbbs/auth/users.py` and `netbbs/net/login_flow.py` (#62).** The
   original text said self-registration was "an opt-in path a SysOp can
   enable/disable per node." No such switch exists: self-registration is
   always reachable on Telnet/web/SSH. The actual node-wide setting,
   `require_registration_approval` (round 76, default off), only
   controls whether a self-registered account activates immediately or
   is created `pending_approval` and locked out of every auth path until
   a SysOp approves it. §5 now describes this precisely instead of
   conflating "gate approval" with "gate registration itself."
2. **§5's "active SysOp" lockout description was incomplete — confirmed
   against `count_sysops`/`_refuse_if_last_sysop` in
   `netbbs/auth/users.py` (#62).** Round 87's text said only disabled
   accounts don't count. The real invariant, fixed under issue #44 and
   restated verbatim in `count_sysops`'s own docstring, is **usable**:
   level ≥ `SYSOP_LEVEL`, not disabled, *and* not `pending_approval`. A
   pending level-255 row can't authenticate on any path, so it must not
   count toward the lockout guard any more than a disabled one does —
   §5 now states all three conditions, plus the promote-while-pending
   refusal that enforces it.
3. **§5's closing bullet on username/migration/retention was a
   deflection back to history, not a normative answer (#62) — replaced
   with the actual current behavior stated directly:** usernames are
   immutable (no rename code path exists anywhere in the codebase);
   account/node migration is undesigned, deferred to Phase 3-or-later
   work entangled with #51's key-lifecycle question; retained data after
   deletion is fully covered by the preceding `ON DELETE` bullet, so the
   new text points there instead of adding a third, redundant claim.
4. **§15's Phase 3 gate wording was too coarse — refined per #61's
   follow-up, not changed in substance.** The single "settle before any
   wire-visible Link-core implementation" bucket bundled together
   dependencies that don't actually block each other: a pure envelope
   type or handshake prototype doesn't create the same risk as
   continuous background sync/ingestion, which doesn't carry the same
   stakes as a deployed, externally-reachable node. Replaced with an
   explicit four-tier matrix (before wire schemas/signatures are frozen;
   before continuous background Link work; before the first end-to-end
   Linked feature is called complete; before deployment beyond a
   controlled local/private harness), with #59's test harness now
   explicitly understood to grow in lockstep with later features rather
   than needing to be fully built before anything else starts, and #60
   added alongside #58 in the deployment-readiness tier (it was missing
   from round 87's version).
5. **#11 — no doc change; confirmed the round 87 gate framing already
   matches Thiesi's intent.** Thiesi's reply reaffirmed the three-stage
   split (semantic model before wire-visible persistence; exact
   canonicalization before real IDs/signatures; golden vectors run
   through at least two independent serializers before claiming
   interoperability) and confirmed it doesn't need to block #57/#58.
   Recorded here only because the reply asked for the ordering to be
   preserved going forward, not because anything needed correcting.
6. **#62 disposition — Thiesi is comfortable closing it once these two
   corrections land**, since the direct-cleanup scope (not the original
   five-document restructuring, which was already dropped in round 87)
   is what's left of its acceptance criteria. Left open one more round
   rather than closed in the same breath as the fix, so Thiesi can
   confirm the corrected §5 text reads right before the issue is marked
   done.

No round 87 text was found wrong beyond the three §5 points and the
Phase 3 gate granularity above — the origination-authority rewrite,
terminology correction, "same content" wording, file-chunk clarification,
and Communities "thin" wording all held up against this follow-up
review.

## Sign-off notes, round 89 (node/user key lifecycle — resolves issue #51, UX-first redesign)

The first pass at this design (proposed as part of starting Phase 3
design work, before any of it was written into the doc) modeled node and
user identity identically: one root key plus purpose-specific
operational keys, transition records, the works — enterprise-PKI shaped.
Thiesi's pushback was the actual design driver here, and it's worth
recording precisely because it's a recurring risk for this project, not
a one-off: **a mechanism can be perfectly sound cryptographically and
still be the wrong design if it assumes a level of key-management
comfort the target audience doesn't have.** Thiesi's own framing: he
already runs Pageant on every machine and handles keys professionally,
but recently mentioned running Mystic on NetBSD (with Linux emulation)
on a Mystic community board and found people didn't recognize the
concept at all — evidence that even people actively running/using BBS
software today aren't necessarily technical in the way a sysadmin is.
Building identity infrastructure that assumes everyone is Thiesi would
have been a real mistake.

**Resolution: split by audience and stakes, not one identity model for
everyone.** Three tiers, written up in full in §5's new "Key lifecycle"
section:

1. **Password-only users (the default, expected majority): no personal
   keypair, no cryptographic concept exposed at all.** Represented on
   Link as an opaque, node-vouched local ID — this was already the right
   answer sitting unused in issue #11's own suggestion for password-only
   author identity; round 89's contribution was recognizing it should be
   the *default* shape for ordinary users generally, not a fallback case
   for people who merely skipped keypair setup. Rotation/revocation/
   recovery aren't concepts that apply here; it's the node's problem,
   invisible to the user.
2. **Opt-in personal user keypair, for people who already run a
   key/agent for other reasons (Thiesi's actual situation) and want to
   reuse it for passwordless login.** Single key, deliberately no root/
   operational split — a compromised personal key costs one person their
   session and reputation, not network-wide provenance, so the extra
   structure wouldn't buy anything proportionate to the risk. No new
   recovery mechanism either: losing it just demotes that identity to
   new-arrival status under §6's existing probation/vouching model,
   reusing infrastructure that already exists for unrelated reasons
   rather than building bespoke key recovery.
3. **Node keys (mandatory, SysOp-operated): root key + two operational
   keys (signing, transport) via signed transition records**, matching
   the original proposal — but with the ceremony stripped out. Root and
   operational keys auto-generate silently at bootstrap; rotation is one
   guided admin action; root-key custody folds into #60's ordinary
   backup story rather than assuming offline/HSM handling. The
   justification for *this* tier having real structure, unlike tiers 1–2,
   is that a node key underwrites every post/board/moderator-grant it
   originates — the blast radius of getting this wrong is network-wide,
   not personal, which is exactly the stakes threshold that justifies
   asking more of a SysOp (who, per §3's existing target-audience
   assumptions, self-hosts a server already) than of an arbitrary dial-in
   user.

**Also fixed as a direct consequence:** §11's real-time-chat transport
description previously said Noise reuses "the same keypairs" as content
signing — the exact reuse issue #51 flagged as needing justification or
separation. Now correctly describes the node's dedicated transport key
(distinct from its signing key) as the Noise static key.

**Explicitly still deferred, matching the shape of every other
extension point in this document (§6's jurisdiction-bound authority
keys is the direct precedent):** the exact transition-record wire/
signature format (§11 owns this as part of #11's canonical event
envelope work), multi-device support for the opt-in user tier, and
social/M-of-N root-key recovery for nodes. None of these block the
shape above from being sound; they're additions for if/when a real need
appears, using the same transition-record mechanism rather than a new
one.

This substantially resolves issue #51's design-shape question. What
remains open on that issue is narrower: the exact wire/signature format
(owned by #11), and the deferred refinements just listed.

## Sign-off notes, round 90 (canonical event model, semantic layer — resolves stage 1 of issue #11)

Second piece of Phase 3 design work, following round 89's key-lifecycle
model per the round-88 dependency ordering (#51 before #11, since the
event envelope's identity-reference field needed round 89's identity
shape to build on). Scoped deliberately to *only* Thiesi's stage 1
("semantic specification first... golden vectors... second") from the
#11 thread — exact canonical bytes remain untouched, stage 2, for
whenever real Phase 3 event-store/endpoint code is actually being
written.

**Central move: one mechanical rule (event chains with head pointers)
replaces what would otherwise have been per-feature special-casing.**
Post edits, tombstones, moderator grants/revocations, and round 89's key
transitions were each independently described elsewhere in this document
using compatible but not explicitly-unified language ("a new signed
event that references and amends it," "a transition record... delegates
to operational keys"). Round 90 names the shared shape once: an append
to a per-object chain, headed by a current-state pointer, where a new
event must reference what it extends.

**This directly resolves the reopened issue's three added protocol-state
concerns:**

1. **Immutable event identity vs. effective-state resolution** — §7's
   blanket "no CRDT/vector-clock resolution needed" claim was narrowed
   to immutable content objects specifically; mutable objects get the
   event-chain/head-pointer model instead, stated as its own mechanism
   rather than silently falling under the same claim.
2. **Replay memory vs. local deletion** — the sharpest result of this
   round: replay safety for state-changing events no longer depends on
   the seen-event dedup table (which is pruned after a retention
   window and was never actually sufficient on its own). Projection-
   level idempotency — replaying an event that's already an ancestor of
   an object's current head is a safe no-op — is structural and
   permanent, the same property that makes `git push` of an old commit
   harmless. Dedup, tombstones, and local storage pruning are now three
   explicitly separate things instead of one conflated "have we handled
   this" question.
3. **Compatibility negotiation** — `netbbs_protocol` version-bump policy,
   a peer handshake version-range exchange, and — the one genuine
   security-relevant default confirmed with Thiesi this round — unknown
   event types/versions are stored and relayed **opaquely**, never
   locally projected, matching how Git already handles refs it doesn't
   understand. Confirmed safe on the reasoning that a node which can't
   parse an event also can't act on it or display it, so nothing
   bypasses local moderation/quarantine merely by being unrecognized.

**Two forks confirmed with Thiesi, not decided unilaterally:**
- **Random nonce, not an author-local monotonic sequence counter**, to
  distinguish two identical posting actions (same content, same parent,
  submitted twice — which would otherwise hash identically and look
  like a dedup hit). Chosen over the sequence-counter alternative
  specifically to avoid needing persistent per-identity counter state,
  which would have been awkward once round 89's opt-in user tier grows
  multi-device support (still deferred, but not worth designing around
  prematurely).
- **Opaque store-and-relay for unknown/newer-than-supported events**,
  confirmed as the deliberate compatibility default rather than
  rejecting or attempting best-effort partial parsing.

**Also fixed as a direct consequence:** §7's author-reference model now
has a concrete shape — the tagged union matching round 89's three
identity tiers (`node_vouched_user`, `user_key`, `node`) — resolving the
original issue #11's "password-only author identity" question, which
round 89 had already answered for a different reason (the node-vouched
opaque local ID) without this round having connected it to the event
envelope until now.

**Explicitly still deferred to stage 2, unchanged from round 27's
original list:** Unicode normalization form, numeric type/range rules,
duplicate-key and absent-vs-null field handling, and signed golden test
vectors run through at least two independent encode/decode paths. None
of this round's decisions require stage 2 to be resolved first, and none
of stage 2's eventual answers should require revisiting anything decided
here — the semantic model and the exact byte representation were
deliberately kept as separable concerns, per Thiesi's own framing of the
staged gate.

## Sign-off notes, round 91 (database execution model — resolves issue #57)

Third piece of Phase 3 design work, following round 89 (key lifecycle)
and round 90 (canonical event model) per the round-88 dependency
ordering — #57 sits in the "before continuous background Link work"
tier, alongside a minimal #59 harness.

**Chose two dedicated single-worker-thread lanes (foreground,
background), each owning its own SQLite connection in WAL mode against
the same file — full detail in §14's new "Database execution model"
subsection.** Rejected two more thorough-looking alternatives, both for
concrete reasons rather than by default preference for the simpler
option:

- **A full typed-message actor (issue #57's option 1, taken literally)**
  — would require rewriting every one of the ~90 existing business-logic
  functions' call contracts into async messages. The chosen model gets
  the same "off the event loop" property by wrapping existing call sites
  in `run_in_executor`, with every function body — and its already-
  correct transaction ownership — completely untouched.
- **A full N-reader-connection pool (option 3 in full)** — evaluated
  properly, not dismissed by assumption, after Thiesi asked directly how
  much code it would touch and where two lanes would actually become a
  bottleneck. Findings:
  - The pool mechanism itself is cheap (~100–150 lines), but classifying
    which of the ~90 functions are safe to route to a reader connection
    requires tracing each one's *full call graph* for hidden writes, not
    just scanning for `SELECT`. Concrete near-miss found while doing this
    analysis: `netbbs.auth.users.authenticate_password` looks read-only
    but calls `_finish_password_login` → `_touch_last_login`, which
    writes `last_login_at` — exactly the kind of thing a quick pass
    would misclassify, and exactly the shape of bug this project's own
    history says only shows up by actually running things (round 30's
    dict-mutation bug, the `goto` bug, the password-masking bug).
  - Bottleneck estimate has two genuinely separate axes, not one number:
    the foreground/interactive lane has comfortable headroom well past
    this document's declared scale (§14), since BBS usage is human-paced
    and bursty; the background/Link lane's ceiling is a currently
    unmeasurable question of aggregate peer event volume against
    SQLite's own single-writer throughput, which no amount of estimation
    before Phase 3 code and #59's harness exist can responsibly answer.
    Critically, **deferring the reader pool doesn't make the background
    axis worse** — a saturated background lane queues its own work
    without touching the foreground lane's latency either way.
  - Given the audit cost is real (not hand-waved) and the benefit is
    unmeasurable before #59 exists, deferred rather than built
    speculatively — cheap to add later via the same mechanism (a third+
    lane) if a specific hot path is ever shown to need it.

**Also resolved, as direct consequences of choosing the lane model:**
cross-lane write serialization needs no new application code (SQLite's
own single-writer lock plus the already-configured `busy_timeout`,
round 30, handles it); cancellation safety is a property of threads
outliving coroutine cancellation, not an added check — a disconnected
session's in-flight DB call still runs to completion, never leaving a
transaction half-open; backpressure is a bounded semaphore per lane
rather than the executor's unbounded default queue, with exact numeric
limits left to #60's operational model; the standalone admin CLI is
unaffected, already covered by existing WAL/`busy_timeout` handling.

**Explicitly still open:** the actual implementation (wrapping the real
call sites, wiring the two executors into node startup) — this round is
a design decision, not code. Benchmark validation of both bottleneck
estimates above is deferred to #59's harness, matching #57's own
acceptance criteria.

## Sign-off notes, round 92 (minimal deterministic Link test harness — implemented, resolves the entry-gate half of issue #59)

Fourth piece of Phase 3 design work, and the first to produce real code
rather than a doc-only decision — the round-88/61 dependency matrix
scopes #59 to a **minimal** harness at this gate ("before continuous
background Link work"), with the full multi-node fault-injection/
convergence harness explicitly deferred to a later gate ("before the
first end-to-end Linked feature is treated as complete").

**Confirmed with Thiesi: in-process, not a small set of subprocesses**
(the issue's own text allowed either). Each harness "node" is a real
`Database` plus a real identity keypair, all living in the same test
process — full OS-level process isolation was judged to add IPC/process-
lifecycle complexity the *minimal* gate doesn't need, and is cheap to add
later as a separate, subprocess-based test tier for whatever specifically
needs it, rather than paying that cost up front.

**Built `tests/link_harness.py`** (shared test-support module, following
this project's existing convention of importing helpers across test
files rather than a separate support package):
- `FakeClock` — fixed arbitrary epoch, only ever advances explicitly,
  never reads real wall-clock time.
- `spawn_node(tmp_path, label)` / `HarnessNode` — an isolated `Database`
  (its own file under a per-node `tmp_path` subdirectory) plus a freshly
  generated node identity keypair.
- `ScriptedTransport` — enqueues signed messages between nodes; delivery
  only happens when a test explicitly calls `deliver()`/`deliver_all()`,
  and `deliver(index)` lets a test script out-of-order delivery. This is
  deliberately *not* yet the full duplicate/reorder/drop/partition fault-
  injection matrix from #59's later gate — just enough test-controlled
  ordering to prove real Phase 3 code behaves correctly once it exists,
  without inventing a fuller mechanism the later gate hasn't asked for
  yet.

**Deliberately does not exercise round 89's key-transition model or
round 90's event envelope** — neither has been implemented in code yet
(round 86 confirmed no Link/DAG/gossip modules exist in the tree; still
true). Building a harness that pretends to test unbuilt logic would be
hollow, so `HarnessNode` wraps only what already exists in the codebase
today (`netbbs.storage.database.Database`, `netbbs.identity.keys.
Identity`) and real Phase 3 feature work plugs into it as rounds 89/90's
designs actually get implemented, rather than each landing with its own
bespoke mock.

**Verified, not just written:** `tests/test_link_harness.py` (6 tests —
node isolation, fake-clock forward-only/fixed-epoch behavior, scripted
delivery being fully test-gated, explicit reordering, and a 3-node
signed-message exchange satisfying #59's "at least 3–5 independent node
identities" acceptance criterion) — all pass. Full suite re-run after
adding these files: **1682 passed, 4 skipped** (up from 1676 at round 81,
the last round with a worklog entry — 6 net new tests, matching this
round's addition exactly).

**Explicitly still open:** everything scoped to the later gate — fault
injection (duplicate/reorder/drop/partition), 3+ node convergence
assertions, adversarial/malformed-event injection, and a subprocess-based
tier if real process-boundary issues ever need covering. Not built now;
this harness is meant to grow into that as later Phase 3 features
actually need it, per #61's own framing of #59 growing in lockstep with
features rather than needing to be complete up front.

## Sign-off notes, round 93 (local asynchronous mail + Link messages — resolves issue #52, the first feature-specific gate)

Fifth piece of Phase 3 design work, and the first of round 88's two
feature-specific gates (the second, #53, is next). Full design lives in
§7's new "Personal mail" subsection; this note records the reasoning and
what was confirmed with Thiesi rather than assumed.

**Local mail is deliberately a new domain, not a persistence bolt-on to
`/msg`.** Round 32 already forbade `/msg` silently falling back to
async delivery, for good reason — the two have opposite lifecycles
(ephemeral/session-addressed vs. persistent/account-addressed) and
conflating them would have undone that earlier decision. One message
table (sender, recipient, subject, body, `read_at`, independent
per-side deletion timestamps), a stored-message quota per recipient, and
a **bounce-not-silently-drop** rule when an inbox is full of unread mail
— reusing the same drop-oldest-when-safe / never-silently-lose-something-
unread principles already established for `/msg`'s own bounded queues
and `ChatHub`'s. Placed in the after-Phase-2/before-Phase-3 addendum
slot alongside Communities/self-update/identity-attestation (§15), since
it has no Link dependency at all.

**The Link-message extension turned out simpler than expected, because
of one structural fact: a Link message has exactly one intended
recipient node, unlike a board's flood-fill-to-everyone-carrying-it
shape.** That single fact eliminates most of the hard questions the
original issue raised:
- Recipient discovery needs no protocol — addresses already encode the
  home node (§5).
- No multi-hop routing means no loop-prevention machinery.
- Duplicate delivery is already solved by round 90's seen-event dedup,
  since a Link message is just another signed event under that model.

**Two things confirmed with Thiesi rather than decided unilaterally:**

1. **Confidentiality is honestly tiered, not uniformly promised.**
   Round 89's identity tiers reassert themselves here: a personal-
   keypair recipient (tier 2) gets true end-to-end encryption; a
   password-only recipient (tier 1 — the expected majority) can only be
   encrypted to their home node's key, since they have no personal key
   of their own, meaning that node's operator can technically read it —
   exactly as already true of all their other local data, not a new
   exposure. Chose **best-effort E2E, disclosed plainly** over
   **requiring a personal keypair for Link messages at all**, since the
   latter would exclude round 89's expected default majority from a
   core feature to guard against exposure they never had a stronger
   guarantee against in the first place.
2. **No dedicated relay/opaque-envelope-storage mechanism for Link
   messages.** The original issue raised third-party relay storage as
   an open question; punted entirely to #58 (WAN reachability) instead
   of designing it here — if a relay/rendezvous mechanism ever exists at
   the transport layer, Link messages benefit automatically, and a 1:1
   message doesn't need its own separate relay story.

**Also resolved, following directly from the "one recipient node" and
round 90 facts above, without needing separate confirmation:** direct
point-to-point store-and-forward over the existing HTTP+JSON transport
(§11) rather than gossip; separate signed events for transport ACK,
user-level delivery acceptance, and bounce/rejection (issue #52's own
recommendation) rather than conflating them; node-level blocking reusing
the existing local blocklist (§6) rather than a new mechanism; read
receipts explicitly left out, optional and privacy-sensitive, nothing
requires them.

**Explicitly still open:** the actual implementation (schema, command
surface) — this round is a design decision, not code, matching rounds
89–91's pattern (round 92 is the exception, since a test harness has no
"real feature" to defer building). Node/account migration (#51) and any
future relay mechanism (#58) remain separately tracked, not solved here.

## Sign-off notes, round 94 (origin succession, transfer, and orphan/fork policy — resolves the remainder of issue #53)

Sixth piece of Phase 3 design work, and the second of round 88's two
feature-specific gates. Full design lives in §13's board/channel
lifecycle bullets (new "Origin succession, transfer, and orphan/fork
policy" entry); this note records the reasoning.

**The central move was recognizing this had almost no new ground to
cover, because round 87 already made origin authority *the originating
node's identity*, and round 89 already gave that identity a full key-
lifecycle model.** Applying round 89 directly, rather than designing a
separate resource-level authority system:
- **Routine rotation and compromise-with-root-intact** need nothing
  new — the same transition-chain verification and revocation already
  cover a Linked resource's origin the moment its authority is
  understood as "the node's identity," not a separate credential.
- **Voluntary transfer** is modeled as one more entry in the resource's
  own event chain (round 90's general shape: an append that references
  what it extends), requiring **mutual consent** — old origin signs the
  handoff, new origin signs acceptance — reusing §13's own existing
  "an invitation alone never creates membership" pattern for channel
  invitations rather than inventing a new trust shape for this one case.
- **Root-key loss or compromise has no cryptographic recovery**,
  symmetric with round 89's stance on node/user keys generally — a
  resource in that state becomes **orphaned**: it keeps existing exactly
  as last known, but accepts no further origin-authorized events.

**Confirmed with Thiesi: orphan recognition and fork handling stay
purely local, not a network-wide protocol state.** The alternative
considered — a lightweight opt-in signal piggybacking on §6's existing
quarantine-flag machinery, so nodes could converge on "X is orphaned"
faster than each independently noticing — was rejected in favor of the
simpler, already-established principle: there's no cryptographic proof
an origin is gone versus merely offline, so no single node's observation
should get an automatic network-wide effect, the same reasoning §6
already applies to quarantine and trust generally. A fork is just a new
resource with a new origin, optionally carrying a non-authoritative
`forked_from` pointer for discoverability; each node locally decides
whether to carry the frozen original, the fork, both, or neither — the
same default-carry-with-visible-opt-out shape already used everywhere
else in this section.

**This resolves issue #53 in full** (round 87 already covered the
genesis/carry/closure conceptual model; this round covers the succession/
orphan/fork half that remained). What's still open is the actual
implementation of both halves, and the general event-chain/transition-
record mechanics remain #11's exact-wire-format territory, not
duplicated here.

## Sign-off notes, round 95 (WAN reachability/relay selection + operational model — resolves #58 and #60, the deployment-readiness gate)

Seventh and eighth pieces of Phase 3 design work, closing out round 88's
deployment-readiness gate. Full designs live in §12's new subsection
(#58) and §14's new subsection (#60); this note records the reasoning
and what was confirmed with Thiesi.

**#58's central problem, prompted directly by Thiesi's question about
automatic relay selection:** round 93 modeled Link messages as direct
point-to-point delivery, but a sender can never dial an outgoing-only
recipient — round 93 explicitly left "does a relay exist" for this
round. The design that emerged reuses three mechanisms this document
already has, rather than inventing a fourth:

1. **§6's local reputation model, repurposed as a reliability score.**
   An outgoing-only node is already dialing its peers on a schedule for
   ordinary Link participation; every attempt is a free direct-
   observation sample. True hop-count/topology-graph awareness was
   considered and explicitly rejected — disproportionate complexity for
   this project's declared scale (§14), a cheap reliability proxy does
   the real job.
2. **§12's own signed peer-list exchange and endpoint descriptors**,
   extended so an outgoing-only node's descriptor names its accepted
   relay(s) — making relay location automatically discoverable by any
   sender resolving that address, no separate discovery mechanism
   needed.
3. **Round 93's opaque-envelope confidentiality tiers**, unchanged — a
   relay only ever custodies ciphertext it can't read, bounding the
   damage a bad-actor relay can do to traffic analysis (who's talking to
   whom, roughly how much) rather than content exposure, stated
   plainly as an honest limitation rather than hidden.

The result is a fully automatic loop — select candidates, request
consent, publish, and self-heal by replacing an underperforming relay
using the same observation loop that chose it — matching Thiesi's
stated goal: a new SysOp brings a node online and it eventually uses
every Link feature without required human interaction, aside from the
already-accepted seed-list bootstrap step.

**Confirmed with Thiesi: relay-serving defaults to *on*, not opt-in**,
with a conservative resource cap and an easy opt-out — same shape as the
existing ANSI welcome-banner toggle (round 63). The alternative
(opt-in-only) was rejected specifically because it would leave a young
or small Link without enough relays for outgoing-only nodes to reliably
reach anyone, defeating the zero-touch goal that motivated designing
automatic selection in the first place. Autonomy is preserved via the
opt-out; automation is preserved via the default.

**#60 is mostly consolidation of precedent already set** (rounds 51, 56,
59, 82, 89, 91, 93) rather than new ground, with one genuine gap closed:
round 82's self-update rollback was scoped as "protocol-agnostic
plumbing only," before Phase 3 schema concerns existed — if an update
bundles a schema migration and then fails, rolling back the binary
without also rolling back the schema would leave code unable to read its
own database. Fixed by requiring self-update to snapshot the database
(this round's backup mechanism, itself ordered DB-before-blobs so a
partial backup can only ever be missing-a-blob, never
referencing-a-missing-one) before applying any migration.

**Both gates now closed.** What's still open on both issues is the
actual implementation — this round is a design decision, not code,
matching every Phase 3 round except 92 (the harness, which had no
feature to defer building).

## Sign-off notes, round 96 (three-way registration posture: open/approval-required/closed)

Not tied to a GitHub issue — raised directly by Thiesi while reviewing
the open Phase 3 issues, prompted by noticing that round 87's own
correction to §5 ("there is no node-wide switch that disables the
registration path itself") was describing a real product gap, not just
fixing a documentation error.

**Thiesi's own framing, confirmed accurate:** three real BBS operating
postures exist, not a binary — **(a) public**, the vast majority of
systems, new accounts active immediately; **(b) closed-but-signups-open**,
accounts need explicit SysOp approval; **(c) private**, signups disabled
entirely, every account SysOp-created. (a) and (b) already exist in the
shipped code (round 76's `require_registration_approval` boolean); (c)
was the missing piece round 87 had just described accurately.

**Resolved as a single tri-state `registration_mode` enum
(`open`/`approval_required`/`closed`), not two independent booleans** —
confirmed with Thiesi over keeping two separate flags, since one
combination of the old shape (registration disabled + still requiring
approval) would have been a representable-but-meaningless state, the
same category of problem round 84's nullable-vs-explicit-zero fix
resolved for Community inheritance. Migration is a non-event: old
`False`/`True` map onto `open`/`approval_required` exactly, default
stays `open`, so no existing deployment's behavior changes.

**`closed` mode hides the registration option at the login prompt
entirely**, confirmed with Thiesi over presenting a registration flow
that always fails at the end — reusing the existing conditional-
visibility pattern for menu options with nothing behind them (round 84's
`[E]nter a Community`) rather than a confusing dead end.

**Explicitly kept independent of §6's probation/reputation system** —
`registration_mode` governs whether an account can log in *at all*; §6
governs what an already-active account is currently allowed to *do*
(new-user read-only probation, graduating via time/vouching). A type (a)
public system's brand-new users are still in §6's probation; the two
axes were kept from being conflated on purpose.

**Phase placement:** Phase 2 scope, extending round 76's existing
self-registration feature — not a Phase 3 gate, no dependency on
anything in rounds 89–95. **Implemented round 100** — see the worklog's
round 100 entry for the full writeup; §5's normative bullet above is
updated to point here.

## Sign-off notes, round 97 (live seed-list refresh, reusing the self-update channel — resolves a gap in issue #58's seed-bootstrap model)

Raised directly by Thiesi as a brainstorming session, immediately after
round 96 landed self-update's first implementation pass — noticing that
the same GitHub-Releases-API channel just built for code delivery could
also address something round 95's WAN-reachability design left
unresolved: the seed list is fixed at ship time, so a node installed at
time T stays anchored to whatever seeds existed then, forever, absent
manual SysOp intervention. Round 95's "learn more peers once connected"
resilience helps *after* first contact; it does nothing for the list a
brand-new node starts from.

**Core argument, and why this isn't a new trust decision:** round 82
already accepted HTTPS + the GitHub API as self-update's entire trust
boundary, for delivering *code* — the highest-stakes payload a network-
facing server can receive. A seed list is strictly lower-stakes: the
damage a hostile list can do is already bounded by decisions made
elsewhere (seed introduction never implies trust; seed compromise can't
impersonate peers, since identity is keypair-based and independent of
network location). Trusting the same already-accepted channel for a
lower-stakes payload isn't a new risk category, just a reuse of one
already signed off on.

**Named explicitly rather than left implicit, matching round 82's own
transparency about self-update's trade-offs:** this turns a one-time
bootstrap artifact into an ongoing dependency on the same channel,
widening the window (not the shape) of an already-existing eclipse-
attack risk — if the channel were compromised at exactly the moment a
new node's entire candidate pool were attacker-supplied, its early Link
view could be distorted before it builds its own peer relationships. A
hardcoded list compromised at ship time already carries this same risk
today; this mechanism doesn't introduce the risk, it makes the window
recurring rather than one-time.

**Mechanism: a well-known raw file path (e.g. `seeds.json`), not tied to
the release cycle — confirmed with Thiesi over two alternatives.**
Rejected piggybacking on release assets (couples seed-list freshness to
software-release cadence, the wrong coupling) and a parallel "release
train" reusing `check_latest_release`'s machinery (more code reuse, but
stretches what a GitHub release is conceptually for). A plain file
fetched via GitHub's raw-content delivery lets the list update
independently of versioned releases — seed churn and software releases
are genuinely different cadences.

**Supplements, never replaces, existing seed sources** — operator-
configured seeds first, the shipped fallback list next (used if the
live fetch fails), the live list layered in as a freshness improvement
on top, not a new dependency of the "every configured seed unavailable"
resilience path round 95 already built. Reuses self-update's existing
three trigger points (startup/manual/daily) rather than inventing a
fourth.

**Phase placement:** Phase 3 (issue #58), since it has nothing to plug
into until Phase 3's peer-connection code exists — the fetch/parse logic
itself is self-contained and could be built earlier if useful, mirroring
`check_latest_release`'s shape, but that's a future implementation
choice, not decided now.

## Sign-off notes, round 98 (real-name attestation display spoofing — caught and fixed before implementation)

Raised directly by Thiesi while reviewing round 85's identity-
attestation design, before any of it had been built — good timing,
since this is a correctness fix to a design, not a retrofit to shipped
code. The concern: round 85's display format unconditionally appends
`(attested real name)` after whatever `display_name` the user already
chose, with no restriction on what `display_name` itself may contain.

**The actual vulnerability, once traced through:** it's not merely
"confusing" if `display_name` contains parentheses — it's a complete
forgery of the entire feature. A user with **no verification at all**
could set `display_name` to `Alice (Robert Smith)` and render
*identically* to a genuinely verified `Alice` whose real name is
`Robert Smith`. Nothing in the rendered string is actually reserved for
the system-appended part, so there is no way for a viewer to distinguish
"this person typed this themselves" from "a SysOp-delegated verifier
attested this" — which defeats the entire point of the feature, since
that distinction *is* the feature.

**This is a direct structural repeat of a problem this project already
solved once, for a different field — round 53's `/nick` marker
rejection.** Round 53 wraps a nick in `~marker~` for the live chat
stream specifically so it's visually distinct from plain text, and
"`/nick` now rejects the marker character from submitted nick content"
was the fix that made that distinction actually trustworthy — a
delimiter meant to convey trusted, system-attached meaning is worthless
if the untrusted text it wraps can contain that same delimiter. Applying
the identical fix here: `display_name` rejects literal `(` and `)` at
write time. §18 updated directly (not left for an implementer to
rediscover), applied unconditionally on every node rather than only once
attestation is actively enabled, so a node that turns the feature on
later never has to retroactively confront pre-existing display names
that already violate the rule.

**Not yet implemented** — identity attestation as a whole (round 85/86)
remains on the addendum backlog, next after registration mode. This
round exists so the character restriction is part of the design from
the start of that implementation, not bolted on after the fact once
someone notices real display names already violate it.

## Sign-off notes, round 99 (real-name attestation anti-forgery: color replaces the parens ban)

Thiesi asked, immediately after round 98 landed, whether the same
distinction could instead be made with color (or `/nick`'s existing
marker convention) rather than restricting `display_name`'s character
set — a genuine "would this unify things or just be convoluted"
question, worked through rather than answered reflexively.

**Color turns out to be a strictly stronger mechanism, not merely a
prettier one.** Round 98's fix relied on parentheses being a reserved
*plain-text pattern* — the guarantee only held because the character was
forbidden. Color is a *rendering-layer* guarantee instead: `VERIFIED_COLOR`
(new, following `NICK_COLOR`'s precedent) is applied directly to the
trusted `attested_value`, never derived from user text, and round 29's
existing terminal-sanitization boundary already strips any ANSI a user
might try to smuggle into `display_name` — so there is structurally no
way for user-supplied text to acquire that color. This let round 98's
`(`/`)` restriction be **lifted entirely** — `display_name` is
unrestricted again with respect to parentheses.

**Thiesi's own follow-up caught a real flaw in the first version of this
round's plan.** The initial proposal kept a reserved plain-text marker
(`=`) purely as a color-stripped fallback, without being explicit that
the marker only works *if it's also rejected from `display_name`* —
Thiesi asked directly whether an unrestricted `display_name` of `"Alice
=Roger Smith="` would be indistinguishable from a genuinely attested
`Roger Smith` in a plain-text view. Correct: it would be, and for the
identical reason round 98 existed in the first place — a delimiter only
protects anything if the untrusted text it wraps can't contain it. The
marker isn't a free enhancement over banning parens; it's the *same*
trade-off relocated to a different, rarer character.

**Final decision — color as the primary mechanism, plus a narrowly-
rejected marker for color-stripped contexts, chosen by Claude at
Thiesi's explicit invitation to pick based on whichever metric seemed
right, given Thiesi had no preference either way.** Weighed accessibility
against implementation cost: the population most affected by a color-
stripped view — screen-reader users — has no alternative way to ever
perceive a color-only distinction, a permanent equity gap rather than a
rare edge case; and the fix costs little, since it reuses round 53's
exact validation pattern (reject one character from a text field at
write time) rather than building anything new. Final format:
`"{display_name or username} (={attested real name}=)"`, the whole unit
colored, `=` rejected from `display_name` the same way `~` is already
rejected from `/nick`. Deliberately a *different* marker than `/nick`'s
`~` — round 53 itself notes `/who`/`/whois`/`/names` use the plain,
markerless `nick|username` form, so the two contexts rarely coincide,
but keeping the glyphs distinct costs nothing and removes any residual
ambiguity where they might.

**Net effect versus round 98:** the actual security property is
preserved and strengthened (a rendering-layer guarantee, not just a
text-pattern one) while the practical restriction on ordinary display
names gets *lighter* — parentheses are freed for legitimate use (e.g.
`Alex (they/them)`), and only a genuinely rare character (`=`) remains
reserved. Not yet implemented, same as round 98 — still part of the
identity-attestation addendum-backlog item, not built yet.


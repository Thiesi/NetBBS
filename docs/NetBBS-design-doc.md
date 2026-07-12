# NetBBS — Design Document (v0.1, draft for review)

Second attempt at a modern, TCP/IP-native BBS system. First attempt got far
(multi-user chat, file areas, message boards) but required a rewrite once
mesh networking ("NetBBS Link") entered scope — this attempt builds NetBBS Link in
as a foundational principle from day one instead of retrofitting it.

Status: **CONFIRMED — signed off by Thiesi through 6 rounds of review
(initial design, first-attempt-docs review, first-attempt team Q&A,
permissions & moderation, board/channel lifecycle, phasing scope review).
Ready for implementation.**

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

- **Cryptographic keypairs** for both **nodes** and **individual users** —
  not hierarchical (no FidoNet-style zone:net/node addressing), since
  routing happens dynamically based on live Link membership rather than a
  fixed topology.
- **Addressing:** Matrix-federation-style human-facing addresses
  (`user@node-fingerprint`), but using a pubkey fingerprint instead of a DNS
  domain for the node part. This avoids DNS as a single point of
  failure/censorship (domains can be seized; a pubkey can't be revoked by
  anyone but its owner) while keeping addresses legible.
- **User login/auth:** both supported — traditional password auth as a
  simple/fallback option, and keypair-based (passwordless) auth available
  for those who want it.

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
- **Ordering:** causal via DAG parent pointers, timestamp as tiebreaker. No
  CRDT/vector-clock conflict resolution needed, since content-addressed IDs
  mean nodes can never disagree about what a given message *is* — only
  about which ones they've seen yet.
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

## 8. Real-time chat

Explicitly **not** routed through the DAG/store-and-forward system — real
time chat needs low latency, board/PM sync doesn't need to be real-time.
Direct relay between currently-connected nodes; if a node is offline, its
users simply miss chat traffic until reconnect rather than the system trying
to replay chat history through the propagation layer.

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

- **Store-and-forward Link traffic** (boards, Link messages, file chunk
  transfer — everything in §7's DAG/gossip system): **HTTP+JSON**, carrying
  payloads authenticated via signatures from the existing keypair identity
  system (§5) rather than a shared-secret HMAC model. The first attempt's
  experience showed HTTP+JSON causes essentially no firewall/NAT friction
  on modern infrastructure, and it's a natural fit for async, queued,
  request/response-shaped traffic. Signatures replace their bootstrap-
  secret/per-peer-HMAC scheme, removing the shared-secret handshake
  entirely — the thing you authenticate is the identity you already have,
  same reasoning as originally applied to Noise.
- **Real-time chat** (§8): **Noise Protocol Framework**, using the same
  keypairs as Noise static keys for mutual authentication. A persistent,
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

**Chat permissions & moderation:**
- Channels support minimum-age gating, same mechanism as existing
  age-restriction support (§5's user model), extended to chat.
- Channels support minimum user-level gating, optionally combined with
  individual per-user access grants that bypass the level requirement —
  and optionally hidden entirely (not just inaccessible) from users who
  don't meet the requirement.
- `mute`/`ban`/`unmute`/`unban` commands. Bare numeric argument = minutes;
  no argument = indefinite; suffix alters unit: `s`/`m`/`h`/`d`/`w`/`y`
  (seconds/minutes/hours/days/weeks/years). All actions logged and echoed
  in-channel for transparency.
- Chat moderators (non-SysOp) can `kick`/`mute`/`ban` within their scope.

**Moderator scope tiers** (three levels, each reusing the same underlying
permission primitives — a "global" moderator is just "moderator of every
object in a category," not a different mechanism):
1. **Per-object** — authority over one specific board/area/channel.
2. **Local-blanket** — authority over every *local-only* board/area/channel
   on a given node (i.e., content not carried on NetBBS Link).
3. **Link-blanket ("global")** — authority over every Link-participating
   board/area/channel that node carries.

**Global does not imply local, by design (opinion, confirmed with
Thiesi).** Local-only content is a single-SysOp trust domain; Link-wide
moderator authority is a separate, multi-party trust domain governed by the
mechanism below. Merging them automatically increases blast radius (a
compromised global-mod identity would also inherit local keys) without a
corresponding need, and it violates the same "no automatic power grants"
principle already applied in §6. A SysOp wanting one person to hold both
grants both explicitly.

**Privilege separation, SysOp vs. global moderator:** SysOp remains root —
the only role that can grant/revoke *any* moderator tier, change node
configuration, and originate boards/channels (which is itself what creates
the ability to grant Link-board moderator status — see below). A global
moderator's authority is strictly content-scoped: they can moderate content
but cannot appoint other moderators or touch node configuration. This
prevents a compromised or bad-acting moderator identity from escalating or
self-perpetuating — directly informed by the Master Node lesson in §2/§6.

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

- **Creation:** global board/channel moderators and the SysOp can create
  new Linked boards/channels. Creating a board makes you its *origin* —
  matching the grant-authority model already defined above — which is a
  narrow, self-contained power, not a blanket administrative one. Creation
  propagates as a **signed announcement**, not a forced action: other
  nodes decide whether to carry the new board (see default-carry policy,
  below), rather than having it appear on their system without their
  node's consent.
- **Deletion:** a board/channel's origin can mark it **closed/archived**
  (a signed event; no new posts accepted) — but this cannot force other
  nodes to purge data they've already stored. Real deletion of stored
  content remains a purely local, per-node decision (see maintenance,
  below). Rationale: unrecoverable data loss triggered by another node's
  action is the same shape of problem as the Master Node — a small set of
  privileged users able to act on infrastructure they don't own — even
  though the stakes here are lower than that original failure.
- **Default-carry policy for Link participation:** joining NetBBS Link
  **carries every Linked board/channel by default** — this gives the
  "same content available on any node" guarantee automatically, with zero
  configuration, for the overwhelming majority of SysOps who'll never want
  to deviate from it. A SysOp retains the ability to **explicitly exclude**
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
- Hybrid ANSI/TUI rendering framework
- Password + keypair auth
- SQLite storage layer
- **Permission/level-gating plumbing in the menu/command dispatch layer**
  (§13) — built early even though most gated features ship later, per §13's
  explicit anti-retrofit rationale
- Local message boards (not yet linked)
- Local file areas
- Local real-time chat (single-node)
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
- SysOp admin tools (user/board/node management, beyond blocklists)
- ANSI art support for login/welcome screens
- Fullscreen editor (see editor implementation notes, below) — a natural
  fit here since post/PM editing is exercised heavily once moderation and
  boards are both fully functional

**Phase 3 — Link connectivity & sync core**
- Seed-node bootstrapping
- Node-to-node transport: **HTTP+JSON with keypair signatures** (§11) —
  *not* Noise, which is reserved for Phase 5's real-time chat only
- Content-addressed DAG message format + flood-fill gossip sync
- Persistent seen-event dedup table + file-chunk transfer ID scheme (§7)
- Store-and-forward for offline nodes
- Linked boards (distribution across NetBBS Link)
- Link messages (cross-Link PMs)
- Interim abuse defense: the local blocklist mechanism from Phase 1,
  extended to remote nodes/traffic — acceptable given near-term testing is
  single/VM-node scale (§14), not a live public rollout. Full reputation
  system arrives in Phase 4, deliberately not co-developed with sync
  mechanics.

**Phase 4 — Link trust & reputation**
Isolated as its own phase specifically because it's the hardest,
least-precedented part of the whole design — built and tested against
already-working Phase 3 sync mechanics rather than developed alongside
them.
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
- Who's-online (local + Link-wide)
- On-demand cross-node file area discovery/download

**Phase 6 — Linked governance & lifecycle**
The most structurally novel part of the whole design — nothing like it
existed in the first attempt. Isolated here specifically because everything
it depends on (trust system, chat) is already proven by this point.
- **Link-blanket ("global") moderator tier and Linked board/channel
  moderation** (§13): signed grant/edit events, verified against the
  granting event
- **Global-moderator board/channel creation & closure** (§13): signed
  announcement/opt-in-carry model, default-carry-with-visible-opt-out
  policy
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
- Door game native API
- Message board threading refinements
- Classic DOS door compatibility (legacy game support)

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

---

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

Prompted by Thiesi noticing raw microsecond-precision timestamps leaking
into user-facing board post displays.

1. **Storage vs. display timestamps formally split.** `utc_now_iso()`
   (microsecond-precision, for storage/content-ID hashing) is untouched;
   a new `format_for_display()` in the same module handles what a user
   actually sees, and never includes sub-second precision regardless of
   configuration.
2. **Configurability level, confirmed with Thiesi:** node-wide default
   (SysOp-configurable) now; per-user preference later, once a user
   preferences system exists (no such system exists yet — see §13/§15
   phasing). `format_for_display()`'s resolution order (future per-user
   override > node config > hardcoded default) is built in now
   specifically so adding per-user preferences later needs no changes to
   this function, only a caller passing the user's stored value through.
3. **New `netbbs.config` module**: a generic node-wide key-value store
   backed by a new `node_config` table, not a single hardcoded setting —
   more node-wide settings are inevitable as the project grows.
4. **European-style default** (`%d.%m.%Y %H:%M`, 24-hour clock) per
   Thiesi's preference, fully overridable.
5. **Real bug caught by actually running the code, not just syntax-
   checking it:** the first implementation used `try/except ValueError`
   around `strftime()` to detect a malformed custom format and fall back
   to the default. Verified directly that this doesn't work reliably —
   glibc's `strftime` does not raise for an unknown directive (e.g.
   `%Q`), it returns the directive back out literally instead, and
   behavior for invalid directives is undefined by the C standard and
   platform-dependent (NetBSD's libc could differ again). Replaced with
   upfront allowlist validation of directive characters before ever
   calling `strftime`, which is deterministic regardless of platform.
   Invalid formats are now rejected at set-time (`set_display_format`,
   with an immediate error) rather than silently discovered later at
   display time.

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

1. **New architectural piece: `netbbs.chat.hub.ChatHub`.** Everything
   built before this was pure per-session request/response; chat is the
   first feature requiring a session to *receive* a message while idle,
   waiting for its own next input. Solved with a per-node, in-memory,
   queue-per-participant broadcast hub and two concurrent asyncio tasks
   per chat session (one reading input, one draining the queue),
   stopping — with cleanup — as soon as either finishes.
2. **Real, verified concurrency bug caught and fixed before shipping,
   not just reasoned about:** `broadcast()`'s first draft iterated the
   live participant dict while awaiting inside the loop
   (`queue.put`), which yields control back to the event loop between
   iterations. Confirmed directly (a minimal reproduction, then again
   against the real `ChatHub` class) that another coroutine calling
   `leave()` mid-broadcast raises `RuntimeError: dictionary changed size
   during iteration`. Fixed by iterating a snapshot of the participant
   list instead. Covered by a regression test
   (`test_broadcast_survives_concurrent_leave_mid_iteration`) so this
   can't silently regress.
3. **Channels mirror boards' content-addressing** (§7) for the same
   forward-compatibility reason, but with a single `min_level` rather
   than a read/write pair — chat access has no meaningful read/write
   split, confirmed explicitly during the earlier permissions design
   discussion.
4. **Chat messages are not persisted.** Ephemeral by design for Phase 1;
   revisit if local chat history/scrollback turns out to be wanted later.
5. **Known, deliberate UX limitation, not a bug:** because Telnet stays
   in the client's default line-editing mode (a decision made when the
   transport layer was first built, deferring character-at-a-time input
   to the hybrid ANSI/TUI rendering framework), an incoming chat message
   can land on screen while a user is mid-typing their own line,
   interleaving with it — the same behavior classic line-mode chat tools
   (Unix `talk`, `wall`) have always had. Fixing this properly needs the
   character-mode/redraw machinery the rendering framework is meant to
   provide.
6. **Main menu introduced** (`[B]oards [C]hat [Q]uit`) — the first real
   menu-loop structure in `login_flow.py`, replacing the previous purely
   linear "log in, browse boards, goodbye" flow, since there are now
   genuinely two independent things to route between.
7. **Testing note:** `ChatHub` has no PyNaCl dependency and its 11 tests
   (including the concurrency regression test) were actually executed,
   not just syntax-checked — worked around `netbbs.chat`'s package
   `__init__.py` pulling in the (nacl-dependent) `channels`/`auth` chain
   by loading `hub.py` directly via `importlib`, bypassing the package
   init for verification purposes only; the shipped code imports
   normally. `channels.py` and the full `chat_flow.py` two-task
   interleaving behavior over a real connection remain unverified by
   Claude — manual two-terminal testing (see README) is the most direct
   way to confirm the real-time behavior actually works end-to-end.

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

1. **New `netbbs.moderation` package**, the natural home for the richer
   §13 moderation model (mute/ban/kick, board moderator roles) once
   that's built in Phase 2 — started now with just the blocklist rather
   than waiting, since a dedicated package was clearly right regardless
   of how much lives in it yet.
2. **Entries key on fingerprint when possible, local user ID otherwise**
   — mirrors the same keypair-vs-password-only duality already handled
   in `posts.author_fingerprint` and `users.fingerprint`. Fingerprint-
   based entries are the exact form design doc §15's Phase 3 extends to
   remote nodes/users ("the local blocklist mechanism from Phase 1,
   extended to remote nodes/traffic"); local-user-ID entries exist
   specifically for password-only accounts, which have no fingerprint.
3. **Verified directly, not assumed:** the SQLite `CHECK ((fingerprint IS
   NOT NULL) != (local_user_id IS NOT NULL))` XOR constraint and the
   partial unique indexes (`WHERE fingerprint IS NOT NULL`) both actually
   work as intended — tested standalone first, then again against the
   real migrated schema, including a rejection test for a row with both
   fields set.
4. **Blocklist enforcement lives in the login flow, not inside
   `netbbs.auth`.** Authentication ("are these credentials correct") and
   this kind of authorization ("is this correctly-authenticated account
   allowed to proceed") are different concerns — same layering principle
   already applied to keep `netbbs.permissions` separate from
   `netbbs.auth`.
5. **Edge case identified and handled defensively:** a user blocked while
   password-only (by local user ID) could theoretically later gain a
   keypair and no longer show as blocked under a naive fingerprint-only
   check. Not reachable today — there's no "add a keypair to an existing
   account" feature yet — but `is_blocked` checks both fields whenever a
   fingerprint is present, closing the gap now rather than leaving it for
   whenever that feature exists.
6. **Testing note:** `netbbs.moderation.blocklist` needs `netbbs.auth`
   (for `User`), which needs PyNaCl — unavailable in Claude's sandbox, so
   the 15 tests in `test_blocklist.py` are syntax-checked only. The
   schema itself (migration, XOR constraint, partial unique indexes) was
   verified against a real `Database` instance, since `netbbs.storage`
   has no PyNaCl dependency — all 5 migrations apply cleanly and the
   constraint correctly rejects an invalid row when tested directly
   against the actual migrated table, not just a standalone
   reproduction.

## Sign-off notes, round 13 (implementation: ANSI rendering framework)

1. **Scoped to the "ANSI half" only, per discussion:** color/cursor
   helpers (`netbbs.rendering.ansi`) and text reflow
   (`netbbs.rendering.reflow`), both built now since they benefit every
   existing screen immediately. The "TUI half" (character-mode input,
   screen-buffer diffing) remains deliberately deferred until a real
   heavy screen (fullscreen editor, a future file browser) needs it —
   confirmed with Thiesi rather than assumed. **Superseded in part by
   round 14:** character-mode input specifically was pulled forward
   after real testing surfaced client-side line-editing problems
   (Backspace not working, `^M` instead of a newline) — see round 14.
   Screen-buffer diffing for full heavy-screen TUI rendering remains
   deferred; character-mode *input* alone doesn't require it.
2. **256-color/extended ANSI**, confirmed with Thiesi over classic
   16-color — richer, at the accepted cost of some old/dumb clients not
   rendering it correctly. No 16-color fallback/downgrade path built.
3. **Terminal width: NAWS negotiation with an 80-column fallback**,
   confirmed with Thiesi. Implemented in `netbbs.net.telnet` (the one
   piece of the rendering framework that's inherently transport-specific,
   hence not living in `netbbs.rendering` — see that module's docstring).
   `Session.terminal_width`/`terminal_height` added as a transport-
   agnostic interface every `Session` implementation populates however
   its transport allows.
4. **A genuine protocol correctness detail, verified rather than
   assumed:** NAWS subnegotiation bodies need the same IAC-escaping as
   regular data — a terminal reporting a width whose byte value happens
   to equal `0xFF` must have that byte doubled per RFC 854. Verified
   directly with a constructed test case (a simulated 255-column-wide
   terminal) that the un-escaping in `_read_subnegotiation_body` handles
   this correctly, not just small width/height values that never
   collide with the IAC byte value.
5. **A second, unrelated protocol gap found and fixed while verifying
   the framework end-to-end, not while building any single piece in
   isolation:** `netbbs.rendering.reflow` correctly produces multi-line
   text using plain `\n` (it's transport-agnostic, so hardcoding `\r\n`
   into it would itself be a layering mistake) — but
   `Session.write_line()` only appends `\r\n` once, at the end. Without
   normalization, every internal line break in reflowed text (e.g. a
   wrapped post body) would reach the wire as a bare LF: tolerated by
   lenient modern terminals (they auto-CR on LF) but not correct Telnet
   per RFC 854. Fixed by normalizing all line endings to CRLF at the
   transport boundary (`TelnetSession.write()`), not by changing
   `reflow()` — line-ending convention is a transport concern, not a
   text-utility one.
6. **Testing note — the most thoroughly executed piece of work so far,
   not just syntax-checked:** none of `netbbs.rendering` or the NAWS
   additions to `netbbs.net.telnet` have any PyNaCl dependency. All 23
   ANSI/reflow tests, all 13 telnet tests (including 5 new NAWS-specific
   tests and 2 new CRLF-normalization regression tests), and a full
   end-to-end smoke test (negotiate a real 40-column terminal via NAWS,
   confirm reflowed output actually respects it, confirm no bare LF
   reaches the wire) were all genuinely executed against real loopback
   sockets, not assumed correct from reading the code. This caught three
   real bugs before Thiesi ever saw them: a test that mis-constructed an
   IAC-escaped byte sequence (tripling instead of doubling), and the
   `write_line`/`reflow` CRLF gap described above, found only by testing
   the full chain together rather than each piece independently. Only
   `netbbs.net.login_flow`'s actual color/reflow wiring (which imports
   `netbbs.auth`) remains unverified by Claude.

## Sign-off notes, round 14 (color consistency + character-mode input)

Prompted by two things Thiesi raised after testing round 13: color
hadn't been applied consistently (chat/channel listings, valid menu
inputs weren't highlighted), and — separately — real testing surfaced
Backspace not working and Enter's CR showing literally as `^M`.

**Color consistency:**
1. New `netbbs.rendering.theme` — the actual palette (header/accent/
   muted/menu-key colors) in one place, replacing local color constants
   that had started drifting independently between `login_flow.py` and
   what would have become `chat_flow.py`'s own copies.
2. New `netbbs.rendering.menu.menu_key()` — highlights the actual valid
   keystroke in a menu option (e.g. the `B` in `[B]oards`), directly
   answering "valid inputs should stand out." Applied to the main menu
   and to `/quit` in chat.
3. Chat now matches boards' existing color treatment: channel listing,
   join/leave system notices (muted color, distinct from actual chat
   content), and colored usernames on each message.

**Character-mode input — a genuine architectural reversal, not an
incremental addition:**
4. **Root cause explained to Thiesi:** both symptoms traced to the
   Phase-1-era decision to stay in the client's default line-editing
   mode — the client's own terminal driver, not the server, was
   responsible for local echo, Backspace, and Enter display. Different
   clients implement that inconsistently. Asked whether to keep deferring
   character-mode input (the actual fix) or pull it forward now — Thiesi
   chose to pull it forward, reversing the earlier deferral decision from
   when the rendering framework was first scoped.
5. **Scope confirmed with Thiesi before implementation:** whole-session
   character mode (not mixed per-prompt), Backspace/Delete-only editing
   (both `0x08` and `0x7F` byte values treated as backspace), no arrow-
   key/cursor-movement support — full cursor-addressable editing remains
   out of scope, arguably actual fullscreen-editor territory. Password
   masking changed from "no visual feedback" to `*` per character
   (Thiesi's choice), now purely a local rendering decision rather than a
   protocol-level toggle, since the server controls all echo persistently
   from connection start.
6. **`netbbs.net.telnet` rewritten**: `IAC WILL ECHO` now sent once,
   persistently, at connection start (alongside SGA and NAWS) rather than
   toggled per-read. `read_line()` replaced entirely — reads one byte at
   a time via a new `_read_byte()` primitive (centralizing all IAC/
   negotiation handling in one place, reused by every higher-level read),
   builds the line itself, echoes/masks each character, and handles
   Backspace/Delete (erase sequence `\b \b`), Enter (CR/LF/CRLF, all
   correctly collapsed to one terminator), and unsupported escape
   sequences (CSI `ESC [ ... <final byte>` and SS3 `ESC O <letter>`
   forms both consumed and discarded as complete units, never leaking
   raw escape bytes into the line).
7. **A correctness detail identified during design, not discovered as a
   bug later:** multi-byte UTF-8 characters (umlauts, the Euro sign,
   etc. — everyday input, not an edge case, given the project's context)
   need explicit continuation-byte handling once reading byte-by-byte;
   a naive per-byte decode would have corrupted every non-ASCII
   character. Implemented via `_read_utf8_continuation`, using the
   standard UTF-8 lead-byte ranges to determine how many continuation
   bytes to read.
8. **A real latent bug fixed as a side effect, present since the
   original line-mode implementation:** the old CR-handling code did an
   unbounded `await self._reader.read(1)` to check for a following LF —
   if a client ever sent a bare CR with nothing immediately after it,
   this could have hung indefinitely. Never surfaced because typical
   line-mode clients always send CRLF together. Replaced with a bounded
   50ms timeout (`_consume_optional_lf_or_nul`), verified directly to
   resolve correctly within that window rather than hanging.
9. **Defensive line-length cap added** (4096 chars) — cheap insurance
   against unbounded memory growth from a client that never sends Enter.
   Verified that characters beyond the cap are neither stored nor
   echoed (not just "doesn't crash") — echoing what gets silently
   dropped would show a complete line on screen while actually storing a
   truncated one, a worse failure mode than the truncation itself.
10. **A real bug in `login_flow.py` caught during review, not testing:**
    the password-prompt code had an explicit `write_line("")` to move to
    a fresh line after masked input, needed because the old line-mode
    `read_line()` never wrote anything itself. The new character-mode
    `read_line()` always writes its own trailing CRLF after Enter,
    regardless of echo — left unchanged, that explicit call would have
    produced a duplicate blank line. Removed, with the reasoning
    documented in place. A similar stale comment in `chat_flow.py`
    (attributing the "you see your own message twice" tradeoff to "the
    client's local echo," no longer accurate now that the server does
    the echoing) was corrected — the underlying design decision was
    still correct, only the attribution needed fixing.
11. **Testing note — the most extensively verified single piece of work
    in this project so far:** `netbbs.net.telnet` has no PyNaCl
    dependency, so all 22 tests (a near-total rewrite of the previous
    suite, given how fundamentally `read_line()`'s contract changed)
    were genuinely executed against real loopback sockets, alongside
    extensive ad-hoc verification before formalizing each test —
    including basic echo, password masking, Backspace/Delete, bare-CR
    timeout behavior, CRLF-pair handling, two- and three-byte UTF-8
    characters, both escape-sequence shapes (CSI and SS3), negotiation
    sequences arriving mid-input, NAWS continuing to work correctly
    inside character mode, and the line-length cap. This rigor caught
    two more real issues before Thiesi saw them: a test-scenario design
    mistake (attempting to fix a mid-word typo with too few backspaces,
    not accounting for end-only editing — itself a good illustration of
    the Backspace-only limitation) and the `login_flow.py` double-
    blank-line regression described above. A full end-to-end smoke test
    (character-mode typing with a real backspace correction, NAWS
    negotiation, and reflow, all together) was run and passed before
    this was considered done.

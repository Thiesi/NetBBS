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

**Channel membership and invitations:**
- New `manage_members` permission, deliberately separate from `edit`: membership management is authorization, not metadata editing.
- Covers sending/revoking invitations, viewing members, granting/removing persistent channel access, and configuring whether ordinary members may invite.
- Invite-only channels use an invitation-plus-acceptance workflow; an invitation alone never creates membership.
- Channel visibility and join policy are independent axes: listed vs. hidden, and open-to-eligible vs. members-only. `hidden + open` is permitted but documented as obscurity rather than access control.
- Local invitations may be delivered immediately to online users and retained as pending invitations for offline users, with configurable expiry. Membership persists until revoked unless explicitly configured otherwise.
- Default invitation policy is moderators/SysOp only; channels may opt into ordinary members being allowed to invite.
- Linked-channel membership is Phase 6 scope: invitations, acceptances, grants, removals, and revocations become signed governance events.
- Access-restricted Linked channels are not described as end-to-end confidential from participating node operators. True encrypted group confidentiality requires a separate future design covering group keys, rotation, history access, and compromised members.

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
- Link-wide `/private`, `/who`, `/whois`, `/names`, `/list`, and identity completion where remote presence/directory visibility permits.
- Multiple simultaneous channel memberships, with active-channel selection, background delivery, and unread-state handling.
- Who's-online (local + Link-wide)
- Link-wide extension of `/msg` over the real-time Noise transport for currently-online recipients only; asynchronous Link messages remain a separate store-and-forward mechanism.
- Link-wide propagation of `/me`, `/away`, and transparent display aliases as typed presence/chat events.
- On-demand cross-node file area discovery/download
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

## Sign-off notes, round 16 (shared paginated list picker)

Prompted by Thiesi's dissatisfaction with typing full board/channel
names to select them, and openly uncertain between two of his own
proposed alternatives.

1. **Two proposals evaluated, neither adopted as-is.** Pure two-digit
   paginated numbering (Thiesi's idea) solved "don't make me type" but
   not "jump to item #769" without still paging through everything
   first. Tab completion (Thiesi's other idea) doesn't solve long-range
   jumps either, and Thiesi himself flagged it as inconsistent with
   single-key navigation elsewhere in the BBS.
2. **Synthesis landed on and confirmed with Thiesi:** always-exactly-
   2-digit page-relative selection (for browsing) + a search command
   (filters by substring, subsumes what tab completion would have
   offered, auto-selects on a unique match) + a goto command (jumps
   directly to an absolute index). Page size adapts to the session's
   actual negotiated terminal height (NAWS), confirmed with Thiesi over
   a fixed size.
3. **Built once as `netbbs.net.picker.pick_item()`, shared across
   boards, chat channels, and (once built) file areas** — same
   underlying problem in all three, not reimplemented per feature.
   Board selection (`login_flow.py`) and channel selection
   (`chat_flow.py`) both now use it, replacing their previous
   type-the-exact-name flows.
4. **A real design gap found and fixed during review, not by Thiesi
   reporting it:** the initial implementation's `goto` command indexed
   into whatever a prior search had narrowed the visible list to, not
   the stable original list — confirmed directly with a live scenario
   (searching "item1" against a 20-item list, then "goto 3", returned
   "item11" — the 3rd search match — instead of "item3", the 3rd item
   overall). Worse, no version of the display ever showed a stable
   absolute number anywhere, so `goto` was effectively undiscoverable —
   a user would have no way to know what number to type for it in the
   first place. Fixed by carrying `(stable_index, item)` pairs through
   pagination and search filtering, so `goto` always resolves against
   the original list regardless of active filtering, and displaying that
   stable index — `(#N)` — alongside the page-relative selection number
   on every line. Caught a second bug fixing the first: the "clear
   search filter" branch reset the working set to the plain item list
   instead of the now-indexed-pairs list, a type mismatch that would
   have broken on the very next render after clearing a filter.
5. **Testing note:** `netbbs.net.picker` has no PyNaCl dependency, and
   its 20 tests (17 pre-existing plus 3 new regression tests added for
   the goto/stable-index fix) were run for real against actual loopback
   sockets, the same rigor as `test_telnet.py` — including reproducing
   the exact broken scenario before the fix, then confirming it resolved
   correctly after. `chat_flow.py`'s actual picker wiring (imports
   `netbbs.auth` via the rest of the module) remains unverified by
   Claude; manual testing is the way to confirm boards and channels both
   feel right end-to-end.

## Sign-off notes, round 15 (self-color in chat + immediate menu keys)

Prompted by Thiesi testing round 14 successfully and requesting two
further refinements.

1. **Own chat messages now visually distinct.** New `SELF_COLOR`
   (bright magenta) in `netbbs.rendering.theme`, distinct from
   `ACCENT_COLOR` (gold, used for everyone else's names). Required a
   real architectural change, not just a new color constant: the sender
   can no longer receive the identical broadcast string as everyone
   else, since they need different formatting. `send_loop` now writes a
   self-colored copy directly to the sender's own session and broadcasts
   a separately-formatted, accent-colored copy to everyone else
   (sender now excluded from that broadcast, the reverse of round 10's
   original "include everyone" choice — that choice was about which
   *content* to send, this one is about using two different strings
   instead of one).
2. **A concurrency question this raised, actually verified rather than
   assumed:** two asyncio tasks (`send_loop`, `receive_loop`) now both
   call `session.write()` on the same connection. Confirmed safe with a
   real stress test (two tasks racing 20 writes each) — zero byte-level
   interleaving, only message-level reordering (equivalent to any
   real-time chat's inherent ordering ambiguity). Safe specifically
   because `TelnetSession.write()` buffers its bytes with one
   synchronous `self._writer.write()` call before ever `await`ing
   `drain()` — documented in `_chat_loop`'s docstring with the
   reasoning, not just the conclusion.
3. **New `Session.read_key()`** — reads one character and returns
   immediately, no Enter required, added to the `Session` ABC (`SSH`/
   web transports will need their own implementations later) and
   implemented in `TelnetSession` by reusing the same `_read_byte`/
   `_read_utf8_continuation`/`_discard_escape_sequence` primitives
   `read_line` already used. Deliberately scoped to genuine single-
   choice menu selections only (the main menu) — free-text prompts
   (board/channel names, post subjects, chat messages) correctly stay on
   `read_line`, since they're not a small enumerable choice set.
4. **A real, deliberate behavior loss, flagged rather than silently
   dropped:** the main menu previously accepted the full word ("boards")
   as an alternative to the single letter. Immediate single-key dispatch
   can't support that — acting on the very first keystroke means there's
   no way to know whether more characters are about to follow. Only the
   letter works now; documented in the code and called out to Thiesi
   directly.
5. **A real bug caught by actually running the tests, not just writing
   them:** the first `read_key()` implementation only echoed when
   `echo=True` and wrote nothing at all for `echo=False`, instead of
   masking with `*` the way `read_line()` correctly does. Caught
   immediately by the masking test failing with an unexpected EOF/empty
   read, fixed, then re-verified passing. Kept as a permanent regression
   test.
6. **Testing note:** all of this — `read_key()` (4 new tests) and the
   concurrency stress test — was executed for real against loopback
   sockets, same rigor as round 14, bringing `test_telnet.py` to 26
   passing tests. `chat_flow.py`'s actual self-color wiring (imports
   `netbbs.auth`) remains unverified by Claude; manual two-terminal
   testing is the way to confirm it end-to-end.

## Sign-off notes, round 18 (sort order, stable-ID goto, pinning, categories)

Prompted by Thiesi's own review of the picker/sort work, raising a
concrete example (vintage-computing boards with a single politics board
created in between them, sitting awkwardly in the middle under creation-
order) that surfaced several related design gaps at once.

1. **Creation-order sorting rejected as a real user-facing default** —
   confirmed as a pure implementation convenience (trivially stable,
   append-only) that was never actually chosen *for* the user. Three
   sort orders now supported for boards: **activity** (most recent post,
   confirmed as the default), **alphabetical**, and **volume** (total
   post count — a deliberately different signal from activity: a board
   with one post today but otherwise dead ranks high on activity, low on
   volume; the reverse is also possible). Channels support activity
   (in-memory, via `ChatHub.last_activity` — see point 4) and
   alphabetical; no volume sort, since channel messages aren't persisted
   at all. Per-user sort preference remains real future scope, pending a
   user-preferences system that doesn't exist yet — these are node-wide
   defaults in the meantime.
2. **A genuine technical tension identified and resolved: configurable
   sort order breaks `goto`'s whole premise.** `goto`'s value is a
   number you can remember and return to — that only holds if the
   underlying order never reshuffles existing items. Creation-order
   happens to guarantee this (append-only); alphabetical doesn't (insert
   one new item and everything after it shifts); activity-based
   sorting actively guarantees instability (changes on every new post).
   Resolved by decoupling entirely: `pick_item` now takes a
   `stable_id_of` callable (database ID, typically) supplying each
   item's permanent identity, fully independent of whatever order the
   caller's list happens to be sorted in for browsing. Confirmed with
   Thiesi over the alternative (accepting `goto` as only stable within
   one sort choice). Verified with genuinely non-positional, non-
   sequential stable IDs (not just IDs that happen to equal list
   position, which wouldn't have proven the decoupling actually works).
3. **Favorites — designed for, not built.** A per-user favorites list is
   just another list through the same picker with the same stable IDs,
   once user-scoped storage exists (it doesn't yet). Deliberately called
   out as deferred rather than silently dropped.
4. **Channel "activity" tracked in-memory (`ChatHub.last_activity`), not
   in the database.** Chat messages themselves aren't persisted (design
   doc §15/round 10) — adding a persisted "last activity" column would
   need a database write on every single chat message, working directly
   against that same ephemeral-by-design reasoning. `netbbs.chat.
   channels.list_channels` stays free of any dependency on the in-memory
   hub (pinned + alphabetical only, both genuinely DB-queryable); the
   caller (`netbbs.net.chat_flow`) combines both sources via two stable
   Python-level sorts.
5. **Pinning added for both boards and channels** — a boolean column,
   always sorting first regardless of the chosen order otherwise. Direct
   parallel to the post-pinning already designed in §13 (folds under the
   `edit` permission); no new mechanism needed, matching Thiesi's own
   instinct that explicit manual ordering wasn't necessary beyond this.
6. **Two-level categories added for both boards and channels** — a
   category, optionally containing sub-categories, capped there
   (checked with Thiesi explicitly: arbitrary depth wasn't wanted, but
   two levels were worth having if not overly complex to add; judged
   cheap enough to include). Enforced in application code at creation
   time (SQLite can't express "does this row's parent itself have a
   parent" as a plain CHECK) — verified directly that a third-level
   attempt is correctly rejected. Separate `board_categories`/
   `channel_categories` tables rather than one shared polymorphic table,
   consistent with boards and channels already being fully independent
   subsystems everywhere else in the schema.
7. **A real correctness detail surfaced by mixing categories and boards/
   channels in one picker call, caught before shipping, not after:**
   `Category` and `Board`/`Channel` rows come from different tables, so
   their raw database IDs can collide (both start at 1) — mixed into one
   picker call so a user can pick either a category to drill into or an
   item directly, that collision would make `goto` ambiguous between two
   different things sharing the same displayed number. Resolved by
   negating category IDs for picker purposes only (`-item.id`); board/
   channel IDs stay unchanged, so no existing `goto` numbers were
   affected. Verified directly with genuinely colliding IDs (a category
   and a board both assigned raw id=1), confirming `goto` resolves to
   the correct one.
8. **Browsing is now recursive, capped naturally at two levels**: pick a
   category to drill in (recursing into the same function scoped to that
   category), or a board/channel to open it directly. A level with no
   categories falls back to the exact flat picker experience from
   before categories existed — most nodes with few boards will likely
   never see the category UI at all, which is the right default.
9. **Testing note:** `netbbs.boards.categories`/`netbbs.chat.categories`
   have no PyNaCl dependency (only need `netbbs.storage`), so the full
   depth-cap enforcement, CRUD operations, and the category/board ID-
   collision disambiguation were all genuinely executed against real
   databases and real sockets, not just syntax-checked — including
   reproducing the exact three-level-rejection and colliding-ID
   scenarios directly before writing the corresponding formal tests.
   `netbbs.boards.boards`/`netbbs.chat.channels`'s actual sort-order SQL
   (activity/alphabetical/volume, combined with pinning) was verified by
   executing the raw queries against a real database seeded with
   realistic data (mirroring Thiesi's own vintage-computing/politics
   example), confirming pinned-first ordering and all three sort modes
   produce the expected order. `login_flow.py`/`chat_flow.py`'s actual
   recursive category-browsing wiring (imports `netbbs.auth`) remains
   unverified by Claude; manual testing (see README) is the way to
   confirm the full experience end-to-end.

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

Implements round 19's design. Also closes out `list_boards`' default-sort
test coverage gap surfaced by a pytest run on the actual NetBSD deployment
target (see below) before this round's chat work began.

1. **Retention limit: 100 events per channel, node-wide, confirmed with
   Thiesi.** Configurable via `node_config` (key
   `chat_scrollback_limit`), same mechanism and same validate-with-
   fallback-to-hardcoded-default pattern as `netbbs.timeutil`'s display
   format/timezone settings (`netbbs.chat.scrollback.
   get_scrollback_limit`/`set_scrollback_limit`).
2. **Real design addition beyond round 19, raised by Thiesi during
   review: join/leave presence events are persisted and replayed
   alongside chat messages, not just message text.** Without this, a
   replayed message from someone who has since left the channel carries
   no indication of that — it reads as if they're still present. A
   `kind` discriminator column (`'message' | 'join' | 'leave'`) on one
   `channel_messages` table carries this, rather than two separate
   tables, since both share identical channel/ordering/trimming
   semantics and there's no case where a replay would want one without
   the other. Trimming (count-based, per round 19) counts all three
   kinds against the same shared budget, not a separate one per kind.
3. **Storage is structural, not pre-rendered ANSI** — `ChannelMessage`
   rows store `kind`/`author_label`/`author_fingerprint`/`body`, and
   `netbbs.net.chat_flow` renders them the same way it renders live
   events. Same storage/display separation `netbbs.boards.boards.
   list_boards` already keeps; means a future theme change, or a
   non-ANSI client (the web/xterm.js connectivity work later in Phase 1)
   needs no data migration.
4. **Replayed messages are never shown in `SELF_COLOR`** — that color is
   a live-typing affordance ("this is what I just sent"), which doesn't
   carry meaning when reading back history that may have originated from
   a different session than the one now viewing it.
5. **Round 19 point 5's privacy note implemented as literal bracketing
   text** around the replay (`--- scrollback ---` / `--- end scrollback
   (last N events retained) ---`, both muted-color), rather than a
   one-line disclaimer — verified directly, over a real Telnet
   connection with two sequential sessions, that this reads clearly:
   session A joins an empty channel (no scrollback shown at all — a
   channel with no history shows nothing extra), posts one message,
   quits; session B then rejoins and sees exactly session A's join,
   message, and leave events replayed, bracketed by the retention note,
   followed by its own fresh join line.
6. **Trim-on-insert, not a background job** — after every insert, delete
   rows for that channel beyond the configured limit in the same
   transaction. Consistent with there being no background-job machinery
   anywhere else in Phase 1; revisit only if per-insert trimming cost
   ever actually shows up as a real bottleneck (design doc §14 already
   flags write contention, not CPU, as the predicted first bottleneck at
   this project's target scale).
7. **Unrelated fix noted here for the record, not a design decision:**
   before this round's work began, running the existing test suite on
   the actual NetBSD deployment target (rather than only Windows, where
   development happens) surfaced a stale test asserting board-listing
   creation-order — behavior round 18 had already explicitly rejected in
   favor of activity-order-by-default. It only passed on Windows by
   accident (two boards created back-to-back can tie on a coarse clock,
   and the tie-break happened to match creation order); NetBSD's finer
   clock resolution broke the tie and exposed the stale assertion. Fixed
   by asserting the actually-confirmed round-18 behavior instead, using
   explicit timestamps rather than relying on wall-clock timing between
   two calls — plus added missing coverage for `alphabetical`/`volume`/
   pinned ordering, none of which had any tests at all. No `list_boards`
   behavior changed; this is a test-correctness fix, not a design
   change, and it's the reason this repo's tests should periodically
   actually be run on NetBSD, not just Windows — the environment split
   this project already has to account for.

## Sign-off notes, round 21 (file area core — implemented; upload/download transfer deferred)

Phase 1's file areas, split deliberately into two pieces after a real
design gap surfaced mid-discussion (point 3 below): browsable, level-
gated file area core shipped now; actual upload/download transfer is
its own separate, not-yet-built piece of work.

1. **Schema mirrors `netbbs.boards` closely** — `file_area_categories`/
   `file_areas`/`files`, content-addressed IDs (§7), separate read/write
   level-gating (confirmed already in §13: "Board & file area
   permissions... separate read/write access"). Strict §1 terminology
   followed: "area," never "board," anywhere in code, tables, or
   messages.
2. **Categories, pinning, and sort order (activity/alphabetical/volume)
   built in from the start**, unlike boards/channels, which got this
   shape retrofitted in round 18 after shipping without it. Confirmed as
   the right call given the project's own stated anti-retrofit principle
   (§2/§13) — no reason to knowingly repeat a migration already done
   twice.
3. **A real design gap found and resolved through two rounds of
   back-and-forth, not decided casually:** the first proposed transfer
   mechanism (a simple length-prefixed raw-byte protocol layered
   directly on the Telnet byte stream, IAC-doubled per RFC 854) was
   initially confirmed, but turned out to assume a NetBBS-aware client
   driving it — a generic Telnet client (PuTTY, Windows Telnet, `nc`)
   has no way to stream a local file's bytes through a raw-byte protocol
   a human can't type. Classic BBSes solved exactly this with
   client-side protocols like Zmodem that terminal *emulators*
   auto-detect and drive; nothing analogous exists here. Presented as a
   three-way fork (companion CLI tool / real Zmodem / defer transfer
   entirely) — **confirmed: real Zmodem support**, authentic to the BBS
   tradition, but explicitly scoped as its own separate task (packet
   framing, CRC16/CRC32, the ZRQINIT/ZRINIT/ZFILE/ZDATA/ZEOF/ZFIN
   handshake state machine, retry/error recovery) rather than
   improvised inline here, given its size and correctness risk.
4. **File area core ships now without live upload/download**, exactly
   the same bootstrap sequencing boards and channels already went
   through — both existed, browsable and level-gated, well before any
   admin/SysOp creation UI existed for them; files reach a node today via
   `scripts/create_test_file.py`, mirroring `create_test_board.py`/
   `create_test_channel.py`.
5. **Storage: filesystem, not SQLite blobs** — content-addressed by
   sha256, sharded two hex characters deep (`netbbs.files.storage`,
   `<root>/<aa>/<aabbccdd...>`, the same pattern git's own object store
   uses), rooted at `<db_path>_files` alongside the node's database file.
   A deliberate side effect, not the primary motivation: two uploads
   with byte-identical content share one stored blob regardless of
   filename/area/uploader.
6. **A file's content-addressed `file_id` is computed from its sha256
   *and* upload metadata (area, filename, uploader, timestamp) — not a
   pure content hash.** Two uploads of byte-identical content are still
   distinct events (distinct `file_id`s, sharing only the underlying
   stored bytes) — matches how two boards created by the same creator
   already get different `board_id`s despite otherwise-similar content.
7. **`upload_file` takes a complete file as one in-memory `bytes`
   buffer, not a stream** — appropriate at this project's stated scale
   (§14) and for how files reach it today (a dev script reading a local
   file whole). Revisit once real Zmodem transfer (point 3) exists and
   actually streams bytes incrementally rather than handing over a
   complete buffer.
8. **Verified directly over a real Telnet connection**, not just
   pytest: logged in, opened the new `[F]ile areas` main-menu option
   (added alongside `[B]oards`/`[C]hat`), picked an area seeded via
   `scripts/create_test_file_area.py`/`create_test_file.py`, and
   confirmed the file listing (name, human-readable size, uploader,
   upload time, description) renders correctly.
9. **A real, same-class bug caught while writing tests, not shipped:**
   a test asserting two identical-content uploads get different
   `file_id`s initially flaked — two `upload_file` calls close enough
   together landed on the exact same microsecond-resolution timestamp,
   colliding on `file_id` and raising the same "identical content
   uploaded twice in the same instant" error `netbbs.boards.posts.
   create_post` already documents as an accepted edge case. Fixed by
   patching `utc_now_iso` to guaranteed-distinct values in that specific
   test, the same "don't rely on wall-clock timing between two nearby
   calls" lesson round 20's point 7 already surfaced for board listing —
   now the second time this exact class of flakiness has shown up,
   worth remembering as a standing hazard whenever a test wants two
   observably-different timestamps close together.

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

Implements round 22's SSH design (points 1-5). Web connectivity (round
22 points 6-9) remains unimplemented.

1. **A real architectural finding, not anticipated in round 22: Telnet's
   character-mode line/key-reading logic (`netbbs.net.telnet`) needed to
   move to a new shared module, `netbbs.net.char_input`, rather than
   being duplicated in `netbbs.net.ssh`.** Once `asyncssh`'s own
   client-visible line editor is disabled (`line_editor=False` at server
   construction — the SSH equivalent of Telnet's character-mode
   negotiation), it hands over exactly the same kind of raw,
   un-echoed byte stream Telnet does, needing the exact same backspace/
   UTF-8-continuation/escape-sequence-discarding/CR-LF handling.
   Duplicating ~250 lines of that logic would have meant two copies
   free to drift out of sync on subtle correctness properties (e.g. the
   CSI-vs-SS3 escape-sequence shapes). Extracted behind a small
   `ByteSource` protocol (`read_byte`/`read_byte_with_timeout`) that
   each transport implements against its own primitives; verified
   directly that the extraction was behavior-preserving by running
   `TelnetSession`'s full existing test suite unchanged afterward — all
   27 tests passed with zero modifications needed, and 15 new
   transport-agnostic unit tests were added directly against
   `char_input` using a fake `ByteSource`.
2. **`asyncssh` configuration confirmed empirically, not just from
   docs:** `line_editor=False` (server-wide) plus `encoding=None`
   (binary mode, both directions) reproduces Telnet's character-mode
   contract exactly — verified with a real loopback `asyncssh` client
   before writing `SSHSession` itself, then again through the actual
   `SSHSession`/`SSHServer` classes. Binary mode specifically avoids
   `asyncssh` decoding UTF-8 one raw byte at a time itself, which would
   corrupt multi-byte characters the same way a naive byte-at-a-time
   decode would anywhere else in this codebase.
3. **Terminal resize delivered as an exception (`asyncssh.
   TerminalSizeChanged`) raised out of `stdin.read()`,** not a callback
   — confirmed directly against a real client resizing mid-session.
   `SSHSession.read_byte` catches it, updates `terminal_width`/
   `terminal_height` in place, and returns `None` (a transport-level
   action with no data, exactly matching how `TelnetSession.read_byte`
   already treats a Telnet NAWS subnegotiation) — `char_input`'s shared
   reading loop needed no changes to handle this uniformly.
4. **A new auth entry point, `netbbs.auth.users.authorize_public_key`,
   added alongside the existing `authenticate_keypair`, not reusing
   it.** `authenticate_keypair` expects a caller-generated challenge and
   a signature over it — built for a hypothetical NetBBS-aware client
   that doesn't exist. SSH's own protocol already proves private-key
   possession before `SSHServer.validate_public_key` is ever called;
   calling `authenticate_keypair` there would mean demanding a second,
   redundant signature over a challenge nothing generated.
   `authorize_public_key` only checks authorization (does this key
   belong to this username), trusting the transport's already-completed
   proof of possession.
5. **A dedicated SSH host key, not the node's `netbbs.identity`
   keypair** — generated on first use and persisted at
   `<db_path>_ssh_host_key`, mirroring `netbbs.files.storage`'s
   `<db_path>_files` convention for keeping a node's data predictably
   co-located. Reusing the node's Link identity keypair as its SSH host
   key was considered and rejected: it would tie two independent
   concerns together (this node's eventual Link identity vs. its SSH
   host identity) for no real benefit, and SSH host keys have their own
   file-format/rotation conventions separate from `netbbs.identity`'s.
6. **`asyncssh` (and therefore `cryptography`) kept as a separate `ssh`
   extra in `pyproject.toml`, not a core dependency** — confirming round
   22 point 2's scoping. `netbbs.__main__` starts the SSH listener only
   if `asyncssh` is importable, logging a one-line note and continuing
   Telnet-only otherwise, rather than failing the whole node startup —
   a Telnet-only deployment genuinely never needs to install
   `cryptography`/Rust's build chain at all.
7. **Verified at three levels, not just pytest:** (a) 12 new integration
   tests in `tests/test_ssh.py`, spinning up a real `SSHServer` and
   connecting with `asyncssh`'s own client — covering password auth,
   Ed25519 pubkey auth (success, wrong key, and each auth method
   correctly rejected for an account that doesn't support it),
   terminal size/resize, character echo, and abrupt-disconnect handling;
   (b) manual probe scripts against the real `SSHSession`/`SSHServer`
   classes confirming the same; (c) the actual, unmodified OpenSSH
   client (not `asyncssh`'s client library) — `ssh -o BatchMode=yes -i
   <ed25519 key> bob@host whoami` — completing a full handshake,
   authenticating via Ed25519 pubkey alone with no password fallback,
   and receiving the exact NetBBS welcome banner and login prompt.
   Fully-interactive verification via the real `ssh` CLI (typing through
   a live session, not a single batch command) was attempted but ran
   into Windows-specific PTY/pipe semantics scripting an interactive
   subprocess reliably — a platform quirk of the Windows dev environment
   piping to a real terminal client, not a NetBBS or protocol issue
   (the identical interactive flows already passed against asyncssh's
   own client, which implements the same wire protocol). Worth a real
   interactive `ssh` session directly from a POSIX shell (Thiesi's
   NetBSD target, or any Linux/macOS machine) as a final sanity check,
   not re-litigating the protocol-level work already verified here.

## Sign-off notes, round 24 (real Zmodem support — implemented)

Implements the file-transfer piece deferred out of round 21 and further
scoped in this round's own discussion (below) before any code was
written.

1. **Build-vs-buy checked before writing anything, per Thiesi's
   question.** Three existing options surveyed: `modem` (PyPI) — ports
   XMODEM/YMODEM/ZMODEM to Python, but abandoned since 2011, Python-2-
   era, synchronous/callback-based (would need as much adaptation work
   to fit this project's asyncio `Session` abstraction as writing fresh
   code, while inheriting 15 years of unaudited code); `trzsz` — not
   actually ZMODEM, a different proprietary protocol incompatible with
   real Zmodem terminals (SyncTERM, lrzsz), which would have defeated
   the entire point; `xmodem` (actively maintained) — XMODEM only, not
   ZMODEM. None viable; confirmed writing this from scratch.
2. **Scope, confirmed with Thiesi before writing the state machine:**
   CRC-16 only (the mandatory baseline, not the negotiated CRC-32
   enhancement); no resume/crash-recovery (every transfer starts at
   offset 0); no batch mode (one file per transfer, matching the
   file-area upload/download model); **no retry/timeout resync state
   machine** — classic ZMODEM's retries exist for a noisy serial line
   dropping/corrupting bytes, a failure mode that essentially doesn't
   happen over Telnet/SSH's TCP transport. A CRC mismatch or malformed
   frame raises `ZmodemError` and aborts immediately rather than
   attempting recovery.
3. **One deliberate, narrow exception to "no timeouts," added during
   implementation, not part of the original ask:** every point waiting
   on the peer's *next expected response* (not mid-transfer bulk data,
   which has no fixed duration) is bounded by a 15-second timeout. Without
   it, invoking `/upload` or `/download` against a terminal that simply
   doesn't support Zmodem — the most likely real failure mode, not data
   corruption — would hang the whole session forever. A bounded
   wait-then-abort is still "abort on error, don't attempt recovery,"
   not a retry loop.
4. **`netbbs.net.session.Session` gained two new abstract methods,
   `read_byte`/`write_raw`**, formalizing raw byte I/O both
   `TelnetSession` and `SSHSession` already had the pieces for (Telnet's
   IAC-transparent `read_byte` already existed from the char_input
   extraction in round 23; `write_raw` is new — IAC-doubles literal
   0xFF bytes for Telnet, since ZMODEM's own framing can genuinely
   produce them, unlike `write()`'s UTF-8 text where 0xFF never
   appears). SSH's `write_raw` needs no escaping — a binary-mode SSH
   channel is already 8-bit clean. `netbbs.net.zmodem` works against
   either transport uniformly through this, the same abstraction
   boundary the rest of the networking code already relies on.
5. **A real desync bug found and fixed while testing, not shipped:** the
   receiver initially sent `ZRINIT` twice — once proactively at start,
   then again upon seeing the sender's `ZRQINIT` (mirroring the spec's
   literal wording, "if the receiving program receives a ZRQINIT header,
   it resends the ZRINIT header," applied too literally). Since both
   sides in this implementation always send their opening frame
   unconditionally, the second `ZRINIT` was always redundant and
   desynced the header stream — the sender's next `_wait_for_header`
   (expecting `ZRPOS`) picked up the extra `ZRINIT` instead. Caught
   immediately by the round-trip test suite (every `test_round_trip_*`
   test failed identically), not discovered later.
6. **Verified at three levels**, escalating in realism: (a) 15 unit/
   round-trip tests in `tests/test_zmodem.py`, including this module's
   own sender talking to its own receiver over an in-memory duplex
   pipe — covering CRC-16, ZDLE escaping (including all 256 byte
   values and every reserved protocol byte appearing in file content),
   multi-chunk transfers, corruption detection, cancel-signal handling,
   and the handshake timeout; (b) 2 new integration tests confirming
   `write_raw`/`read_byte` correctly IAC-double/undouble through the
   *real* `TelnetServer` over an actual loopback socket, not just the
   in-memory fake; (c) a full manual run against a real, running node:
   logged in over actual Telnet, navigated to a file area, ran
   `/download` and `/upload` through the genuine menu flow
   (`netbbs.net.file_flow`), with a client-side test harness re-using
   `netbbs.net.zmodem`'s own `send_file`/`receive_file` as the *client*
   role — proving the full server ↔ real-socket ↔ client path round-trips
   file bytes byte-for-byte, including a payload deliberately packed
   with every reserved protocol byte (ZDLE, ZPAD, XON/XOFF, IAC, CR/LF).
7. **What (c) does *not* prove, flagged honestly rather than glossed
   over:** genuine interoperability with an actual external Zmodem-
   capable terminal client (SyncTERM, lrzsz) — no such client is
   available in this sandboxed dev environment. The framing/CRC/
   escaping logic is now thoroughly exercised and matches the spec as
   researched, but a real third-party client is the only thing that can
   confirm actual interop. Worth a direct test from Thiesi's own
   machine or NetBSD target once convenient — the same "verify with a
   real client, not just your own code talking to itself" gap round 23
   flagged for interactive SSH, now flagged here for the same reason.

## Sign-off notes, round 25 (web/xterm.js connectivity — implemented)

Implements round 22's web design (points 6-9), the last open piece of
Phase 1 connectivity.

1. **`netbbs.net.web`: `aiohttp`-based, mirroring `TelnetServer`/
   `SSHServer`'s shape exactly** (`WebServer` with `start`/
   `serve_forever`/`stop`/`port`; `WebSession` implementing the same
   `Session` ABC) — confirms the abstraction boundary `netbbs.net.
   session.Session` was designed for actually holds across a third,
   structurally very different transport (request/response HTTP plus a
   push-based websocket, not a persistent byte stream).
2. **`WebSession` does *not* reuse `netbbs.net.char_input`'s byte-level
   `ByteSource` protocol** — confirmed deliberate, not an oversight.
   That abstraction exists to reconstruct UTF-8 characters from a raw
   byte stream one byte at a time; a browser's `onData` event already
   hands over complete, decoded Unicode characters (and can hand over
   *several at once* — a paste, or a multi-byte escape sequence — unlike
   Telnet/SSH's strictly one-byte-at-a-time delivery). `WebSession`
   instead queues individual characters fed by a background task
   draining the websocket, with its own character-level (not
   byte-level) `read_line`/`read_key`, escape-sequence stripping, and
   backspace handling — structurally parallel to `char_input` in what
   it does, deliberately not sharing code with it, since the underlying
   unit (a byte vs. an already-decoded character) genuinely differs.
3. **File transfer is explicitly not available over this transport —
   decided during implementation, not part of round 22's original
   scope, but a direct consequence of it.** Real Zmodem interop
   (`netbbs.net.zmodem`) depends on the *terminal client* auto-detecting
   and driving the protocol — a capability of native terminal emulators
   (SyncTERM, lrzsz), not of a JS widget in a browser tab. Building a
   from-scratch in-browser Zmodem implementation (or any alternative
   web-native transfer mechanism, e.g. a plain HTTP upload/download
   endpoint) was explicitly out of scope for "connectivity" and would
   need its own design pass if ever wanted. `WebSession.read_byte`/
   `write_raw` exist only to satisfy the `Session` ABC and raise
   `NotImplementedError`; `netbbs.net.file_flow`'s upload/download
   handlers now catch that alongside `ZmodemError`, so a user on the
   web transport sees a clear in-session "not available" message
   rather than a crashed session.
4. **xterm.js 6.0.0 + the official fit addon vendored into the repo**
   (`netbbs/web/static/` — a separate top-level package from `netbbs.
   net`, per round 22 point 8, holding only static assets; the actual
   `WebSession`/`WebServer` code lives in `netbbs.net.web` alongside
   the other transports), MIT-licensed (`static/xterm-LICENSE.txt`
   included), fetched directly rather than via `npm` (not a dependency
   this project needs at install or build time). `aiohttp` serves both
   the static assets and the websocket endpoint from one process, and
   is its own optional `web` extra in `pyproject.toml` — same "a
   Telnet/SSH-only node shouldn't need this installed" reasoning as
   the `ssh` extra, though `aiohttp` itself has no Rust dependency
   chain the way `cryptography` does, so this is a lighter-weight
   opt-in.
5. **Verified at multiple levels, with one honest, explicitly-flagged
   gap:** 15 new integration tests spin up a real `WebServer` and
   connect a real `aiohttp` client websocket — covering the structured
   JSON protocol, character echo, backspace, escape-sequence stripping,
   resize, disconnect handling, the `NotImplementedError` file-transfer
   guard, and static-asset/index-page serving; a real running node was
   also smoke-tested via `curl` for HTTP-layer delivery (correct
   content-type, correct byte-for-byte file sizes matching the vendored
   assets). **What could not be verified: actual browser rendering and
   interaction** — no browser-automation tool is available in this
   sandboxed environment. Mitigated as far as possible without one: every
   xterm.js API the custom JS shim (`static/netbbs-terminal.js`) calls
   (`Terminal`, `cursorBlink`, `scrollback`, the `background` theme key,
   `onData`, `loadAddon`, `FitAddon.FitAddon`) was directly grepped for
   and confirmed present in the actual vendored bundle, not just
   assumed from memory — but this is not a substitute for opening the
   page in a real browser. Worth a direct check from Thiesi (or a
   future session with browser tooling) before considering this fully
   done, the same standard round 23/24 already held SSH/Zmodem to.

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

Three audit issues (#15, #1, #3) bundled deliberately into one round
rather than three: #1's "insecure listeners require explicit opt-in"
and #15's "runtime behavior driven by validated configuration" are the
same underlying config model looked at from two angles, and #3's
throttling policy needed to be *part of* that config model (an
operator-tunable `[throttle]` table) from the start rather than bolted
on separately. Doing them as one pass avoided building the config
plumbing twice.

1. **New `netbbs.net.nodeconfig` module — an optional TOML file plus
   CLI overrides (CLI wins), validated before use.** Python 3.11's
   stdlib `tomllib` (read-only) was used rather than adding a
   dependency — consistent with this project's general dependency-
   minimalism (see CLAUDE.md re: PyNaCl over `cryptography`). Unknown
   config-file sections/keys are a hard error (`ConfigError`), not
   silently ignored — a typo'd `[trottle]` table should fail loudly at
   startup, not silently run with defaults an operator thinks they
   overrode. `netbbs.__main__.main()` catches `ConfigError` and exits
   with a one-line message, never a raw traceback.
2. **Secure defaults, resolving issue #1:** SSH defaults *enabled*
   ("make SSH the secure default interactive transport" — the issue's
   own recommended direction); Telnet and the plain-HTTP web transport
   both default *disabled*, and even when explicitly enabled without
   an operator-chosen `host`, default to `127.0.0.1` rather than every
   interface. `NodeConfig.describe_insecure_bindings()` logs a
   prominent warning for either plaintext transport bound somewhere
   other than loopback; SSH is excluded from this check at any bind
   address, since it isn't plaintext regardless of where it's exposed.
   **No TLS support was built directly into the web transport** —
   confirmed as the deliberately smaller-scope option (the issue
   explicitly offered "support TLS directly... or clearly require/
   document a reverse proxy" as alternatives): a TLS-terminating
   reverse proxy in front of a loopback-bound `aiohttp` instance is
   the documented, supported path (see README), avoiding ongoing
   certificate-handling maintenance surface this project doesn't need
   to own when every mainstream reverse proxy already solves it well,
   and SSH already offers a secure, no-extra-infrastructure option for
   operators who don't want to run one.
3. **Cross-connection login throttling, resolving issue #3 — new
   `netbbs.net.throttle.LoginThrottle`:** three independent token-
   bucket budgets (per-source-address, per-username, node-wide global),
   node-lifetime shared state that reconnecting does not reset, plus a
   separate concurrent-unauthenticated-session cap. Token buckets over
   hard lockouts, per the issue's own explicit preference — a bucket
   run dry simply refills, so there's no persistent locked-out state
   either a legitimate user gets stuck in or an attacker could
   weaponize against someone else's account. All-or-nothing
   consumption across the three budgets (checked via a non-consuming
   `peek`, only actually consumed if all three currently have a token)
   so a rejected attempt never drains an unrelated budget — e.g. two
   legitimate users sharing a NAT'd source address don't have each
   other's per-username budgets affected by the rejection itself. Per-
   key buckets (source/username) are capped at a configurable
   `max_tracked_keys` via LRU eviction, directly answering the
   acceptance criterion that this be "bounded in memory and cannot be
   abused with arbitrary usernames/IP keys" — **an honest, explicitly
   accepted limitation**, not silently glossed over: an attacker who
   also rotates *both* source and username defeats per-key throttling
   by construction (each fresh key gets a fresh bucket), which is
   exactly why the global budget layer exists as a backstop that
   doesn't depend on key identity at all (verified directly by a test
   exercising 20 distinct source/username pairs against a small global
   budget — only the global cap's worth get through).
4. **The expensive-verification budget check happens *before*
   `authenticate_password_async` runs, not after** — a throttled
   attempt never pays Argon2's real cost, which is the actual DoS half
   of issue #3 (issue #2's off-loop/bounded-concurrency fix bounds
   *concurrent* Argon2 cost; this bounds how often it runs at all).
   Verified with a test that makes a real call to
   `authenticate_password_async` an assertion failure once the budget
   is exhausted, not just checking the final rejection message.
5. **Idle timeout and overall login deadline are two genuinely
   different mechanisms, both new `netbbs.net.login_flow._login`/
   `handle_session` behavior:** each individual prompt read
   (username, password) is wrapped in its own
   `asyncio.wait_for(..., timeout=unauthenticated_idle_timeout_seconds)`
   — resets on any activity, catches a client that goes silent
   mid-prompt. Separately, the *whole* login attempt loop is wrapped
   in one `asyncio.wait_for(..., timeout=login_deadline_seconds)` —
   catches a client that stays active but never actually finishes
   (verified with a fake session that responds every 50ms forever,
   confirming the overall deadline fires even though no individual
   read ever times out). Both new `LoginOutcome` members
   (`IDLE_TIMEOUT`, `THROTTLED`) get their own distinct user-facing
   message, matching the existing `ATTEMPTS_EXHAUSTED`/`BLOCKED`
   pattern.
6. **A real, if narrowly-averted, correctness risk deliberately
   checked rather than assumed:** this session's own round-5 sign-off
   note records a genuine asyncio bug where a *nested* `asyncio.
   wait_for` (an outer one wrapping a call chain using `asyncio.
   wait_for` internally) intermittently misbehaved when both timeouts
   were tuned to the same narrow window. The new idle-timeout wrapper
   is exactly this shape — `TelnetSession.read_byte_with_timeout` does
   use `asyncio.wait_for` internally, for CSI escape-sequence handling
   — so this was verified directly against a real Telnet socket rather
   than trusted on the (correct, but round-5 already proved
   insufficient on its own) reasoning that a 60-second-scale idle
   timeout and sub-second internal timeouts are never close enough to
   race. `tests/test_telnet_idle_timeout.py` specifically stresses the
   case that would trigger it — an arrow-key escape sequence arriving
   *while* the outer idle-timeout wait_for is armed — parametrized to
   repeat 5x, the way the original round-5 bug only ever surfaced
   under repetition. All reliable.
7. **The concurrent-unauthenticated-session budget is scoped to
   exactly the login phase**, acquired at the top of `handle_session`
   and released in a `finally` immediately once `_login` resolves one
   way or another — *before* the main menu ever runs, not held for a
   session's whole connected lifetime. An authenticated user sitting
   connected for hours doesn't count against it; only genuinely
   unauthenticated connections do, which is the actual risk this
   budget guards against.
8. **SSH gets a deliberately narrower slice of this throttling story,
   not the full treatment — a scope boundary, not an oversight:** SSH
   authenticates during the SSH protocol handshake itself
   (`_NetBBSSSHServer.validate_password`), a different code path from
   `netbbs.net.login_flow.handle_session` that Telnet/web share. It
   *does* consult the same shared `LoginThrottle.allow_attempt`
   (per-source/per-username/global budgets) — confirmed by this
   session's own prior discovery, during PR #22's review, that issue
   #2's Argon2-offload fix originally missed this exact code path, so
   the same transport-agnostic-acceptance-criteria lesson was applied
   proactively here. But the idle-timeout/login-deadline/concurrent-
   session-cap machinery is *not* reimplemented for SSH: asyncssh
   already owns that connection's handshake lifecycle via its own
   `login_timeout` option (configured from the same
   `login_deadline_seconds` value, passed through `SSHServer.start`),
   which is asyncssh's documented mechanism for exactly this ("the
   time allowed for a client to send a version string, complete key
   exchange, and complete user authentication, before the connection
   is dropped") — reimplementing it independently would mean two
   competing timeout mechanisms racing on the same connection for no
   benefit. Verified directly against a real, deliberately-silent TCP
   connection (not assumed from reading asyncssh's docs) that the
   configured `login_timeout` actually disconnects it at the
   configured time, not eventually via some other mechanism. Also
   verified that a per-source budget exhausted on one SSH connection
   stays exhausted on the next SSH connection reusing the same
   `LoginThrottle` — the actual "reconnecting doesn't reset it"
   acceptance criterion, confirmed for SSH specifically, not just
   assumed to follow from the Telnet/web case.
9. **`netbbs.__main__` rewritten around one testable `run(config, *,
   shutdown_event)` coroutine, resolving issue #15:** starts every
   enabled+available listener, blocks on `shutdown_event.wait()`, then
   stops every listener and closes the database in a `finally`. The
   injectable `shutdown_event` was a deliberate design choice so the
   whole coordinated-shutdown path is unit-testable without a real
   subprocess or real OS signals (`tests/test_main_lifecycle.py`) —
   `main()`'s only job is wiring real SIGTERM/SIGINT into that same
   event via `loop.add_signal_handler`, falling back to `signal.signal`
   + `call_soon_threadsafe` where `add_signal_handler` raises
   `NotImplementedError` (Unix-only per the stdlib docs).
10. **Partial-start failure cleanup, verified against a real bind
    conflict, not simulated:** every listener start is wrapped so a
    failure partway through (a real second `asyncio.start_server` on
    an already-occupied port, in the test) stops whatever already
    started, in reverse order, before the failure is reported — an
    operator never ends up with an unintended subset of listeners
    silently running because a later one failed to bind. **A real bug
    found via manual testing, not caught by the first pass of unit
    tests:** the first implementation re-raised the raw underlying
    exception (`OSError`, an `ImportError`-shaped message, etc.)
    straight out of `_start_servers`, which `main()`'s
    `except StartupError` clause didn't catch — an actual port
    conflict on this dev machine (SSH's default port 2222 already in
    use) produced a multi-frame asyncio/asyncssh traceback instead of
    the clear message issue #15 asks for. Fixed by having every
    listener-start failure — not just the "nothing started at all"
    case — wrap into `StartupError` with the transport name and
    underlying reason, so `main()` has exactly one exception type to
    catch. Left as a explicit lesson in the commit/PR trail: this is
    exactly the kind of gap synthetic unit tests (which construct
    their own controlled failure scenarios) can miss, and only running
    the real command surfaced it — consistent with CLAUDE.md's
    "actually run it" standard.
11. **Zero listeners started is also a hard, clear failure** (`"no
    listener actually started"`), not a silently-idle process — e.g.
    every configured transport unavailable (SSH configured but
    `asyncssh` not installed). Verified by forcing an `ImportError` via
    `sys.modules`, not just asserted from reading the code.
12. **The flagged verification gap from this round's first pass was
    real, and Thiesi's own NetBSD machine caught an actual test bug in
    it — not just a hypothetical worth checking.** The first version
    of `tests/test_signal_handler_registration_triggers_shutdown_event`
    called `signal.getsignal(SIGTERM)` after `_install_signal_handlers`
    ran, then invoked whatever it returned directly. That's only a
    valid test of the Windows-only `signal.signal` fallback branch. On
    real POSIX (confirmed by an actual `pytest` run on Thiesi's NetBSD
    machine — a genuine `AssertionError`, not passed on faith),
    `_install_signal_handlers` takes the `loop.add_signal_handler`
    branch instead, which installs asyncio's own internal no-op C
    handler and dispatches the real callback later via a self-pipe —
    `signal.getsignal(SIGTERM)` in that case returns *asyncio's*
    placeholder, not this module's `_request_shutdown`, so the test was
    silently exercising the wrong code path on the one platform that
    actually matters (NetBSD is the deployment target; the Windows dev
    sandbox this was originally written in only ever exercises the
    fallback). Exactly the "actually run it, don't just reason about
    it" lesson this project has hit before (round 5's nested-`wait_for`
    bug), this time catching a test bug via real cross-platform
    execution rather than a code bug. **Fixed**, not worked around: the
    test now calls `signal.raise_signal(SIGTERM)` — a genuine C-level
    `raise()` — which correctly reaches whichever dispatch mechanism is
    actually installed on either platform, and then waits on
    `shutdown_event` for real, rather than asserting immediately.
    Deliberately not `os.kill(os.getpid(), sig)`: on Windows, `os.kill`
    with a real (non-zero) pid calls `TerminateProcess` instead of
    delivering a handleable signal (confirmed by hand earlier this
    round) — it would have killed the test process outright rather
    than exercised the fallback branch. Re-verified passing on the
    Windows dev sandbox with this fix, **and confirmed by Thiesi
    re-running the full suite on the actual NetBSD machine — all green,
    including this test.** Issue #15's graceful-shutdown requirement is
    now closed out end-to-end, on the real deployment target, not just
    reasoned about from a Windows sandbox.
13. **README's run instructions rewritten** — the stale "minimal
    manual-test entry point" framing and positional `db_path` argument
    are gone, replaced with the config-file/CLI-flag invocation, a
    worked `netbbs.toml` example, and explicit documentation of the
    secure-by-default posture, the throttling policy, graceful
    shutdown, and the rc.d-friendly foreground-process expectation for
    NetBSD deployments (this process still does not daemonize itself —
    confirmed as intentionally out of scope, the same as when `python
    -m netbbs` was first introduced; daemonization remains the service
    supervisor's job).

## Sign-off notes, round 29 (terminal rendering sanitization — implemented)

Issue #8: usernames, board/channel/file-area names and descriptions,
post subjects/bodies, chat messages, uploader labels, and filenames all
ultimately reach an ANSI-capable terminal, but until now nothing
stopped one of those values from containing a real ESC byte and
smuggling an arbitrary CSI/OSC/DCS sequence into a user's terminal —
spoofing the UI, faking a prompt, clearing the screen, altering the
window title via OSC, or (with a bidi override) making displayed text
visually differ from its actual content.

1. **New `netbbs.rendering.sanitize.sanitize_text()` — the one
   documented sanitizer every terminal-visible untrusted string now
   passes through, immediately before interpolation, per the
   acceptance criteria.** Two genuine either/or forks here were
   confirmed with Thiesi rather than picked unilaterally:
   - **Silent removal, not visible-marker replacement**, for stripped
     characters — simpler, no unpredictable length change, no new
     marker-choice surface of its own to get wrong.
   - **Only the 9 well-documented bidi embedding/override/isolate
     controls** (U+202A–U+202E, U+2066–U+2069 — the "Trojan Source"
     set with genuine visual-reordering capability), not the entire
     Unicode "Format" (Cf) category. The broader category also
     contains zero-width joiners/non-joiners and similar characters
     with legitimate uses in real multilingual/emoji text and no
     reordering capability of their own — stripping those would
     corrupt legitimate content for no benefit specific to this
     issue's terminal-injection/UI-spoofing threat model. Recorded as
     an explicit, narrower-than-maximal scope boundary, not an
     oversight.
2. **Removes every Unicode "Control" (Cc) character** — C0 (U+0000–
   U+001F, which includes ESC — removing ESC alone is sufficient to
   prevent untrusted text from ever forming a live CSI/OSC/DCS/APC
   sequence, since all of those require an ESC byte to introduce
   them), DEL (U+007F), and C1 (U+0080–U+009F, the 8-bit single-byte
   alternate encodings of the same sequence-introducers some terminals
   accept — stripping only the 7-bit ESC form would leave this path
   open, verified directly with a single-byte-CSI test case). Two
   narrow, explicit exceptions matching the issue's own "except
   explicitly permitted newline/tab semantics": tab is always kept;
   newline is kept only when the caller passes `allow_newlines=True`
   (post bodies — genuinely multi-line content — opt in; every
   single-line field leaves this at the default `False`, since an
   embedded newline in content displayed as one line is exactly the
   kind of thing that could fake extra output lines or spoof a
   prompt). Carriage return is **always** stripped regardless of
   `allow_newlines` — unlike `\n`, a lone `\r` isn't touched by
   `Session.write()`'s CRLF normalization and would reach the wire as
   a raw cursor-to-column-0 move.
3. **A real, avoidable mistake caught and fixed during this round, not
   shipped:** the first draft of `_BIDI_CONTROLS` used the actual
   invisible bidi characters as literal string content in the source
   file. Caught before commit — having genuinely hard-to-review,
   near-invisible characters sitting in the sanitizer's own source
   would have been an ironic, self-defeating risk (unauditable in a
   diff, exactly the property that makes them dangerous in untrusted
   content). Rewritten to build the set from explicit numeric code
   points via `chr()`, with the reasoning for why left as a comment
   directly above it so a future editor doesn't reintroduce the same
   mistake.
4. **Sanitizes on output, not on storage — the exact split the issue
   asked for.** Nothing `create_post`/`create_user`/`create_board`/
   `record_message`/`upload_file`/etc. writes to the database is ever
   touched; `sanitize_text()` is called only at the point a value is
   about to be interpolated into something written to a `Session`. A
   moderator, or a future Link re-transmission, still sees the
   original content. Verified explicitly at the one place this
   distinction is easiest to get backwards — `netbbs.net.chat_flow`'s
   live chat send loop calls `record_message(..., body=line)` with the
   *raw* typed line, while the direct self-write and the broadcast
   payload queued for other participants both use a separately
   sanitized copy.
5. **Distinguishing trusted NetBBS-generated ANSI markup from
   untrusted content is structural, not a property `sanitize_text`
   itself tracks:** callers sanitize only the untrusted piece, at the
   point of interpolation, before handing it to `netbbs.rendering.ansi.
   colored()` — never the whole already-composed line. `colored()`
   only *adds* its own SGR codes around whatever text it receives, so
   an ESC byte `sanitize_text` already removed from the untrusted
   piece can never be reintroduced by wrapping. Verified with a test
   asserting a picker header's own generated CSI/SGR codes survive
   completely intact alongside a hostile board name in the same
   picker's output — proving sanitization hit only the untrusted piece,
   not the whole rendered line.
6. **Centralized in `netbbs.net.picker.pick_item()` for board/channel/
   file-area listings** — `name_of`/`description_of` results are
   sanitized inside the shared picker itself, once, rather than
   requiring every current and future caller to remember it
   individually. Search matching (`query.lower() in name_of(item).
   lower()`) deliberately still uses the *raw* name — matching is a
   text comparison, not something written to a terminal, so there's
   nothing there to protect. Every other injection point — the login
   welcome banner (username), board post display (subject/body/
   author label, subject/author sanitized before the ANSI wrap, body
   sanitized *before* `reflow()` since `textwrap`'s width math counts
   raw characters too), live and scrollback chat (author label,
   message body, join/leave notices), and file area listing (filename,
   description, uploader label, plus the user-typed `/download
   <filename>` echoed back in error messages) — was audited and fixed
   directly at its own call site. A final grep sweep across every
   `session.write`/`write_line` call in `netbbs.net` confirmed no
   remaining untrusted-content interpolation was missed; the only
   other raw-string writes found (`netbbs.net.zmodem`'s `write_raw`
   calls) are real Zmodem protocol frames, not rendered terminal text
   — explicitly out of scope for a *rendering*-boundary sanitizer, and
   sanitizing them would corrupt the actual file transfer.
7. **Verified as actually wired, not just correct in isolation:** unit
   tests (`tests/test_sanitize.py`, 21 cases) cover exactly the
   acceptance criteria's named categories — ESC, all C0/C1 controls,
   OSC- and CSI-shaped payloads (including the single-byte C1 CSI
   form), all 9 bidi controls, and ordinary UTF-8 text passing through
   unchanged. Separately, `tests/test_terminal_sanitization.py` drives
   the *real* board/chat/file-area/picker code paths with a realistic
   combined hostile payload (fake-title OSC + BEL, clear-screen CSI,
   raw C1 control, embedded in otherwise-ordinary text) stored via the
   real `create_post`/`create_channel`/`record_message`/`upload_file`
   functions, and inspects what actually reaches a fake session's
   `write`/`write_line` calls — confirming the wiring at each site, not
   just that the sanitizer function itself is correct. One of these
   was deliberately used to catch a real mistake: an early version of
   the test asserted "no ESC byte anywhere in the output" at all,
   which is wrong once `colored()`'s own legitimate SGR codes are also
   present in the same output — caught immediately by the test itself
   failing against known-correct code, not by inspection, and fixed by
   asserting against the hostile payload's *specific* byte sequences
   instead. A second check, reverting sanitization at one call site
   and confirming the corresponding test fails before restoring it,
   confirmed the tests are actually meaningful rather than vacuously
   passing.
8. **Explicit non-goals for this round**, matching the issue's own
   framing as "Medium now; High once remote Link content is accepted"
   — nothing here is Link-specific, since no Link code exists yet
   (Phase 3); this is purely the local-node rendering boundary every
   current content path already goes through, positioned to need no
   rework once federated content arrives and flows through the same
   `Session.write`/`write_line` calls. Also explicitly not attempted:
   broader Unicode confusable-character detection (homoglyph
   spoofing of usernames/board names) and moderation-side content
   filtering — both real, separate concerns from terminal-injection
   safety, worth their own design pass if/when they matter.

## Sign-off notes, round 30 (board post pagination + honest concurrency docs — implemented)

Issue #10: `list_posts()` fetched a board's *entire* history on every
visit and `_show_board()` rendered all of it, oldest first — an active
board only grows less pleasant to open over time, and the `Database`
class's own docstring overclaimed what WAL mode buys given this
project's actual single-connection architecture.

1. **Default view flipped to newest-first, a genuine UX fork confirmed
   with Thiesi rather than assumed:** opening a board now jumps to its
   most recent activity, not its oldest post. The issue's own framing
   (an active board's *full history* being what's unpleasant to
   re-render on every visit) pointed at this directly, but it's a real,
   user-facing behavior change worth confirming explicitly rather than
   deciding unilaterally.
2. **`netbbs.boards.posts.list_posts_page()` replaces `list_posts()`
   entirely** — not added alongside it. The old function had exactly
   one production caller (`login_flow._show_board`, now updated) and
   no other real dependents; keeping an unbounded query function
   sitting in the module would just be a footgun for some future
   caller to reach for instead of the bounded one. `tests/test_boards.
   py`'s three `list_posts` call sites were migrated to the paginated
   function rather than left broken.
3. **Cursor-based (keyset) pagination, not `OFFSET`/`LIMIT`** —
   deliberate, not the simpler default: stable under concurrent
   inserts (a new post arriving between two page loads can't shift
   already-seen posts into an adjacent page or duplicate one across
   pages the way an offset-based boundary would) and avoids an
   ever-growing `OFFSET` scan cost when paging deep into an old
   board's history. Ordering is `(created_at, post_id)` ascending,
   `post_id` breaking ties deterministically (per the issue's own
   recommended direction) since `created_at` alone is not a total
   order — genuinely exercised, not just theoretical: a test
   deliberately forces two posts to share an identical timestamp and
   confirms the query result is both correct (sorted by `post_id`) and
   *repeatable* across separate query executions, not incidental row-
   storage-order luck.
4. **`has_older`/`has_newer` are computed with their own small indexed
   `EXISTS` queries against the fetched page's actual boundary posts,
   not inferred from which of the three fetch modes (newest/`before`/
   `after`) was used.** An earlier, simpler design considered inferring
   "arrived via `before`, so `has_newer` must be true" — rejected once
   it became clear that doesn't hold if the cursor passed in was
   already the newest post available. The `EXISTS` approach costs two
   cheap indexed lookups per page but is correct in every case, including
   the empty-page edge case, without needing to reason about caller
   behavior at all.
5. **New composite index** `idx_posts_board_id_created_at_post_id ON
   posts(board_id, created_at, post_id)`, matching the acceptance
   criteria's `(board_id, created_at, post_id)` example exactly, added
   as a new migration (never editing a shipped one, per this project's
   existing migration discipline). The old single-column
   `idx_posts_board_id` (round 2) was deliberately *not* dropped in
   the same migration — the new composite index's leading column
   already makes it redundant for query planning, but dropping a
   shipped index is its own separate, non-urgent cleanup with no
   user-visible benefit at this project's declared scale (§14), not
   worth bundling into a migration whose actual point is the new
   index.
6. **`Database`'s docstring corrected, resolving the acceptance
   criterion directly** — round 2's original version claimed WAL lets
   "concurrent asyncio readers and writers... not block each other
   more than necessary," true of WAL in general but not of *this*
   architecture: exactly one `sqlite3.Connection`, called
   synchronously with no `await` around any query, meaning every
   `self.connection.execute(...)` blocks the *entire event loop* — not
   just the calling coroutine — until it returns. Concurrent sessions
   are not concurrent database access; they're serialized by Python's
   own cooperative scheduling, same as any other synchronous call from
   a coroutine. Rewritten to say this plainly, while explaining WAL is
   still worth keeping for the one place multiple genuinely independent
   connections against the same file exist today: an admin/dev script
   (e.g. `scripts/create_test_board.py`) run against a live node's
   database file — a second OS process, not a second coroutine.
7. **`PRAGMA busy_timeout = 5000` added**, directly answering the
   issue's "configure busy_timeout" — the concrete fix for that same
   separate-process scenario: without it, SQLite's default is to fail
   a locked-database access immediately rather than retry, so a
   momentary overlap between a running node and an admin script would
   surface as a raw `OperationalError` instead of the script simply
   waiting a moment. 5000ms is a conservative, commonly-used default,
   not separately benchmarked — the scenario it protects is
   occasional, not a hot path.
8. **The larger architectural question the issue itself only asks to
   be *considered* — "before targeting low hundreds of users, consider
   a bounded connection pool, database actor, or off-loop execution
   for expensive queries" — is deliberately not attempted this round.**
   Documented honestly (point 6) as a real, current limitation rather
   than silently ignored, but actually redesigning the connection
   model is a substantially larger, riskier change than pagination,
   and the issue's own phrasing scopes it as forward-looking
   groundwork rather than a Phase 1 requirement. Worth its own
   dedicated design round if/when the declared scale (§14) is actually
   being approached.
9. **Interactive navigation**: `[O]lder`/`[N]ewer`/`[R]ecent`/`[B]ack`
   (or a bare Enter), only offering the options that currently apply
   (`has_older`/`has_newer`) — a single-page board shows only `[B]ack`,
   matching the acceptance criterion that navigation supports "at
   least next/previous and newest content" without cluttering the
   prompt with dead options. `[R]ecent` exists specifically so a user
   who's paged deep into old history isn't stuck pressing `[N]ewer`
   repeatedly to get back to now.
10. **Explicit non-goals, consistent with the issue's own scope:**
    first-unread state — the issue itself says "eventually", and no
    read-tracking exists anywhere yet to build it on. File area
    listings (`netbbs.files.entries.list_files`) have the exact same
    unbounded-fetch shape `list_posts` used to, but were never named
    in issue #10's affected code — left as an explicitly flagged,
    known gap (see that function's updated docstring) for a future
    round rather than silently inconsistent or silently expanded
    scope. Chat scrollback (`netbbs.chat.scrollback.get_scrollback`)
    does *not* have this problem at all — it's already bounded by a
    different, earlier mechanism (round 19/20's trim-on-insert
    retention cap), confirmed while auditing the doc-comment that used
    to compare it to `list_posts`.
11. **Testing**: `tests/test_post_pagination.py` (10 cases: empty/
    single-page boards, newest-page-default correctness, paging both
    directions, a full backward traversal visiting every post exactly
    once, the timestamp-tie determinism check, input validation, and
    the composite index's existence) exercises `list_posts_page`
    directly against a real SQLite database. `tests/test_board_
    pagination_ui.py` (6 cases) drives the real `_show_board` loop
    with a fake session to confirm the bounded-rendering acceptance
    criterion concretely (a board with three pages' worth of posts
    renders exactly one page's worth, not the whole thing) and that
    `[O]lder`/`[N]ewer`/`[R]ecent`/`[B]ack` actually navigate rather
    than being inert labels. `tests/test_storage.py` gained a
    `busy_timeout` pragma-value check, matching this file's existing
    `journal_mode`/`foreign_keys` test pattern. One real test-
    construction bug caught and fixed during this round, not shipped:
    an early version of a UI test matched post content via bare
    substring (`"Subject 1" in output`), which false-positived against
    `"Subject 10"`/`"Subject 12"` etc. once board sizes reached double
    digits — fixed by matching the exact post-header separator
    (`"Subject 1 --"`) instead.

## Sign-off notes, round 31 (file-area pagination — implemented)

Round 30 explicitly flagged `netbbs.files.entries.list_files` as
having the exact same unbounded-fetch shape `list_posts` used to,
scoped out only because issue #10 named board posts specifically —
Thiesi asked directly for the same fix to be applied to file areas as
a follow-up, so this round does that.

1. **`list_files_page()` deliberately mirrors `list_posts_page()`'s
   design byte for byte**, not a fresh design pass: same cursor-based
   (keyset) pagination, same `(created_at, file_id)` ordering with
   `file_id` (content-addressed, globally unique) as the deterministic
   tie-breaker, same three `before`/`after`/neither modes, same
   `has_older`/`has_newer` computed via their own indexed `EXISTS`
   checks against the page's actual boundary rather than inferred from
   fetch mode. `FileEntryPage`/`FileEntryCursor` mirror `PostPage`/
   `PostCursor` the same way. `list_files` replaced entirely, same
   reasoning as `list_posts`'s replacement (one production caller, no
   value in leaving an unbounded footgun around) — `tests/test_file_
   areas.py`'s three call sites migrated to the paginated function.
2. **New composite index** `idx_files_area_id_created_at_file_id ON
   files(area_id, created_at, file_id)`, added as a new migration —
   same reasoning as round 30's posts index, including the same
   decision to leave the old single-column `idx_files_area_id` (round
   2) in place rather than drop it in the same migration.
3. **`_show_area` mirrors `_show_board`'s pagination *semantics*
   exactly** — same newest-first default (applied directly per
   Thiesi's "same fix" instruction, not re-litigated as a fresh UX
   fork this round), same `[O]lder`/`[N]ewer`/`[R]ecent`/`[B]ack`
   options, shown only when they currently apply.
4. **One deliberate mechanical difference from `_show_board`, not an
   inconsistency worth ironing out:** `_show_area` reads the
   navigation choice via `read_line()`, not `read_key()`.
   `_show_board`'s options are all single immediate keystrokes;
   `_show_area` also has to accept free-text multi-character commands
   (`/download <filename>`, `/upload`) in the same prompt, which
   single-keystroke dispatch structurally can't support — `read_key()`
   returns after exactly one character, before `/download ` could ever
   be typed. `o`/`n`/`r`/`b` are still accepted as their own line,
   consistent in spirit even though the input mechanism differs.
5. **A real correctness issue pagination would otherwise have silently
   introduced, found and fixed proactively rather than shipped as a
   regression:** the old `_handle_download` matched `/download
   <filename>` against `files: list[FileEntry]` — the *entire* area's
   listing, since `list_files` was unbounded. Once browsing is
   paginated, that in-memory list is only ever one page; a user
   referencing a file by name from outside the current page (an
   earlier page, or told about it by someone else) would have silently
   stopped working — a real regression a purely mechanical "swap the
   function name" port would have shipped. Fixed with a new
   `get_file_by_name(db, area, filename)` doing its own direct,
   indexed lookup against the whole area regardless of what's
   currently paged into memory — pagination bounds what's fetched for
   *browsing*, not what can be *referenced by name*. `filename` isn't
   unique within an area (unlike `file_id`); `get_file_by_name`
   preserves the old scan's oldest-match tie-breaking behavior
   (`ORDER BY created_at ASC, file_id ASC LIMIT 1`), confirmed with a
   dedicated test, not just assumed to not matter. Boards have no
   equivalent concern — nothing in the interactive board UI currently
   lets a user reference a post outside the visible page by name/ID, so
   this class of bug is specific to file areas' `/download` command.
6. **Testing**: `tests/test_file_pagination.py` (11 cases, matching
   `tests/test_post_pagination.py`'s coverage exactly, plus three cases
   specific to `get_file_by_name`: finding a file that's genuinely not
   on the newest page, returning `None` for a truly nonexistent name,
   and the oldest-match tie-breaking behavior for duplicate filenames).
   `tests/test_file_area_pagination_ui.py` (6 cases, matching
   `tests/test_board_pagination_ui.py`'s coverage, plus a dedicated
   end-to-end test driving the real `_show_area` loop that uploads
   more than a page's worth of files, never pages backward, and
   confirms `/download`-ing the *oldest* one by name still works —
   the concrete regression test for point 5, not just unit coverage of
   `get_file_by_name` in isolation.

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

Prompted by sequencing Phase 2 into separable tracks (foundation-first,
confirmed with Thiesi) and starting on the first one: the §13
moderator/permission model everything else in Phase 2 depends on —
board/file moderation and expiry, chat mute/ban/kick, `/topic` editing,
and invite-only channels' `manage_members` all needed this to exist
first, and nothing beyond a numeric `user_level` did.

1. **Bitmask (`IntFlag`) representation, not a junction table.** A
   single integer `permissions` column per grant row, chosen because it
   matches §13's own phrasing directly — "settable individually or
   combined" — and avoids a join table for what's a small, closed set
   per object type.
2. **Two permission enums, not one shared set.** `BoardPermission`
   (`READ/WRITE/EDIT/DELETE/APPROVE`) is shared by boards and file
   areas, matching their existing identical shape elsewhere in the
   schema (`min_read_level`/`min_write_level` on both). `ChannelPermission`
   (`EDIT/MODERATE/MANAGE_MEMBERS`) is deliberately smaller and
   different: chat access itself has no read/write split (existing
   `Channel.min_level`), and `MODERATE` bundles kick/mute/ban into one
   bit rather than three separately-grantable ones, since §13 describes
   them as one bundled capability of being a chat moderator, not as
   independently combinable permissions the way board permissions are.
3. **Local-blanket grants (`object_id IS NULL`) are all-or-nothing —
   no partial-exception carve-outs** (e.g. "blanket edit except board
   Z"). Nothing in §13 asks for this, and it would turn every
   permission check into "blanket AND NOT excluded" instead of a single
   lookup. Cheap to add later if a real case surfaces — same shape as
   the existing pin/exempt-under-`edit` coupling, already documented
   elsewhere in §13 as "known, accepted... cheap to split later if it
   matters in practice."
4. **Link-blanket ("global") is deliberately not modeled yet.** Only
   per-object and local-blanket grants exist in the schema today —
   the third tier is unreachable until Phase 6's Link-wide moderation
   exists; its shape (a third `object_id` sentinel? a separate scope
   column?) is left to be decided then, against real Phase 6
   requirements, rather than guessed now.
5. **A generic `moderation_log` audit table was built now**, ahead of
   most of its consumers — this round's grant/revoke actions are the
   only thing writing to it today, but it's designed to also carry
   mute/ban/kick and moderated-board approval once those later tracks
   land, rather than have each track invent its own logging. Same
   anti-retrofit reasoning `netbbs.permissions.levels` was already
   built on in Phase 1 ("built before there's a menu/command dispatch
   layer to plug into"). Free-text `action`/`detail` columns rather
   than a `CHECK`-constrained action allowlist, specifically so later
   action types don't require editing an already-shipped migration
   (disallowed per `storage/migrations.py`'s own rule).
6. **Grants are additive, revokes are subtractive, both by bit.**
   Calling `grant_permissions` again for the same (user, object_type,
   object_id) ORs the new bits into whatever's already granted;
   `revoke_permissions` ANDs the complement in, deleting the row
   entirely once its mask reaches zero. Chosen so callers never need to
   fetch-then-recompute-then-write the existing mask themselves.
7. **Two partial unique indexes, not one compound `UNIQUE`** —
   `moderator_grants` needed the exact same fix as the existing
   `blocklist` table: SQLite treats every `NULL` in a `UNIQUE`
   constraint as distinct from every other `NULL`, so a single
   `UNIQUE(user_id, object_type, object_id)` would not stop a user
   acquiring two separate local-blanket grants for the same
   `object_type`. Mirrors `blocklist`'s existing
   `fingerprint`/`local_user_id` partial-index pattern exactly.
8. **New module homes: `netbbs.moderation.roles` (both permission
   enums, plus grant/revoke/query functions) and `netbbs.moderation.log`
   (the audit table), not `netbbs.permissions`.** `netbbs.permissions.
   levels` is left untouched — still pure `user_level` gating, exactly
   matching what its own docstring already promised ("the finer-grained
   per-board permissions... build on top of this in Phase 2; they are a
   different, richer permission model, not a replacement"). The richer
   model instead landed in `netbbs.moderation`, whose own docstring had
   already earmarked it as "the natural home for the richer §13
   moderation model... once that's built in Phase 2."
9. **Deliberately not wired into any existing call site this round.**
   `boards/posts.py`, `files/entries.py`, and chat's `ChatHub`/
   `chat_flow.py` still only call `require_level`/`meets_level` — none
   of them consult these new grants yet. This round ships the grant
   model as standalone plumbing with no consumer, the same precedent
   `netbbs.permissions.levels` itself set. Actual enforcement is each
   later track's own job: moderated-board approval and expiry need
   `APPROVE`/`EDIT`; chat mute/ban/kick, `/topic` editing, and
   invite-only `manage_members` need `MODERATE`/`EDIT`/
   `MANAGE_MEMBERS`.
10. **Testing:** `tests/test_moderator_roles.py` and
    `tests/test_moderation_log.py`, following the existing
    one-file-per-module convention. Full suite re-run after adding
    both (568 passed, 1 skipped) — actually run, not just
    syntax-checked, per this project's own standing rule.

## Sign-off notes, round 35 (moderated-board approval + post maintenance/expiry — implemented)

Track 2 of the foundation-first Phase 2 sequencing plan, built on
round 34's grants. **Scope: posts + boards only** — files/file areas
are a structurally identical mirror, deliberately deferred to its own
follow-up pass rather than doubling this round's diff.

A codebase survey ahead of this round confirmed there is no scheduled/
background-task mechanism anywhere in this codebase, which directly
shaped point 3 below. Three forks were raised and confirmed with
Thiesi before implementation:

1. **Expiry applies only to `'approved'` content.** A `'pending'`
   post's age doesn't count toward expiry — a stale unreviewed post is
   a moderation-queue hygiene problem, not an expiry one. Deliberately
   not solved this round; revisit only if a real need for auto-expiring
   stale pending items shows up.
2. **`'expired'` content stays individually reachable** — as a
   reply-parent, and (once files land) via `/download <filename>` —
   even though it's delisted from normal paginated browsing. Only
   `'pending'` is a genuine access gate; the grace period is real
   insurance against an overly aggressive age setting, not a second
   soft-block.
3. **The moderation queue is a separate, simple function
   (`list_pending_posts`)**, not a mode bolted onto the existing
   cursor-paginated `list_posts_page` — keeps round 30's already
   five-query-site pagination logic untouched.

Further decisions made during design and implementation:

4. **No separate `'rejected'` status.** `delete_post` (requires
   `BoardPermission.DELETE`) is also how a pending post gets rejected —
   avoids inventing a state nothing asked for. It logs `action=
   "reject"` when the post's status was `'pending'` at the moment of
   deletion, else `action="delete"`, so the audit trail still
   distinguishes the two without new API surface.
5. **Expiry mechanism: lazy sweep-on-access, not a background job.**
   `_sweep_expired_posts` runs at the top of `list_posts_page` (the
   natural "someone is looking at this board" trigger) and does two
   plain SQL statements — age `'approved'` posts past
   `board.max_post_age_days` into `'expired'`, then hard-delete any
   already-`'expired'` post whose grace period has also elapsed.
   Not logged to `moderation_log` — that log is for explicit human
   moderation decisions, not mechanical time-based housekeeping.
6. **Grace period is a single node-wide default** (`netbbs.config.
   get_expiry_grace_period_days`/`set_expiry_grace_period_days`,
   default 7 days), not a per-board column — nothing in the design doc
   asks for per-board control over it, unlike max post age, which
   genuinely is per-board (`Board.max_post_age_days`, nullable = retain
   indefinitely).
7. **Post-level `pinned` and `exempt_from_expiry` are independent
   flags**, both gated by the existing `BoardPermission.EDIT` bit
   (matching the already-settled §13 pin/exempt-under-`edit` note).
   `Post.pinned` is a distinct concept from the existing
   `Board.pinned` (which board sorts first among *all* boards) —
   same word, different table, different meaning entirely.
8. **Pinned posts do not reorder `list_posts_page`'s feed — caught
   during implementation, not fully resolved in the original plan.**
   Sorting pinned posts first, the way `Board.pinned`/`FileArea.pinned`
   already do in their own (non-paginated) listing functions, would
   have broken keyset pagination's stability guarantee: a pinned old
   post would reappear on every "newest page" fetch, and the
   `has_older`/`has_newer` boundary math (built entirely around
   `(created_at, post_id)` comparisons) would no longer hold. Resolved
   the same way the moderation queue was: a separate, small, non-
   paginated `list_pinned_posts` function instead of reordering the
   cursor-paginated feed. `list_posts_page` itself needed only one new
   filter: `AND status = 'approved'`, backed by a new composite index
   `(board_id, status, created_at, post_id)` — a plain equality, not
   an `OR`, so it stays a clean index range scan.
9. **`get_post` (unbounded by-ID lookup) is left unfiltered** — used
   for `create_post`'s own return path and reply-parent lookup.
   Reaching a `'pending'` post this way requires already knowing its
   exact `post_id`, which isn't discoverable through any listing a
   non-author, non-moderator would see — accepted as a practically
   unreachable gap rather than adding complexity for it.
10. **Testing:** new `tests/test_post_lifecycle.py` (28 cases:
    initial status by `moderated`, the pending queue's visibility
    rules, approve/delete/reject logging, pin/exempt permission
    checks, the expiry sweep at multiple ages, grace-period boundaries,
    and replying to an expired parent), reusing the existing
    direct-SQL `created_at` backdating approach from `test_boards.py`
    rather than introducing a new clock-mocking mechanism for post
    age. Full suite re-run after adding it (596 passed, 1 skipped) —
    actually run, not just syntax-checked.

**Deferred, explicitly:** the files/file-area mirror of this entire
round (identical shape — `list_files_page` already duplicates
`list_posts_page` "byte for byte" per this project's existing
precedent of fully parallel, non-shared board/file implementations).

## Sign-off notes, round 36 (moderated-area approval + file maintenance/expiry — implemented)

The files/file-areas mirror of round 35, deferred there explicitly.
Structurally identical: `file_areas` gains `moderated`/
`max_file_age_days` (named for files rather than reusing
`max_post_age_days`, even though §13 itself loosely says "post age"
for both — "post" doesn't fit a file); `files` gains the same
`status`/`pinned`/`exempt_from_expiry` columns as `posts`; the same
lazy sweep-on-access mechanism (`_sweep_expired_files`, mirroring
`_sweep_expired_posts` exactly, including the "no background
scheduler exists anywhere in this codebase" reasoning); the same
`approve_file`/`delete_file` (doubling as reject)/`set_file_pinned`/
`set_file_exempt`/`list_pending_files`/`list_pinned_files` set,
gated by the identical `BoardPermission` bits (shared between boards
and file areas since §13 already gives both the same
read/write/edit/delete/approve permission surface).

One genuine difference from posts, not just a mechanical rename:

1. **`get_file_by_name` needed its own pending-visibility check,
   which `get_post` doesn't.** Posts have exactly one unbounded lookup
   path (`get_post`, by ID) — reaching a pending post through it
   requires already knowing its exact content-addressed `post_id`,
   effectively unreachable in practice. Files have a *second* unbounded
   path, `get_file_by_name`, added in round 31 specifically so
   `/download <filename>` works by a name a user might simply already
   know or guess — a real, practical route to a pending file, not a
   theoretical one. So `get_file_by_name` gained an optional
   `requesting_user` parameter: a `'pending'` match is only returned to
   its own uploader or a holder of `BoardPermission.APPROVE` on the
   area; passing no `requesting_user` is treated the same as an
   unauthorized one (the safe default), and an `'expired'` match is
   always returned regardless, consistent with round 35's "expired is
   delisted, not access-blocked" decision. `net.file_flow._handle_download`
   was updated to pass its session's `user` through.
2. **`delete_file` only removes the database row, not the underlying
   bytes in `netbbs.files.storage`.** Storage-level garbage collection
   of orphaned content-addressed blobs (a different file entry could
   in principle share the same bytes) is out of scope for this round —
   noted explicitly since it's a real gap this mirror pass introduces
   that round 35 has no equivalent of (posts have no separate
   byte-storage layer to leave behind).
3. **Testing:** new `tests/test_file_lifecycle.py` (32 cases —
   round 35's 28-case shape plus 5 covering `get_file_by_name`'s new
   pending-visibility branches specifically). Full suite re-run after
   adding it (628 passed, 1 skipped) — actually run, not just
   syntax-checked.
4. **Bug found and fixed, spanning both rounds:** neither
   `netbbs.boards.__init__` nor `netbbs.files.__init__` had been
   updated with round 35/36's new functions — both package `__init__`s
   still only re-exported the original create/get/list surface.
   Existing tests never caught this because they import directly from
   `netbbs.boards.posts`/`netbbs.files.entries`, not the package root.
   Fixed for both packages in this round.

Nothing else diverges from round 35's decisions or reasoning — see
that round for the full rationale behind the status model, the
sweep-on-access mechanism, the grace-period config, and the
separate-function resolution for both the moderation queue and pinned
views.

## Sign-off notes, round 37 (chat mute/ban/kick — implemented)

Track 3 of the foundation-first Phase 2 sequencing plan, built on
round 34's `ChannelPermission.MODERATE` grant bit. §13: "mute/ban/
unmute/unban commands... All actions logged and echoed in-channel for
transparency."

The real architectural question this round had to answer, found by
reading `chat/hub.py` and `net/chat_flow.py` directly: **there was no
mechanism by which one live session could force another to
disconnect.** `ChatHub` only ever pushed plain strings onto a
participant's queue, and `receive_loop` only ever printed whatever
arrived — no participant registry, no cancellation handle, nothing.
Mute doesn't need this (enforced lazily at the muted user's own next
send attempt); kick and ban do.

1. **`ChatHub` gained exactly two primitives**, staying inside its
   existing "opaque `participant_id` string" abstraction rather than
   teaching it the `username:id(session)` convention `chat_flow.py`
   itself invented: `participant_ids(channel_name)` (a snapshot) and
   `async send_to(channel_name, participant_id, message)` (deliver to
   one specific queue, `False` if they'd already left). `chat_flow.py`
   does the username-prefix matching itself, since it already owns
   that convention.
2. **A small `_KickNotice` object, not a string, forces the exit.**
   `receive_loop` checks for it and **returns** instead of looping —
   picked up by `_chat_loop`'s existing `asyncio.wait({receive_task,
   send_task}, return_when=FIRST_COMPLETED)` exactly the way `/quit`
   finishing `send_loop` already is, running the same existing
   `finally` (hub.leave, a `"leave"` scrollback event, a leave
   broadcast). A kicked/banned user's own disconnect is thus
   indistinguishable from an ordinary leave to everyone else, by
   design — the moderation action itself gets its own separate
   transparency notice (`_announce_moderation`), so nothing is lost.
3. **One unified `channel_restrictions` table for mute and ban**,
   discriminated by `kind`, not two tables — structurally identical
   (same duration/expiry shape, same "is there a live, non-expired
   row" check), mirroring the existing `channel_messages`
   kind-discriminator precedent. `UNIQUE(channel_id, user_id, kind)`
   makes re-muting/re-banning an upsert (replaces duration/reason)
   rather than accumulating rows.
4. **No cleanup sweep for expired mute/ban rows** — unlike round
   35/36's board/file expiry, where deleting the row was the point of
   the feature, a stale expired restriction causes no problem sitting
   there; `is_muted`/`is_banned` just filter on `expires_at IS NULL OR
   expires_at > now` at check time. Simpler than the sweep-on-access
   mechanism, deliberately not built here since nothing needs it.
5. **`parse_duration`** matches §13 exactly: no argument = indefinite;
   bare number = minutes; `s/m/h/d/w/y` suffix = that unit (`y` fixed
   at 365 days — `timedelta` has no calendar-aware year, and a
   mute/ban duration doesn't need that precision). Nothing else in the
   codebase parses durations (confirmed by a grep — `net/throttle.py`
   works in raw floats).
6. **Command shape: `/mute <user> [duration] [reason...]`** (same for
   `/ban`). The first token after the username is tried as a duration;
   if it parses, it's consumed and the rest is the reason; if it
   fails, duration defaults to indefinite and the *entire* remainder
   is the reason. A deliberate call (matches a common `!mute @user 10m
   spamming`-style convention), explicitly flagged as reconsiderable
   rather than fully settled — revisit if it turns out to surprise
   people in practice.
7. **Ban is checked once, at the top of `_chat_loop`**, before
   `hub.join` — an unexpired ban means the loop is never entered.
   `/ban`/`kick` both call the same `_kick_live_sessions` helper
   afterward to remove a currently-present target; a target with no
   live session is simply a no-op, not an error.
8. **`kick_user` (in `netbbs.chat.moderation`) persists no state at
   all** — only the permission check and audit trail. Actually
   removing a live session is `chat_flow.py`'s job (point 1/2 above);
   `netbbs.chat.moderation` knows nothing about `ChatHub` or live
   sessions by design, same layering as `netbbs.boards.posts`/
   `netbbs.files.entries` not knowing about `net.chat_flow`.
9. **New module `chat/moderation.py`**, not `netbbs.moderation` —
   same precedent as `approve_post`/`delete_post` living in
   `boards/posts.py`: feature-specific moderation actions live with
   the feature; `netbbs.moderation` stays limited to the cross-feature
   grant/log primitives.
10. **Bug found and fixed during implementation, not before:** this
    round's own pre-implementation research claimed
    `channel_messages.kind` had no DB-level `CHECK` constraint, only
    an application-level `Literal` type hint — wrong, confirmed the
    hard way when the first integration test raised `sqlite3.
    IntegrityError: CHECK constraint failed`. A real `CHECK (kind IN
    ('message', 'join', 'leave'))` has existed on the table since its
    original migration. Fixed with the standard SQLite table-rebuild
    (`CREATE ... _new` with the widened `CHECK`, copy rows, `DROP`,
    `RENAME`, recreate the index) folded into this round's migration —
    SQLite has no `ALTER TABLE` for changing a `CHECK` in place.
    Worth remembering: pre-implementation research claims about
    schema constraints should be verified against the actual
    migration SQL, not trusted at face value, before code is written
    against them.
11. **Testing:** `tests/test_chat_moderation.py` (28 cases — duration
    parsing, permission gating, mute/ban state and upsert-on-repeat,
    audit logging) plus a new `tests/test_chat_flow_moderation.py` (7
    cases) exercising the real `/mute`/`/ban`/`/kick` commands through
    `_chat_loop` itself, including the cross-session force-disconnect.
    The latter needed a fake `Session` — confirmed none existed
    anywhere in `tests/` before this round — built as a small, local
    `FakeSession` (scripted input list; `read_line` blocks forever via
    an unset `asyncio.Event` once its lines run out, the same shape a
    real session has while genuinely waiting for input, which is what
    lets a kick notice — not the target's own `/quit` — be the thing
    that ends their loop). Full suite re-run after adding both (663
    passed, 1 skipped) — actually run, not just syntax-checked.

## Sign-off notes, round 38 (user directory & vCard/finger — implemented)

Track 4 of the foundation-first Phase 2 sequencing plan. §13's own
spec is terse here — "users control which profile fields are public
via their preferences menu... vCard: short free-text bio (6-line
cap)... vCard visibility independently toggleable... table-style user
directory listing public info... finger-style lookup... accessible
from the directory, main menu, and chat." Confirmed with Thiesi before
implementing:

1. **This is also the first per-user (not node-wide) settings storage
   this codebase would have** — `config.py`'s own docstring already
   anticipated "per-user overrides" as a future layer. Built as a
   generic, reusable mechanism now (`user_preferences(user_id, key,
   value)` + `netbbs.user_preferences.get_user_preference`/
   `set_user_preference`, mirroring `netbbs.config` exactly) rather
   than a narrow vCard-only table, specifically so Track 5's planned
   per-user chat timestamp preference (round 32 sign-off note) doesn't
   need to invent its own storage later — the same anti-retrofit
   reasoning already behind several earlier rounds' plumbing-first
   decisions.
2. **vCard fields: bio only**, matching what §13 concretely names —
   not inventing real-name/location/contact fields it doesn't ask for.
   Cheap to extend later: each additional field is just another
   preference key, no schema change.
3. **Bio visibility defaults to hidden**, not shown, until the owner
   explicitly opts in — matches this project's consistent
   privacy-safe-by-default posture elsewhere (hidden channels, no
   automatic power grants, §6/§2's core lesson).
4. **New `netbbs.directory` module**, not nested in `auth` or
   `moderation` — a distinct concern from both, same layering already
   used to keep those two separate. `get_vcard(db, target, *,
   requesting_user)` always shows the bio to its own owner regardless
   of visibility (hiding your own profile from yourself would be a
   bug, not a feature); anyone else sees it only if the owner has made
   it visible.
5. **`auth.users.list_users`** (ordered by username, case-insensitive)
   is the directory's underlying listing — deliberately not
   paginated, unlike `list_posts_page`/`list_files_page`: total
   registered users is naturally bounded at this project's declared
   scale (§14), unlike posts/files, which is exactly why those needed
   round 30/31's cursor pagination.
6. **Full vertical slice, matching Tracks 1-3's precedent**: `net.
   login_flow`'s main menu gained `[D]irectory` (browse via the
   existing `pick_item` picker, description line showing "member
   since X, bio: public/private"; selecting an entry shows the full
   finger detail) and `[P]rofile` (view current bio/visibility, then
   `[E]dit bio`/`[V]isibility` sub-choices); `net.chat_flow` gained a
   `/finger <user>` command (same ad hoc command-branch style Track 3
   already established for `/mute`/`/ban`/`/kick`) — satisfies §13's
   "accessible from... chat" explicitly.
7. **Confirmed directly: there is no multi-line text-input mechanism
   anywhere in this codebase** — even a board post's `body` is
   collected via one single `read_line()` call today. Bio entry
   therefore loops over `read_line` up to 6 times, a blank line ending
   it early — not a new terminal capability, the same repeated-single-
   line-read shape every other multi-step prompt here already uses. A
   blank *first* line clears the bio entirely (choosing not to edit at
   all is what the profile screen's own `[B]ack` option is for).
8. **Bug found while writing tests, not before:** an early version of
   the directory UI test suite included a test asserting a
   registered-but-otherwise-empty directory "never prompts," passing a
   `FakeSession` with zero scripted keys. Wrong premise — a directory
   with even one registered account (the viewer themselves) is a
   *non-empty* list, so `pick_item` correctly enters its interactive
   loop and calls `read_key()`, which the fake session answers with an
   endless stream of empty strings once its scripted list runs out —
   an infinite loop, not a crash, since `pick_item`'s unrecognized-key
   branch just re-prompts. Caught only because the test run visibly
   hung rather than finishing; fixed by correcting the test's premise
   (a directory of one still needs a `"q"` to quit cleanly) rather
   than changing any production code, since the code's behavior was
   correct throughout — same "verify the test's assumption, not just
   the code" lesson as round 37's schema-constraint miss, a different
   shape of the same underlying discipline.
9. **Testing:** `tests/test_user_preferences.py` (5 cases),
   `tests/test_directory.py` (16 cases, library-level), `tests/
   test_directory_ui.py` (11 cases, the directory/profile screens via
   the same lightweight duck-typed `FakeSession` `tests/
   test_board_pagination_ui.py` already established — no need to
   subclass the `Session` ABC the way round 37's chat test needed to,
   since `pick_item`/these screens don't need genuinely concurrent
   sessions), plus 3 new `/finger` cases added to `tests/
   test_chat_flow_moderation.py`. Full suite re-run after adding all
   four (697 passed, 1 skipped) — actually run, not just
   syntax-checked.

## Sign-off notes, round 39 (chat command dispatch infrastructure — implemented)

Track 5a of the Phase 2 sequencing plan — Track 5 (rounds 32-33's full
chat command surface) is by far the largest remaining block, so it's
being split into sub-pieces the same way Track 2 was split into
posts-then-files. This slice is the dispatch mechanism itself:
everything else in Track 5 (presence/identity, discovery, channel
switching, private messaging, completion, invite-only channels) needs
this to exist first, and rounds 37/38 had already bolted
`/mute`/`/ban`/`/unmute`/`/unban`/`/kick`/`/finger` onto an
`if line.lower().startswith(...)` chain one at a time.

1. **A small `ChatCommandContext` dataclass** (`session`, `db`, `hub`,
   `channel`, `user`, `participant_id`) replaces what used to be a
   different ad hoc positional-argument list per handler — most took
   `session, db, hub, channel, user, args_str`, but `/finger` omitted
   `hub` entirely. All six existing handlers were migrated to the new
   `(ctx, args)` signature; no test called any of them directly
   (confirmed by grep before starting), so this was a safe,
   behavior-preserving refactor — the existing round 37/38 test suites
   pass unchanged against it.
2. **Handler contract: `async def handler(ctx, args) -> bool | None`.**
   A truthy return means "exit the chat loop after this command" —
   only `/quit`/`/leave`'s handler returns `True`; every other handler
   returns `None` implicitly. Chosen over a control-flow exception for
   quitting: an explicit return value reads more plainly here than
   exception-driven flow would for what is, after all, the ordinary
   way to leave.
3. **Explicit dict registry (`_COMMANDS: dict[str, CommandHandler]`),
   not a decorator-registration pattern** — a plain literal built once
   after every handler is defined, `/quit` and `/leave` both mapping
   to the same handler. Matches this codebase's existing preference
   for explicit, greppable structures over registration magic (e.g.
   `storage/migrations.py`'s plain ordered list, not auto-discovery).
4. **Real bug fixed as a natural side effect of building a real
   dispatcher, not a separate round:** any line starting with `/` is
   now always treated as a command attempt — looked up in
   `_COMMANDS`, and "Unknown command: /x" shown if not found, with
   nothing broadcast. Previously, an unrecognized `/x` line fell all
   the way through the old ad hoc `if` chain to being sent as an
   ordinary chat message — a typo'd command (`/mtue bob`) silently
   became public chat text. Standard behavior for slash-command chat
   systems generally (IRC/Discord/Slack all reserve leading `/` the
   same way).
5. **The existing mute check deliberately stays exactly where it
   was** — gating only the plain-message fallthrough after dispatch,
   not `_dispatch_command` itself, so a muted moderator can still,
   say, unmute themselves. Not a new decision, just confirmed
   unchanged during the refactor.
6. **`/quit`/`/leave` keep their exact current meaning this round**
   (both fully exit the chat loop) — deliberately not redesigning
   `/leave` into "leave the current channel, stay in chat," since
   that's Track 5d's (channel-switching) scope, not this one's.
   Flagged explicitly so it doesn't get silently redecided piecemeal
   across rounds.
7. **A `/help` command was added**, listing every registered command
   name — small, essentially free once a real registry exists, so
   not deferred to a later round.
8. **Testing:** new `tests/test_chat_dispatch.py` (8 cases: unknown
   commands rejected and not broadcast, including the exact `/mtue`
   typo scenario that motivated point 4; plain messages unaffected;
   `/quit`/`/leave` both still exit; `/help` lists known commands and
   needs no special permission), reusing `tests/
   test_chat_flow_moderation.py`'s `FakeSession` directly via a
   cross-file import (`tests/__init__.py` already makes `tests` a
   real package). All ten existing round 37/38 chat command tests
   re-run and pass unchanged against the refactored dispatcher — the
   regression check for point 1's signature migration. Full suite
   re-run after adding the new file (705 passed, 1 skipped) — actually
   run, not just syntax-checked.

## Sign-off notes, round 40 (/me action events — implemented)

The first slice of Track 5b (presence/identity). `/nick` and `/away`
were deliberately not attempted in this same round: `/nick` needs
rendering changes across every chat display path (join/leave/message/
action all need to show alias+canonical together, per round 32's
spec), and `/away` needs a new node-wide session-count tracking
mechanism ("clears only when the account's final session
disconnects") that doesn't exist anywhere yet and crosses into
`net.login_flow`'s session lifecycle, not just `chat_flow.py` — both
genuinely larger design surfaces than `/me`, which is small and
mechanical, following round 37's exact precedent for adding a new
message kind + command handler.

1. **New `channel_messages.kind` value: `'action'`.** Same standard
   SQLite table-rebuild as round 37's widening (still no `ALTER TABLE`
   for changing a `CHECK` in place). Deliberately widened only for
   what this round needs, not speculatively for `/nick`'s not-yet-
   designed event kind too — consistent with this project's "don't
   build for hypothetical future requirements" stance, even though it
   means another rebuild migration is a known, accepted cost whenever
   `/nick` actually lands.
2. **`/me <action>` renders identically for the actor and everyone
   else** (`* alice waves`) — unlike an ordinary chat message, there's
   no "my own words" distinction worth making for a shared narrative-
   style action, so it skips the self-color/others-color split
   `send_loop` uses for plain messages.
3. **Registered in the round 39 dispatcher** (`_COMMANDS["me"] =
   _handle_me`) — no new dispatch mechanism needed, confirming 5a's
   infrastructure was built correctly ahead of its consumers.
4. **Testing:** new `tests/test_chat_action.py` (5 cases: shown to the
   actor, broadcast to others, recorded with the right kind/body,
   usage message on empty action text, correct scrollback replay).
   Full suite re-run after adding it (710 passed, 1 skipped) —
   actually run, not just syntax-checked.

**Remaining Track 5b scope, explicitly deferred:** `/nick` (transparent
display aliases, needs a rendering-wide change), `/away` (needs new
session-lifecycle presence tracking), and the per-user chat timestamp
preference (round 32, point 3) — each merits its own dedicated round.

## Sign-off notes, round 41 (/nick transparent display aliases — implemented)

The second slice of Track 5b, per design doc round 32, points 7-10.

1. **New module `netbbs.chat.nick`**, not folded into
   `netbbs.directory` — round 32 discusses `/nick` alongside `/me`/
   `/away` as chat presentation, a different concern from the user-
   directory/vCard feature even though both sit on the same generic
   `netbbs.user_preferences` store underneath (round 38).
2. **Validation**: a 32-character length cap (not specified exactly by
   §13; a reasonable default in line with typical IRC/Discord nick
   limits, easy to reconsider later, same spirit as the bio's 6-line
   cap being "a reasonable default, easy to reconsider" in round 38),
   and a case-insensitive check against every other account's
   canonical username (round 32, point 8: "may not exactly match
   another account's canonical username" — checked case-insensitively
   for actual anti-impersonation effect, since a same-cased-only check
   would let `ALICE` masquerade as `alice`). Setting your own username
   as your own nick is explicitly allowed — harmless, not
   impersonation. Character content itself isn't validated at set
   time, matching this codebase's consistent "sanitize on output, not
   storage" convention already used for bios, post bodies, and chat
   messages.
3. **`/nick off` clears the alias; `/nick` alone shows the current
   one.** Chosen over accepting a bare blank line as "clear" (which
   `/mute`-style commands don't need to disambiguate, but this one
   does) — an explicit reserved word avoids the ambiguity of "did they
   mean to clear it, or did they just hit enter by mistake."
4. **Nickname changes are their own scrollback event kind (`'nick'`)**,
   per round 32 point 10 — another `channel_messages.kind` CHECK
   widening via the same standard SQLite table-rebuild as rounds 37/40
   (still no `ALTER TABLE` for changing a `CHECK` in place; this is
   the third such rebuild this phase, a known, accepted friction of
   the "add a new event kind" pattern rather than something worth
   over-engineering around by pre-widening for hypothetical future
   kinds).
5. **Live rendering shows the *current* alias, computed fresh at each
   point of use — not cached from channel-join time** — so a nick
   change via `/nick` mid-session is reflected immediately in the very
   next message/action, not just after a rejoin. This applies to join,
   leave, regular messages, and `/me` actions.
6. **Scrollback replay also shows the *current* alias, not whatever
   was set at the original moment** — there's no per-message nick
   snapshot; a new `_resolve_display_label` helper re-resolves
   `ChannelMessage.author_label` (the stored canonical username) back
   to a `User` and looks up their alias live. Falls back to the bare
   canonical label if the account can no longer be found (defensive;
   nothing can actually delete an account yet). `_render_scrollback_
   message` therefore now takes `db: Database` — its only call site
   (the scrollback-replay loop in `_chat_loop`) was updated, and one
   existing direct-call test (`tests/test_terminal_sanitization.py`)
   needed a matching one-line update, caught immediately by the full
   suite re-run rather than missed.
7. **Moderation notices and command targeting deliberately stay
   canonical-only, unchanged** — `_announce_moderation`/
   `_moderation_detail`/`_resolve_target` still use `user.username`/
   `target.username` directly, never `display_label`. Matches round
   32 point 7 explicitly ("moderation, permissions, blocking,
   reputation, auditing, and addressing always operate on canonical
   identity") — confirmed with a dedicated test
   (`test_moderation_notices_stay_canonical_only`) rather than left as
   an unverified assumption.
8. **Resolving a *nick* to a canonical identity for command targeting
   (round 32 point 9: "an alias may be accepted only when it resolves
   uniquely") is explicitly out of scope for this round** — `/mute`,
   `/ban`, `/kick`, `/finger` etc. still only resolve canonical
   usernames via `_resolve_target`, unchanged. That's addressing/
   completion scope (Track 5e/5f), not this round's.
9. **Testing:** `tests/test_chat_nick.py` (11 cases, library-level:
   validation, case-insensitive collision, clearing) and `tests/
   test_chat_flow_nick.py` (12 cases: the command itself, and that the
   alias actually shows up across every live/replay rendering path,
   plus the canonical-only moderation-notice guarantee). Full suite
   re-run after adding both, catching and fixing the
   `test_terminal_sanitization.py` call-site break from point 6 in the
   same pass (733 passed, 1 skipped) — actually run, not just
   syntax-checked.

## Sign-off notes, round 42 (/away node-wide presence — implemented)

The third and final slice of Track 5b, per design doc round 32,
points 5-6. This closes out Track 5b entirely — `/me` (round 40),
`/nick` (round 41), and now `/away` are all implemented; the per-user
chat timestamp preference from round 32 point 3 was folded into this
round too (see point 6 below), since it turned out to need no new
mechanism beyond what `/away` already required.

1. **New `PresenceRegistry` class (`netbbs.chat.presence`), separate
   from `ChatHub`** — `ChatHub` tracks per-*channel* participants;
   this tracks per-*account* state that has nothing to do with which
   channel, or whether the account is in a channel at all. A user
   just browsing boards is just as "online" as one actively chatting.
   One instance per node, constructed in `netbbs.__main__` alongside
   `ChatHub`.
2. **The real plumbing question this round had to answer**: round
   32's "clears only when the account's final session disconnects"
   means "session" is a *login connection*, not a chat-channel visit —
   nothing in the codebase tracked that before. Solved by threading
   `PresenceRegistry` through `handle_session` (`netbbs.net.
   login_flow`, the actual per-connection entry point) →
   `_main_menu` → `browse_channels`/`_browse_channels_in_category` →
   `_chat_loop` → `ChatCommandContext`. `presence.enter(username)`/
   `presence.leave(username)` wrap the `_main_menu` call in
   `handle_session`, `leave()` inside a `finally` so an exception
   during the authenticated portion can't leak an "online forever"
   session count — verified directly with a test that makes
   `_main_menu` raise and confirms `leave()` still ran.
3. **`/away [message]` sets; `/away` alone clears** — matching §13's
   literal wording exactly ("`/away [message]` sets... `/away`
   without an argument clears it"), not a toggle. There's no "away
   with an empty message" path via bare `/away` — typing nothing *is*
   the clear command.
4. **Not written to scrollback or broadcast** — round 32 explicitly
   scopes away-status visibility to "local presence views and
   private-message feedback," neither of which exists yet (Track
   5c's `/who`/`/names`, Track 5e's `/msg`). This round only builds
   the state + a private confirmation to the user themselves; nothing
   else consumes `PresenceRegistry.is_away`/`get_away_message` yet,
   the same "ship plumbing ahead of its consumer" shape as several
   earlier rounds.
5. **Sending a message while away reminds, doesn't clear** (round 32,
   point 6) — `send_loop` checks `presence.is_away` immediately after
   a message is sent (not before, and not blocking the send) and
   writes "(You are still marked away.)" if so.
6. **Per-user chat timestamp preference (round 32, point 3) — found
   to need no new work this round.** On inspection, `format_for_
   display` (`netbbs.timeutil`) already accepts an `override_format`/
   `override_timezone` parameter reserved for exactly this, per that
   function's own docstring: "an eventual per-user value... nothing
   calls these with real per-user values yet, but the parameters
   exist now so wiring that in later needs no changes here." Wiring a
   real per-user value through (via `netbbs.user_preferences`, round
   38's generic store) is a small follow-up left for whenever
   Track 5's UI actually needs a `/timestamps` command — not blocking
   anything else in Track 5b, so not done speculatively here.
7. **Testing:** `tests/test_chat_presence.py` (15 cases, library-level
   `PresenceRegistry`: session counting, multi-session away sharing,
   away clearing only on the *final* leave), `tests/test_chat_flow_
   away.py` (8 cases: the command, scrollback/broadcast silence, the
   send-while-away reminder), and `tests/test_login_presence.py` (2
   cases: `handle_session` actually calls `enter`/`leave` around the
   main menu, including the exception-safety path). Full suite
   re-run after adding all three, catching and fixing two more
   direct `_chat_loop`/`handle_session` call-site breaks across seven
   existing test files from threading the new `presence` parameter
   through (`test_chat_dispatch.py`, `test_chat_flow_moderation.py`,
   `test_chat_action.py`, `test_chat_flow_nick.py`,
   `test_terminal_sanitization.py`, `test_login_outcomes.py`,
   `test_login_throttling.py`) — all caught immediately by the full
   suite re-run, not left for later discovery (758 passed, 1
   skipped) — actually run, not just syntax-checked.

**Track 5b is now complete.** Track 5c (discovery: `/who`, `/names`,
`/list`, `/whois`) is next per the sequencing plan.

## Sign-off notes, round 43 (discovery commands — implemented)

Track 5c: `/who`, `/names`, `/list`, `/whois`, per design doc rounds
32/33. No new storage needed — Track 4's `get_vcard` and Track 5b's
`PresenceRegistry` already provide everything this round reads from;
this is purely new views over existing state.

1. **`/names`/`/who` both operate on `ctx.channel`'s roster**, via a
   new `_roster_usernames` helper that parses `ChatHub.participant_
   ids`' opaque `"username:id(session)"` strings back into
   deduplicated, sorted canonical usernames — the one place that
   parsing happens for discovery purposes, mirroring how `/kick`/`/ban`
   already parse the same convention to find live sessions to remove.
   `/names` is one comma-separated line (alias-aware, via
   `display_label`); `/who` is one line per person with an away
   indicator where applicable — matching round 32/33's "compact
   roster" vs. "more detailed presence view" framing exactly.
2. **`/list` is a flat, sorted text dump** (pinned-first-then-
   alphabetical, matching `list_boards`'s existing sort precedent),
   not the interactive category-nested `pick_item` picker — a quick
   reference from inside chat, not a second navigation UI competing
   with the main menu's existing Chat browsing.
3. **`/whois` reuses `get_vcard` (Track 4) via a newly extracted
   `_write_vcard_detail` helper, shared with `/finger`** — both
   commands rendered near-identical blocks before this round; now one
   function renders it and `/whois` appends online/away/channel-
   membership lines afterward. Works for offline/never-online
   accounts too, same as `/finger` — a directory lookup, not an
   online-only one.
4. **A new `_channel_names_for_user` helper answers "which channels is
   X currently in"** — `ChatHub` only ever exposed the reverse
   direction (per-channel participant lists), so this iterates every
   channel the *requesting* user can see (`meets_level`, the same
   filter `/list` uses) and checks each one's roster for the target.
   This *is* round 32/33's "must respect... hidden-channel visibility"
   requirement for `/whois`, applied consistently now even though no
   channel is actually hidden yet (Track 5g) — nothing to revisit
   later, the filter is already in the right place.
5. **Caught while writing tests, not a production bug:** `/whois`'s
   online-status check reads `PresenceRegistry`, which only reflects
   the *login* session count (`handle_session`'s `enter`/`leave`, round
   42) — driving `_chat_loop` directly in tests, as this whole test
   family already does, bypasses `login_flow` entirely, so a target
   "in" a channel during a test isn't automatically "online" per
   presence unless the test also calls `presence.enter` itself,
   matching what a real connection actually does first. Fixed in the
   test, not the code — the behavior is correct (online reflects being
   logged in at all, not specifically being in a chat channel; a user
   just browsing boards is just as online as one actively chatting,
   per round 42's own reasoning).
6. **Testing:** new `tests/test_chat_discovery.py` (13 cases: roster
   listing and dedup across two sessions of the same account, away
   indicators in `/who`, level-gating in `/list`, and `/whois`'s
   online/away/channel-membership/bio-visibility behavior). Full suite
   re-run after adding it (771 passed, 1 skipped) — actually run, not
   just syntax-checked.

**Track 5c is now complete — per Thiesi's instruction, this is the
commit/push checkpoint.** Remaining Track 5 scope: 5d (channel
switching: `/join`, `/leave` redefined, `/topic`), 5e (private
messaging: `/msg`, `/private`, `/close`), 5f (completion), 5g
(invite-only/hidden channels).

## Sign-off notes, round 44 (menu/navigation UX consistency — implemented)

Prompted by Thiesi actually testing the current build and raising a batch
of small, real UX inconsistencies — plus a separately-reported flaky test
on Thiesi's NetBSD hardware, fixed in the same pass since it surfaced
first.

1. **Flaky test fixed, not just noted:**
   `tests/test_main_lifecycle.py`'s Telnet-listener-reachability tests
   raced a fixed `asyncio.sleep(0.1)` against `Database.__init__`'s fully
   synchronous startup (opening the file plus all 20 migrations, no
   `await` anywhere in between) — reliable on the Windows dev sandbox,
   but failed with `ConnectionRefusedError` on Thiesi's real NetBSD
   hardware, the same "wall-clock timing between two nearby operations"
   hazard rounds 20/28 already hit elsewhere. Fixed with a bounded
   retry-connect loop (`_open_connection_when_ready`) instead of a bigger
   guessed delay, which would only move the same race to whatever machine
   is slower next.
2. **"Boards" → "Message Boards" everywhere user-facing**, matching
   "File Areas"' full-name convention (main menu label, board-picker
   title/empty-message). The main menu's `[B]oards` hotkey itself is
   unchanged — rendered as `Message [B]oards` rather than switching the
   highlighted letter to `M`, to avoid quietly changing a keybinding
   nobody asked to change as a side effect of a label rename.
3. **`[Q]uit` → `[B]ack`, scoped to `netbbs.net.picker.pick_item` only —
   both the label and the actual keystroke, confirmed with Thiesi after
   inventorying every place "quit"/"back"/"Enter" appears in the UI.**
   The picker's `[Q]uit` was the one genuine mislabeling (it exits a
   list without ending anything, same as every other `[B]ack` screen);
   chat's typed `/quit` command and the main menu's `[L]ogoff` both
   genuinely end something (chat participation; the connection) and were
   confirmed to stay as-is — renaming `/quit` specifically would be a
   functional/muscle-memory change, not a cosmetic one, and would blur
   against Track 5d's upcoming `/leave` (a different action: back to the
   channel picker, not out of chat entirely).
4. **No redraw and no error message on an invalid single-keystroke menu
   choice — a silent bell (`\a`) instead.** A holdover from the
   pre-round-15 line-mode menu, no longer meaningful once dispatch is
   immediate (single-keystroke `read_key()`): reprinting an entire
   menu/page plus "Unknown choice" just because one stray key didn't
   match anything. Fixed by separating "draw the menu/page" from
   "prompt for a choice" in `_main_menu`, `pick_item`, `_show_board`, and
   `_show_area` (the latter two weren't explicitly named in the original
   report but have the identical pattern, caught while fixing the
   others) — drawing now happens once on entry and again only after a
   real state change (returning from a submenu, paging, a completed
   search), never on a no-op keystroke. A sub-prompt a user deliberately
   typed into (`pick_item`'s `search`/`goto`) still gets its own specific
   text response on failure ("No matches.", "Not a number.") — a direct
   answer to a specific question, unlike a stray top-level keystroke.
5. **`_edit_profile` had a real, separate bug, found while checking the
   "(or Enter)" claim below, not just a stale label:** it had no
   `else`/unknown-choice branch at all, so any key that wasn't `e`/`v` —
   not just `b` — silently exited back to the main menu. Fixed by
   restructuring it into a loop (redrawing the profile after an edit or
   toggle, same shape as the other screens above) with an explicit
   `b`-only back check.
6. **`_show_board`'s and `_edit_profile`'s "Back (or Enter)" label was
   false — confirmed by reading the code, not just the report:**
   `read_key()` never returns an empty string for Enter on any transport
   (Telnet/SSH/Web all discard CR/LF internally and keep waiting for a
   real keystroke) — `_show_board`'s old `elif choice in ("", "b")` had a
   dead `""` branch that could never fire. Text removed from both;
   `_show_area` (which reads its choice via `read_line()`, not
   `read_key()`, since it also has to accept free-text `/download`/
   `/upload`) genuinely *did* accept a bare Enter as "back" before this
   round — removed there too for the same one-consistent-key reason,
   confirmed with Thiesi as a deliberate extension of the principle
   rather than left as a working exception.
7. **A real, previously-latent test bug found and fixed, not just
   papered over:** several `FakeSession` test doubles (in
   `tests/test_board_pagination_ui.py`, `test_file_area_pagination_ui.py`,
   `test_directory_ui.py`, `test_terminal_sanitization.py`) returned `""`
   once their scripted keys ran out, and multiple tests silently relied
   on the *old* `_show_board`/`_edit_profile`/picker code treating that
   `""` the same as `"b"` — dead code for any real transport (confirmed
   above), but load-bearing for these tests. Removing that dead branch as
   part of point 4 turned those tests into infinite loops instead of
   clean passes/failures, caught because the run hung rather than
   finished. Fixed by scripting an explicit trailing `"b"` in every
   affected test, and — in the two files where `read_key()` has no other
   legitimate reason to return `""` — hardening the fake to raise a clear
   `AssertionError` on exhaustion instead of looping forever, so a future
   under-scripted test fails fast rather than hanging (left unchanged in
   `test_terminal_sanitization.py`, where the same fake's `read_line()`
   legitimately still relies on an `""` fallback for an unrelated
   optional-prompt skip case, and blanket-raising there would conflate
   the two).
8. **Testing:** full suite re-run after every fix in this round
   (771 passed, 1 skipped, unchanged from before — this round touched
   behavior and test fixtures, not test count) — actually run, not just
   syntax-checked, including the specific hung-test scenario reproduced
   and confirmed fixed before moving on, not just assumed fixed from
   reading the diff.

## Sign-off notes, round 45 (Phase 2 Track 5d: channel switching — implemented)

Implements the plan agreed with Thiesi for Track 5d (`/join`, `/leave`
redefined, `/topic`) — see design doc §8 and rounds 32/33's original
scoping. Also resequences the remaining Track 5 work: a new Track 5f
(command history & cursor-addressable line editing) is inserted before
what was Track 5f (tab completion, now 5g), which in turn pushes
invite-only/hidden channels from 5g to 5h — prompted by Thiesi actually
using the chat command surface and finding retyping long commands
painful, discussed and confirmed before any of Track 5 continued.

1. **`CommandHandler`'s return type widened from a bare `bool` to a
   small tagged union, `ChatAction`** (`_Quit | _ToPicker | _SwitchTo`),
   not replaced with something else — a direct, mechanical extension of
   round 39's own "explicit return contract, not exceptions" choice, now
   needing to distinguish three outcomes instead of one. `_dispatch_command`
   and `send_loop` propagate whatever a handler returns instead of
   coercing to bool; `_chat_loop` itself now returns `ChatAction`,
   resolving to `_Quit()` whenever `receive_task` (not `send_task`)
   is what finished — a kick/ban or a dropped connection, same as before.
2. **`_chat_loop` itself needed no internal concept of "switching
   channels"** — `browse_channels` became a small outer loop instead of
   a single call, reacting to whatever `ChatAction` `_chat_loop` returns:
   `_SwitchTo(channel)` loops straight back into `_chat_loop` with the
   new channel (which naturally re-runs the existing leave-then-join
   sequence — no special-casing needed), `_ToPicker` re-enters channel
   selection, `_Quit` returns to the main menu. The old
   `_browse_channels_in_category` was split into `_pick_channel` (pure
   picking, returns `Channel | None`, no `_chat_loop` call inside it)
   so `browse_channels` could own the loop without duplicating the
   category-recursion logic.
3. **`/leave` stops aliasing `/quit`'s handler and gets its own**,
   returning `_ToPicker()` — a deliberate divergence from round 39's
   "both map to the same handler," flagged there as a placeholder
   specifically pending this track. `/quit` is unchanged.
4. **`/join <channel>`'s handler resolves and validates, but never
   touches `hub`/the database itself** — it looks up the channel,
   checks `meets_level` (the same filter `browse_channels`/`/list`
   already use), rejects a not-found/unauthorized/already-active target
   with a friendly message, and returns `_SwitchTo(channel)` on success.
   All the actual joining (ban check, `hub.join`, scrollback replay,
   join broadcast) happens for free via `_chat_loop`'s existing entry
   sequence once `browse_channels`' loop calls it again with the new
   channel — confirmed this required no new "switch" code path at all
   during design, not just assumed.
5. **`/topic`: new nullable `channels.topic` column** (migration 21),
   distinct from the existing `description` (a creation-time listing
   blurb, never moderator-edited). `netbbs.chat.channels.set_topic`
   gated by `ChannelPermission.EDIT` — already reserved for exactly this
   in `netbbs.moderation.roles` since round 34
   (`"EDIT = auto()  # gates /topic changes (round 33, point 5)"`).
   Logged via the existing `moderation_log` audit trail, satisfying
   round 33 point 5's "recorded... with setter identity and timestamp"
   with no new table. Deliberately **not** persisted into
   `channel_messages`/scrollback, unlike `/nick`'s explicit scrollback
   requirement — round 33 point 5 only asks for moderation-log history,
   and a topic scrollback kind would be a fourth `CHECK`-widening
   migration for something nothing asks for.
6. **A real, if narrow, bug found and fixed during testing, not
   shipped:** `_handle_topic`'s "view" branch initially read
   `ctx.channel.topic` directly — `ctx.channel` is a snapshot taken once
   per `_chat_loop` invocation (a frozen dataclass, never mutated in
   place), so after a successful `/topic <text>` change, viewing the
   topic again in the *same* session still showed the stale pre-change
   value for the rest of that visit. Caught by a test that set a topic
   and then immediately viewed it. Fixed by re-fetching the channel
   fresh from the database on every view, the same "look it up fresh,
   don't trust a cached snapshot" reasoning `display_label` already
   follows for `/nick` mid-session changes.
7. **`TopicError` is a new, small, local exception in
   `netbbs.chat.channels`, not `netbbs.chat.moderation`'s existing
   `ChatModerationError`.** `chat.moderation` already imports `Channel`
   from `chat.channels`; importing back the other way for `set_topic`
   to raise `ChatModerationError` would be a circular import. Confirmed
   directly (not just reasoned about) that `channels.py` importing
   `netbbs.moderation` (the generic package, not `netbbs.chat.moderation`)
   for `ChannelPermission`/`has_permission`/`record_action` has no such
   cycle — the same package `netbbs.chat.moderation` itself already
   depends on.
8. **Testing:** new `tests/test_channel_topic.py` (7 cases: permission
   gating including confirming `MODERATE` alone doesn't grant `EDIT`,
   per-object and local-blanket grants, clearing via an empty string,
   moderation-log recording) and `tests/test_chat_flow_join.py` (18
   cases — `/quit`/`/leave`/`/join`/`/topic` driven through the real
   `_chat_loop` dispatcher via the existing `FakeSession`
   (`test_chat_flow_moderation.py`), including a kick still resolving to
   `_Quit()`, and `browse_channels`' outer-loop dispatch tested in
   isolation via `monkeypatch`-ed `_pick_channel`/`_chat_loop` fakes,
   since exercising the real picker needs a `read_key()`-capable session
   this project's existing chat `FakeSession` deliberately doesn't
   implement). Full suite re-run after adding both (796 passed, 1
   skipped) — actually run, not just syntax-checked; the topic-staleness
   bug (point 6) was caught this way, not by review.

## Sign-off notes, round 46 (Phase 2 Track 5e: private messaging — implemented)

Implements the plan agreed with Thiesi for Track 5e (`/msg`,
`/private`/`/query`, `/close`) — see design doc round 32 point 1-2 and
round 33 point 1's original scoping.

1. **Delivery: mailbox + next-prompt, confirmed with Thiesi over full
   session-wide live interrupt delivery.** Round 32 requires `/msg` to
   reach "every active session belonging to that canonical account,"
   but only a session actually inside `_chat_loop` has any live receive
   mechanism today — `_main_menu`, board/file browsing, and the
   directory all just block synchronously with nothing listening in the
   background. True interrupt delivery to *any* screen would mean
   threading a persistent receive-task through `handle_session` itself,
   a much bigger change than this track's scope. Resolved instead as
   two paths: a recipient with a live participant_id in *some* channel
   right now gets pushed instantly via the existing `ChatHub` (new
   `_find_live_participant`, scanning every channel's roster the same
   O(channels) way `_channel_names_for_user`/`_kick_live_sessions`
   already do); otherwise the message queues in a new
   `netbbs.chat.mailbox.MessageMailbox` and is shown at the recipient's
   next natural prompt.
2. **Exactly one flush point needed: the top of `_main_menu`'s loop**
   (folded into the existing `_draw_main_menu` helper, which already
   ran on entry and after every submenu return) — every screen (boards,
   files, directory, profile, chat) already passes through there before
   its next redraw, so no other call site needed touching.
   `MessageMailbox` is constructed once in `__main__.py` alongside
   `hub`/`presence` and threaded the same way, all the way down through
   `handle_session` → `_main_menu` → `browse_channels` → `_chat_loop` →
   `ChatCommandContext`.
3. **`/msg`/`/private` both check `presence.is_online(...)` at send
   time and refuse outright if not online** (round 32 point 1) — no
   queuing for a genuinely offline user, only for the
   online-but-not-reachable-right-now gap the mailbox exists for. A
   mailbox entry for someone who disconnects before their next
   `_main_menu` flush is simply dropped, an accepted edge case matching
   live `/msg`'s fundamentally ephemeral nature (round 32 point 2: never
   silently falls back to Phase 3's store-and-forward Link messages).
   Never written to scrollback or the moderation log, confirmed by a
   dedicated test, not just assumed.
4. **`/private <user>` layers on `/msg` via new `_EnterPrivate`/
   `_ExitPrivate` `ChatAction` variants** — unlike `_Quit`/`_ToPicker`/
   `_SwitchTo` (Track 5d), these never propagate past `send_loop`:
   they're consumed there, updating a local `private_target: User |
   None` closure variable. **Confirmed with Thiesi: other slash-commands
   still dispatch normally while in private mode** (matching round 39's
   existing "leading `/` is always a command attempt" rule, no special-
   casing needed) — only non-slash lines change meaning, routed to the
   private conversation via a shared `_deliver_private_message` helper
   instead of posted to the channel. `/close` (`_handle_close`) always
   returns `_ExitPrivate()` unconditionally; `send_loop` itself is the
   only place that actually knows whether private mode is active, so it
   decides what message to show ("Returned to #channel" vs. "You are
   not in a private conversation").
5. **`/query` is registered as a plain alias for `_handle_private` in
   `_COMMANDS`** (round 33 point 1: "accepted only as an IRC-
   compatibility alias") — no separate handler.
6. **A real edge case identified and handled, not left implicit:** if
   `private_target` goes offline *during* an active private
   conversation (not just before `/private` is first typed), the next
   plain line sent checks `presence.is_online` again before delivering,
   clears `private_target`, and tells the user — rather than silently
   queuing into a mailbox for someone who's no longer reachable at all.
   Verified with a dedicated test using a small `FakeSession` subclass
   that drops the target's presence between two scripted lines, not
   just reasoned about.
7. **Testing:** new `tests/test_chat_mailbox.py` (6 cases, library-level
   `MessageMailbox`), `tests/test_chat_flow_private.py` (13 cases —
   `/msg`'s online-check refusal, live delivery to a recipient in a
   *different* channel, mailbox fallback for online-but-not-in-any-
   channel, never written to scrollback/moderation-log, `/private`
   entry and plain-line routing, commands still dispatching mid-private-
   conversation, `/close`, `/query`'s alias behavior, and the mid-
   conversation offline edge case from point 6), and `tests/
   test_login_mailbox_flush.py` (4 cases driving `_main_menu` directly —
   a pending message shown before the menu on entry, shown exactly
   once, no spurious output when the mailbox is empty, and a message
   queued while "in" a submenu appearing after the return-to-menu
   redraw). Threading the new `mailbox` parameter through `_chat_loop`/
   `browse_channels`/`handle_session`/`_main_menu` required updating
   call sites across nine existing test files (the same "widening a
   threaded parameter breaks many call sites" pattern round 42's own
   sign-off note already described when `presence` was first
   introduced) — caught immediately by the full suite re-run, not left
   for later discovery. Full suite re-run after all of this (819
   passed, 1 skipped) — actually run, not just syntax-checked.

## Sign-off notes, round 47 (Phase 2 Track 5f: command history & cursor-addressable line editing — implemented)

Implements the track Thiesi added mid-session after actually using the
chat command surface and finding retyping long commands (especially
ones with a trailing free-text reason, e.g. `/mute bob spamming in
#general again`) genuinely painful. Confirmed scope up front: history
*and* full cursor editing, not history alone, sequenced as its own
track before Track 5g (tab completion) rather than folded into it —
see the plan's decision 5. Explicitly **not** the deferred "TUI half"
(round 13/26's screen-buffer-diffing scope for the eventual fullscreen
editor): a single, non-wrapping input line is a bounded reprint-and-
reposition problem, closer to a shell's readline than to a screen
editor, and this doesn't pull that heavier machinery forward or
substitute for it.

1. **Two new transport-agnostic primitives added to
   `netbbs.net.char_input`**, reused directly by both the byte-oriented
   Telnet/SSH path and (unlike everything else in that module) the
   already-decoded-character Web path: `move_cursor(count, *,
   forward)` (emits `ESC[<n>C`/`ESC[<n>D`) and `redraw_tail(write, *,
   terminal_col, edit_pos, line, new_cursor)` — reposition to
   `edit_pos`, erase-to-end-of-line (`ESC[K`), reprint the line's tail,
   reposition to `new_cursor`. One redraw operation covers every edit
   shape: mid-line insert, backspace, forward-delete, and full-line
   history recall (`edit_pos=0`) all just call it with different
   arguments, rather than each having its own erase/reprint logic.
2. **Escape-sequence handling changes from discard-only to
   parse-and-act, for a specific, still-bounded set of sequences.**
   `_discard_escape_sequence` is renamed `_read_escape_sequence` and
   now returns a symbolic key string (`"UP"`/`"DOWN"`/`"LEFT"`/
   `"RIGHT"`/`"HOME"`/`"END"`/`"DELETE"`/`"INSERT"`) instead of `None`,
   via new `_CSI_FINAL_TO_KEY`/`_CSI_TILDE_TO_KEY`/`_SS3_TO_KEY`
   lookup tables covering both `ESC[<letter>` and `ESC[<n>~` forms
   (some terminals send Home/End as `ESC[1~`/`ESC[4~` rather than
   `ESC[H`/`ESC[F`) plus the SS3 (`ESC O <letter>`) variants. Anything
   not in those tables is still discarded as a complete unit exactly as
   before — the existing bounded-length/bounded-time safety properties
   (`_MAX_ESCAPE_SEQUENCE_LENGTH`, `_ESCAPE_SEQUENCE_TIMEOUT`, round
   13/14) are unchanged, only loosened in *which* recognized sequences
   now carry meaning. `read_key` keeps its old behavior unchanged
   (still discards every escape sequence, now via the renamed
   function, ignoring its returned key).
3. **The line buffer became cursor-aware**, replacing the old
   append/pop-at-the-end-only model: `line: list[str]` plus a live
   cursor index. Backspace removes the character before the cursor
   (not necessarily the buffer's last character) and moves the cursor
   back; Delete removes the character at the cursor without moving it;
   Insert toggles overwrite mode (replace-at-cursor instead of
   insert-and-shift, falling back to append at the end of the line,
   matching today's behavior, when there's nothing to overwrite).
   Left/Right/Home/End move the cursor without touching the buffer.
4. **`InputHistory`** (`char_input.py`): `record(line)` (skips blank
   lines), bounded to a fixed `max_entries` (default 50 — the same
   bounded-not-unbounded posture as chat scrollback's 100-event cap and
   the picker's 99-item page cap), in-memory only, no persistence, same
   ephemeral posture as chat itself. Up/Down recall preserves whatever
   was mid-typed before the first Up (a `saved_in_progress` slot
   restored when Down is pressed past the newest recalled entry) —
   standard shell-history behavior, not something Thiesi asked for
   explicitly but assumed as baseline given the "full cursor editing"
   framing.
5. **Owned per connected session, not per node and not per channel.**
   `history = InputHistory()` is constructed once inside
   `handle_session` (unlike `hub`/`presence`/`mailbox`, which are
   node-wide, constructed once in `__main__.py`) — so recall works
   across a `/join` channel switch within one connection, but each new
   connection starts with empty history, and one user's two concurrent
   sessions don't share recall state. Threaded through only as far as
   chat needs it today (`_main_menu` → `browse_channels` →
   `_chat_loop` → `send_loop`'s `session.read_line(history=history)`)
   — board/file/directory/profile browsing deliberately do not receive
   it, out of scope per the plan.
6. **Masked reads (`echo=False`, i.e. password entry) deliberately
   keep the old simple append/pop behavior** — no cursor movement, no
   history recall, nothing that would echo characters of a password
   back via redraw. Split into `_read_line_masked` vs.
   `_read_line_editable` in both `char_input.py` and `web.py`, selected
   by `echo`, so this isn't a special case bolted onto the new logic
   but a clean fork at the top of `read_line`.
7. **`netbbs.net.web.WebSession` gets a full parallel
   implementation**, not a shared one — consistent with round 25's
   already-accepted deliberate non-sharing between the byte-oriented
   and character-oriented transports (the same duplication shape Track
   5g's tab completion will also need there). It imports and reuses
   `move_cursor`/`redraw_tail`/`InputHistory` directly from
   `char_input.py` (those three have no bytes dependency at all, so
   nothing prevented sharing them specifically), but has its own
   symbolic-key parsing: `_strip_escape_sequences` is replaced by
   `_parse_input_events(data) -> list[str | _SpecialKey]`, and
   `WebSession._char_queue` is retyped to carry that union so
   `_read_line_editable` can distinguish a literal typed character from
   an arrow/Home/End/Delete/Insert event the same way the byte path
   does with its symbolic-string return.
8. **`Session.read_line`'s ABC signature grows an optional `history:
   InputHistory | None = None` parameter**, threaded straight through
   by both `TelnetSession` and `SSHSession` into `char_input.read_line`
   (both already delegate everything else there). A `TYPE_CHECKING`-
   guarded import of `InputHistory` into `session.py` avoids a circular
   import, since `char_input.py` already imports `SessionClosedError`
   from `session.py`.
9. **Two real bugs found by actually running the tests, not by
   review — both from the same "byte-exact assertion meets dangling
   connection" failure shape rounds 44's sign-off note already
   described once:**
   - `tests/test_telnet.py` had two tests hardcoding the old
     backspace-at-end-of-line wire output (`b"\b \b"`, 3 bytes — the
     classic terminal trick). The new unified `redraw_tail` primitive
     legitimately sends `b"\x1b[1D\x1b[K"` (7 bytes) instead for the
     same visual effect. The resulting assertion mismatch triggered a
     **hang**, not a clean failure — the connection was left dangling
     past the failed assertion and `server.stop()` blocked waiting for
     the handler task. Diagnosed via a standalone repro script
     confirming the real bytes sent were correct and prompt, isolating
     the hang to the test's own missing cleanup after the assertion.
     Fixed by updating both hardcoded assertions to the new sequence.
   - A self-caught timing hazard in my own new
     `tests/test_web_line_editing.py`: an initial helper used
     `await asyncio.sleep(0.1)` to "let the server catch up" before
     closing the connection — exactly the guessed-fixed-delay
     anti-pattern this project's own history has flagged repeatedly
     (rounds 20, 28, 44). Caught and fixed before the first run,
     replaced with deterministic draining of `ws.receive_json()` until
     a message ending in `"\r\n"` is observed.
10. **Testing:** `tests/test_char_input_line_editing.py` (22 cases —
    cursor movement including tilde-form Home/End, exact
    escape-sequence byte assertions for mid-line insert/backspace/
    delete, Insert/overwrite toggle, masked-read exemption confirmed)
    and `tests/test_char_input_history.py` (15 cases — `InputHistory`
    in isolation plus Up/Down wired into `read_line`). New
    `tests/test_web_line_editing.py` (10 cases — the same shapes via
    `WebSession`, plus one exact-wire-message-sequence test). All 19
    pre-existing `test_char_input.py` tests re-verified passing
    unchanged, confirming behavioral backward-compatibility for
    everything not newly recognized. Threading the new `history`
    parameter through `_chat_loop`/`browse_channels`/`_main_menu`
    required updating call sites across eleven existing test files —
    the same mechanical "widening a threaded parameter breaks many
    call sites" pattern rounds 42 and 46 already described, not a new
    kind of problem. Full suite re-run after all of this: **866
    passed, 1 skipped** — actually run, not just syntax-checked.

## Sign-off notes, round 48 (menu invalid-key consistency fixes + Boards hotkey rename — implemented)

Thiesi tested a real session against round 44's "no redraw on invalid
input" fix and found it wasn't actually consistent across screens —
pasted a real transcript showing the `Choice: ` prompt visibly running
together across repeated invalid keystrokes in one screen but not
another. Investigated by reading the four screens that share this
"single keystroke, bell-only feedback" shape (`_main_menu`, `pick_item`,
`_show_board`, `_edit_profile`) side by side rather than guessing from
the transcript alone (the pasted text itself had every real `\r\n`
flattened by whatever copied it, so it couldn't be trusted to show
*which* screen was actually missing one — confirmed by reading the code
instead). Two distinct, real bugs found, plus a separate labeling
request actioned in the same round:

1. **`netbbs.net.picker.pick_item`'s unrecognized-key fallback never
   emitted a newline before the bell** — unlike `_main_menu`/
   `_show_board`, which both unconditionally move to a fresh line after
   *every* keystroke (valid or not) before evaluating it. The picker's
   final `else` branch (an unrecognized letter that isn't `n`/`p`/`s`/
   `g`/`b` and isn't a digit) just wrote the bell and looped straight
   back to `write("Choice: ")` with nothing in between — so two
   consecutive invalid keys produced `Choice: zChoice: yChoice: ...`,
   growing on one line forever, exactly what Thiesi's transcript showed.
   The digit-selection and page-boundary invalid paths were already
   correct (they already had a `write_line("")` earlier in their own
   branches) — only this one fallback was missing it. Fixed with a
   single added `write_line("")` before the bell, bringing it in line
   with the other three screens. Regression test:
   `tests/test_picker.py::test_repeated_invalid_keys_each_land_on_their_own_line`,
   asserting the exact byte sequence (`b"z\r\n\aChoice: "`) rather than
   just "a bell appears somewhere" the way the two pre-existing invalid-
   key tests in that file already did — those would not have caught
   this bug, which is presumably why it shipped in round 44 unnoticed.
2. **`_edit_profile` still redrew the entire profile screen on every
   loop iteration, including right after an invalid keystroke** — a
   real regression against round 44's own agreement and against that
   function's own docstring, which claimed "an unrecognized key now
   sounds a bell and re-prompts, same as the main menu" while the code
   actually redrew the bio/visibility/options block unconditionally at
   the top of the loop every time. `_show_board` had already been fixed
   correctly in round 44 (state-rendering split into `_render_board_page`,
   called only on entry and after a real page change) but `_edit_profile`
   was missed. Fixed the same way: new `_render_profile` helper, called
   once on entry and again only after `e`/`v` actually change something;
   the loop itself now only does `write("Choice: ")` → read → bell-on-
   invalid, never redrawing. Regression test:
   `tests/test_directory_ui.py::test_edit_profile_invalid_key_does_not_redraw_the_screen`,
   asserting `"Your profile:"` appears in the output exactly once even
   after an invalid keystroke — the four pre-existing `_edit_profile`
   tests never drove an invalid key at all, so none of them could have
   caught this.
3. **Main menu's Boards hotkey changed from `B` to `M`, at Thiesi's
   explicit request.** Round 44 renamed the label text ("Boards" →
   "Message Boards") but left the underlying hotkey and its bracket
   position unchanged (`Message [B]oards`) — since that same round also
   made `B` the universal "back" key everywhere else in the system
   (picker's `[Q]uit`→`[B]ack`), having the main menu's own *entry* key
   for one specific section also be `B` reads as a collision, not just
   an odd mnemonic. Changed to `[M]essage Boards`, dispatch updated from
   `choice == "b"` to `choice == "m"`. One existing test
   (`tests/test_login_mailbox_flush.py`) scripted `"b"` to enter boards
   via a monkeypatched `_browse_boards`; updated to `"m"`. No other test
   exercises the main menu's boards entry key directly (everything else
   monkeypatches `_browse_boards` or drives it via `_browse_boards`
   itself, not through `_main_menu`'s dispatch).
4. **Testing:** two new regression tests (above) plus the one updated
   existing test. Full suite re-run: **868 passed, 1 skipped** —
   actually run, not just syntax-checked.

## Sign-off notes, round 49 (Phase 2 Track 5g: slash-command + username tab completion — implemented)

Implements the plan agreed with Thiesi for Track 5g (design doc §15
phasing, resequenced after 5f per round 45's decision 5) — Tab
completion for chat's slash-commands and username arguments, plus the
folded-in addendum wiring the same mechanism into the picker's
`"Search: "` prompt.

1. **`apply_tab_completion` added to `netbbs.net.char_input`, alongside
   a new `Completer = Callable[[str], Sequence[str]]` type** — reusing
   round 47/Track 5f's transport-agnostic split: this function and its
   two small helpers (`_current_word_start`, `_common_prefix`) have no
   dependency on bytes or `ByteSource`, so `netbbs.net.web.WebSession`
   imports and calls it directly rather than re-deriving the same
   redraw arithmetic a second time — the same reuse shape `move_cursor`/
   `redraw_tail`/`InputHistory` already established. Deliberately
   generic: the function has no idea what a "command" or "username" is,
   only the generic notion of "word" (the whitespace-delimited token
   ending at the cursor) needed to know how much of the buffer a
   candidate replaces. Zero candidates does nothing (not even a bell —
   an empty Tab press while composing free text isn't itself an error);
   one candidate replaces the word plus a trailing space; multiple
   candidates extend to their longest shared prefix (bash-style) and
   list every candidate below, then reprint the in-progress line.
   Wired into `_read_line_editable`'s main loop as a new `_TAB = 0x09`
   branch, positioned alongside the existing Backspace/Delete handling;
   `_read_line_masked` (password prompts) is untouched, matching the
   same echo=False scope boundary `history` already established.
2. **Deliberate simplification, not spelled out in the original plan
   text: no caller-side prompt label is redrawn alongside a
   multi-candidate list.** `read_line` has no idea a prompt string like
   `"Choice: "`/`"Search: "` even exists (chat's `send_loop` has no
   static prompt at all), so after printing candidates it only reprints
   the raw line buffer, not any label preceding it. Chat is unaffected
   (nothing to redraw); the picker's `"Search: "` label simply doesn't
   reappear until the next real prompt cycle. Documented in
   `apply_tab_completion`'s own docstring rather than left as a silent
   gap.
3. **`Session.read_line`'s ABC signature, and `TelnetSession`/
   `SSHSession`/`WebSession`, all gain the same optional `completer`
   parameter** — mechanically identical to how `history` was threaded
   in round 47, `Completer` imported via the same `TYPE_CHECKING`-
   guarded pattern in `session.py` to avoid the pre-existing circular
   import with `char_input.py`.
4. **The BBS-specific completer lives in `netbbs.net.chat_flow`, built
   fresh by `_build_completer(db, presence, channel, user)` on every
   `send_loop` iteration** — cheap (a handful of string comparisons,
   at most one permission lookup), and always reflects the actor's
   *current* moderator status rather than a snapshot taken once at
   channel entry, since grants can change mid-session. Three shapes,
   checked in order:
   - A bare `/word` with no space yet completes against `_COMMANDS`
     keys, filtered through a new, deliberately separate
     `_COMMAND_VISIBILITY: dict[str, Callable[[Database, Channel,
     User], bool]]` rather than widening `_COMMANDS`' own value type —
     `_dispatch_command` and `/help`'s listing need no changes at all.
     Only `/mute`/`/unmute`/`/ban`/`/unban`/`/kick` are gated (on
     `ChannelPermission.MODERATE`); everything else is always
     suggested. **This is purely a suggestion filter, not an
     authorization check** — the handlers themselves, via
     `ChatModerationError`, remain the sole source of truth for what's
     actually allowed to run; a non-moderator who already knows `/mute`
     exists can still type it, they just won't see it offered.
   - `/msg `, `/private `, `/query ` complete against
     `PresenceRegistry.online_usernames()` — a new method there,
     alongside the existing single-account `is_online` check, matching
     those three commands' own online-only refusal at send time (round
     46/Track 5e).
   - `/whois `, `/finger ` complete against every registered account
     (`netbbs.auth.users.list_users`), online or not — both commands
     already work for offline accounts.
   - Anything else (plain chat text, an unrecognized command, or past
     the first argument of a username-completing command) returns no
     candidates. All matching case-insensitive (round 33 point 6).
   - `/invite` (Track 5h) is explicitly not wired up yet — nothing to
     complete against until that track's membership model exists;
     `_COMMAND_VISIBILITY` is already shaped to accept its
     `MANAGE_MEMBERS` gate whenever it lands.
5. **Picker addendum, folded in as agreed:** `pick_item`'s `"Search: "`
   prompt gets a `_search_completer(candidates)` built fresh each time
   from `[name_of(item) for item in working_set]` — the *current*
   filtered set, not the caller's full original list, so a completion
   offered after an earlier search never suggests something that
   search already excluded. Purely additive, confirmed by test: the
   substring-match-on-Enter behavior is completely unchanged, Tab only
   ever helps when what's typed is already a real prefix of some
   candidate.
   - **One real scope boundary found and deliberately handled, not
     glossed over:** `apply_tab_completion`'s generic word-boundary
     logic only ever replaces the *last* whitespace-delimited word —
     exactly right for chat's single-word command/username
     completions, but capable of corrupting a multi-word picker
     candidate name (e.g. the category `"Vintage Computing"` seen in
     Thiesi's own round-48 transcript) if allowed to complete past an
     already-typed internal space: the generic replace-last-word logic
     would only overwrite the second word, duplicating the first.
     `_search_completer` sidesteps this by returning no candidates at
     all once the query already contains a space — completes the
     *first* word of a name reliably, never corrupts a multi-word one.
     Redefining the picker's own search matching to prefix-only (which
     could support the general multi-word case properly) is a separate,
     larger, round-16-reversing question, explicitly out of scope here,
     same as the original plan flagged.
6. **Testing:** `tests/test_char_input_completion.py` (10 cases — the
   generic word-replacement mechanics: no-completer/zero-candidate
   no-ops, single-candidate replacement with trailing space, multi-
   candidate shared-prefix extension and listing, and mid-line
   completion at a cursor position behind trailing untouched text),
   `tests/test_chat_completion.py` (17 cases — command-name completion
   including permission-gating and its exact channel-scoping, `/msg`/
   `/private`/`/query`'s online-only matching, `/whois`/`/finger`'s
   any-account matching, case-insensitivity, and plain-text/no-
   candidate cases), `tests/test_web_completion.py` (5 cases, the
   `WebSession` parallel), and `tests/test_chat_presence.py` (4 new
   cases for `online_usernames`). `tests/test_picker.py` gains 3
   completer-wired cases including the working-set-scoping and
   multi-word-safety behaviors above. Full suite re-run: **905 passed,
   1 skipped** — actually run, not just syntax-checked. (This round's
   verification was briefly interrupted by an unrelated platform-side
   tool outage blocking all command execution; every change was
   manually re-traced by hand against the actual source during that
   window rather than assumed correct, and the full suite passed
   without a single fix needed once execution resumed — a rare case
   worth noting, not the norm this project's history has otherwise
   shown for byte-level line-editing code.)

## Sign-off notes, round 50 (Phase 2 Track 5h: invite-only/hidden channels, manage_members — implemented)

Implements the plan agreed with Thiesi for Track 5h (design doc §8/
round 33 points 8/9/11) — the last of Tracks 5d-5h. **Phase 2 Track 5 is
now complete in its entirety.**

1. **Schema: two independent axes plus an opt-in, all defaulting off.**
   `channels.hidden`/`channels.members_only`/`channels.allow_member_
   invites` (all `INTEGER NOT NULL DEFAULT 0`) — an existing channel's
   behavior is unchanged unless a moderator explicitly opts in. New
   `channel_members(channel_id, user_id, granted_by_user_id, created_at,
   PRIMARY KEY(channel_id, user_id))` — deliberately its own table, not
   folded into `moderator_grants`: membership is access/visibility
   eligibility, not a permission bit, and conflating the two would be
   the same layering mistake this codebase has consistently avoided
   elsewhere. New `channel_invitations(id, channel_id, invited_user_id,
   invited_by_user_id, status, created_at, expires_at)`, `status IN
   ('pending', 'accepted', 'revoked')`, `UNIQUE(channel_id,
   invited_user_id)`. Expiry follows `channel_restrictions`' precedent
   (filter `expires_at` at check time, no sweep-on-access needed)
   rather than posts/files' round 35 sweep-and-delete pattern — nothing
   in the command surface actually sets an expiry yet (no duration
   argument on `/invite`), but the schema/query already honor one if
   ever set directly, confirmed by a dedicated test at the row level
   rather than left unverified.
2. **New `netbbs.chat.membership` module**, distinct from `netbbs.chat.
   channels` (CRUD/topic) and `netbbs.chat.moderation` (mute/ban/kick)
   for the same reason those two are already separate: membership is
   its own concern. `is_member`/`add_member`/`remove_member` (direct,
   persistent access — round 33 point 8's "granting or removing
   persistent access" as its own capability, distinct from invitations,
   bypassing the invite-then-accept flow entirely) and `create_
   invitation`/`revoke_invitation`/`has_pending_invitation`/`accept_
   invitation` (the invite-then-accept flow). Every mutating function
   checks `ChannelPermission.MANAGE_MEMBERS` itself and raises a new
   `MembershipError` — same "library function is the one place the
   authorization decision is made" pattern `netbbs.chat.moderation`'s
   `_impose`/`_lift` already established, not a new shape. `create_
   invitation`'s check is the one exception with an OR clause: MANAGE_
   MEMBERS, **or** `allow_member_invites` set and the actor already a
   member (round 33 point 11's opt-in) — checked in exactly one place,
   not duplicated at the command layer.
3. **No `/accept` command.** Accepting an invitation is just
   successfully `/join`-ing the channel — reuses Track 5d's existing
   "look up, check authorization, switch" flow instead of inventing
   parallel command surface for the same action. `_handle_join`'s
   eligibility now extends to `meets_level(...) AND (not members_only
   OR is_member(...) OR has_pending_invitation(...))`; a successful join
   consumed via a pending invitation calls `accept_invitation` to mark
   it accepted, so it doesn't dangle as still-pending after being used.
4. **Command surface, all gated by `MANAGE_MEMBERS` except `/invite`
   (its own OR-condition predicate) and `/members` (ungated — viewable
   by anyone already in the channel, reviewing your own channel's
   roster is different from administering it):** `/invite <user>`,
   `/uninvite <user>` (revokes a pending invitation only — nothing to
   revoke if none is pending, raises `MembershipError`), `/grantaccess
   <user>`/`/revokeaccess <user>` (direct `channel_members` add/remove).
   `/invite` notifies the invitee through Track 5e's mailbox/live-push
   mechanism directly (`_deliver_private_message`, reused verbatim, not
   duplicated) — this already works for a currently-offline invitee,
   unlike `/msg`'s own online-only send-time check, since mailbox
   delivery doesn't require the recipient to be online right now. No
   `/createchannel` exists (matches every earlier track's precedent);
   `create_channel` and `scripts/create_test_channel.py` both gained the
   three new optional parameters for seeding, the latter via three new
   trailing CLI flags.
5. **Visibility consolidated into one shared `_visible_channels_for(db,
   user)` in `chat_flow.py`**, replacing three separate, slightly-
   duplicated `meets_level`-only list comprehensions round 43 left
   behind (the picker's channel listing, `/list`, and `/whois`'s
   channel-membership display via `_channel_names_for_user`) — exactly
   the consolidation round 43's own sign-off note anticipated. A
   `hidden` channel is now actually excluded unless the user is already
   a member, holds a pending invitation, or holds *any* moderator grant
   on it (checked via `has_permission` with every `ChannelPermission`
   bit OR'd together as the argument — "does the user hold any of
   these," not one specific bit, avoiding a separate "has any grant at
   all" library function nothing else needs). "Hidden + open is
   obscurity, not access control" (round 33 point 9): a `members_only`-
   but-not-`hidden` channel still appears in every listing — you can see
   it exists and that you can't `/join` it without access; only `hidden`
   controls listing visibility itself. Confirmed as genuinely
   independent axes by a dedicated test, not assumed from the code
   reading alone.
6. **Tab completion extended, not just left as a Track 5g gap:**
   `/invite `, `/uninvite `, `/grantaccess `, `/revokeaccess ` added to
   the permission-aware `_COMMAND_VISIBILITY` predicate dict from round
   49 — three gated on `MANAGE_MEMBERS` alone (`_requires_manage_
   members`), but `/invite` gets its own predicate (`_can_invite`)
   matching `create_invitation`'s real OR-condition authorization
   exactly, a deliberate improvement on the original plan text's literal
   "gated on MANAGE_MEMBERS" for all four — a plain member on an
   `allow_member_invites` channel now correctly sees `/invite` suggested
   even without a permission grant, rather than a suggestion/reality
   mismatch. `/invite <user>` completes against registered users who
   aren't already direct members (a new, `/invite`-specific completion
   branch — not one of round 49's existing online/any-user prefix
   groups, since "invite-eligible" is its own definition).
7. **Testing:** `tests/test_channel_membership.py` (24 cases — schema/
   library-level `channel_members`/`channel_invitations` CRUD, the
   `allow_member_invites` OR-condition from both sides, expiry
   filtering at the row level, cross-channel scoping), `tests/
   test_chat_flow_membership.py` (22 cases — `/invite`/`/uninvite`/
   `/grantaccess`/`/revokeaccess`/`/members` through the real
   dispatcher, `/join` consuming a pending invitation and marking it
   accepted, a rejected `/join` against a `members_only` channel with no
   invitation, and a regression guard confirming ordinary open-channel
   `/join` is unaffected), `tests/test_chat_visibility.py` (12 cases —
   hidden/members_only exercised as genuinely independent combinations,
   `/list` and `/whois` through the real dispatcher, and the "grant on a
   *different* channel doesn't leak visibility" scoping check). `tests/
   test_chat_completion.py` gained 7 new cases for the membership-admin
   completion predicates. One real test-setup mistake caught and fixed
   before this round's tests were considered done, not just before they
   passed: an early version of the `/whois`-hides-a-hidden-channel test
   granted the *requester* `MANAGE_MEMBERS` on the hidden channel purely
   to let her perform `add_member` as the test's admin action — which
   then legitimately made the channel visible to her too, defeating the
   test's own premise. Fixed by introducing a separate admin account to
   perform that setup step, keeping the actual requester's grants at
   zero. Full suite re-run: **968 passed, 1 skipped** — actually run,
   not just syntax-checked.

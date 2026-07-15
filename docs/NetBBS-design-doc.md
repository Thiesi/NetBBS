# NetBBS — Design Document (v0.1, draft for review)

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

**Communities (topic-oriented navigation layer):** directionally agreed
but not yet assigned to a phase or fully specced — see §16 below for
what's decided and what's still open.

---

## 16. Communities (topic-oriented navigation layer)

Not yet phase-assigned — a directional decision, not a specced
feature. Recorded here so the reasoning doesn't need rediscovering once
it's actually scheduled; see round 71 sign-off note for the full
alternatives-considered discussion.

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
Communities do not replace or absorb them. A Community is a new, thin
object above them: a shared parent for navigation, and an optional
inheritance point for permissions/moderation defaults and presentation
(branding, visibility) that individual boards/channels/areas can still
override.

**Naming:** local ones are **Communities**; ones distributed across
NetBBS Link are **Link Communities** — not "Linked Communities" — for
consistency with the project's existing naming convention of
prefixing anything distributed over the mesh with "Link" rather than
"Linked" (Link messages, Link boards, Link chat; see §7, §15 Phase
3/5/6).

**Why this fits NetBBS specifically, beyond the general UX argument:**
it lines up with the node-operator autonomy principle already
established elsewhere in this doc (§2, §14) — just as a user
selectively enters the Community whose topic interests them, a SysOp
can selectively decide which Link Communities to carry on their node,
and in turn receives everything the Link has to offer about that
topic. This is the same "default-carry-with-visible-opt-out" shape
already documented for Phase 6's Linked board/channel creation (§15).

**Deliberately not decided yet — needs its own design round before
implementation:**
- Data model: does a board/channel/file-area belong to exactly one
  Community, zero-or-one, or can it belong to several?
- Permission inheritance mechanics: does a grant on a Community
  cascade to its boards/channels, and how does a per-resource override
  interact with that?
- What happens to resources that don't cleanly belong to any topic —
  private mail, a SysOp-announcements board, a general lobby channel.
  These will need to remain reachable outside the Community hierarchy;
  the shape of that "uncategorized" bucket isn't designed yet.
- Navigation/UX: veteran users lean on jump-shortcuts to skip menu
  layers; adding a Community layer without equivalent shortcuts would
  be a real regression for that workflow. Needs first-class design,
  not a follow-up afterthought.
- Migration path for the existing flat, single-Community-shaped
  install (i.e. Thiesi's own node today) into this model.
- Phase placement — genuinely unassigned; likely doesn't belong in
  Phase 3–5 (Link/trust/chat work) or Phase 7 (door games), but hasn't
  been scoped against Phase 6 governance work either.

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


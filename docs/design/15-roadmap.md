# NetBBS design — §15. Roadmap and phase boundaries

Part of the [NetBBS design](../NetBBS-design-doc.md). Section numbers are stable: cite this area's rules by their section numbers (§15.n).

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

### Phase 3 — Link connectivity and asynchronous services — implemented

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
- authenticated inventory/pull-based catch-up and multi-hop relay across
  boards, channels, and file-area catalogues, including empty-inventory
  discovery and responder/freshness/replay binding (§8.8, issues
  #85/#94/#106/#124).
- correctness-preserving `key_transition` retention, and the chain-
  idempotency fix that made any retention provable (§8.9, issue #86,
  closed) — board-scoped types remain intentionally unbounded.
- linked channels — genesis, promotion, materialization, and message
  propagation (§9.6, issue #87, closed); origin succession reused by
  reference only, not built, and moderator governance out of scope
  (Phase 6).
- board closure, origin-authorized moderator post edits, and tombstones
  (§9.5, issue #88, closed); linked-board moderator grants/revocations
  remain out of scope.
- remote file area catalogue exchange and on-demand, resumable, deduplicated
  chunk transfer (§11, issue #89, closed); file-area origin succession
  remains out of scope.
- linked-channel messages wired into the live interactive chat send path
  (issue #91, closed) — closes the gap issue #87 left open.
- interactive browse/fetch UI for remote file catalogues: a
  `[L]ink catalogue` hotkey reachable from file areas, both paginated and
  empty (§11, issue #92, closed) — closes the gap issue #89 left open.
- inventory/pull catch-up extended to file-area catalogues (§11.4,
  issue #93, closed) — closes the gap issue #89 left open; content
  bytes still require an explicit fetch, only catalogue metadata is
  recoverable this way.

Operational validation continuing independently of the development cycle:

- broader real-world multi-node deployment validation (issue #83).

### Phase 3 stabilization status and Phase 4 transition

Phase 3 contains enough tested federation behavior to support Phase 4
implementation. Issue #83's sustained dogfood continues as an independent
operational-validation track: its findings remain roadmap evidence and produce
focused fixes, but its calendar duration no longer blocks the development
cycle. Issue #71's independent non-Python interoperability proof is explicitly
deprioritized and remains open as deferred validation rather than a Phase 4
dependency.

Trust/reputation implementation and the shipped Phase 5/7 features have since
advanced. Phase 6 remains future work. Operational validation remains separate
from implementation sequencing.

The Phase 3 validation record is:

- every currently implemented Link product vertical (linked boards,
  linked channels, remote file areas, and Link mail) has at least one
  end-to-end regression test that exercises the real
  sender/receiver/acknowledgement or sender/receiver/materialization
  boundary across a restart, not only isolated unit coverage (issue #80);
- offline/missed-event catch-up exists and demonstrably converges after a
  partition, not only live delivery during an already-connected pass
  (§8.8, issues #85/#94, closed — including discovery from an empty
  inventory when the resource origin is independently known);
- retained event/dedup state has a correctness-preserving retention policy:
  purging the fast dedup cache must not make an old control event
  re-applicable, nor let suppressed or deleted content reappear (§8.9,
  issue #86, closed — `key_transition` alone is purged; every board-scoped
  type is provably still needed and stays unbounded, not silently deferred);
- issue #60's operational controls have been *rehearsed*, not only
  implemented: backup/restore and an upgrade/rollback have each been
  exercised against a real running node at least once beyond their original
  implementation test;
- a sustained real-world multi-node dogfood deployment (issue #83) is ongoing
  independently, with findings converted into issues or worklog invariants
  rather than left as a diary;
- the README, this design document, and the worklog agree on Phase 3's
  actual boundary, and a newcomer can install and run a node from a
  documented path (issues #76, #82);
- known protocol correctness issue #70 is closed. Issue #71's independent
  implementation is deferred: the Python reference implementation and checked
  canonical vectors remain the Link-v1 compatibility authority for now;
  external implementation interoperability remains explicitly unclaimed, and
  every wire change still requires versioning and vector updates.

Advancing development does not imply public federation. Phase 4's persistence,
protocol, enforcement, and UI are implemented; the remaining human/operational
validation in #131 is still a public-readiness gate. The threat model is in §12.

### Phase 4 — Trust, reputation, and public readiness — implemented; operational gate open

Completed product work informed by dogfood includes:

- direct-chat discoverability, single rendering, and field color (#134) —
  implemented: the pinned status row retains `/close` with a compact narrow-
  width form, submitted input is cleared before its committed rendering, and
  identity/message spans are independently sanitized and colored;
- safe line-mode composition and review-before-commit (#133) — implemented:
  the shared line buffer can list/insert/replace/delete submitted lines, and
  local mail, Link mail, and new posts share an explicit editable review state
  before persistence or dispatch;
- truthful single-key yes/no confirmations with Enter defaults (#135) —
  implemented through one shared structured-key primitive without weakening
  generic menu hotkeys; invalid keys retry and accepted choices end their row;
- current-build visual/capability verification plus bounded semantic-color
  polish on named mature surfaces (#136) — implemented: caller and SysOp Who
  share one picker palette; Who, mail, vCards, the session-history screens,
  profile fields,
  picker feedback, and welcome-banner administration distinguish labels,
  values, metadata, success, and failure through shared theme roles; colored
  narrow output is truncated by visible width rather than raw ANSI length.
  The default web login banner visibly exercises truecolor while the
  256-color rendering remains equivalent and readable. Profile diagnostics
  state the transport's detected capability or limitation; the banner preview
  no longer does, since a SysOp read it as developer output (issue #841). A
  custom SysOp banner bypasses the generated showcase. Before sign-in the
  node's own chrome is plain ASCII over Telnet, where CP437 terminals such as
  SyncTERM call from, and Unicode on the web and SSH; a custom banner is sent
  as authored either way (issue #841). Issue #929 is replacing this with a
  character set per session (§3.2). Both
  Telnet's and SSH's initial banners are shown before capability negotiation
  completes -- Telnet's can precede NEW-ENVIRON, and SSH's own pre-auth
  banner (asyncssh's `send_auth_banner`, sent from `begin_auth` before any
  session channel, and therefore any forwarded environment, exists at all)
  has no client capability to read yet either. Telnet's pre-login banner
  renders the same welcome-banner content at the safe 256-color depth; the
  later profile diagnostic reports the real, negotiated result once
  available. SSH's pre-auth banner (issue #203, dogfood report) sends the
  same content as plain text with every ANSI/VT100 escape sequence
  stripped instead -- `SSH_MSG_USERAUTH_BANNER` is shown during
  authentication itself, and real clients (PuTTY confirmed) commonly route
  it through a display path that never runs an ANSI parser over it at
  all, dumping literal escape bytes rather than color regardless of depth
  chosen; no color depth fixes a client that never interprets escapes at
  this stage. SSH's own *post-auth* welcome screen is unaffected and
  keeps full negotiated color, same as Telnet/web. SSH's pre-auth banner
  is shown regardless of authentication outcome, since it is the only
  screen SSH ever gets before the protocol-level handshake either
  succeeds into the authenticated session or fails.

- sectioned, paginated create/edit screens (dogfood report: the main menu's
  grouped, multi-column layout and the Profile/Board/Area/Channel screens'
  own flat field lists read as wildly different levels of polish) —
  implemented: `edit_resource_draft`'s `FieldSpec.section` groups a screen's
  fields under bold uppercase headings in both the value list and the
  hotkey/menu row, opt-in per screen (Profile, Board, File area, Channel;
  every other screen renders byte-for-byte as before). A dense, sectioned
  screen that still doesn't fit the caller's terminal even at its most
  compact menu tier paginates by section — `Page Up`/`Page Down` cycle
  between them, wrapping at either end, while every field's number
  keeps working regardless of which page is showing (jumping straight to
  it, switching pages to match; issue #1158 replaced the field letters) and `[S]ave`/`[B]ack` stay reachable from
  every page. An unsectioned screen has no natural page boundary and keeps
  today's behavior unchanged if it doesn't fit. Profile (14 fields across 4
  sections, plus a bio-preview/transport-diagnostic preamble) is the first
  real screen dense enough to exercise pagination in practice.

These shipped improvements preserve Phase 4's security dependencies and
public-readiness gate. Its implemented foundation is:

- formal threat model from issue #55 — specified in §12;
- persisted local trust inputs, projections, probation, and policy evaluation
  (issue #126) — implemented: separate reporters/domains, node/user
  subjects, dimension-scoped evidence and vouches, transactional state/audit,
  startup reconciliation, recovery hold, and bounded inactive retention;
- signed trust-signal/vouch subscriptions and evidence verification (issue
  #127) — implemented with explicit bounded pulls, immutable carrier storage,
  issuer verification, replay/freshness checks, and verified digest evidence;
- enforcement across Link transport, sync, relay, content, and users (issue
  #128) — implemented at pre-persistence admission, outbound selection, and
  read-time materialization/display boundaries with stable public reason codes;
- SysOp explanation, overrides, and recovery workflows (issue #129) —
  implemented in the shared SysOp System menu: configuration, effective-state
  explanations, mandatory-reason overrides, recovery, audit history, and
  visibly flagged/confirmed category-scoped sole-authority exceptions;
- remote attestation authority and local acceptance policy (issue #130) —
  implemented with per-attribute opt-in, signed/revocable records, separate
  authority scopes, fail-closed local projections, resource gates, and
  reasoned SysOp explanation/override workflows;
- adversarial distributed validation and the public-readiness gate (issue
  #131) — automated §12.10 evidence is tracked in
  `docs/NetBBS-phase4-readiness.md`; real-node manual recovery,
  independently administered multi-node validation, and sustained private
  dogfood remain pending, so the issue and public-readiness gate remain open.

No public/untrusted federation claim precedes this phase.

### Phase 5 — Real-time Link chat

- Implemented: authenticated Noise transport and live linked-channel chat
  (§8.10, #148), node-wide presence (#164), bounded trusted scrollback-on-join
  (#194), live private messages and relay (#168/#219), and chained relays plus
  cross-node `/private` (#270).
- Cross-node `/dm` invitation chats: built (§16, issue #1368).
- Deliberately deferred: simultaneous channel memberships with background
  delivery; the existing durable unread model remains available.

### Phase 6 — Advanced Link governance and Link Communities

- Link Communities and signed Community membership/carry changes (§16, issue
  #1308) -- built (#1310, v7.27.0);
- delegated Link moderators for linked boards and channels, channel mute/ban
  and topic across the Link, and linked-channel origin succession (§16, issue
  #1357) -- built (#1359: #1360, #1361, #1363, #1364, #1366 and its last
  slice, trust and history);
- invite-only linked-channel membership governance;
- advanced creation, closure, and lifecycle surfaces;
- curated governance audit board and live activity feed.

### Phase 7 — Doors and legacy compatibility

Implemented (issue #172, closed — supersedes #63 and #167, both closed;
full design record on those two issues' own comment threads, summarized
here since #172 is self-contained):

- subprocess isolation under the same OS user as the main NetBBS process
  — no dedicated door-runner user, no privilege-drop helper, no root
  requirement (deliberately chosen: operational frictionlessness for a
  SysOp outweighs the defense-in-depth a dedicated-user model would buy);
  `resource.setrlimit()` (CPU, memory, process count) set before exec,
  plus an async wall-time watchdog and unconditional reap via the owning
  async task on every exit path (crash/timeout/normal exit/disconnect),
  matching this codebase's standing "creator cancels, gathers, retrieves
  failures" convention;
- a versioned, deliberately minimal v1 API, drop-file-shaped rather than
  a live protocol: static session metadata (handle, stable numeric user
  ID, terminal width/height, color-depth capability, node name) written
  before spawn; stdio is pure raw passthrough for the session's
  duration, with no framing or control messages interleaved; exit code
  is the only completion signal;
- terminal size is followed for the duration of a run (issue #468),
  without interleaving anything into that passthrough stream. A PTY
  door's own terminal is resized and its group signalled with
  `SIGWINCH`, exactly as any full-screen program already expects. That
  signal may arrive more than once per resize (issue #586): the kernel's
  own delivery to the terminal's foreground group and NetBBS's explicit
  one to the process group, kept because a door which never made the PTY
  its controlling terminal hears only the second. Detecting which case
  applies is racy, and a spurious extra `SIGWINCH` is something every
  full-screen program already tolerates from a real terminal, so the
  documented contract is an idempotent handler, not exactly one signal. A
  stdio or socket door is notified only if its profile opts in, by
  republishing the same static metadata file with the new geometry and
  signalling the door leader with `SIGUSR1`; opt-in because that
  signal's default action terminates a process which does not handle
  it. The one exception is NetBBS's own doors (issue #645): the bundled
  catalogue records which of them handle the signal, and the runtime
  signals such a door without any opt-in when the script a registration
  launches resolves to this install's own copy, or names it as a module.
  Host and door ship in one wheel, so the handler is in the script; a
  copy of the script elsewhere may be any version and follows the profile
  like anybody's door. A handler being in the script is not the same as
  its being installed: that takes an interpreter start and the compiling
  of a ten-thousand-line file, most of a second on a small host. So the
  node ignores `SIGUSR1` in its own process before it spawns such a door.
  An ignored disposition is inherited across `fork` and `exec`, which
  closes the window for the launcher and the door alike; the door
  replaces it when it installs its handler, and a wrapper that does not
  `exec`, or an older copy run as a module, merely never hears it. This was chosen over setting
  the profile flag from the door gallery because a bundled door is
  usually registered with no profile at all, and because it needs no
  migration for doors already registered on running nodes. The doors'
  handlers only set a flag, which an action bar notices within a quarter
  of a second. A profile which pins width/height, a DOS door and a remote
  service are each left alone. This supersedes v1's original "no live
  terminal-resize propagation" rule, which matched every classic door's
  static 80x24-era assumption; the metadata itself stays a file rather
  than becoming a live protocol;
- a door may declare **one** long-lived companion service (issue #466), for
  a game whose world must keep running while nobody is connected. This is
  the single exception to "a door is one process per caller", and is
  deliberately not a general process manager: one service per door, no
  inter-session channel inside NetBBS (the service's own socket is the
  channel), and no privilege separation beyond what native doors already
  have. It starts with the node or on the first caller, under the service
  account, in the door's installation directory, with the same narrow
  environment rules as a door launch. Its own memory ceiling applies and no
  CPU-seconds ceiling does, because a long-lived process legitimately
  accumulates CPU time. Exits restart with lengthening backoff behind a
  circuit breaker, after which NetBBS reports the door's service as failed
  rather than respawning a misconfigured program indefinitely. A caller is
  admitted only while the service is up, has been up long enough to mean it,
  and passes its optional health check; otherwise they get one line and the
  door list back. Stopping is bounded at every level — SIGTERM, the
  configured grace, SIGKILL, then a deadline after which an unkillable
  process is abandoned rather than delaying node shutdown — and services
  stop before listeners and background tasks. NetBBS supervises the process
  only; installing it stays the operator's, exactly as for the door itself.
  A door's installation directory is outside the node's own state and is not
  backed up by default, because it is operator-owned and unbounded in size; a
  node-level setting includes every door's installation in each backup for an
  operator who wants one artifact holding everything. Capture only — restore
  never writes such a directory back, since putting a game installation back
  over a live one is a deliberate operator action, not part of restoring node
  state;
- door output (stdout) is trusted and relayed unmodified, like a SysOp's
  own welcome-banner file, not run through the chat/post sanitizer —
  NetBBS provides the interface and best-effort abuse prevention within
  its own infrastructure, but the SysOp who chooses to run a given door
  is the one vouching for it, the same posture Phase 4 identity
  attestation already takes;
- SysOp registration (path, name, description) and attachment to a
  board/community, reusing the existing file-area-style permission-level
  gating; caller-facing launch and interactive play across Telnet, SSH,
  and web; door-session start/end audit-logged through the existing
  moderation/audit-log mechanism — no new subsystem;
- three real bundled doors ship as installed package data
  (`netbbs.doors.bundled`, not loose example files), proving the
  pipeline end to end: Retro Trivia, a deliberately stateless one-off
  round that is the floor a door must clear; War Dialer, a persistent
  competitive world shared by a node's callers; and Voidrunner, a
  persistent Elite/Trade-Wars-style space-trading and exploration game
  that grew substantially past its own proof-of-concept scope across
  several post-launch feature and hardening rounds (economy, missions,
  combat, a futures exchange, crew, faction reputation, notoriety/patrol
  encounters, ship progression, retirement/New Game+, and a systemic
  fix for a box-alignment overflow bug that turned out to affect nearly
  every screen in its tactical HUD — v5.4.0 release notes carry the full
  list, not repeated here).

**Voidrunner is vendored, and its design lives with it.** The game is
developed in its own repository, Thiesi/Voidrunner, and its rules, careers,
saves, combat, economy and presentation contract are specified there, in
`docs/design.md` (issues #1214, #1216). NetBBS ships one tagged release of its
single file, unchanged: `scripts/vendor_door.py voidrunner <tag>` fetches it and
records its origin in `src/netbbs/doors/bundled/VENDORED.json`, and the test
suite refuses a copy that differs from that record. A fix to the game goes
upstream and arrives with the next vendored release.

**Retro Trivia and War Dialer are vendored the same way** from
Thiesi/RetroTrivia and Thiesi/WarDialer (issues #1226, #1228), recorded in the
same `VENDORED.json`. NetBBS imports nothing from Retro Trivia and keeps only
the tests of how it launches and sizes the door. War Dialer's world paths,
ownership, node backup and SysOp controls stay NetBBS's (§16, issue #200).

**Lunar Lander is vendored the same way** from Thiesi/LunarLander (issue
#1542). NetBBS owns where its pilot records live: a bundled launch sets
`DOOR_LL_DATA_DIR` to `<db path>.doors/lunar_lander/`, because the door's own
default is `data/` beside the script, inside the installed package. A guest
call gets a copy (§4.6), and the backup carries the records (§13.4).

NetBBS owns how it hosts Voidrunner:
- **Launch.** It runs as a native door that reads `door_info.json`.
- **Save directory.** NetBBS resolves it, as below.
- **Resize.** NetBBS rewrites the drop file and signals the door.
- **Guest calls.** A guest call gets a sandboxed save directory (§4.6).
- **Backups.** Capture and restore run under the door's own maintenance gate
  (§13.4).

The backup imports three names from the door: `_default_save_dir`, `PilotBusy`
and `maintenance_session`. A release that renamed them would break NetBBS's
backups rather than the game, so NetBBS's suite pins them.

**Voidrunner and War Dialer require a terminal of at least 40 columns by 12
rows, and have exactly one layout above it (issue #495).** A launch on anything smaller is
refused, by name and with the size the terminal reported, before any save or
world is opened. The doors used to carry a second, stripped presentation for
terminals down to 20 columns, and that is what flattened them: art, gauges and
rules were dropped so content would fit a caller nobody dials in from, and the
stripped result was then what an 80-column caller saw as well. A narrow case may
never set the ceiling again -- which is why there is no second layout to leak
upward, and why a screen that will not fit 40 columns is redesigned rather than
given a compact variant. Nothing in the host has ever supported below 40 either:
the prose editor already clamps to 40, and the menu layout's only thresholds are
72 and 120 for *wide* screens.

**Lunar Lander is the stated exception: its floor is 80x24 (issue #1542).** A
flight view with its instrument panel does not survive forty columns, so the
door pauses the simulation below 80x24 and asks for a larger terminal rather
than drawing a reduced view. The rule's substance holds: one layout, no narrow
variant. Its gallery is drawn at 80x24 and 132x50.

Door presentation is reviewed by looking at rendered screens. `scripts/
door_gallery.py <door>` drives every screen in a subprocess at 80, 64 and 40
columns in each display preset and writes an HTML page through the website's own
ANSI emulator; a change to a screen comes with that page. Voidrunner's screens
change in its own repository and are reviewed with its gallery there; NetBBS's
gallery keeps its walks for the website's captures and for checking a vendored
release. The test suite can only
assert that a screen fits -- never that it looks like anything -- which is how an
entire visual design was lost with every slice passing review.

The resolved save directory is the installation namespace, and it belongs to
one node: `<db path>.doors/voidrunner/`, beside War Dialer's world, unless the
SysOp sets `VOIDRUNNER_SAVE_DIR`. The node resolves it and passes it to every
launch as an absolute path; the door's own `~/.netbbs/voidrunner_saves` fallback
is for standalone play only. That home-directory path was the node default until
issue #648, and it made every node one OS account ran share careers by user id.
On its first start after the change, a node with no override whose own record
names the legacy directory copies it into its own under the legacy directory's
maintenance gate, staged and renamed into place whole. The record is the evidence
the careers are this node's: a brand-new node under the same account has none, and
never adopts careers another node left there, since user ids do not transfer. A
target that already holds careers is never overwritten. It copies rather than moves, because a second node
under the same account may still be reading it; each node then owns its copy. A
busy or unreadable legacy directory is left alone and the node keeps using it
until a later start copies it. Changing a node's display name does not change
career identity. See the door guide for the upgrade and override steps.

Compatibility extension (issues #296/#297):

- A nullable, versioned profile preserves the original stdio API for existing
  registrations: no drop files, no adapter, raw UTF-8 through stdin/stdout.
  SysOps can explicitly remove a profile in the draft editor without recreating
  the registration; executable/argv and game data are retained. Native socket
  profiles require DOOR32 descriptor metadata.
- The launch metadata file (`door_info.json`) is itself versioned, by a
  `door_api` integer, and grows additively (issue #469): a reader treats any
  absent field as unknown, and a door may refuse a version it does not
  understand rather than probing for fields. A field may also be added
  within a version when ignoring it costs a door nothing, as
  `datetime_format` (the node's display format, issue #1545) and
  `charset` (the character set the caller's terminal shows, issue #1557)
  and `quadrants` (whether it shows quarter blocks, issue #1560)
  were in `door_api` 5: a bump would make every door that refuses an unknown
  version stop launching. Removing a profile restores the
  stdio API but does not pin the metadata to an older version — the file is a
  property of the platform, not of the profile. It never carries a credential,
  an email address, a user level or a network address.
- Which door a caller is in is presence, not catalogue data, and is scoped to
  the viewer (issue #470). It is held per *session*, not per account: both Who
  screens render a row per session, and the SysOp one acts on the row
  selected, so an idle connection must never claim the door its sibling is
  playing. A caller-facing screen names a door only when that viewer could
  currently open it — the same `min_play_level` gate the door picker applies —
  so Who can never advertise a door a caller is not allowed to see. A door
  deleted since, or one whose registration no longer matches the activity
  recorded, is omitted rather than named; door ids are reusable, so identity
  is checked, not just the number. A SysOp screen names everything, being
  SysOp-only already. Remote presence carries no door: a linked node reports
  who is online, not what they are doing.
- A door may post to boards and speak in chat channels a SysOp allowlists for
  it (issue #520, the outbound half of #470), and to nothing else. It posts under a **label**, not
  an account: `author_user_id` and `author_fingerprint` are NULL and
  `author_label` alone carries the identity, which is the shape a Link-carried
  post has always had. An account was rejected as the answer -- `users`
  requires at least one credential by CHECK constraint, so a credential-less
  service row could not exist without weakening it -- and with no account
  there is nothing to exclude from login, listings, mail or moderation, and
  the infrastructure level band sketched under issue #63 is not needed for
  this. The label ends in a reserved suffix, is unique across accounts and
  doors, and is checked at the moment the hook is switched on: it is shaped
  like a handle because it federates as `local_user_id`, and chat resolves a
  stored author by username where boards resolve by id, so a collision would
  let a door speak in a real account's presentation. The SysOp's allowlist is
  the only gate on who may *post* on this path; level is deliberately not a
  second one, because two gates can disagree where only one is visible to the
  SysOp. Who may *read* what a door sends is a separate question, with one
  answer (issue #1401): a door posts and chats only while every caller may
  play it, `min_play_level` 0, the only gate a door has. A gated door would
  otherwise announce itself, and name its players, to callers who cannot see
  it -- the bot announcement rule (§16, "Issue #1208 — chat bots") applied to
  doors, as door events also apply it. Allowing a board or a channel is
  refused while the door is gated, and every request is refused again at
  publication, with the level read fresh, because a SysOp can raise it after
  allowing targets; the allowlist is kept, so lowering the level again is all
  it takes, and the door's `[O]utbound` screen says why nothing it sends
  reaches anyone. Transport is
  a file drop rather than a socket, chosen so a DOS door can use it at all,
  and a refusal is always returned to the door and never queued -- a held post
  would publish after the allowlist was revoked. Posting is rate-limited per
  door and audit-logged against the SysOp whose authority it runs on; if that
  account is deleted the hook lapses rather than posting unattributably, and
  another SysOp vouches for it without disturbing its identity or allowlist.
  An outcome is written where the door can still read it after the run: the
  working directory is gone by then, so a result left there would make the
  promise that a refusal is always visible untrue in practice. Those receipts
  are node state and travel with the node (§13.4, issue #556): a backup
  captures them before its database snapshot, and a restore pairs the receipts
  it carries with the database generation they describe. They remain a bounded,
  best-effort record rather than a publication ledger, so a door may not read
  exactly-once delivery out of them. Processing is at-most-once, though: a
  drain claims each request by renaming it out of the request pattern before
  reading it, so no later drain can see it again even if its removal fails --
  the property that makes draining during a session safe. A running door's
  requests are picked up every two seconds, a few at a time, and once more at
  exit; only the drain at exit refuses what is left over. A SysOp's test
  launch is drained too, but answered with a `rehearsal` verdict: nothing is
  posted, debited or audit-logged. A chat line (slice 2) is one line,
  control characters stripped, `kind="message"` only, recorded exactly as a
  caller's line is -- scrollback, search, Link queue -- and delivered live by
  the session that launched the door, since only it holds the chat hub. It
  has its own hourly ceiling (30 by default) so neither budget spends the
  other, and is not audit-logged per line: lines scroll away and a busy door
  would bury the moderation log, while the label names the speaker. Three
  rules keep it civil. An MRC-bridged channel is never a target, because the
  bridge would present the door to the hub as a local caller. A Linked
  channel needs the SysOp's explicit confirmation, because chat has no
  retraction and the line reaches every peer. And a channel's moderators can
  `/mute` a door there, timed like a caller's mute, by suspending its
  allowlist entry -- a door has no account for `channel_restrictions` to name.
  A door's line renders muted and marked; peers key on the reserved `.door`
  suffix, so a newer peer styles a door's line too and an older one shows it
  plain. Reads of any
  kind remain out of scope, and the hook is available to locally-launched
  doors only -- a remote registration shares no filesystem, and a DOS guest
  cannot read the launch metadata that names the drop directory.
  Profiles add persistent installation directories,
  disposable node directories, exact CRLF classic drop files, native stdio,
  controlling PTYs, private inherited DOOR32 sockets, DOSBox-X COM1 sockets,
  and allowlisted outbound RLogin services. Never pass the caller's socket.
- Door API 5 adds **door events** (issue #1378, implemented in #1408):
  through the same file drop, a door whose SysOp switched events on (a
  per-door switch on its `[O]utbound` screen, off by default, with its own
  hourly ceiling of 12 by default, advertised as `outbound.events`) reports
  one of five fixed event types -- `rank`, `achievement`, `medal`,
  `capture`, `perfect` -- with the subject's `user_id` (a `medal` event
  names up to three, gold first, as `user_ids`) and a title of at most 80
  characters, and may write a standings snapshot of at most 10 rows, each
  replacing the last. A request is refused, with a receipt, while events are
  off or the door is gated; one naming only callers who may not be named is
  answered `dropped`, neither stored nor debited. NetBBS keeps the events in a bounded
  journal and offers them to bots granted that door as a third announcement
  source beside boards and file areas; the host composes the chat text and
  serves the snapshot as the `top` node query. A door's events are announced
  only while its `min_play_level` is 0, checked at the grant and again at
  publication, and only for subjects whose profile switch "Game results
  announced in chat" (on by default) allows it; off also hides them from
  `!top`. Guests and rehearsals emit nothing, and a door emits only what was
  earned live, never catch-up unlocks. Door API 4 chat lines stay as a door's
  own narration. See §16, "Issue #1378 — door events as a bot announcement
  source".
- A door's stream is transcoded to and from the caller's character set (§3.2):
  a CP437 door reaches a CP437 terminal unchanged, and is transcoded for UTF-8
  and ASCII ones; a UTF-8 door is mapped for CP437 and ASCII. Browser door
  mode uses bounded base64 output frames and stream-scoped raw key events;
  stale door input cannot become menu actions. Resize stays out of band;
  fixed geometry is restored to browser-fit geometry on exit. This does not
  add browser Zmodem support. Explicit raw profiles are native-terminal only.
- Each POSIX launch owns a process group. An internal, freshly exec'd Python
  helper applies limits and the optional controlling terminal, then execs the
  operator-selected argv; no threaded-process `preexec_fn`. Cancellation
  during spawn or cleanup retains ownership. Descendants are terminated on
  every exit path, even when their leader has already exited. Leader exit
  does not discard buffered terminal output: it drains to EOF while still
  respecting the session watchdog, caller disconnect and shutdown.
- Legacy installations default to one active session. Cross-process advisory
  node leases are keyed by resolved installation directory; higher limits
  require explicit SysOp certification of the game's locking. Persistent
  scores are owned by the door, not a new NetBBS database capability.
- DOSBox-X is external software. NetBBS generates narrow C: installation and
  D: node mounts, headless configuration, transparent inherited COM1 and
  secure mode after mounting. Optional FOSSIL software is operator supplied.
  Runtime diagnostics are bounded and SysOp-only; emulator stderr is never
  relayed to callers. DOS program status is checked separately from emulator
  status, including LORD's normal return code 255.
- The VM adapter (issue #474) runs a foreign-platform native door in a
  per-caller qemu guest the SysOp builds. NetBBS owns the command line: no
  network, display or host devices, one virtio console on the door's
  socketpair, and 9p exports of exactly the installation and node
  directories. The guest reports its door's status through `exit.status`;
  a graceful stop is an ACPI power-button press over a private QMP
  socketpair before the process group is signalled.
- RLogin requires a fixed allowlist and caller-visible service identity.
  Loopback SSH/TLS tunnels are the default; direct plaintext requires an
  explicit insecure-operation acknowledgement. Provider identity templates
  may read a private operator-created credential file. No caller chooses a
  destination and no privileged source port is requested.
- A provider with its own protocol gets its own adapter, never a creatively
  filled RLogin template (issue #565). BBSLink is the first: an HTTP token
  and authorisation step, then Telnet, both plaintext with no tunnel route,
  so every non-loopback destination needs the insecure acknowledgement.
  Both fixed destinations must be allowlisted, the service name is shown as
  for RLogin, and the three provider codes live only in a private
  operator-created file. The caller's user number and the door code are the
  only caller-related values sent. The provider binds the Telnet session to
  the authorisation by source address, so a node pins one resolved address
  per launch and serialises handshakes per provider host.
- A SysOp draft editor provides packaged setup templates, JSON import,
  preflight and an explicit test launch. Back saves nothing; Test may modify
  the game's persistent data. No game, driver, emulator, tunnel or host
  package is automatically downloaded or installed.

The bounded verification matrix and exact manual setup instructions live in
[the door guide](../NetBBS-door-guide.md). A template is not a certification of
arbitrary game versions, platforms, or multiplayer operation. Same-user
native code can read the service account's files, including keys and database;
resource limits are not filesystem/network isolation. DOS mounts improve the
practical boundary but cannot protect against emulator vulnerabilities. A VM
door is isolated from the host except for its two exports, but that boundary
is the SysOp's qemu build and guest image, not something NetBBS certifies.
Optional external runner argv is operator-managed and does not imply a
privileged or automatically provisioned containment environment.

Still outside the supported scope:

- Wine/Win32, Linux ELF emulation on NetBSD, DOSEMU, other BBSes' private
  scripting APIs, graphical/VGA scraping, RIP, sound or IPX forwarding;
- universal legacy compatibility, untrusted native binaries or caller uploads;
- any door capability beyond the raw-terminal-I/O metadata handshake
  above — deliberately minimal by design, add only what a real door
  actually needs;
- UI-only conversation/message-threading refinements — an item from
  this phase's original planning stub, never folded into #172's own
  scope and not otherwise picked up; still open, unscoped.

`RLIMIT_CPU`/`RLIMIT_AS`/`RLIMIT_NPROC` are set by the internal launcher
before the real program's exec (`src/netbbs/doors/launcher.py`), matching the locked design's
CPU/memory/process-count ceilings; the wall-time watchdog, crash
reporting, and cleanup-on-disconnect each have a real regression test
(`tests/test_doors_runtime.py`). Not yet independently verified: a
dedicated adversarial test that actually exercises a door hitting the
CPU/memory/process-count ceilings themselves (as opposed to the
watchdog's own wall-time path) — those three `setrlimit` calls are
implemented but currently rely on the OS enforcing them correctly, not
on a test proving it. What the CPU ceiling does when reached was observed
directly on NetBSD (issues #509 and #585, not through a test): the launcher
sets the soft and hard `RLIMIT_CPU` equal, so the kernel delivers `SIGKILL`
with no preceding `SIGXCPU`, and a door cannot checkpoint at the ceiling.
That is the documented contract (door guide, "CPU seconds"), chosen over a
soft-below-hard split: an unprivileged process cannot raise the hard limit
above the one it inherited, so the headroom may not exist at all under a
login class that pins `cputime`; the kernel re-sends `SIGXCPU` about once a
second after the soft limit, which a door handling it badly spins on; and a
split either moves the effective ceiling above what the SysOp typed or
quietly shortens every existing profile. `0` is the supported answer for a
door which should not be cut that way, with the limit `0` has: it raises
the door to the hard limit the service inherited, so under a login class or
unit file which pins `cputime` the door is still killed, without warning,
at that inherited value. Making a door truly uncuttable means removing the
service's own hard limit as well, which is a host decision outside NetBBS.
This design explicitly does not
attempt filesystem/network isolation regardless.

# NetBBS — developer project notes

NetBBS is a modern, TCP/IP-native BBS with an ad-hoc mesh network
(**NetBBS Link**). Phases 1–3 are complete. Phase 4 (trust, reputation, and
public-readiness) is implemented; its public-readiness gate (issue #131)
stays open pending human/operational validation, not further code-level
work. Phase 5 (real-time Link chat) is active: Noise XX transport
authentication and the first real-time linked-channel chat vertical
(issue #148) are shipped, released as v5.0.0. Node-wide presence and
per-message trust-filtered scrollback (issue #164) shipped in v5.3.0.

## Start here

Read these in order before substantial work:

1. `docs/NetBBS-design-doc.md` — current product and architecture decisions.
2. Current GitHub issues — active work, dependencies, and acceptance criteria.
3. `docs/NetBBS-worklog.md` — curated engineering invariants, implementation
   traps, operational constraints, and durable lessons.
4. Relevant source, tests, and migrations.

Git history is the archive for old round-by-round implementation narratives.
Do not recreate that chronology in the worklog.

## Documentation policy

### Design document

Record a decision in the design document when it changes what the system means,
what users or nodes may rely on, or how a future protocol must behave. Keep one
current normative answer per topic. Explain important rationale and rejected
alternatives, but replace superseded wording rather than piling corrections
under it.

### Engineering record

Update `docs/NetBBS-worklog.md` only for durable engineering knowledge:

- non-obvious invariants;
- platform/protocol/SQLite behavior which constrains future changes;
- migration or compatibility requirements;
- current subsystem implementation boundaries;
- unresolved operational or verification limitations;
- testing methods needed to prove a class of behavior.

Do not append:

- passing-test totals;
- round or commit narratives;
- exhaustive changed-file/test lists;
- transient “next step” status;
- debugging transcripts once the lesson is extracted;
- closed bugs with no lasting implementation consequence.

### Issues and commits

Use GitHub issues for outstanding work and acceptance criteria. Use commit and
PR descriptions for the implementation narrative of the change being made.

## Development direction

NetBBS should leave proof-of-concept territory before the entire roadmap is
finished. Do not treat protocol, background, and architectural work as a queue
which must be exhausted before user-facing work resumes.

Maintain an intentional cadence between two tracks:

- **Foundation track:** complete bounded protocol, persistence, reliability,
  security, and operational slices with explicit invariants and realistic tests.
- **Product track:** turn mature foundations into complete, discoverable, and
  pleasant caller/SysOp experiences; repair usability problems exposed by real
  use; and make already-built capability visible through the interface.

After a substantial backend slice, ask what a caller or SysOp can now actually
do, whether the capability is discoverable, and whether its success and failure
states make sense to a human. Conversely, user-visible work must build toward
the product rather than becoming an unbounded cosmetic pass over unstable
surfaces.

Prefer coherent vertical increments which include enough implementation, UI,
validation, and failure handling to feel like product capability. It is valid to
polish a mature surface—such as chat—even while later roadmap foundations remain
unfinished. Broad visual redesign and theming may remain deferred, but broken,
ambiguous, inaccessible, or needlessly hostile interactions are current product
work, not post-roadmap luxuries.

The standing principle is:

> Alternate meaningful foundation work with complete, user-visible slices, and
> periodically polish mature surfaces enough that NetBBS becomes a usable,
> lived-in product long before its ultimate roadmap is complete.

## Working conventions

- **Design before code for non-trivial choices.** Check existing decisions
  before reopening them. Ask when a change would create a new product,
  protocol, security, or long-lived UX decision.
- **Preserve subsystem boundaries.** Domain functions remain synchronous and
  `db`-first. Async network/UI flows dispatch through `DatabaseLane` where
  required. Rendering, storage, protocol, and transport concerns stay
  separated.
- **Treat migrations as immutable.** Never edit a shipped migration. Test
  migrations against realistic related data, especially before rebuilding a
  table which is a foreign-key parent.
- **Actually run tests.** Prefer regression tests which demonstrably fail
  without the fix. Confirm scripted UI tests still reach the path their name
  claims after signature/menu changes.
- **Use real boundaries.** Real SQLite files/connections for transactions,
  real loopback sockets for transports, serialization and restart for Link
  state, and the deterministic multi-node harness for ordering/partition
  behavior.
- **Bound remotely influenced resources.** Queues, transfers, retained events,
  retries, and mailboxes need explicit limits and visible failure behavior.
- **Own async tasks.** The creator cancels, gathers, and retrieves failures on
  every exit path. Cleanup failures must not mask the original error.
- **Sanitize before styling.** Sanitize untrusted segments before adding ANSI;
  never sanitize a completed trusted ANSI string. Compose nested colored
  segments independently because SGR reset does not restore an outer color.
- **Wrap every terminal-facing text.** Ordinary lines go through
  `Session.write_line`; interactive prompts go through `write_prompt`. Both
  wrap by display columns at the negotiated terminal width, remove whitespace
  at wrap boundaries, and retain all text instead of relying on terminal
  soft-wrap or clipping. Screen-specific renderers should wrap sanitized plain
  text with `wrap_to_width` before styling where practical, and must not use
  `break_long_words=False` for terminal output. Trusted, deliberately
  preformatted ANSI art should use `write_preformatted_line` to retain authored
  rows that fit; even it must wrap an over-width row rather than overflow.
  Width measurement must normalize tabs and account for cursor-positioning
  controls, not treat every ANSI escape as zero-width. Absolute positions reset
  column accounting, save/restore and bare carriage returns update it, and
  numeric cursor spans are capped without per-column expansion.
  CLI prose uses `print_wrapped`; errors must measure stderr when stderr receives them.
  Self-contained bundled doors enforce the same boundary in their local
  `out_line`/`out_prompt` helpers.
- **Fail clearly.** Administrative lockout, identity ambiguity, incompatible
  databases, protocol rejection, and resource exhaustion should not degrade
  silently.
- **Scale safeguards to the operator model.** The managed netbbs.org DNS
  service is a single instance run by the project, its occupancy is small,
  and a stuck registration row is repaired by hand. Crash consistency
  between two writes, sweep-timing races, cooldown-expiry races, and
  capacity-cap races in its rename and recovery paths are not defects
  unless a SysOp can trigger them from the UI; prefer a clear failure plus
  manual recovery over another journal or lock.

## Environment

- Primary target: NetBSD. NetBBS itself is distributed through GitHub only;
  prefer external dependencies available through pkgsrc.
- Python 3.11+, asyncio.
- SQLite in WAL mode.
- PyNaCl/libsodium for core cryptography; the optional SSH extra's
  `cryptography` source build requires Rust on NetBSD.
- User transports: Telnet, SSH, web/xterm.js.
- NetBBS Link transport: signed HTTP+JSON for asynchronous federation;
  Noise XX authenticates the real-time Link chat transport (Phase 5).

## Current scope summary

The local BBS includes boards, files, chat, mail, Communities, permissions,
moderation, identity attestation, SysOp tools, ANSI/TUI editors, registration,
and update infrastructure.

Phase 3 (Link connectivity and asynchronous services) is complete: node-key
lifecycle, canonical event bytes, hello/endpoint protocol, configured-seed
sync and peer exchange, persistent peer/event state with restart
reconstruction, foreground/background database lanes, linked boards/
channels/file areas/mail with genesis/materialization/origin-succession
where applicable, tier-1 Link messages, authenticated inventory/pull catch-up
across every content kind, and WAN reachability (reliability scoring, relay
consent/selection, bounded relay mailboxes).

Phase 4 (local trust/reputation policy, signed trust-signal/vouch
subscriptions, enforcement across every Link boundary, SysOp
explanation/override/recovery workflows, and remote age/name attestation)
is implemented. Its public-readiness gate (issue #131) stays open: the
automated adversarial-validation evidence is complete, but a manual
recovery exercise, independently administered multi-node validation, and
continued sustained dogfood (issue #83) remain pending — human/operational
work, not further code-level validation.

Phase 5 (real-time Link chat) is active. Shipped: Noise XX transport
authentication and the first real-time linked-channel chat vertical (issue
#148) — direct sessions and one linked channel, live; node-wide presence and
per-message trust-filtered scrollback (issue #164, v5.3.0); trusted
scrollback-on-join (issue #194) — a freshly-subscribing peer gets a bounded,
ephemeral catch-up snapshot of the origin's own recent scrollback alongside
the existing presence snapshot. Not yet built: multiple simultaneous channel
memberships with background/unread delivery (investigated and deliberately
deferred — no observable benefit over the existing durable unread-count
model). Live relay for two mutually-unreachable nodes and Link-wide live
direct messages (`/msg user@node-fingerprint`, issue #168) shipped with the
reliable-nodes onboarding (issue #219): a raw-socket proxy below Noise at any
full peer with relay serving on, anchored at the reliable nodes; chained
two-relay bridges plus anchor advertisement and cross-node `/private`
followed (issue #270). Still not built: cross-node `/dm` invites. The MRC
gateway scoped in issue #165 shipped as issue #275: `netbbs.mrc`, an
opt-in per-channel bridge to the external Multi Relay Chat network, with
DB-backed SysOp configuration and no change to Link's own protocol.

Phase 7's first vertical (issue #172, closed — supersedes #63/#167, both
closed) shipped in v5.4.0: a native door-game execution model (subprocess
isolation under the same OS user, `resource.setrlimit` CPU/memory/
process-count ceilings, an async wall-time watchdog, and unconditional
reap on every exit path) behind a deliberately minimal, drop-file-shaped
v1 API, plus two real bundled doors proving the pipeline end to end —
Retro Trivia and Voidrunner, the latter a full persistent space-trading
game after several post-launch expansion and hardening rounds. DOSBox/
legacy-DOS-door compatibility and multiplayer/persistent-state doors are
explicitly deferred, not part of this vertical.

It does **not** yet imply public federation, Phase 4's public-readiness gate
being closed, Phase 5 complete beyond its first vertical, Phase 6 (advanced
governance/Link Communities) work, or DOS-door/legacy compatibility within
Phase 7. Check the design document and open issues for the current roadmap
rather than extending this summary.

<!-- moradin-forge:start -->
## Moradin's Forge

- Local sidecar: `.moradins-harness/`
- Agent entrypoint: `.moradins-harness/FORGE.md`
- Harness entrypoint: `.moradins-harness/Harness/entrypoints/forge.md`
- Keep Moradin local unless the user explicitly requests external tooling.
- Treat host tool installation as request-only: write install requests, do not run installs.
- Preserve existing repo workflows and prefer repo-local deterministic commands.
<!-- moradin-forge:end -->

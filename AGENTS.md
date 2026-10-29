# NetBBS — developer project notes

NetBBS is a modern, TCP/IP-native BBS with an ad-hoc mesh network
(**NetBBS Link**). It is feature complete for now (see the freeze notice under
"Development direction"). NetBBS Link remains private, experimental
federation: public readiness waits on issue #131's human/operational checks
and sustained dogfood (#83), and independent implementation compatibility is
unclaimed (#71). The current status and roadmap live in
`docs/NetBBS-design-doc.md`; this file does not track them.

## Start here

Read these in order before substantial work:

1. `docs/NetBBS-design-doc.md` — current product and architecture decisions:
   the current status and a map of every section. Follow its links into
   `docs/design/` for the areas your work touches.
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

The design document is split by area (issue #1394):

- `docs/NetBBS-design-doc.md` is the entry point. It holds the current status
  and the section map, and nothing normative of its own.
- Each top-level section lives in `docs/design/NN-<area>.md`, where `NN` is
  its section number. Section numbers and headings never change when text
  moves; a new subsection takes the next free number in its section, and
  gets a line in the section map.
- Each §16 decision lives in its own file, `docs/design/decisions/NNNN-<slug>.md`,
  named after its issue (zero-padded), with a row in
  `docs/design/16-decisions.md`. A decision that has no issue of its own is
  named by its slug alone, `docs/design/decisions/<slug>.md`, and its row
  shows no issue number. A scoping decision adds a new file and a row;
  it never grows another decision's file. Write the resulting rule into the
  area section it changes as well: the area text is the normative answer, and
  the decision file keeps the reasoning and the rejected alternatives.

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

> [!IMPORTANT]
> **Feature freeze since v7.40.0 (2026-10-24, #612 step 9).** NetBBS is
> feature complete for now. Until NetBBS Link opens publicly (#1384), work is
> bug fixes, usability, reliability, documentation, and the Link readiness work
> (#131, #83). New features, settings, and protocol surface wait unless the
> maintainer approves them on #612.

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
- **Review door presentation by looking at it.** A change to a bundled door's
  screens comes with pictures attached to the PR. Tests can only assert that a
  screen fits its terminal; they cannot fail because it is ugly, and both games
  lost their visual design to slices that all passed. All four bundled games
  are vendored from their own repositories -- Thiesi/Voidrunner,
  Thiesi/RetroTrivia, Thiesi/WarDialer and Thiesi/LunarLander (issues #1216,
  #1226, #1228, #1542): change their screens there, reviewed with that
  repository's gallery, and never edit `src/netbbs/doors/bundled/voidrunner.py`,
  `retro_trivia.py`, `war_dialer.py` or `lunar_lander.py` here -- bump them with
  `scripts/vendor_door.py <door> <tag>`.
  `scripts/door_gallery.py <door>` renders every screen of the vendored War
  Dialer, Voidrunner and Lunar Lander, for website captures and bump checks, at
  every supported size and preset (Lunar Lander at 80x24 and 132x50, since it
  needs 80x24, and Voidrunner at 132x50 too, where its console widens; `!` photographs a real-time screen that never goes quiet); it works from a cached career, so a door that keeps no state
  between launches (Retro Trivia, whose round is drawn fresh from an unseeded
  RNG) is not in it yet and is reviewed by running it. Add the screen you changed
  to `WALKS` if no walk reaches it -- the door's own dispatch is the authority on
  which key opens what, not the action bar. A walk keeps only the screen it stops
  on, so a screen on the way to another screen needs its own walk; a screen no
  walk can *reach* -- Voidrunner's combat, which happens on a jump -- needs a
  fixture of its own in `FIXTURE_BUILDERS`, built by playing the door rather than
  by writing a save; `%` keeps every
  page of a card stack rather than fast-forwarding to the last; `^` keeps what a
  door drew *before* its first screen cleared, which is the only way to reach a
  masthead -- War Dialer's scanline was rebuilt once with no panel to review it
  on because nothing could photograph the screen it lives on; `SETUP` plays a
  panel's world into the state a screen needs; and each walk says in `SHOWS` what
  its screen must show, with the build refusing a panel that does not show it. What the tests *can* assert, and do:
  that color reaches every body row, that a hotkey, a label, a value and the
  frame are four different colors, that one thing reads the same color on every
  screen that shows it, and that a table's columns do not wander from row to row.
  A screen is built from components that return already-styled rows; a renderer
  that flattens a row to plain text is how both games became grey walls -- War
  Dialer by coloring the whole line from outside, Voidrunner by stripping the
  row in its paginator before it was ever printed. Each door's contract is
  normative for that door and lives in its repository's `docs/design.md`.
- **The public site's source is `web/`, not a published artifact.** Both pages
  of www.netbbs.org live in the repo and are reviewed as a diff; `web/README.md`
  has the deploy procedure, the checkers to run afterwards, and the line-ending
  and capture rules that are easy to get wrong. Before v7.2.0 the source was two
  Claude Artifacts, which made every edit cost a full re-read of 650 KB of
  per-character markup; those copies are historical now.
- **Treat migrations as immutable.** Never edit a shipped migration. Test
  migrations against realistic related data, especially before rebuilding a
  table which is a foreign-key parent.
- **Actually run tests.** Prefer regression tests which demonstrably fail
  without the fix. Confirm scripted UI tests still reach the path their name
  claims after signature/menu changes.
- **Targeted tests per PR, one full check per release.** Before a PR merges,
  run the tests for what it touches: its own test files plus the modules that
  import the changed code. Before a release, run the full check once on the
  exact release tree, all merged PRs together: `pytest -n auto --dist
  worksteal`, then `pytest -m timing_sensitive`. A failure there is fixed
  before tagging, not after. Also run the full check on a PR that changes
  shared machinery every test goes through (`tests/conftest.py`, the database
  layer, migrations, `Session` output).
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
- **Menus and screens follow design doc §3.5 (issue #282); no dialog
  chains.** A hotkey-reached screen shows its content first and can be
  left with `[B]ack` (or a "Press any key" pause) without answering a
  question and without writing anything (a draft editor whose draft has
  changed asks "Discard unsaved changes?" first: discarding typed work is
  the destructive action the yes/no rule below allows). Actions are
  action-bar hotkeys, `edit_resource_draft` fields, or `pick_item`
  entries. A yes/no prompt is only ever the last keystroke behind a hotkey
  the caller chose, immediately before an irreversible, destructive, or
  network-touching action. A toggle toggles. A "blank keeps the current
  value" prompt writes nothing else. Anything gathering more than two
  values is a draft editor or a picker and persists nothing before
  `[S]ave`; a `save` that must reject raises the editor's `error_type` so
  the draft survives (a bare `return None` closes the editor). The only
  exceptions are once-only first-run decisions (Link participation, node
  name, managed-DNS opt-in, the Unicode-style probe), type-the-name
  confirmations before deletes, and masked credential entry (a password
  typed twice, after the current one where the account acts on itself -- a
  draft editor would have to hold the plaintext across redraws). Never add
  a prompt chain, and never hide one behind an extra hotkey instead of
  converting it.
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

A short orientation, not the status record. `docs/NetBBS-design-doc.md`
(current status) and design §15 (roadmap) are authoritative and are updated in
place; when this summary disagrees with them, they win and this file needs
fixing.

The local BBS is complete: boards, files, chat, mail, Communities,
permissions, moderation, identity attestation, SysOp tools, ANSI/TUI editors,
registration, and self-update, reachable over SSH, Telnet and a browser
terminal. Optional bridges to external networks exist for MRC chat, FTN
(FidoNet-technology networks) and QWK/QWKnet.

NetBBS Link (Phases 3-6) has signed identity, discovery, persistent events and
peers, linked boards/channels/file areas, mail, inventory/pull catch-up, relays
for outgoing-only nodes, local trust/reputation and quarantine, authenticated
real-time chat (Noise XX) with presence, scrollback-on-join, live private
messages and cross-node `/dm` chats, Link Communities, and delegated Link
moderation. Deliberately deferred or not built: simultaneous background
channel memberships, and the remaining Phase 6 governance items listed in §15.
Phase 4's public-readiness gate (#131) stays open: the automated adversarial
validation evidence is complete, but a manual recovery exercise,
independently administered multi-node validation and sustained dogfood (#83)
are human/operational work, not further code-level validation.

Doors (Phase 7) run as supervised subprocesses under the same OS user, with
native, DOSBox-X, VM-hosted and remote adapters, door API 4 with optional
outbound posting, companion services, and four bundled games vendored from
their own repositories. Compatibility is bounded to the host/game profiles in
`docs/NetBBS-door-guide.md`.

None of this implies public federation, a closed Phase 4 gate, or universal
legacy-door compatibility.

<!-- moradin-forge:start -->
## Moradin's Forge

- Local sidecar: `.moradins-harness/`
- Agent entrypoint: `.moradins-harness/FORGE.md`
- Harness entrypoint: `.moradins-harness/Harness/entrypoints/forge.md`
- Keep Moradin local unless the user explicitly requests external tooling.
- Treat host tool installation as request-only: write install requests, do not run installs.
- Preserve existing repo workflows and prefer repo-local deterministic commands.
<!-- moradin-forge:end -->

# NetBBS — developer project notes

NetBBS is a modern, TCP/IP-native BBS with an ad-hoc mesh network
(**NetBBS Link**). Phases 1–2 are complete; Phase 3 is active and already
includes working Link identity, transport, persistence, seed synchronization,
linked-board event propagation, and tier-1 Link messages.

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
- **Fail clearly.** Administrative lockout, identity ambiguity, incompatible
  databases, protocol rejection, and resource exhaustion should not degrade
  silently.

## Environment

- Primary target: NetBSD via pkgsrc.
- Python 3.11+, asyncio.
- SQLite in WAL mode.
- PyNaCl/libsodium rather than a Rust-dependent crypto stack.
- User transports: Telnet, SSH, web/xterm.js.
- NetBBS Link transport: signed HTTP+JSON for asynchronous federation; Noise
  remains planned for later real-time Link chat.

## Current scope summary

The local BBS includes boards, files, chat, mail, Communities, permissions,
moderation, identity attestation, SysOp tools, ANSI/TUI editors, registration,
and update infrastructure.

Current Phase 3 includes:

- root and operational node keys with signed transitions;
- canonical Link event bytes;
- hello/endpoint protocol and `aiohttp` adapter;
- configured-seed background synchronization, peer-list exchange, and live
  seed-list refresh;
- persistent peers/events and restart reconstruction;
- foreground/background database lanes;
- deterministic multi-node fault injection;
- linked-board genesis, posts, and self-authored edit propagation, including
  carry-materialization (a node that merely carries a board now gets a real
  local, browsable copy, not just relayed raw events);
- board origin succession: mutual-consent transfer, orphan detection, forks;
- Link messages, scoped to tier-1 (locally-known) recipients only;
- WAN reachability for outgoing-only nodes: direct-observation reliability
  scoring, automatic relay selection/consent/self-healing, and a bounded
  relay store-and-forward mailbox for `link_message` delivery.

It does **not** yet imply public federation, inventory/pull catch-up, tier-2
Link messages, channel-side Link support (boards only so far) or the
origin-succession work that depends on it, advanced governance, or
trust/quarantine. Check the design document and open issues for the current
roadmap rather than extending this summary.

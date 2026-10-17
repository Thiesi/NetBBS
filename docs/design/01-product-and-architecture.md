# NetBBS design — 1. Product identity, terminology, and principles and 2. Platform, architecture, and scale

This is the normative design reference for this area of NetBBS. Section numbers are preserved from the design index so existing `§n.n` references remain stable.

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

**Platform support tiers (issue #81).** Without an explicit contract,
portability becomes accidental — a NetBSD-specific detail creeping into
core code, or Linux/macOS breakage going unnoticed, or (the opposite
failure) primary-target constraints getting quietly weakened to
accommodate a platform that was never meant to drive architecture.
Four tiers, in order:

- **Tier 1 / primary — NetBSD.** Every design and dependency choice must
  work here; this is the platform "does this work?" defaults to. NetBBS
  itself is obtained and updated only from its official GitHub releases
  or tagged source; shipping NetBBS through pkgsrc or another external
  package manager is neither planned nor supported. pkgsrc remains the
  preferred source for external dependencies on NetBSD. Existing
  Tier-1-driven decisions include PyNaCl/libsodium for core identity and
  FTS5 availability confirmed by tracing
  pkgsrc's actual `lang/python312` → `databases/sqlite3` build chain
  (worklog §6), not assumed.
- **Tier 2 / supported — mainstream Linux distributions**, using
  ordinary Python packaging (venv/pip) and system service managers
  (systemd). A regression here is a real bug — *unless* fixing it would
  weaken a Tier-1 constraint, in which case Tier-1 wins; Tier 2 never
  gets to relax what Tier 1 needs.
- **Tier 3 / best-effort — other POSIX systems** (macOS, FreeBSD,
  illumos, etc.), until a maintainer or user demonstrates a build/run
  exercised with some regularity. No dedicated design effort beyond
  staying ordinary POSIX-portable Python.
- **Development-only compatibility — Windows.** Convenient for local
  development and testing (this project's own dev sandbox routinely
  runs here) — never a target for production semantics that depend on
  POSIX facilities: real signal delivery (`asyncio.loop.
  add_signal_handler`), `os.kill(pid, 0)`-style liveness probes, POSIX
  file permission bits, `termios`/`tty` raw-mode terminal control.
  Existing platform branches — `netbbs.net.local_terminal` (raw-mode
  input), `netbbs.backup._process_is_running` (restore's liveness
  probe), `netbbs.__main__`'s signal-handler setup — already draw
  exactly this line, each with its own `sys.platform`/`os.name` check
  and a comment naming Windows as the dev/test fallback, never the
  deployment target; this tier list makes that existing practice an
  explicit policy rather than an implicit one three separate modules
  happened to agree on.

Consequences of the tier list, not separate rules:

- A new dependency is evaluated against the Tier-1 target *before*
  adoption: is it available through pkgsrc, does it need an unusual
  compiler toolchain, or does it assume behavior unlikely to hold on
  NetBSD? We make a strong effort to choose dependencies available in
  pkgsrc so a NetBSD deployment remains straightforward. Tier-2/3
  convenience is never sufficient justification by itself.
- Platform-specific code stays isolated in a small number of narrow
  modules/functions (the three named above), never scattered
  `sys.platform`/`os.name` checks through domain code. Core/domain/
  protocol modules (`netbbs.boards`, `netbbs.link`, etc.) are plain
  POSIX-portable Python with no platform branching at all.
- Installation/service examples (issue #82) use GitHub-hosted NetBBS
  releases on every platform and cover Tier 1 (NetBSD/rc.d) and Tier 2
  (Linux/systemd) explicitly; a Tier-3 or
  development-only path is documented as such, never presented as an
  equally-supported option.

- Runtime: Python 3.11+ with asyncio.
- Storage: one SQLite database per node, using WAL mode.
- Core cryptography: PyNaCl/libsodium. The optional SSH transport uses
  AsyncSSH and therefore `cryptography`; its source build on NetBSD needs
  Rust, a C compiler and Python headers, OpenSSL and libffi headers, and
  build-discovery tooling. Keep that toolchain isolated to the optional
  feature and avoid adding comparable build burdens without compelling
  benefit.
- User transports: Telnet, SSH, and web/xterm.js.
- Asynchronous Link transport: signed HTTP+JSON.
- Real-time Link chat transport: Noise Protocol Framework.

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

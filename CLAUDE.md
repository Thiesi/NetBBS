# NetBBS — Claude Code project notes

A modern, TCP/IP-native BBS with an ad-hoc mesh network ("NetBBS Link").
Phases 1–2 of the 7-phase roadmap are complete; Phase 3 (Link
connectivity & sync core) has not started yet — see below.

## Start here, every session

**Read `docs/NetBBS-design-doc.md` before implementing anything.** It is
the actual source of truth for this project, not this file and not any
chat history — every real design decision, including the reasoning
behind it and what was considered and rejected, is recorded there as a
dated, numbered "sign-off note." If something seems ambiguous or you're
about to make an architectural call, check whether it's already been
decided before deciding it again. Section §15 has the full phase
breakdown; the sign-off notes at the end are in roughly chronological
order and are the design doc's most information-dense part.

The full round-by-round implementation/bugfix history (what actually
got built, bugs found and fixed, "N tests passing" confirmations) lives
separately in `docs/NetBBS-worklog.md`, not in the design doc — that
split happened specifically so the design doc stays a design doc rather
than reading like a work log. Round numbers are shared/consistent
across both files, so a sign-off note in one may reference a round that
only exists in the other.

## Working conventions established so far

- **Design-before-code for anything non-trivial.** Significant decisions
  (data model choices, protocol behavior, UX mechanisms) get discussed
  and confirmed before implementation, not decided unilaterally mid-code.
  If a request bundles several genuinely separable pieces, it's fine —
  encouraged, even — to flag that explicitly and propose tackling one
  piece at a time rather than attempting all of it in one pass. Thiesi
  has explicitly said he prefers correctness over speed and is fine with
  a task spanning multiple sessions.
- **Every real design decision gets a sign-off note** appended to
  `docs/NetBBS-design-doc.md`, following the existing numbered-round
  format. Include: what was decided, why, what alternatives were
  rejected and why, and anything left deliberately open/deferred. Pure
  implementation narrative and bugfix writeups — "implemented X,
  N tests passing," a bug found and fixed with no lasting design
  implication — go to `docs/NetBBS-worklog.md` instead, same
  numbered-round format, so the design doc doesn't drift back into
  being a work log.
- **Modular package structure, not a monolithic script** — see design
  doc §3 for why (the first attempt at this project became an
  unmaintainable single file). Each subsystem (`boards`, `chat`,
  `moderation`, `rendering`, etc.) is its own package.
- **Actually run the tests, don't just syntax-check.** This is the one
  place Claude Code has a real advantage over the chat interface this
  project was largely built in: that environment's sandbox didn't have
  PyNaCl installed, so anything touching `netbbs.auth` (most of the
  codebase, transitively) could only ever be syntax-checked, not
  executed. If PyNaCl is available in this environment, actually run
  `pytest` — several real bugs were only caught this way (see the sign-
  off notes for examples: a `RuntimeError` from mutating a dict during
  iteration, a `goto` command silently resolving against the wrong list,
  password masking that silently did nothing). Assume other latent bugs
  of that shape exist wherever something was only syntax-checked.
- **Write tests alongside new code**, following the existing style in
  `tests/` — real integration tests against loopback sockets for
  anything touching the network/telnet layer, straightforward unit tests
  elsewhere.

## Environment specifics

- Primary deployment target: **NetBSD**, via pkgsrc.
- Python 3.11+, asyncio-based.
- **PyNaCl was deliberately chosen over `cryptography`** specifically
  because `cryptography`'s recent versions pull in a Rust toolchain,
  which is a tier-3 target on NetBSD (more build friction). PyNaCl wraps
  libsodium (C), no Rust anywhere in the dependency chain.
- SQLite (WAL mode), one file per node, no separate DB server.

## Where things stand

Check `docs/NetBBS-design-doc.md` §15 for the authoritative phase
breakdown and current status — it will be more current than anything
written here. As of this update, **Phase 1 and Phase 2 are both
complete** — a genuinely full-featured standalone single-node BBS with
no NetBBS Link dependency yet: boards (including post editing), chat,
file areas with real Zmodem upload/download, permissions and full
moderation tooling, expiry/maintenance, a user directory, SysOp admin
tooling, ANSI/TUI rendering including a fullscreen nano-keybound
editor, and all three planned connectivity methods (Telnet, SSH,
web/xterm.js). Phase 3 (Link connectivity & sync core) has not started.
For how any of that got built — which round did what, bugs found along
the way — see `docs/NetBBS-worklog.md` rather than duplicating that
history here.

Flagged, not blocking further work: real third-party-client/browser
verification still hasn't been done from this sandboxed dev
environment for three things — interactive SSH sessions, real
Zmodem-client interop (SyncTERM/lrzsz), and actual browser rendering of
the xterm.js terminal (no browser-automation tool available here). All
three are worth a direct check from Thiesi's own machine, or a future
session with the right tooling, before considering this fully verified
end-to-end.

A "Communities" (local) / "Link Communities" (federated) concept — a
topic-oriented navigation layer sitting above boards/chat/file areas —
is directionally agreed but not yet phase-assigned or fully specced;
see design doc §16 for what's decided and what's still open.

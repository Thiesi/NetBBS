# NetBBS — Claude Code project notes

A modern, TCP/IP-native BBS with an ad-hoc mesh network ("NetBBS Link").
Currently in Phase 1 of a 7-phase roadmap — see below.

## Start here, every session

**Read `docs/NetBBS-design-doc.md` before implementing anything.** It is
the actual source of truth for this project, not this file and not any
chat history — every real design decision, including the reasoning
behind it and what was considered and rejected, is recorded there as a
dated, numbered "sign-off note." If something seems ambiguous or you're
about to make an architectural call, check whether it's already been
decided before deciding it again. Section §15 has the full phase
breakdown; the sign-off notes at the end are in roughly chronological
order and are the most information-dense part of the document.

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
  rejected and why, and anything left deliberately open/deferred.
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
written here. As of this update, Phase 1 (local single-node BBS —
boards, chat (including bounded, disk-backed scrollback per channel —
design doc round 19/20), file areas (browsable, level-gated core —
design doc round 21; upload/download transfer not yet built, see below),
permissions, blocklist, ANSI rendering, character-mode Telnet input, a
shared paginated picker with categories/pinning/sort order) is
substantially built. Two pieces remain open within Phase 1:

- **Real Zmodem support** for file upload/download (design doc round
  21) — a generic Telnet client can't drive a custom raw-byte transfer
  protocol on its own, so this needs the actual Zmodem packet
  framing/state machine, not something to improvise inline. Deserves its
  own design pass against the real spec before implementation.
- **SSH/web connectivity** — Telnet-only so far. Also deserves its own
  design-before-code pass (new dependencies: an SSH library, a
  websocket/xterm.js frontend), e.g. checking an SSH library is as
  NetBSD/pkgsrc-friendly as PyNaCl was for crypto.

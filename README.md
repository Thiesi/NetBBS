# NetBBS

A modern, TCP/IP-native BBS package: not stuck at 80x24, not stuck on an
EOL operating system, and built around **NetBBS Link** — an ad-hoc mesh
network that lets independent NetBBS nodes discover each other, exchange
message boards and personal messages, and (later) real-time chat, without
requiring any central authority.

Primary deployment target: **NetBSD** (via pkgsrc). Expected to run on
other POSIX systems too.

## Status

Design is complete and signed off — see [`docs/NetBBS-design-doc.md`](docs/NetBBS-design-doc.md)
for the full architecture, rationale, and phased roadmap. Implementation
is in **Phase 1 (Foundation)**.

This is a second attempt at this project. The first attempt got a long way
(multi-user chat, file areas, message boards) but needed a significant
rewrite once mesh networking entered scope, because NetBBS Link wasn't
designed in from the start. This attempt builds NetBBS Link in as a
foundational principle from day one — see the design doc for the full
history and the lessons carried forward.

## Requirements

- Python 3.11+ (asyncio-based)
- [PyNaCl](https://pynacl.readthedocs.io/) for identity/cryptography
  (`security/py-nacl` in pkgsrc) — chosen specifically because it wraps
  libsodium (C) rather than pulling in a Rust toolchain, unlike
  `cryptography`'s recent pkgsrc versions.
- SQLite (bundled with Python's standard library)

## Project layout

```
netbbs/
├── docs/                 Design documentation
├── src/netbbs/           Main package (modular, not monolithic — see
│                         design doc §3 for why)
│   ├── identity/         Keypair generation, storage, addressing (§5)
│   ├── storage/          SQLite connection + schema migrations (§3)
│   ├── auth/             Account creation, password + keypair login (§5)
│   ├── permissions/      User-level gating plumbing (§13)
│   ├── net/              Telnet transport (character-mode input: server-
│   │                     driven echo, Backspace/Delete, NAWS window-size
│   │                     negotiation) + Session abstraction, login flow
│   │                     (SSH/web to follow on the same Session
│   │                     abstraction). `picker.py`: shared paginated
│   │                     list selector (2-digit select, search, goto by
│   │                     stable index) used by both board and channel
│   │                     selection, and (once built) file areas
│   ├── boards/           Local message boards + posts, content-addressed
│   │                     IDs from day one (§7) so Linked-board support
│   │                     later needs no ID-scheme migration. Two-level
│   │                     categories, pinning, and configurable sort
│   │                     order (activity/alphabetical/volume) in
│   │                     `categories.py`/`boards.py`
│   ├── chat/             Local real-time chat: channels (content-
│   │                     addressed IDs, same reasoning as boards) + an
│   │                     in-memory per-node broadcast hub. Same
│   │                     categories/pinning as boards; "activity" sort
│   │                     is in-memory only (chat isn't persisted)
│   ├── moderation/       Local blocklist (moderation stub, pre-dates the
│   │                     full reputation system)
│   ├── rendering/        The "ANSI half" of the hybrid rendering
│   │                     framework (§4/§15): 256-color/cursor helpers +
│   │                     text reflow to each session's detected width.
│   │                     The "TUI half" (character-mode input, screen-
│   │                     buffer diffing) is deliberately deferred until
│   │                     a real heavy screen needs it
│   ├── config.py         Node-wide key-value settings (currently just
│   │                     display timestamp format)
│   ├── __main__.py       Minimal runnable entry point for manual testing
│   └── timeutil.py       Storage-format timestamps (utc_now_iso) and
│                         user-facing display formatting, kept separate
├── scripts/
│   ├── create_test_user.py    Dev utility: create an account to test the
│   │                          login flow with (no self-registration UI
│   │                          exists yet)
│   ├── create_test_board.py   Dev utility: create a board (+ seed post)
│   │                          to test board browsing with. Optional
│   │                          category name and pinned flag
│   ├── create_test_channel.py Dev utility: create a chat channel to
│   │                          test real-time chat with. Optional
│   │                          category name and pinned flag
│   ├── create_test_category.py Dev utility: create a board or channel
│   │                          category, optionally as a sub-category
│   ├── block_user.py          Dev/admin utility: block a user from
│   │                          logging in
│   ├── unblock_user.py        Dev/admin utility: remove a user from the
│   │                          blocklist
│   └── set_node_config.py     Dev/admin utility: set a node-wide config
│                               value, e.g. the display timestamp format
├── tests/                Test suite (pytest; conftest.py speeds up
│                         Argon2id-heavy tests automatically)
├── pyproject.toml
└── README.md
```

As phases progress, expect new top-level modules under `src/netbbs/`
roughly mirroring the design doc's sections: `transport/`, `link/`
(DAG/gossip/sync), `boards/`, `areas/` (files), `chat/`, etc. Each stays a
separate, testable module — see design doc §3 for the reasoning against a
single monolithic script.

## Manually testing the Telnet connection

```sh
python scripts/create_test_user.py netbbs.db thiesi hunter2 100
python scripts/create_test_board.py netbbs.db general "General discussion"
python scripts/create_test_channel.py netbbs.db lobby "General chat"
python scripts/set_node_config.py netbbs.db display_timezone Europe/Berlin
python -m netbbs netbbs.db
```

For real-time chat specifically, open two separate `telnet localhost
2323` sessions (two terminals, or one real connection plus you at the
console testing solo won't show the broadcast effect) and join the same
channel from both — messages sent from one should appear in the other
immediately.

To see terminal-width-aware reflow in action, resize your terminal
narrower (e.g. ~40 columns) *before* connecting — most Telnet clients
report their window size via NAWS on connect, and post bodies should
wrap to match. A client that doesn't support NAWS falls back to an
80-column assumption.

**Line editing:** the server now handles all echo and Backspace/Delete
itself (character mode), not the client — this fixed the `^M`-instead-
of-newline and non-working-Backspace issues seen with client-side line
editing. Known limitation: Backspace only removes from the *end* of what
you've typed — there's no cursor movement (arrow keys, Home/End), so
fixing a mid-word typo means backspacing past everything after it and
retyping, not editing in place. Full cursor-addressable editing is out of
scope for this pass; see design doc phasing notes.

The main menu now dispatches immediately on a single keystroke — no
Enter needed for `B`/`C`/`Q`. Real behavior change: the old "b" or
"boards" (full word) alternative no longer works, only the single letter.

Your own chat messages now show in a distinct color (magenta) from
everyone else's (gold), so they stand out in the conversation.

**Board and channel selection** now uses a shared paginated picker
instead of typing exact names: browse with 2-digit numbers, `[S]earch`
by substring (auto-selects if there's a unique match), `[G]oto #` to
jump straight to a stable absolute index shown as `(#N)` next to every
item — that number stays valid regardless of paging or an active search
filter.

**Categories, pinning, and sort order:** boards and channels can now be
organized into categories (at most two levels — a category and,
optionally, sub-categories under it), pinned to always sort first, and
sort by activity (default), alphabetically, or by post volume (boards
only). Try it:

```sh
python scripts/create_test_category.py netbbs.db board "Vintage Computing"
python scripts/create_test_category.py netbbs.db board "Commodore" "Vintage Computing"
python scripts/create_test_board.py netbbs.db c64 "Commodore 64 talk" Commodore
python scripts/create_test_board.py netbbs.db announcements "" "" yes
```

Browsing boards should now show "Vintage Computing" as a category to
drill into (revealing "Commodore" as a sub-category, then `c64` inside
that), while `announcements` (pinned, uncategorized) appears at the top
level, ahead of anything else.

To test the blocklist:

```sh
python scripts/block_user.py netbbs.db thiesi "testing the blocklist"
```

Then try logging in as `thiesi` — you should see "Your access to this
system has been revoked." instead of reaching the main menu. Reverse with
`python scripts/unblock_user.py netbbs.db thiesi`.

Then, from another terminal:

```sh
telnet localhost 2323
```

Port 2323, not 23 — binding 23 needs root. See `src/netbbs/__main__.py`
for why, and for what a real deployment would need instead.

## Development

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest
```

To enable the repo's pre-commit hook (blocks commits containing
unresolved git merge conflict markers — see `.githooks/pre-commit`):

```sh
git config core.hooksPath .githooks
```

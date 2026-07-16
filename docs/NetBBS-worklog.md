# NetBBS worklog

This file is the full, round-by-round implementation and bugfix log
for NetBBS. It is a companion to
[docs/NetBBS-design-doc.md](NetBBS-design-doc.md), which holds only
standing design decisions — what was decided, why, what alternatives
were rejected and why, and what was deliberately left open. This file
holds everything else from the same sequence of work: bugs found and
fixed, debugging journeys, "N tests passing" confirmations,
code-diff-heavy walkthroughs, and other session-log-style material
whose value is historical/procedural record-keeping rather than
standing architectural guidance.

Entries here are **historical record, not currently-binding design
rationale** — if you're about to make an architectural decision, consult
the design doc, not this file. Round numbers, dates (where present),
and titles are preserved unchanged and unrenumbered from the original
combined log; some rounds appear here in full because they are pure
implementation/bugfix narrative, and some appear here in full *in
addition to* a condensed version in the design doc, because the round
mixed genuine design decisions with implementation narrative and both
halves were kept, in full, somewhere.

---

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

## Sign-off notes, round 51 (deliberate node shutdown: SIGTERM=graceful, SIGINT=immediate — implemented)

Prompted by Thiesi testing real shutdown behavior directly: sending the
node process a signal while users were connected acknowledged the
request but didn't visibly finish until the last connection closed on
its own — with no warning ever shown to anyone still connected.

1. **Root cause identified by direct code reading, not assumed.**
   `_install_signal_handlers` already wired both signals to
   `shutdown_event.set()`; `run()`'s `finally` block already only calls
   `server.stop()`, which just closes each *listening socket*
   (`TelnetServer`/`SSHServer`/`WebServer` all follow this shape) —
   none of them ever tracked or awaited already-connected sessions. The
   thing actually reaching in-progress connections was
   `asyncio.run(main())`'s own *implicit* end-of-process cleanup
   (cancelling every remaining task once `main()` returns) — opaque,
   undocumented, and not something to keep depending on. Replaced
   outright with a deliberate, awaited sequence rather than further
   diagnosed, since the fix needed regardless was the same either way.
2. **Two new small node-wide pieces, each its own module, mirroring
   existing precedent:** `netbbs.net.session_registry.
   ActiveSessionRegistry` (structurally identical to
   `netbbs.chat.presence.PresenceRegistry` — a `{Session: asyncio.Task}`
   map, `enter`/`leave` via `asyncio.current_task()`) adds
   `broadcast_to_all(text)` (writes directly to every session
   regardless of what it's blocked reading — the same concurrent-
   `write()` safety `netbbs.net.chat_flow`'s two-task chat loop already
   established, and the same snapshot-not-live-dict iteration
   `ChatHub.broadcast` already established after round 10's concurrency
   bug) and `disconnect_all()` (cancels every tracked task, then awaits
   `asyncio.gather(..., return_exceptions=True)` so the caller knows
   every session has actually finished unwinding through its own
   existing `finally: await session.close()`, not just that cancellation
   was requested). `netbbs.net.maintenance.MaintenanceMode` is a single
   plain flag, checked at the very top of `handle_session` — before
   `throttle.try_enter_unauthenticated()` even runs — rejecting a new
   connection immediately with a fixed message.
3. **Registration covers a connection from the moment it arrives, not
   just once authenticated** — deliberately different scope from
   `presence`, which only ever knows about accounts. `handle_session`
   now checks maintenance mode and enters the registry as its very
   first actions, before login begins; the existing login/main-menu
   body was extracted into `_run_authenticated_session` so that
   wrapping stays a thin two-concern prologue rather than adding
   another level of nesting to the whole function.
4. **New `ShutdownConfig`/`[shutdown]`**, exactly mirroring
   `ThrottleConfig`/`[throttle]`'s existing config-file precedent: one
   field, `graceful_delay_seconds` (default 60 — Thiesi's own "a minute
   or 90 seconds"). No configurable message text — scope stayed to the
   one thing actually asked for.
5. **SIGTERM = graceful, SIGINT = immediate — the conventional mapping,
   confirmed with Thiesi after an initial proposal had it backwards.**
   `netbbs.__main__._run_shutdown_sequence`: activates maintenance mode,
   broadcasts a warning, then (graceful only) `asyncio.sleep`s the
   configured delay before `disconnect_all()`; `shutdown_event` is set
   last, so `run()`'s existing `finally: server.stop() → db.close()`
   only ever runs once every session is confirmed gone. Signal handlers
   now schedule this coroutine via `loop.create_task(...)` (both the
   POSIX `add_signal_handler` path and the Windows `signal.signal` +
   `call_soon_threadsafe` fallback ultimately reach it the same way)
   instead of setting `shutdown_event` directly. `run()`'s existing
   `shutdown_event` test-injection seam is unchanged in shape —
   `session_registry`/`maintenance` became two more optional,
   independently-injectable parameters alongside it, not a replacement
   for that seam.
6. **Known, accepted limitation, not solved here:** the broadcast text
   can interleave oddly with whatever a user is mid-typing — the same
   already-documented limitation ordinary chat messages have always
   had.
7. **Testing:** new `tests/test_shutdown.py` (13 cases) — the registry
   in isolation (broadcast/disconnect, including a session that raises
   `SessionClosedError` mid-broadcast not blocking the rest),
   maintenance mode rejecting a new connection before `read_line` is
   ever reached, and two real-socket integration tests through a real
   `TelnetServer`/`run()`: the immediate path broadcasts and force-
   closes the connection with no measurable wait, the graceful path's
   total elapsed time is bounded *below* by the configured delay (a
   lower-bound-only timing assertion, deliberately not an exact-timing
   one, so it can't flake on a slower machine) confirming the wait
   genuinely happened rather than being silently skipped. Every
   existing direct `handle_session(...)` call site across three test
   files threaded the two new parameters through — the same mechanical
   "widening a parameter breaks many call sites" pattern rounds 42/46/
   47 already described. Full suite re-run: **981 passed, 1 skipped**
   — actually run, not just syntax-checked.
8. **Flagged, not closed out here:** this is the one piece that can't
   be fully proven by socket-level tests alone, the same category of
   gap this project's history already carries for SSH/Zmodem/xterm.js —
   worth a direct real-signal check (`kill -TERM`/`kill -INT` against a
   running node) from Thiesi's own machine before considering it fully
   verified end-to-end, flagged explicitly rather than silently assumed
   correct from sandboxed tests alone.

## Sign-off notes, round 52 (invalid-keystroke: bell only, genuinely nothing else — implemented)

Revises round 48. Thiesi tested that round's fix directly and judged
the reprinted `Choice: ` prompt after the bell to add no value — the
prompt was already on screen, so reprinting it communicates nothing
new. His original round-44 request ("we certainly shouldn't redraw the
menu") was interpreted at the time as barring only a *full* redraw;
this round settles that ambiguity the other way: **nothing** beyond the
bell, for a genuinely invalid/inapplicable action.

1. **Structural blocker identified and fixed at its root, not patched
   per-symptom:** all four affected loops (`picker.py`'s `pick_item`,
   `login_flow.py`'s `_main_menu`/`_show_board`/`_edit_profile`)
   printed `Choice: ` unconditionally at the *top* of their `while
   True` loop, every iteration — the actual reason "no reprint on
   invalid input" was structurally impossible before this round, not
   just an oversight in each individual bell branch. Fixed by moving
   the prompt print out of the loop entirely and folding it into the
   end of whatever function already redraws real content (`_render()`
   in the picker, `_draw_main_menu`/`_render_board_page`/
   `_render_profile` in `login_flow.py`) — the loop body itself now
   never prints the prompt directly; it only ever appears as the last
   line of an actual redraw.
2. **The two-bucket distinction from round 48 survives, clarified, not
   abandoned:** a *specific answer to a specific question* — the
   picker's `No matches.`/`Not a number.`/`Out of range.` sub-prompt
   responses — still gets its own text *and* a freshly printed prompt
   afterward (added explicitly at each of those three call sites, since
   they don't go through a `_render()` call), because something was
   actually communicated that the user needs a clean line to respond
   to. A bare invalid/inapplicable keystroke (unrecognized key, a page-
   boundary bell, an out-of-range 2-digit selection) gets neither — the
   picker's page-boundary branches (`N` on the last page, `P` on the
   first) and the 2-digit-selection branch had their `write_line("")`
   calls removed entirely, not just reordered, since even the
   newline-only reprint no longer belongs there.
3. **Testing:** `tests/test_picker.py`'s round-48 regression test
   renamed and rewritten (`test_repeated_invalid_keys_produce_nothing_but_an_echo_and_a_bell`)
   to assert the new exact byte sequence (`b"z\a"`, not
   `b"z\r\n\aChoice: "`) — every other picker assertion in that file
   only checked substring presence/absence, so they needed no changes,
   confirmed by running them rather than assumed. New
   `tests/test_menu_invalid_key.py` adds the same exact-write-sequence
   proof for `_main_menu`/`_show_board`, not previously covered this
   precisely — using a non-echoing `FakeSession` deliberately, since
   character echo is a real transport's job (`netbbs.net.char_input.
   read_key`), not something `login_flow`'s own code ever writes;
   asserting the code's own `write()` calls for an invalid turn are
   exactly one bell is a cleaner proof of the property than trying to
   simulate transport echo. `tests/test_directory_ui.py`'s existing
   `_edit_profile` regression test gained a
   `session.output.count("Choice: ") == 1` assertion alongside its
   existing `"Your profile:"`-count check. Full suite re-run: **983
   passed, 1 skipped** — actually run, not just syntax-checked.

## Sign-off notes, round 53 (`/nick` display: chat-stream marker+color, lists keep both forms — implemented)

Reverses round 32 point 7 / round 41's original "every chat rendering
must keep the canonical username plainly visible" mandate, specifically
and only for the live conversational stream — Thiesi tested `nick|
username` shown on every single line of live chat and judged it
cluttered in practice, not just debatable in theory. Explicitly flagged
by Thiesi as provisional, easy to revisit once seen live — this is not
treated as a final word on the exact marker/color choice.

1. **Two rendering forms, deliberately, not one changed in place.**
   `netbbs.chat.nick.display_label` (`nick|username`, unchanged) stays
   the form `/who`/`/whois`/`/names` use — directory-style listings
   still show canonical identity alongside presentation, matching the
   original anti-impersonation intent exactly. New `chat_stream_label`
   (nick-only, marked `~nick~` and colored, or plain `username` if
   unset) is used everywhere in the live conversational stream instead:
   regular messages, `/me` actions, join/leave notices (live and
   scrollback replay alike). Moderation notices already never called
   either function (canonical-only, round 32 point 7) — untouched.
   **Confirmed scope, including two points not obvious from the
   original request:** `/whois`'s own identity header turned out to
   already be canonical-only (`vcard.username` directly, never
   `display_label` at all) — a pre-existing fact traced during
   implementation, not something this round changed; scrollback replay
   reuses whatever the live format is, per Thiesi's explicit answer,
   itself given with the same "might revisit once I've seen it" caveat.
2. **Marker character: `~` (tildes wrapping the nick), ASCII-safe by
   the same reasoning that ruled out italics for this exact problem
   earlier in the same discussion** — guaranteed to render identically
   on a CP437-only classic BBS client, not font/encoding-dependent, and
   not already used anywhere else in this codebase's chat rendering
   conventions (unlike `*`, already overloaded for `/me` actions and
   moderation notices). `netbbs.chat.nick.set_nick` gained one new
   validation rule rejecting `NICK_MARKER` from submitted nick content
   — today literally any character was accepted, so this is a real,
   if small, behavior change, needed to make Thiesi's own requirement
   ("nothing you could type in that `/nick` would ever accept") true
   rather than just claimed.
3. **New `NICK_COLOR` (sky blue, 39)** in `netbbs.rendering.theme`,
   following the existing `SELF_COLOR`/`ACCENT_COLOR`/`MUTED_COLOR`
   precedent — a new semantic color, not a reused one that already
   means something else.
4. **A real ANSI-nesting bug caught during design, not shipped:** an
   earlier draft had `chat_stream_label` wrap its own colored segment
   *inside* each call site's existing single `colored(whole line)`
   call. Verified directly that `colored()`'s trailing reset always
   returns to the terminal's *default*, not back to whatever color was
   active before the nested call — meaning the text *after* a nick in,
   e.g., a join notice would have visibly lost its `MUTED_COLOR`
   styling for the remainder of that line, every time a nick was
   involved. Fixed by having `chat_stream_label` own sanitization *and*
   coloring itself as one atomic, self-contained unit, with callers
   splicing its result into their template rather than wrapping the
   whole line in one outer `colored()` call. **Known, accepted residual
   quirk, not engineered around further:** the text immediately
   *following* a nick within an already-colored line still reverts to
   the terminal default rather than reapplying the surrounding color,
   for the same reset-doesn't-stack reason — acceptable for now given
   the whole feature is explicitly provisional.
5. **Sanitize-before-color, not after — verified as the only safe
   order, not assumed.** `chat_stream_label` sanitizes the raw nick/
   username *before* wrapping it in `colored()`, never the reverse:
   running `sanitize_text` on an already-colored string would risk
   stripping this function's own legitimate SGR codes right alongside
   (or instead of) any genuinely hostile content — the same "sanitize
   on output, immediately before display" ordering this codebase has
   followed since round 29, just newly relevant here because this is
   the first function in the codebase to own both sanitization *and*
   styling of the same value together.
6. **Testing:** `tests/test_chat_flow_nick.py`'s five existing
   alias-rendering tests updated for the new format — each split into
   multiple substring assertions either side of `chat_stream_label`'s
   own trailing ANSI reset, rather than one assertion spanning it,
   after the first attempt at updating them tripped on exactly that
   boundary. New tests confirming `/names`/`/who` still show both
   forms unchanged, and the `/whois`-is-actually-canonical-only fact
   from point 1. `tests/test_chat_nick.py` gained `chat_stream_label`
   unit tests (bare-username case has no color codes at all; the nick
   case is marked, colored, and never contains the canonical username)
   and two `set_nick` marker-rejection tests. Full suite re-run: **992
   passed, 1 skipped** — actually run, not just syntax-checked.

## Sign-off notes, round 54 (`/query` removed entirely — implemented)

Reverses round 33 point 1's original "`/query` accepted only as an
IRC-compatibility alias" decision. Thiesi confirmed `_COMMANDS["query"]
= _handle_private` was a bare alias with no behavior of its own, then
asked for it gone outright: it was the only command in the entire
surface with two names, and existing purely for a compatibility
convention nobody had actually asked to keep. A future shell-alias-
style user-defined-command-name feature was explicitly floated and
explicitly deferred as out of scope for now.

1. **Removed from every place it was registered, not just the
   dispatch table:** the `_COMMANDS` entry itself, and `"/query "` from
   Tab completion's `_ONLINE_USER_COMMAND_PREFIXES` (Track 5g) — a
   second registration point that would have silently kept suggesting
   a now-dead command if missed.
2. **Testing:** `test_query_is_an_alias_for_private` in
   `tests/test_chat_flow_private.py` replaced with
   `test_query_is_no_longer_a_command`, asserting `/query` now produces
   the ordinary "Unknown command" response every other unrecognized
   slash-command gets — locking in the removal as a real, checked
   behavior rather than just deleting the old test and losing coverage
   of the boundary. `tests/test_chat_completion.py` gained the
   equivalent for Tab completion (`/query` suggests nothing, and no
   longer appears in the bare-`/` command list). Full suite re-run:
   **993 passed, 1 skipped** — actually run, not just syntax-checked.

## Sign-off notes, round 55 (`/help` overhaul + `/?` alias — implemented)

Thiesi found the pre-existing `/help` "mostly useless": it printed the
same bare command-name list Tab completion (round 49/Track 5g) already
surfaces, adding nothing. Asked for a real upgrade — syntax plus a
one-line description per command, permission-aware, `/help <command>`
for single-command detail, and a `/?` alias — then, when asked whether
to centralize command metadata or leave it scattered, said: "Centralize
it, and yes to permission-aware and /help <command>."

1. **`_COMMAND_INFO: dict[str, tuple[str, str]]`** — new single source
   of truth for every command's syntax and one-line description (26
   entries, including commands that take no arguments and previously
   had no `Usage:` string at all, like `/quit`/`/who`/`/members`).
   Replaces 16 independently-worded, scattered `"Usage: /command ..."`
   literals with a single `_show_usage(session, command)` helper that
   looks the syntax up from this table — both `_show_usage` and
   `/help` itself are now generated from the same data, so the two can
   no longer drift out of sync with each other.
2. **Bare `/help`** lists every command visible to the caller, reusing
   `_COMMAND_VISIBILITY` — the exact same predicate dict Tab completion
   already applies — so the list a user sees matches what `/` + Tab
   would offer them. This is the actual value-add over Tab completion
   Thiesi asked for: syntax and description attached to each entry,
   not just bare names.
3. **`/help <command>`** looks up one `_COMMAND_INFO` entry directly
   and shows full detail, **regardless of the caller's own visibility**
   for that command — consistent with Track 5g's established framing
   that `_COMMAND_VISIBILITY` is a suggestion filter, not an
   authorization check. A non-moderator explicitly asking `/help mute`
   gets a real answer; the handler itself remains the sole source of
   truth for whether running it is actually allowed. An unrecognized
   argument gets the same "Unknown command: /x" wording the top-level
   dispatcher already uses elsewhere, for consistency.
4. **`/?` alias**: `_COMMANDS["?"] = _handle_help`, a second dispatch
   key mapped to the *same* handler — deliberately not the same shape
   as round 54's `/query` removal. The distinction: `/query` was two
   *names* for a command that already had one, existing purely as an
   unused compatibility convention; `/?` is a genuinely distinct,
   commonly-expected terse trigger being added on request, for a
   command whose only other name is a full word. Recorded explicitly
   so this doesn't read as quietly reintroducing what round 54 just
   removed.
5. **Testing:** new `tests/test_chat_help.py` — bare `/help` shows
   syntax+description and hides moderation commands from a
   non-moderator while showing them to a moderator; `/help <command>`
   (with or without a leading `/` in the argument) shows detail
   regardless of visibility; `/help <unknown>` gives the friendly
   "Unknown command" message; `/?` behaves identically to `/help`,
   bare and with an argument. `tests/test_chat_dispatch.py`'s
   pre-existing `test_help_lists_known_commands` — which had asserted
   `/mute` appears in a plain user's `/help` output — was updated to
   drop that now-incorrect assumption (permission-gating is the new,
   intended behavior) rather than deleted, keeping coverage of the
   commands every user *does* see. Full suite re-run: **1001 passed, 1
   skipped** — actually run, not just syntax-checked.

## Sign-off notes, round 56 (SysOp foundation: SYSOP_LEVEL, dual-purpose admin tool, bootstrap — implemented)

First slice of §15's "SysOp admin tools (user/board/node management,
beyond blocklists)" line — scoped, on Thiesi's confirmation, to the
foundation plus user management only; node management (exposing the
round-51 shutdown/session-registry machinery as an in-session command)
and board/channel management are explicitly deferred to later rounds.

Investigation before implementation turned up two things the one-line
roadmap entry didn't anticipate: (1) there was no way to create a user
account through the running system at all — `create_user`/
`create_user_async` were only ever called by tests and a dev script —
so this round had to add real account creation, not just management of
accounts that already somehow exist; (2) accounts can be pubkey-only
(no password at all, enforced by a CHECK constraint on `users`), which
directly shaped the admin CLI tool's auth design below.

1. **`SYSOP_LEVEL = 255`** (`netbbs/auth/users.py`) — a level, not a
   separate flag/table, so it composes with the existing
   `meets_level`/`require_level` gating everywhere else already uses
   levels. Thiesi's own choice of 255 over a lower round number, to
   make it visually unmistakable as the top of the range. Replaces the
   `_DEMO_ELEVATED_LEVEL = 100` placeholder in `login_flow.py`, which
   only ever printed "(You have elevated access.)" after login and was
   explicitly documented as not a real SysOp constant.
2. **Dual-purpose admin tool — one shared implementation, two entry
   points.** Thiesi's own idea, refined during discussion: a gated
   `[A]dmin` option on the main menu inside an authenticated BBS
   session (`login_flow.py`, gated on `meets_level(user, SYSOP_LEVEL)`
   both for the menu letter's visibility and its dispatch, so a client
   typing "a" blind still gets refused), and a standalone local CLI
   tool run as `python -m netbbs.admin` (mirroring how the node itself
   runs as `python -m netbbs` — no `[project.scripts]` entries exist in
   `pyproject.toml`, so this follows that existing convention rather
   than introducing packaging). Both call the exact same
   `netbbs.net.admin_flow.admin_menu(session, db, user)` — no command
   logic is duplicated between them.
3. **`LocalCLISession`** (`netbbs/net/local_cli.py`) — a new `Session`
   implementation over local stdin/stdout, modeled directly on
   `TelnetSession`: `read_line`/`read_key` delegate entirely to
   `netbbs.net.char_input` (echo, backspace, UTF-8, history, Tab
   completion all reused, none of it reimplemented), so this class only
   supplies raw bytes in and CRLF-normalized text out. The one
   genuinely new, platform-specific piece — putting the local terminal
   into raw/cbreak mode for single-keystroke `read_key()` to work over
   real stdin — is isolated in its own small module,
   `netbbs/net/local_terminal.py` (POSIX `termios`/`tty.setraw`;
   Windows no-op, since `msvcrt.getch()` already reads unbuffered per
   call). `LocalCLISession`'s byte-read functions are constructor-
   injectable specifically so the class itself stays unit-testable via
   a fake byte source, with the untestable-here platform sliver kept as
   thin as possible around it.
4. **CLI auth: no credential check, ever, when run locally.** Local
   shell/filesystem access to the database file is already the real
   trust boundary — the same reasoning `sudo` relies on for a shell
   that already has root-equivalent access. This resolved a genuine
   design gap Thiesi caught directly: a password prompt would
   permanently lock out a pubkey-only SysOp, since SSH's own handshake
   already proves private-key possession before `authorize_public_key`
   is ever called (see that function's docstring) and there is no local
   equivalent to fall back on at a bare terminal. Instead,
   `netbbs/admin/__main__.py`'s `_resolve_actor` only determines *which*
   SysOp to attribute actions to, for the audit log: `--as <username>`
   if given; auto-selected if there is exactly one active SysOp
   (unambiguous, no cross-system name-guessing involved — an earlier
   idea to default to the local OS shell username was explicitly
   rejected during discussion, since BBS usernames have no required
   relationship to OS account names and a coincidental match could
   silently misattribute an action); an interactive picker
   (`netbbs.net.picker.pick_item`) otherwise. Disabled SysOp accounts
   are excluded from all three paths, matching `count_sysops`.
5. **Bootstrap.** If zero active SysOp accounts exist, `_resolve_actor`
   skips the above entirely and calls `_bootstrap_first_sysop`, which
   walks through the same create-account prompts the admin menu's own
   create screen uses and creates the first `SYSOP_LEVEL` account. Its
   audit-log entry has a genuine chicken-and-egg problem — no actor
   exists yet to attribute the action to — resolved by self-attributing
   the entry to the account it just created, with a `detail` string
   recording that it was a bootstrap creation.
6. **The network-facing server refuses to start at all with zero
   SysOps.** `netbbs.__main__.run()` now checks `count_sysops(db) == 0`
   as the first statement inside its existing `try:` block, before any
   listener starts, and raises the existing `StartupError` (already
   caught by `main()`) rather than a new exception type. A node with no
   SysOp could never be administered once running over the network — no
   one could create a second account, disable a rogue one, or recover
   from any mistake — so this fails loudly at startup rather than
   running in a permanently-stuck state. Every pre-existing test in
   `tests/test_main_lifecycle.py` needed its shared `_config()` helper
   updated to seed a SysOp by default (a real regression this change
   would otherwise have caused across that whole file, caught by
   actually running the suite, not by inspection) — a `seed_sysop=False`
   opt-out was added specifically for the new tests exercising the
   refusal itself.
7. **Testing:** `tests/test_admin_flow.py` (the shared menu, via a
   scripted `FakeSession`), `tests/test_local_cli_session.py`
   (`LocalCLISession` via an injected fake byte source — no real
   terminal), `tests/test_admin_cli.py` (`_resolve_actor`/
   `_bootstrap_first_sysop`/`run_admin_session`), extensions to
   `tests/test_main_lifecycle.py` (zero-SysOp refusal, including a
   disabled-sole-SysOp variant constructed via raw SQL since the normal
   `set_user_disabled` API's own lockout guard makes that state
   unreachable through it), and `tests/test_local_terminal_raw_mode.py`
   — a POSIX-only, `pty`-based test of `raw_terminal()`'s actual mode
   switching, skipped on Windows. That last one cannot be run or
   verified in the current Windows dev sandbox — flagged the same way
   this project already flags SSH/Zmodem/xterm.js interop as needing a
   real check on POSIX/NetBSD hardware before being considered fully
   closed out. A `LocalCLISession` unit test itself surfaced a real,
   separate pytest-capture gotcha (not a bug in the code under test):
   patching `sys.stdout` from inside a `@pytest.fixture` function
   silently didn't take effect under plain `pytest -q`, because
   pytest's own capture plugin resets `sys.stdout` at the start of each
   test's "call" phase, after fixture "setup" has already run — fixed
   by moving the patch into the test bodies themselves. Full suite
   re-run alongside round 57 below.

## Sign-off notes, round 57 (SysOp user management: create/promote/demote/disable/delete — implemented)

Built on round 56's foundation. The five actions Thiesi asked for,
confirmed during discussion: create (SysOp-only for now — public self-
registration is a separate, deferred feature), promote/demote, soft-
disable/enable, and hard delete — all reachable identically from both
the in-BBS `[A]dmin` menu and `python -m netbbs.admin`, since both call
`netbbs.net.admin_flow.admin_menu`.

1. **Schema: `users.disabled_at`** — a nullable ISO timestamp, `NULL` =
   not disabled, following the existing `created_at`/`last_login_at`
   TEXT-ISO convention already on that table. Checked at all three auth
   entry points (`_finish_password_login`, `authenticate_keypair`,
   `authorize_public_key`) with the same generic failure a wrong
   credential produces — a disabled account isn't distinguishable from
   a wrong password/signature, per `AuthError`'s existing anti-
   enumeration docstring.
2. **Schema: real `ON DELETE` behavior for hard delete.** Every foreign
   key into `users(id)` previously used SQLite's bare default (`NO
   ACTION`), so deleting a user row would just raise `IntegrityError`.
   Added via the same table-rebuild pattern rounds 37/40/41 already
   used for `channel_messages` (SQLite has no `ALTER TABLE` to add a
   foreign-key clause in place), across all nine referencing tables in
   one migration. Two shapes: `posts.author_user_id`/
   `files.uploader_user_id` go `SET NULL` (both tables already carry a
   denormalized author/uploader label+fingerprint specifically so
   display survives account removal — confirmed by reading the existing
   round-2/round-18 comments before designing this, not reinvented);
   `moderator_grants`, `channel_restrictions`, `channel_members`,
   `channel_invitations`, `user_preferences`, and `blocklist.
   blocked_by_user_id` go `CASCADE` (administrative data, meaningless
   once the account is gone); `moderation_log.actor_user_id`/
   `target_user_id` (the latter already nullable, the former newly
   made so) go `SET NULL`, since an audit trail should survive the
   account it names rather than being truncated or blocked by its
   removal. `posts.parent_post_id`'s self-reference — the one foreign
   key pointing *into* any of these nine tables — needed no special
   handling: SQLite only checks foreign-key constraints at the end of a
   statement, not per row, so the implicit `DELETE` a `DROP TABLE`
   performs on a table that's its own parent still ends with zero rows
   and nothing left to violate.
   - **A real bug in the original design, caught by actually running
     the migration-cascade test against seeded data, not by
     inspection:** `blocklist.local_user_id` was first written as `SET
     NULL`, matching the posts/files pattern. But that table's own
     `CHECK ((fingerprint IS NOT NULL) != (local_user_id IS NOT NULL))`
     requires *exactly one* of the two columns to be set — a locally-
     blocked row (no fingerprint) hitting `SET NULL` on
     `local_user_id` would leave both columns `NULL` simultaneously and
     violate its own table's `CHECK` the moment the blocked account was
     deleted. Fixed to `CASCADE` instead: with no fingerprint to fall
     back on, the block itself is no longer meaningful once the account
     is gone, so removing the row is correct, not just constraint-
     satisfying. Recorded here specifically because this is exactly the
     kind of latent bug this project's testing discipline exists to
     catch — a plausible-looking `SET NULL` that only breaks under a
     specific, easy-to-miss-by-inspection combination of an existing
     CHECK constraint and nullable-but-not-independently-nullable
     columns.
3. **`netbbs/auth/users.py`: `UserManagementError`, `count_sysops`,
   `set_user_level`, `set_user_disabled`, `delete_user`.** All three
   mutating functions share one lockout guard
   (`_refuse_if_last_sysop`): refuses an action that would leave the
   node with zero *active* SysOps, where "active" deliberately excludes
   already-disabled accounts — an already-disabled SysOp can't rescue a
   lockout, so it shouldn't count toward preventing one; this is an
   interpretation call beyond the literal "count SysOp accounts" spec,
   made explicit here rather than left implicit. Self-delete/self-
   disable of the account you're currently attributed as is allowed,
   gated only by that same lockout guard — confirmed with Thiesi, no
   extra special-casing. All three audit-log via the existing
   `netbbs.moderation.log.record_action` (no second logging mechanism
   built). `delete_user` logs *before* deleting, not after: on a self-
   delete, `record_action`'s own `actor_user_id` insert would otherwise
   reference a row that's already gone by the time it ran; logging
   first also means `target_user_id` naturally goes `NULL` via the same
   cascade once the row disappears, with `detail` preserving the
   deleted username regardless. `auth/users.py` importing
   `netbbs.moderation.log.record_action` at module level would be
   circular (that module already imports `User` from `auth.users` for
   its own type hint), resolved with a small function-local import in
   each of the three call sites rather than restructuring either
   module.
4. **`netbbs/identity/keys.py`: `parse_verify_key`.** Accepts either
   this project's own base64 raw-key form or a standard OpenSSH public-
   key line (`ssh-ed25519 AAAA... comment`) — the two forms a SysOp
   realistically has on hand when creating a pubkey account, needed
   because the admin create-user flow has to support password, pubkey,
   or both, matching what `create_user` already allows underneath
   (otherwise the tool would be strictly less capable than the library
   it wraps, and a key-only account could never be created through it).
5. **`netbbs/net/admin_flow.py`: the shared menu itself** — Create/
   List/Promote-demote/Enable-disable/Delete/Back, following the
   existing submenu shape (`_edit_profile`'s own draw-function-plus-
   dispatch-loop pattern, bell-only-on-invalid-key per round 52).
   Delete requires re-typing the exact username to confirm before
   proceeding — the first destructive-confirmation prompt of its kind
   in this codebase (mute/ban/kick execute directly today) — given
   Thiesi's choice to support hard delete alongside soft-disable and
   its irreversibility.
6. **Testing:** `tests/test_user_management.py` (the five functions,
   the lockout guard both firing and correctly *not* firing once a
   second active SysOp exists, disabled-account rejection at all three
   auth entry points), `tests/test_migration_user_cascade.py` (real
   end-to-end cascade/`SET NULL` verification against a seeded SQLite
   file, including the self-reference regression check and the
   blocklist fix above), plus the create/list/promote/disable/delete
   coverage already listed under round 56's `test_admin_flow.py`. Full
   suite re-run across both rounds: **1062 passed, 3 skipped** (1 pre-
   existing plus the 2 new POSIX-only local-terminal tests) — actually
   run, not just syntax-checked.

**Flagged, not blocking further work:** the same category of gap this
project's history already carries for SSH/Zmodem/xterm.js — real
interactive verification of `python -m netbbs.admin` and the in-BBS
`[A]dmin` menu from an actual terminal (not just this sandbox's scripted
`FakeSession` tests), and of `raw_terminal()`'s POSIX behavior on real
NetBSD hardware, haven't been done from this sandboxed dev environment.
Worth a direct check from Thiesi's own machine before considering this
round fully closed out.

## Sign-off notes, round 58 (chat_loop cancellation orphaned its two child tasks on deliberate shutdown — fixed)

A real bug, caught on Thiesi's own NetBSD deployment (not in this
sandbox) — round 51's `[A]dmin` tooling had barely shipped when a plain
Ctrl-C with a chat session open produced an unhandled-exception
warning at shutdown:

```
ERROR:asyncio:Task exception was never retrieved
future: <Task ... send_loop() ... exception=SessionClosedError('client disconnected during read')>
```

**Root cause:** `_chat_loop` (`netbbs/net/chat_flow.py`) runs two
concurrent child tasks, `receive_task` and `send_task`, and awaits
`asyncio.wait({receive_task, send_task}, return_when=FIRST_COMPLETED)`.
That call's own cancel/gather cleanup (the lines immediately after it)
only runs when the `await` returns normally — i.e., when one of the two
child tasks finishes on its own. When the *outer* task running
`_chat_loop` is itself cancelled from outside instead (design doc round
51's `ActiveSessionRegistry.disconnect_all()`, invoked by SIGINT's
immediate-shutdown path), the `CancelledError` is raised at the
`asyncio.wait(...)` call site — but `asyncio.wait()` being cancelled
does **not** cancel the tasks it was waiting on. Execution jumped
straight past the cancel/gather logic to the function's `finally:`
block, leaving both child tasks orphaned: still scheduled on the event
loop, with nothing left to await their eventual result. Once the
connection's real socket actually closed, `send_task`'s blocked
`read_byte()` failed with `SessionClosedError` — an exception nothing
was left to retrieve, which is exactly what asyncio's default handler
logs at that point.

**Fix:** wrapped the `asyncio.wait(...)` call in its own
`try`/`except asyncio.CancelledError`, cancelling and gathering both
child tasks explicitly on that path before re-raising — the same
cancel-then-gather shape the existing normal-completion path already
used, just also applied to the cancellation path that previously
skipped it entirely.

**Testing:** `tests/test_chat_flow_cancellation.py` (new). First
attempt used a fake session that blocks on a bare `asyncio.Event().
wait()` and asserted no exception was ever logged via a custom
`loop.set_exception_handler` — this passed even *without* the fix,
because the fake never actually raises anything on its own the way a
real closing socket does, so it never exercised the failure mode at
all (caught by deliberately reverting the fix and confirming the test
still passed — a test that can't fail is worth nothing, and this one
initially couldn't). Rewritten to check the actual mechanism directly:
capture `asyncio.all_tasks()` before starting `_chat_loop`, cancel the
outer task, and assert no *still-pending* tasks are left over
afterward — confirmed this version fails without the fix (both child
tasks show up pending) and passes with it, verified by hand in both
directions before trusting it. A second test confirms the existing
`finally:` cleanup (`hub.leave`, the "has left the channel" broadcast)
still runs on this path, i.e. the fix re-raises `CancelledError` rather
than swallowing it. Full suite re-run: **1064 passed, 3 skipped** —
actually run, not just syntax-checked.

## Sign-off notes, round 59 (node management: [N]ode admin menu — implemented)

Second slice of §15's "SysOp admin tools" line: exposing round 51's
shutdown/session-registry machinery as an in-session command, so a
SysOp can list active sessions, force-disconnect a specific one, and
trigger a graceful/immediate shutdown — with an optional custom
broadcast message — without needing OS-level signal access.

1. **Standalone-CLI scope, confirmed with Thiesi: in-session only.**
   List-sessions/force-disconnect/trigger-shutdown all need live in-
   memory state (`ActiveSessionRegistry`, `MaintenanceMode`, the
   shutdown event) that only exists inside the running server process.
   `python -m netbbs.admin` is a separate OS process with no access to
   that memory. Building real IPC (a Unix socket/named pipe) to bridge
   that was discussed and explicitly rejected — the only solid use case
   beyond this one feature would be richer CLI-side diagnostics, not
   enough to justify a remote-control layer for a solo-SysOp target.
   The CLI needs no changes at all: `kill -TERM`/`kill -INT` against the
   running process already triggers the exact sequence this round
   exposes as a menu command. Also confirmed out of scope: no
   standalone reversible maintenance-mode toggle — `MaintenanceMode`
   stays exactly as round 51 built it, a one-way flag only ever part of
   the shutdown sequence.
2. **`netbbs/net/shutdown.py`** (new) — `run_shutdown_sequence` (moved
   out of `netbbs.__main__`, no longer private, since it now has two
   genuinely different callers) and `NodeControls`, a small bundle
   (`session_registry`, `maintenance`, `shutdown_event`,
   `graceful_delay_seconds`) threaded as one optional parameter rather
   than four separate ones. Same split-out-into-its-own-module
   reasoning as `session_registry.py`/`maintenance.py` themselves in
   round 51. Gained a `message: str | None` parameter — per Thiesi's
   own wording, a supplied message *replaces* the default "going down
   in N seconds"/"going down now" text rather than appending to it,
   sanitized like any other free text a SysOp typed.
3. **A self-cancellation hazard, designed around up front rather than
   discovered the hard way** — directly informed by round 58, found
   just before this round started: if the shutdown-trigger command
   `await`ed `disconnect_all()` inline from within the very session
   issuing the command, that session's own task would be cancelling
   itself while being one of the tasks its own `gather()` call is
   waiting on — the identical species of bug round 58 just fixed
   elsewhere. Resolved architecturally, not defensively: the `[S]hutdown`
   screen fires the sequence as an independent background task
   (`asyncio.create_task`, never awaited inline), exactly matching how
   the existing signal-handler path already does it. The calling
   SysOp's own session is then just another `ActiveSessionRegistry`
   entry that gets cleanly cancelled from *outside* once the background
   task reaches it — the same already-proven-safe shape every other
   connected session goes through, no exclusion or special-casing
   needed. A parallel, narrower version of the same hazard exists for
   single-target disconnect (`[W]ho`, force-disconnecting *yourself*)
   — resolved there with a simple UI-level guard instead (refuse,
   "use Logoff instead"), since a single target has no equivalent
   fire-and-forget framing that would still give the SysOp a synchronous
   success/failure confirmation.
4. **`ActiveSessionRegistry` gained per-entry metadata.** Internal
   storage changed from `dict[Session, asyncio.Task]` to
   `dict[Session, _Entry]` (`task`, `username: str | None`,
   `connected_at`). New `mark_authenticated(session, username)`, called
   from `login_flow._run_authenticated_session` right where
   `presence.enter(user.username)` already happens; new
   `disconnect_one(session) -> bool` (cancel just one task, same shape
   `disconnect_all` already uses per-task); new `list_entries() ->
   list[SessionSummary]`, a display-only snapshot (deliberately not
   exposing the raw `Task`) backing the `[W]ho` screen.
5. **`login_flow.py` threading, backward-compatible by construction.**
   `handle_session`'s existing required params (`session_registry`,
   `maintenance`) are unchanged — zero churn for the many existing test
   call sites. It gained two new *optional* params, `shutdown_event`
   and `graceful_delay_seconds`, and now constructs a `NodeControls`
   internally, threaded through `_run_authenticated_session`/
   `_main_menu`/`admin_menu` as one new optional param at each level.
   The standalone CLI calls `admin_flow.admin_menu(session, db, actor)`
   directly, bypassing this whole chain, so `node_controls` simply
   stays `None` there — exactly the signal `admin_menu` needs to hide
   the `[N]ode` option, achieved for free by *which* caller invokes it
   rather than a separate flag.
6. **A real ordering hazard fixed along the way, not originally
   planned:** `run()` used to construct its own `shutdown_event` lazily,
   right before `await shutdown_event.wait()` — *after* listeners were
   already started and could in principle be accepting connections.
   Since `session_handler` now needs a real `shutdown_event` to pass
   into `handle_session`, that lazy construction was moved earlier,
   before `session_handler` is even defined — removing a latent race
   (a connection reaching the closure before the event existed) that
   predated this round but was only surfaced by needing to touch this
   code path at all.
7. **A real docstring/syntax bug caught by actually running the code,
   not by inspection:** an early edit to `handle_session`'s docstring
   accidentally closed the triple-quoted string early and reopened
   backtick-quoted prose as bare code immediately after — a `SyntaxError`
   on the first `import netbbs.net.login_flow`, not a subtle runtime
   issue. Caught immediately by the standing "actually import/run it"
   discipline rather than shipping silently broken.
8. **Testing:** `tests/test_shutdown.py` extended for `disconnect_one`,
   `mark_authenticated`, `list_entries`, and `run_shutdown_sequence`'s
   `message` param (replaces the default text; sanitized). `tests/
   test_admin_flow.py` extended for the `[N]ode` submenu: `[W]ho` lists
   and disconnects another session, refuses to target the caller's own;
   `[S]hutdown` fires as a genuine background task (verified via a
   second, independently-registered `asyncio.Task` rather than the
   scenario's own task awaiting the very event the sequence sets —
   an early draft of these tests recreated the round-58-adjacent
   self-referential hazard *inside the test* by registering the admin's
   session under the same task that later awaited `shutdown_event`,
   caught before trusting the tests, not after), custom message
   replaces the default, declined confirmation does nothing.
   `tests/test_main_lifecycle.py` gained a test confirming `run()`'s
   real `shutdown_event`/`config.shutdown.graceful_delay_seconds`
   actually reach `handle_session` (via a spy on
   `_run_authenticated_session`, not a full simulated login). Full
   suite re-run: **1078 passed, 3 skipped** — actually run, not just
   syntax-checked.

**Flagged, not blocking further work:** same category of gap this
project's history already carries — real interactive verification of
the `[N]ode` menu (listing real concurrent sessions, disconnecting one,
triggering a real shutdown with a custom message) from actual connected
clients, not just this sandbox's scripted `FakeSession` tests, hasn't
been done. Worth a direct check from Thiesi's own machine, especially
since round 58's original bug report came from exactly that kind of
real-world use this sandbox can't reproduce.

## Sign-off notes, round 60 (board & file-area management: [M]anage boards/areas admin menu — implemented)

Third and final planned slice of §15's "SysOp admin tools" line, scoped
to boards and file areas — channels explicitly deferred to a follow-up
round (Thiesi's own call: "Boards and Areas first, then channels").

Investigation before implementation found the gap was bigger than the
roadmap's one-liner suggested: `create_board`/`create_file_area`/
`create_category` (both category tables) all had zero permission gating
and no command path to reach any of them on a running node at all
(direct calls from tests/dev scripts only); no edit or delete function
existed for boards, file areas, or either category table; deleting a
board/area today would hit the same FK problem hard-deleting a user
had before round 57 fixed it; and individual post/file moderation
(`approve_post`/`delete_post`/`set_post_pinned`/`set_post_exempt`/
`list_pending_posts` and their file equivalents) already existed as
fully permission-gated library functions with zero UI anywhere.
Confirmed in scope for this round, on Thiesi's own calls: the last of
these (post/file moderation), category deletion (falls back
boards/areas/child-categories to "uncategorized"), and moderator-grant
assignment via named presets rather than raw per-flag toggles, with
both per-object and local-blanket grants exposed.

1. **No schema migration for board/area/category deletion — a design
   correction made mid-implementation, not originally planned.** The
   first attempt followed round 57's user-deletion migration pattern
   directly: rebuild `posts`/`files`/`boards`/`file_areas`/
   `board_categories`/`file_area_categories` with real `ON DELETE`
   clauses. Tested before trusting it, the same discipline that's
   caught real bugs before in this project: seeded a board with one
   post, ran the exact rebuild sequence, and found the post silently
   deleted by the `DROP TABLE boards` step *before `posts` was ever
   touched* — no error, no warning. A second test confirmed the same
   mechanism nulls out nullable referencing columns instead when the
   child column allows it. **SQLite's `DROP TABLE` under `PRAGMA
   foreign_keys = ON` applies its own SET-NULL/cascade-delete fallback
   to any row in another table still referencing it, regardless of
   that column's actual declared `ON DELETE` behavior (or lack of
   one) — a real, silent side effect of the rebuild itself.** Round 57
   never hit this because it rebuilt nine tables that reference
   `users(id)` without ever rebuilding `users` itself, so it never
   dropped a table that was simultaneously a live parent of another
   table also being touched in the same migration; boards/areas don't
   have that luxury (`boards` is both a child of `board_categories` and
   a parent of `posts` in the same migration). A fully correct schema
   fix exists (strip every cross-table FK in one pass, rebuild
   everything, re-add the real FKs in a topologically-ordered second
   pass) but roughly doubles the migration's size for six interlocking
   tables. Simpler and just as correct: no schema change at all —
   `delete_board`/`delete_file_area`/`delete_category` handle cascade/
   cleanup at the application level with explicit `DELETE`/`UPDATE`
   statements in the right order, the same way `moderator_grants`
   cleanup already had to be (it has no FK at all, being polymorphic).
2. **A genuinely cross-cutting decision, confirmed with Thiesi over the
   narrower alternative**: `has_permission` (`netbbs/moderation/
   roles.py`) now short-circuits to `True` for any `SYSOP_LEVEL`
   caller, with zero grant rows required — input validation still runs
   first regardless of caller identity, so a nonsensical `object_type`/
   `permission` combination is still caught. This is not scoped to
   board/area moderation specifically: it applies to *every* existing
   consumer of `has_permission`, retroactively — chat's `/mute`/`/ban`/
   `/kick`, membership-admin gating, and Tab-completion's visibility
   predicates all now also implicitly pass for a SysOp with no explicit
   grant. Deliberately *not* extended to `get_grant`/
   `list_grants_for_object`, which answer "what grants actually exist"
   for admin displays and must stay literal, not synthesize a grant
   that isn't really there. The full existing suite was re-run
   immediately after this one change, before building anything on top
   of it, specifically to catch any regression in already-shipped
   moderation behavior — none found.
3. **New library functions**, one set each for `netbbs/boards/boards.py`
   and `netbbs/files/areas.py` (structurally identical, written
   separately rather than sharing an abstraction — see this round's
   plan for why): `update_board`/`update_file_area` (full-state
   replacement, not partial/PATCH — the admin UI pre-fills current
   values as editable defaults) and `delete_board`/`delete_file_area`
   (explicit cascade per point 1, logged before deleting, same
   ordering reasoning as round 57's `delete_user`). `create_board`/
   `create_file_area` gained audit-log entries now that they're
   finally reachable through a real command with a known actor.
   `netbbs/boards/categories.py`/`netbbs/files/categories.py` gained
   `delete_category` (falls back referencing boards/areas/child-
   categories to uncategorized/top-level) and `created_by`-logged
   `create_category` — the latter required adding a new required
   parameter to a function that previously took no actor at all,
   updating ~40 existing call sites across two test files
   mechanically.
4. **Admin UI** (`netbbs/net/admin_flow.py`): new top-level `[M]anage
   boards/areas` option opening `_content_menu` — `[M]essage boards`/
   `[A]reas` (create/list/edit/delete, each board/area's detail screen
   also reaching a `[P]ending posts`/`[P]ending files` approve/reject/
   pin/exempt queue), `[C]ategories` (create/list-with-delete for both
   kinds), `[G]rant moderator`/`[R]evoke moderator` (pick a user, a
   scope — one board, one area, or a local-blanket grant across all
   boards/all areas — and a preset: "Full moderator"
   (EDIT+DELETE+APPROVE) or "Approver only"). Revoke removes a target's
   *entire* existing grant on the chosen scope in one action, not a
   partial per-flag revoke, matching the enable/disable screen's
   existing "one clean toggle" precedent.
5. **Testing:** `tests/test_boards.py`/`tests/test_file_areas.py`
   extended for `update_*`/`delete_*` (including that deleting a board/
   area does *not* touch its category) and audit logging on create;
   `tests/test_board_categories.py`/`tests/test_file_area_categories.py`
   extended for `delete_category`'s uncategorized/top-level fallback;
   `tests/test_moderator_roles.py` gained a dedicated `real_sysop`
   fixture (`SYSOP_LEVEL`, distinct from the pre-existing `sysop`
   fixture in that file, which is level 100 and does *not* trigger the
   bypass — confirmed as its own explicit test) covering the bypass for
   every `object_type`, that `get_grant`/`list_grants_for_object` stay
   unaffected, and that input validation still runs first.
   `tests/test_admin_flow.py` extended with end-to-end flows through
   the real UI: create/edit/delete a board and an area, category
   create/delete, and — the test that actually proves the bypass
   reaches production code, not just the library function in isolation
   — a SysOp with zero prior grants successfully approving a pending
   post and a pending file purely through the admin menu. Full suite
   re-run: **1111 passed, 3 skipped** — actually run, not just
   syntax-checked.

**Flagged, not blocking further work:** same as every prior round's
equivalent note — real interactive verification of the full `[M]anage
boards/areas` tree (creating/editing/deleting real boards and areas,
approving real pending posts/files, granting/revoking moderator status)
from an actual connected client hasn't been done outside this sandbox's
scripted `FakeSession` tests. Worth a direct check from Thiesi's own
machine before considering "SysOp admin tools" as a whole closed out —
channel management remains the one deliberately deferred piece.

## Sign-off notes, round 61 (channel management: [H]annels admin menu — implemented)

Closes out the "SysOp admin tools" line from §15 — the piece round 60
explicitly deferred ("Boards and Areas first, then channels," Thiesi's
own scoping call).

Investigation before implementation found channels needed a narrower
slice of round 60's work than boards/areas did, for two structural
reasons specific to chat:

1. **Channels have no moderated-content/approval workflow.** Chat
   messages aren't even persisted beyond bounded scrollback (see
   `netbbs.chat.channels`' own module docstring) — there's no
   equivalent of a `posts`/`files` "pending" queue to build a UI for,
   so this round has no `_pending_*_screen` counterpart.
2. **Membership admin was already fully exposed.** Unlike board/file
   post-approval (round 60's biggest gap — a fully permission-gated
   library function with zero UI anywhere), `/invite`, `/kick`,
   `/mute`, `/ban`, and `/members` already exist as real in-chat
   commands, gated by the same `ChannelPermission` grants this round's
   admin UI now also assigns. Nothing here duplicates that surface.

What *was* actually missing, confirmed by the same kind of direct
investigation round 60 did for boards/areas: `create_channel`/
`create_category` (channel categories) had zero permission gating and
no admin command path to reach them (test-fixture-only, same as boards
before round 60); no edit or delete existed for channels or channel
categories at all; and `_pick_moderator_scope`/the grant/revoke
screens — already generic across `object_type` in principle — had no
channel branch wired in, despite `ChannelPermission` and the
`has_permission` SysOp bypass already covering `object_type='channel'`
generically since round 60.

1. **`chat/channels.py`**: `update_channel` (full-state replace,
   mirroring `boards.update_board` field-for-field except swapping
   `min_read_level`/`min_write_level`/`moderated`/`max_post_age_days`
   for `min_level`/`hidden`/`members_only`/`allow_member_invites`) and
   `delete_channel` (application-level cascade — no schema `ON DELETE`
   change, same reasoning as round 60's empirically-found SQLite
   `DROP TABLE` cascade hazard: `channel_messages`, `channel_
   restrictions`, `channel_members`, `channel_invitations`, and
   `moderator_grants` scoped to the channel are all explicitly deleted,
   in that order, before the channel row itself, logged first). `topic`
   is deliberately *not* part of `update_channel`'s full-state replace
   — it stays gated by `set_topic`'s own `ChannelPermission.EDIT` check
   and audit trail (round 33), not folded into this SysOp-only action.
   `create_channel` gained a `record_action` call, same as round 60 did
   for `create_board`/`create_file_area`.
2. **`chat/categories.py`**: mirrors `boards/categories.py` exactly —
   `create_category` gained a required `created_by: User` param plus
   audit logging, and `delete_category` falls referencing channels/
   child categories back to uncategorized/top-level rather than
   blocking or cascading.
3. **Admin UI** (`netbbs.net.admin_flow`): `_content_menu` gained
   `[H]annels` (letter chosen to avoid colliding with `[C]ategories`,
   which channel management now also feeds into) opening `_channel_menu`
   — `[C]reate`/`[L]ist` → detail screen with `[E]dit`/`[D]elete`, no
   pending-queue option per point 1 above. `_category_menu` gained a
   third branch (`[H]annel category`) reusing `_generic_category_screen`
   unchanged — that function was already fully parametrized over
   create/list/delete/error-type, so no new category-UI code was needed
   beyond wiring it in.
4. **Moderator grant/revoke scope**: `_pick_moderator_scope` gained
   `[H]annel` (per-object) and blanket-across-all-channels `[Z]`
   options (`[X]`/`[Y]` were already taken by blanket-boards/blanket-
   areas). Channel presets are deliberately different from the board/
   file-area ones — `ChannelPermission` has no `READ`/`WRITE`/`APPROVE`
   bits to begin with (see round 34's original reasoning: chat access
   has no read/write split, and `MODERATE` bundles kick/mute/ban as one
   capability rather than several) — so `_grant_moderator_screen`/
   `_revoke_moderator_screen` now branch on `object_type`: "Full
   moderator" = `EDIT|MODERATE|MANAGE_MEMBERS` and "Moderator only" =
   `MODERATE` for channels, versus the existing `EDIT|DELETE|APPROVE`/
   `APPROVE`-only presets for boards/areas. Revoke converts the
   existing grant's bitmask back through whichever enum matches the
   scope (`ChannelPermission` vs `BoardPermission`) before calling
   `revoke_permissions`, rather than assuming `BoardPermission`
   unconditionally the way the pre-this-round code did (harmless before
   now, since channel scope was unreachable through this screen at
   all).
5. **Testing**: `tests/test_channels.py` extended for `update_channel`/
   `delete_channel` (including a cascade test seeding all five
   referencing tables — scrollback, a mute restriction, a member grant,
   and a moderator grant — via the real `record_message`/`mute_user`/
   `add_member`/`grant_permissions` functions, not raw SQL, confirming
   each is actually gone after delete) and audit logging on create;
   `tests/test_channel_categories.py` extended for `delete_category`'s
   uncategorized/top-level fallback, same shape as round 60's board/
   file-area category tests. `tests/test_admin_flow.py` extended with
   end-to-end flows through the real UI: create/edit/delete a channel,
   channel category create/delete, and — proving the channel-scope
   additions to the grant/revoke screens actually reach production code
   — a SysOp granting/revoking both a per-channel and a blanket-across-
   all-channels grant to a zero-privilege user purely through the admin
   menu.
6. **A real regression, caught only by actually running the full suite
   — not by inspection**: `create_channel`'s new `record_action` call
   (point 1) broke `tests/test_chat_flow_private.py::
   test_msg_is_never_written_to_scrollback_or_moderation_log`, which
   asserted the moderation log was completely empty (`== []`) after a
   `/msg`. That assertion's real intent was "`/msg` adds nothing to the
   log," but its fixture creates the test channel via `create_channel`
   — which, as of this round, legitimately logs its own `create_channel`
   entry first. Fixed by rebaselining the test to snapshot the log
   before sending `/msg` and asserting it's unchanged afterward, rather
   than asserting global emptiness — the same category of "only caught
   by running tests, would have shipped broken otherwise" finding
   rounds 58-60 each flagged in their own sign-off notes. Two other
   tests asserting on the same channel's moderation log
   (`test_channel_topic.py::test_set_topic_is_recorded_in_the_
   moderation_log`, `test_chat_flow_membership.py::
   test_grantaccess_and_revokeaccess_are_logged_in_the_moderation_log`)
   were unaffected — both already filtered by specific action name
   rather than asserting on the log's full contents. Full suite re-run
   after the fix: **1125 passed, 3 skipped**.

**Flagged, not blocking further work:** same as every prior round's
equivalent note — real interactive verification of the `[H]annels` menu
(creating/editing/deleting real channels, channel-category management,
channel-scoped moderator grant/revoke) from an actual connected client
hasn't been done outside this sandbox's scripted `FakeSession` tests.
With this round, "SysOp admin tools" as originally scoped in §15 (user
management, node management, board/area management, channel
management) is now feature-complete pending exactly that kind of real-
world verification — the same three-item list (interactive SSH, real
Zmodem-client interop, and browser-rendered xterm.js) flagged since
Phase 1, plus this admin-tools tree, are the standing "verify from
Thiesi's own machine" backlog going into whatever's next.

## Sign-off notes, round 62 (per-user chat timestamp preference: `/timestamps` — implemented)

Closes the one piece of round 42's own investigation that it explicitly
left open: round 42 point 6 found `netbbs.timeutil.format_for_display`
already had `override_format`/`override_timezone` parameters reserved
for a future per-user value, and `netbbs.user_preferences` (round 38)
already existed as a generic store — concluding "wiring a real per-user
value through... is a small follow-up left for whenever Track 5's UI
actually needs a `/timestamps` command... not done speculatively here."
This round is that follow-up, identified as the clear low-effort
candidate among Phase 2's three remaining gaps (the other two —
ANSI-art login screens and the TUI/fullscreen-editor pair — both need
real design decisions or are a genuinely large architectural piece).

1. **New module `netbbs/chat/timestamps.py`**: `timestamps_enabled`/
   `set_timestamps_enabled` (a thin typed wrapper over
   `netbbs.user_preferences`, mirroring how `netbbs.timeutil` wraps
   `netbbs.config` for the node-wide equivalent settings) plus
   `format_with_preference(db, user, text, created_at)` — the single
   place that combines the preference check, `format_for_display`
   (reusing the existing per-user/node display-timezone/format system
   rather than inventing chat-specific formatting, per round 32 point
   3's own wording), and muted-color styling. Deliberately kept in
   `netbbs.chat`, not `netbbs.timeutil`, since it does ANSI coloring —
   `netbbs.chat.nick`'s `chat_stream_label`/`display_label` already set
   the precedent of a small chat-specific helper combining a lookup
   with its own sanitizing/coloring, reused identically by both
   `netbbs.net.chat_flow` (live chat, scrollback replay) and
   `netbbs.net.login_flow` (mailbox-flushed private messages) so the
   combination logic lives in exactly one place.
2. **A real architectural wrinkle, worth documenting explicitly**: a
   genuinely *per-user* toggle can't be satisfied by rendering a
   broadcast string once at send time — `ChatHub.broadcast` pushes one
   shared string onto every recipient's queue, but whether *that*
   recipient wants a timestamp is a decision only knowable per
   recipient, at receive time. Resolved by loosening `ChatHub.
   broadcast`'s type hint from `str` to `object` (matching `send_to`,
   which was already permissive for exactly this reason — round 37's
   `_KickNotice` sentinel) and introducing a small `_TimestampedNotice`
   envelope (`netbbs.net.chat_flow`, carrying the raw text plus a raw
   ISO timestamp) for every broadcast/`send_to` call this round's scope
   touches. `receive_loop` — the one piece of code that already knows
   both "which session is this" and "which account owns it" — is the
   single place that turns an envelope into a final string, via
   `format_with_preference` against its own session's user. The
   sender's own direct writes (their own message, their own `/me` line)
   apply the same formatting call directly against the sender's own
   preference, since those never go through the queue at all.
3. **Scope, held to round 32 point 3's literal wording** ("live
   messages, replayed scrollback, join/leave events, `/me` actions, and
   private messages") rather than extended to every chat event kind:
   `/topic` change notices, `/nick` change announcements, and mute/ban/
   kick moderation notices are deliberately *not* timestamped live —
   nothing in scope asked for them, and extending coverage there would
   have been scope creep, not a requirement. Scrollback replay is the
   one exception applied uniformly regardless of original event kind
   (moderation-verb and `nick` lines included) — the design doc lists
   "replayed scrollback" as its own category, and a replay of *any*
   persisted event answers the same "when did this happen" question a
   timestamp exists to answer, whether or not the live version of that
   same event kind happens to be timestamped.
4. **Private messages needed both delivery paths threaded through**:
   `_deliver_private_message`'s online path (`ChatHub.send_to`) reuses
   the same `_TimestampedNotice`/`receive_loop` mechanism above for
   free. The offline path (`netbbs.chat.mailbox.MessageMailbox`) is a
   separate lifecycle entirely — `deliver`/`flush` changed from
   carrying bare `str` lines to `(text, created_at)` tuples, formatted
   at the one place they're ever displayed
   (`netbbs.net.login_flow._draw_main_menu`, which gained a `db`
   parameter for exactly this). The recipient there is always the
   flushing session's own user, so — unlike the live broadcast case —
   no per-recipient envelope threading through a shared queue is
   needed, just the same `format_with_preference` call applied once at
   flush time.
5. **`/timestamps on|off|toggle`** (no bare-invocation toggle, unlike
   `/away`): shows current state with no argument, since — unlike
   `/away`'s "typing nothing clears it" — this preference has no
   natural meaning for a bare invocation; the design doc's own wording
   already specifies exactly these three subcommands. Defaults to off,
   confirmed unaffected by `_COMMAND_VISIBILITY` (no entry needed —
   absence from that dict already means "always visible," the same as
   `/away`).
6. **Testing**: new `tests/test_chat_timestamps.py` (library-level:
   preference get/set/default, `format_with_preference`'s enabled/
   disabled behavior) and `tests/test_chat_flow_timestamps.py`
   (integration: the command itself, own-message/scrollback-replay
   prefixing, and — the test that actually proves point 2's
   architecture works, not just that a single user's own preference is
   honored — a scenario with the sender's preference off and the
   recipient's on, confirming the *same* live broadcast renders
   differently for each). Updating existing tests to the mailbox's new
   tuple shape and the hub's now-heterogeneous queue payloads surfaced
   the exact set of places a real client actually touches these paths:
   `tests/test_chat_mailbox.py` (direct tuple-shape rewrite),
   `tests/test_login_mailbox_flush.py` (gained a real `Database`
   fixture — `_draw_main_menu` now genuinely queries it, so the
   pre-existing `object()` placeholder would have raised), `tests/
   test_terminal_sanitization.py` and `tests/test_chat_flow_join.py`
   (both read directly off a `ChatHub` queue, bypassing `receive_loop`
   entirely, and needed to unwrap `_TimestampedNotice` the same way
   `receive_loop` itself does), and `tests/test_chat_flow_membership.py`/
   `tests/test_chat_flow_private.py` (mailbox-tuple indexing fixes).
   Full suite re-run: **1145 passed, 3 skipped**.

**Flagged, not blocking further work:** none specific to this round —
unlike node/board/channel management, nothing here needs a real
connected client to verify; `FakeSession`-driven integration tests
already exercise the actual command dispatch, chat loop, and hub/
mailbox delivery paths this feature touches. Phase 2's remaining gaps
are now down to two: ANSI-art login/welcome screens (needs real design
decisions first — file format/storage, how a SysOp sets one) and the
TUI screen-buffer/fullscreen-editor pair (the largest remaining piece
by design, per round 26).

## Sign-off notes, round 63 (welcome banner: ANSI art login screen, Round A of a three-part skinning initiative — implemented)

1. **Phase 2's last two line items reframed as related, not
   independent — Thiesi's own insight, not a decision made
   unilaterally mid-implementation.** Round 62's note treated "ANSI-art
   login/welcome screens" and "the TUI screen-buffer/fullscreen-editor
   pair" as two separate remaining gaps. Thiesi pointed out they're
   coupled: he wants a SysOp to manage as much as possible *through the
   BBS itself*, including an eventual in-BBS WYSIWYG ANSI art
   viewer/editor (not a full paint program, but proper display +
   editing), and wants the whole BBS eventually skinnable beyond just
   the login screen. This directly echoes round 26's own reasoning for
   the TUI abstraction — pairing it with "the fullscreen editor... the
   actual reason it's needed" specifically so the abstraction isn't
   designed without a real consumer to validate against. A WYSIWYG ANSI
   editor is if anything a *better* validating consumer than the
   originally-envisioned prose editor, since art editing is inherently
   2D/cursor-addressable in a way line-based text editing can mostly
   avoid. Agreed sequencing, confirmed with Thiesi: **Round A** (this
   round) ships *display* of a pre-made ANSI file at login, no TUI
   dependency; **Round B** (future, separate round) builds the TUI
   screen-buffer abstraction + a WYSIWYG in-BBS ANSI editor, designed
   against two real consumers (the ANSI editor and the originally-
   planned prose post/message editor); **Round C** (future, separate
   round) generalizes Round A's display mechanism beyond the login
   screen to other hook points. Round A is deliberately scoped to stand
   alone and ship real value now, without foreclosing B or C — see
   point 5 below for what's deliberately left small rather than
   over-built toward a generalized "skin" mechanism that doesn't exist
   yet.
2. **CP437-fallback decoding, not a required-UTF-8 or raw-bytes
   scheme.** `netbbs.rendering.ansi_art.decode_ansi_bytes` tries UTF-8
   first, falls back to the stdlib `cp437` codec (no new dependency) on
   failure. `cp437` is a total function over all 256 byte values — it
   never raises — so once the fallback is reached, decoding cannot
   fail; verified directly (`test_decode_never_raises_for_any_single_
   byte_value`, a 256-value sweep, plus a handful of deliberately
   invalid-looking multi-byte sequences) rather than assumed from the
   codec's documentation. Real scene-authored `.ans` files are raw
   CP437 and will almost always fail strict UTF-8 decoding (their
   high-bit bytes rarely form valid UTF-8 by chance), making the
   try/fallback a reliable, deterministic heuristic; a SysOp who
   directly authors valid UTF-8/Unicode content gets that path instead,
   automatically. This content is trusted, SysOp-authored, and
   deliberately bypasses `sanitize_text` entirely (which strips every
   Unicode "Control" category character, ESC included — exactly what
   real ANSI art needs to keep for cursor positioning and color) —
   same trust tier as `colored()` output, documented explicitly in
   `ansi_art.py`'s own module docstring so a future reader doesn't
   "fix" this into passing through sanitization and silently destroy
   real art.
3. **Filesystem-only storage, no in-BBS upload in this round** —
   mirrors `netbbs.net.ssh.ensure_host_key`'s established pattern
   exactly: a single well-known file colocated with the database
   (`<db_path>_welcome_banner.ans`), not a `node_config` TEXT column
   (reserved for small string settings) and not the content-addressed
   file-area storage scheme (built for many uploaded files, overkill
   for one node-wide singleton). A SysOp places the file externally (a
   normal ANSI-art-scene tool, or a download) — the enable/disable flag
   lives separately in `node_config` (`netbbs.net.welcome_banner.
   is_welcome_banner_enabled`/`set_welcome_banner_enabled`, not
   `netbbs.config` itself — mirrors `netbbs.chat.scrollback`'s and
   `netbbs.timeutil`'s own precedent of a feature module owning its
   typed config wrapper rather than centralizing every wrapper in
   `netbbs.config`), so a SysOp can revert to the default banner
   without deleting their prepared file. Every failure mode the login-
   time loader (`load_welcome_banner`) can hit — missing file, over the
   256 KiB size cap, unreadable (`OSError`) — falls back to the
   original hardcoded banner silently to the connecting user (never a
   raw error shown to an anonymous pre-auth session) but logged
   server-side at WARNING, so a SysOp can still diagnose a vanished or
   broken file after enabling it.
4. **A real regression, caught only by running the full suite — not by
   inspection.** Ten pre-existing tests across `tests/test_login_
   outcomes.py`, `tests/test_login_presence.py`, and `tests/test_login_
   throttling.py` passed a bare `object()` as `db` to `handle_session`
   — safe before this round, since nothing on those code paths touched
   `db` before the point they intentionally diverged (an early
   monkeypatched auth failure, an idle timeout, a deliberately
   exhausted throttle). `load_welcome_banner(db)` is called earlier in
   `_run_authenticated_session` than any of that — a fresh `AttributeError:
   'object' object has no attribute 'connection'` on every one of those
   ten tests. Fixed by giving each affected test a real `Database(tmp_path
   / "node.db")` fixture in place of `object()`; two more `handle_session`
   call sites (`tests/test_shutdown.py`'s maintenance-mode rejection,
   `tests/test_main_lifecycle.py`'s monkeypatched-around-`_run_
   authenticated_session` spy) were checked directly and confirmed safe
   as-is, not just assumed safe from the absence of a test failure.
5. **Deferred to Round B/C, deliberately, not oversights**: no caching
   (re-reads from disk every login; a future editor's save path would
   need its own invalidation story if this ever becomes worth
   revisiting); size enforcement stays a login-time fallback rather
   than an in-editor error, since there's no "save" path to enforce it
   at yet; the config key and file path are named specifically for the
   login banner (`welcome_banner_*`), not a generic "skin" concept —
   Round C's job is generalizing display to multiple named hook points,
   which will need its own shape (several keys/files, or a small
   table — genuinely unclear yet), and guessing that shape now, before
   Round C's real requirements exist, would repeat exactly the mistake
   round 26 already flagged for the TUI abstraction itself.
6. **Testing**: `tests/test_ansi_art.py` (new) — pure decode-logic
   tests including the never-raises sweep. `tests/test_welcome_
   banner.py` (new) — loader/status/flag behavior across disabled,
   missing-file, oversized-file, valid-UTF-8, valid-CP437, and a
   direct end-to-end `handle_session` smoke test with a missing and an
   oversized banner file, confirming login proceeds normally rather
   than raising — the actual risk this whole design defends against.
   `tests/test_admin_flow.py` extended with the `[W]elcome banner`
   submenu's own flows: option visibility, enable with no/oversized
   file (friendly error, flag stays off), enable with a valid file,
   disable (flag clears, file untouched), and preview in both the
   custom-file and default-fallback states. Full suite re-run after
   fixing the regression in point 4: **1175 passed, 3 skipped**.
7. **An unrelated pre-existing test bug, found opportunistically when
   Thiesi ran the suite on a real POSIX machine** (this sandbox is
   Windows-only, where `tests/test_local_terminal_raw_mode.py`'s
   `pty`/`termios`-based tests are skipped entirely — exactly the kind
   of gap `netbbs.net.local_terminal`'s own module docstring already
   flags): both of that file's tests asserted full byte-for-byte
   equality of the entire termios attribute struct before/after
   `raw_terminal()`'s context manager. On a real pty, one `lflag` bit
   outside the set `tty.setraw()` (CPython's own implementation) ever
   touches came back set that wasn't set before — traced via the exact
   failure diff (`536872395 − 1483 = 2^29`, a single extra high bit)
   to kernel-internal pty line-discipline bookkeeping (the `PENDIN`-
   style "raw input still pending canonical reprocessing" family),
   not anything `raw_terminal()` itself set or is responsible for
   restoring. Confirmed this wasn't a real production bug before
   touching anything: every other field (`iflag`/`oflag`/`cflag`/baud/
   control-characters) matched exactly per the failure's own traceback,
   and `raw_terminal()`'s restore path (`tcsetattr(fd, TCSADRAIN,
   previous)`, capturing and replaying the *full* previous struct) is
   the textbook-correct idiom — switching its restore mode to
   `TCSAFLUSH` would have silently discarded any input a user typed
   right as raw mode exits, trading real behavioral correctness for a
   cosmetically stricter test. Fixed in the test instead: a shared
   `_assert_mode_restored` helper checks `iflag`/`oflag`/`cflag`/cc
   exactly (the fields `tty.setraw()` actually mutates) but only the
   specific `lflag` bits it touches (`ECHO`/`ICANON`/`IEXTEN`/`ISIG`),
   not the whole opaque word. Not independently re-verified on real
   POSIX hardware from this sandbox — flagged for Thiesi to confirm on
   his own next run, alongside this project's standing list of things
   only checkable from real hardware.

**Flagged, not blocking further work:** same category of gap this
project's history already carries for anything involving real terminal
rendering — actual visual verification of ANSI art displaying correctly
(colors, cursor positioning, CP437 glyphs) in a real terminal client
across Telnet/SSH/web hasn't been done outside this sandbox's scripted
`FakeSession` tests, which check content/control-flow but can't confirm
how it actually *looks*. Worth a direct check from Thiesi's own machine
with a real `.ans` file before Round B (the WYSIWYG editor) builds on
top of this. Round B and Round C remain fully unplanned beyond the
shape agreed in point 1 — no implementation decisions have been made
for either.

## Sign-off notes, round 64 (TUI screen-buffer core + WYSIWYG ANSI editor, Round B1 of the skinning initiative — implemented)

1. **Round B split into B1/B2, confirmed with Thiesi rather than
   assumed** — round 63 scoped "Round B" as the screen-buffer core plus
   *both* eventual editors (WYSIWYG ANSI art and the originally-planned
   prose post/message editor) together. Given this is, by design doc
   round 26's own framing, the largest remaining piece of Phase 2 —
   confirmed by direct investigation that nothing like a screen-buffer/
   diff abstraction or structured key-event handling existed anywhere
   in this codebase (`Session.read_key()` discarded arrow keys
   outright; the closest existing "editor" was `_edit_bio`'s plain
   `read_line()`-per-line-until-blank loop) — this was split the same
   way every other multi-part feature in this project has been:
   **Round B1** (this round) ships the screen-buffer core, structured
   key events, and a basic WYSIWYG ANSI editor wired into Round A's
   `[W]elcome banner` screen; **Round B2** (future, separate round)
   reuses B1's foundation for the prose editor. Also confirmed:
   periodic **autosave** to a draft file (nothing else in this codebase
   saves in-progress work on disconnect, but for a real art-editing
   session that's a worse loss than a paragraph of prose), and a
   **cursor + type + color-picker** first-version scope with undo/redo,
   block copy/fill/select, and line/box-drawing tools explicitly
   confirmed as a real, planned *later* phase, not abandoned.
2. **`netbbs.rendering.screen_buffer`**: `Cell`/`ScreenBuffer`/
   `diff_ansi`/`full_render_ansi`, pure and I/O-free, built entirely on
   the existing `netbbs.rendering.ansi` primitives (`move_cursor`,
   `colored`, `clear_screen`) rather than inventing new escape-sequence
   handling. Both diff functions group consecutive same-style cells on
   a row into one `colored()` call rather than one per cell — verified
   directly (`test_diff_groups_consecutive_same_style_cells_into_one_
   run`) rather than assumed, since minimizing write count/size is a
   real saving on the web transport (confirmed by this round's own
   investigation: every `Session.write()` there is one full WebSocket
   message with no server-side batching).
3. **A gap round 63 didn't anticipate, surfaced by this round's own
   investigation**: `decode_ansi_bytes` (Round A) only ever does byte
   decoding — it never interprets a file's embedded cursor-positioning/
   SGR-color escape sequences, since Round A only needed to *display* a
   banner (hand decoded text to a real terminal emulator and let it
   interpret the codes). *Editing* an existing file needs the server
   side to actually know what's in each cell. `netbbs.rendering.
   ansi_parse.parse_ansi_into_buffer` fills this gap — a minimal,
   best-effort ANSI interpreter (CSI cursor movement, SGR color/bold,
   bare CR/LF), scoped the same honest way this project's Zmodem
   implementation already is ("CRC-16 only, no resume, no batch,"
   stated plainly rather than silently gapped).
4. **A real bug found only by testing a full round-trip, not by
   inspection**: `parse_ansi_into_buffer`'s first version eagerly
   advanced to the next row the instant the last column of a row was
   written. Real scene `.ans` art is almost always authored at exactly
   the canvas width (80 columns), so a full-width row immediately
   followed by an explicit CRLF — the overwhelmingly common case, not
   an edge case — double-advanced: once from the eager wrap, once more
   from the CRLF, silently skipping a row. Caught by writing an actual
   `encode -> decode -> parse` round-trip test and finding the last row
   of content missing, not by reasoning about the code. Fixed with
   proper deferred-wrap semantics (a real terminal's own "pending wrap"
   behavior: filling the last column marks a wrap as pending without
   moving the cursor yet; only writing another character forces the
   actual advance, and any explicit cursor move — CR, LF, CSI
   positioning — clears the pending flag with no effect). Both the
   round-trip test and a dedicated regression test
   (`test_full_width_row_immediately_followed_by_crlf_does_not_skip_a_
   row`) lock this in.
5. **Structured key events — the actual foundation a screen editor
   needs, which nothing in this codebase provided before this round.**
   `Session.read_key()` discards every escape sequence outright (no
   line for a cursor to move within in a single-keystroke menu); a new
   `Session.read_editor_key() -> EditorKey` (mirroring how `read_key`/
   `read_line` are already abstract per-transport methods) surfaces
   arrows, Home/End, Page Up/Down (newly added to `char_input`'s
   decode tables), and a real standalone Escape press as first-class
   events, alongside characters/Enter/Backspace/Delete/Tab/Ctrl+letter.
   `TelnetSession`/`SSHSession` delegate to a new `char_input.
   read_editor_key`, sharing the byte-oriented decode tables; `WebSession`
   keeps its own independently-maintained escape decoder (confirmed
   pre-existing, not shared with `char_input`'s) and translates its own
   events to the same `EditorKey` type at the boundary — a known,
   accepted duplication rather than an unscoped refactor to unify two
   already-working transports' decoders.
6. **A second real bug, caught by a failing test, not by inspection**:
   `_read_escape_sequence`'s `None` return is genuinely ambiguous — it
   means both "nothing followed ESC at all" (a real standalone Escape
   press) and "something followed but wasn't in the recognized table"
   (e.g. a modified combo like Ctrl+Up). `read_line`/`read_key` never
   needed to tell these apart (both are already "not a match, keep
   going" for them), but `read_editor_key`'s first version collapsed
   both into `EditorKeyKind.ESCAPE`, meaning any unrecognized escape
   sequence would incorrectly look like the SysOp pressing Escape to
   quit. Fixed by peeking the byte following ESC explicitly (reusing
   the same pushback mechanism `_consume_optional_lf_or_nul` already
   relies on for an analogous lookahead-then-replay need) before
   delegating to `_read_escape_sequence` — only a genuinely empty peek
   becomes `ESCAPE`; anything else, recognized or not, is handled or
   silently discarded the same as it always was.
7. **This round's `Session.read_editor_key()` addition made the method
   abstract, so every concrete `Session` subclass needed one — caught
   immediately by 61 test failures, not discovered later.** Beyond the
   three real transports and the standalone admin CLI's
   `LocalCLISession`, three test-only `FakeSession(Session)` doubles
   (`tests/test_admin_flow.py`, `tests/test_chat_flow_moderation.py`,
   `tests/test_zmodem.py`) needed one too. Two got a `raise
   NotImplementedError` stub, matching their existing `read_byte`/
   `write_raw` precedent (never exercised in those contexts);
   `test_admin_flow.py`'s got a real implementation (needed for this
   round's own `[X] edit` end-to-end tests), extending that file's
   existing single-ordered-input-queue convention with a small sentinel
   vocabulary (`"ENTER"`, `"CTRL+S"`, etc.) rather than adding a second,
   incompatible queue just for editor-driven tests.
8. **`netbbs.rendering.ansi_art.encode_ansi_bytes`**: the save-side
   counterpart to `decode_ansi_bytes`, walking a `ScreenBuffer` and
   emitting SGR changes only where style actually changes (not
   per-cell), each character CP437-encoded with `errors="replace"` —
   always succeeds, matching `decode_ansi_bytes`'s own "cannot fail by
   construction" property — producing a genuine CP437-encoded `.ans`
   file real scene tools expect, not merely something that displays
   correctly in this project's own clients.
9. **A real design correction made during this round's own testing,
   not assumed correct from the plan text**: the plan described the
   glyph picker (for CP437 block/line-drawing characters no keyboard
   can type directly) as something that "becomes what typing places
   until changed again," suggesting `state.current_char` as persistent
   brush state separate from literally-typed characters. The first
   implementation kept a `current_char` field but the actual paint
   dispatch always wrote the *literally typed* character
   (`key.char`), never `current_char` — dead state, caught immediately
   by a failing picker test (`assert 'A' == '█'`) rather than
   surfacing later as a silent no-op feature. Resolved by removing
   `current_char` entirely: selecting a glyph now paints it
   immediately, exactly as if that glyph had been typed — the only
   sensible behavior once the actual constraint is stated plainly: a
   real keyboard has no key that ever sends `EditorKeyKind.CHAR` with
   `char="█"`, so there is no future "typing" event for a lingering
   brush mode to apply to. Ordinary typing continues to place the
   literal typed character (colored per the current fg/bg, which *do*
   remain persistent state — genuinely different from the glyph case,
   since a keyboard letter typed after picking a color should keep
   that color, unlike a keyboard letter typed after picking a glyph,
   which should type itself, not repaint the glyph).
10. **`netbbs.net.ansi_editor.edit_ansi_art`**: deliberately generic —
    knows nothing about "welcome banner" specifically, returning bytes
    for the caller to persist wherever it wants, so Round B2/Round C
    can reuse it unchanged later. Fixed 80x24 canvas (matches Round A's
    own "80 columns is the classic BBS standard" precedent); a 16-color
    palette (not the full 256-color range `colored()` elsewhere
    supports — real scene ANSI art overwhelmingly targets the classic
    16, and it keeps the color picker a single unpaginated screen); a
    genuine independent `asyncio.create_task` autosave loop (not a
    "check between keystrokes" approximation, which wouldn't help if a
    long pause precedes a disconnect) that survives the interactive
    session dying, by design; draft recovery offered on entry when a
    prior disconnect/crash left one behind.
11. **Wired into `netbbs.net.admin_flow`'s `[W]elcome banner` screen**
    as a new `[X] edit` option (letter chosen to avoid the existing
    `[E]nable` collision). The screen owns loading the existing file,
    computing the draft path, and writing a real save back to
    `banner_path(db)` — `edit_ansi_art` itself touches no file but the
    draft.
12. **Testing**: `tests/test_screen_buffer.py`, `tests/test_ansi_
    parse.py` (new) — pure unit tests, including the deferred-wrap
    regression and a full encode-decode-parse round trip.
    `tests/test_ansi_art.py` extended for `encode_ansi_bytes`.
    `tests/test_char_input.py` extended for Page Up/Down recognition
    and `read_editor_key`'s full dispatch, including the ESC-ambiguity
    fix. `tests/test_ansi_editor.py` (new) — `FakeSession`-driven
    integration tests: cursor bounds, typing/wraparound, both pickers
    actually changing what gets painted, save/discard/cancel, draft
    recovery, and autosave writing (or correctly not writing, when
    nothing changed) via an injectable interval so tests don't wait 30
    real seconds. `tests/test_admin_flow.py` extended with the real
    `[X] edit` flow end-to-end, including that a saved edit round-trips
    into `banner_path(db)` and logs an audit entry, and that quitting
    without saving leaves an existing file untouched. Full suite
    re-run: **1253 passed, 3 skipped**.

**Flagged, not blocking further work:** same category of gap as Round
A's own note — actual visual verification of the editor (cursor
movement, painting, the glyph/color pickers, save/load) from a real
connected client across Telnet/SSH/web hasn't been done outside this
sandbox's scripted `FakeSession` tests. Worth a direct check from
Thiesi's own machine before Round B2 (the prose editor) builds on this
same foundation. Round B2 and Round C remain fully unplanned beyond the
shape already agreed in round 63 — no implementation decisions have
been made for either.

## Sign-off notes, round 65 (login username case-sensitivity bug — fixed)

1. **The bug**: every login lookup in `netbbs.auth.users`
   (`get_user_by_username`, the password-login row fetch,
   `authenticate_keypair`, `authorize_public_key`) compared
   `username = ?` under SQLite's default BINARY collation, so a login
   only succeeded if typed with the exact case a username was
   registered with. The `users.username` column's `UNIQUE` constraint
   was likewise case-sensitive, so `"Thiesi"` and `"thiesi"` could
   coexist as two distinct, mutually-invisible accounts — a trap for
   both login and registration.
2. **Fix scoped to avoid this project's biggest available foot-gun**:
   `users` is the referenced *parent* of nine tables' foreign keys
   (several carrying `ON DELETE CASCADE`/`SET NULL` since the SysOp
   hard-delete round), and SQLite's `DROP TABLE` performs an implicit
   `DELETE FROM` first whenever `foreign_keys = ON` (as this project's
   connection always runs) — rebuilding `users` itself via the
   drop/rename table-rebuild pattern rounds 37/40/41/56-57 already used
   for other tables would have cascade-wiped every user's moderator
   grants, channel membership, preferences, and blocklist entries, and
   nulled out post/file authorship, as a side effect of fixing a login
   bug. Deliberately avoided in favor of a plain `CREATE UNIQUE INDEX
   idx_users_username_nocase ON users(username COLLATE NOCASE)` — no
   table rebuild, no drop of the parent, closes both the login lookup
   and the registration-uniqueness half of the bug in one migration.
   The four lookup queries above gained an explicit `COLLATE NOCASE`
   on the comparison (SQLite would pick up the column's own declared
   collation automatically if the column itself were redeclared
   `COLLATE NOCASE`, but since the fix here is index-only, the column
   keeps its original BINARY declaration and the comparison needs its
   own explicit collation to actually use the new index).
3. **Left as-is, deliberately**: any case-variant duplicate usernames
   already present in a database from before this migration are not
   auto-merged — the migration will fail loudly (a `CREATE UNIQUE
   INDEX` violation) rather than silently pick a winner between two
   existing accounts. Acceptable at this project's current
   single-sysop-node stage (§14 dozens-low-hundreds scale, Thiesi as
   sole real user); a real migration-time merge tool would be pure
   speculative scope for a scenario that hasn't occurred.
4. **Testing**: `tests/test_auth.py` gained five cases —
   `test_create_case_variant_duplicate_username_fails`,
   `test_get_user_by_username_is_case_insensitive`, and one
   different-case-succeeds case each for password login, keypair
   login, and `authorize_public_key`. Full suite re-run: **1258
   passed, 3 skipped**.

## Sign-off notes, round 66 (ANSI editor status line overran the canvas width — fixed)

1. **The bug, and how it was found**: Thiesi ran the round B1 WYSIWYG
   ANSI editor over a real telnet session (NetBSD's native telnet
   client, via an SSH-tunneled PuTTY session on the deployment host —
   exactly the kind of real third-party-client verification round 64
   flagged as not yet done from this sandboxed dev environment) and
   saw the status line render as a garbled, ever-growing stack of
   near-duplicate lines instead of a single line updating in place.
   Root cause: `_flush`'s status text (`"Row N/H  Col N/W  fg=... bg=...
   Ctrl+G glyph  Ctrl+P fg  Ctrl+B bg  Ctrl+S save  Esc quit"`) is 98-99
   characters even in its shortest form (`fg=White bg=default`) and
   longer with any real palette selection (e.g. `"Bright Magenta"`) —
   comfortably past the 80-column canvas width `_flush` implicitly
   assumed it would fit in, on every single redraw, not just an edge
   case. A real terminal auto-wraps text that overruns the current
   line, so the overflow landed on the row *below* the status line;
   `_flush`'s `clear_line()` only ever clears the status row itself, so
   that spillover was never erased and simply accumulated fresh
   garbage underneath on every subsequent keystroke's redraw — matching
   exactly what Thiesi saw.
2. **Fix**: clip the assembled status text to `state.buffer.width`
   before writing, via the existing `netbbs.rendering.reflow.truncate`
   helper — already the established pattern for this exact class of
   problem elsewhere in the codebase (`netbbs.net.picker` truncates
   list lines to `session.terminal_width`). Canvas width was chosen
   over the real negotiated terminal width deliberately: this editor's
   entire rendering model already assumes the real terminal is at
   least as wide as the canvas it's editing (the canvas itself would
   render incorrectly first, well before the status line, if that
   assumption were violated) — so the canvas width is the correct
   ceiling `_flush` already has in hand, not a new dependency.
3. **Left as-is, deliberately**: no change to the status line's
   *content* — it isn't shortened or abbreviated, since a real 80+
   column terminal (the overwhelming common case, and this project's
   assumed floor per the existing canvas-width default) always sees it
   in full; truncation only ever bites on a narrower-than-assumed
   terminal, where losing the tail end of the shortcut hints is a much
   smaller problem than the corruption this round fixes.
4. **Testing**: `tests/test_ansi_editor.py` gained
   `test_status_line_never_exceeds_the_canvas_width`, which drives the
   real foreground/background color pickers to `"Bright Magenta"` (the
   longest palette name) and asserts every status-line redraw, with SGR
   codes stripped, stays within the canvas width — confirmed to fail
   against the pre-fix code (98 columns on the *default* colors alone,
   no picker interaction needed to trigger it) and pass after. Full
   suite re-run: **1259 passed, 3 skipped**.

## Sign-off notes, round 67 (ANSI editor quit confirmation now single-keystroke; Telnet TCP_NODELAY — fixed)

1. **ANSI editor quit confirmation, single keystroke**: `_confirm_quit`
   (the "Unsaved changes. [S]ave, [D]iscard, or [C]ancel?" prompt
   round B1 added) read a full line via `session.read_line()`,
   requiring Enter — the only sub-interaction in the entire editor
   that did, everything else (cursor movement, typing, Ctrl+combos,
   Esc itself) already dispatches on the keystroke alone. Switched to
   `session.read_key()`; same `s`/`d`/else-is-cancel mapping as
   before, just without waiting for a line terminator that was never
   part of the editor's own interaction model to begin with.
   `tests/test_ansi_editor.py` gained
   `test_quit_confirmation_acts_on_a_single_keystroke_without_enter`,
   which makes `read_line` raise on the scripted session to prove
   `_confirm_quit` never calls it — a regression here would otherwise
   pass silently, since `FakeSession`'s shared input queue doesn't
   itself distinguish which read method consumed a given token.
2. **Telnet bell/echo not reaching a real client — root cause and
   fix**: Thiesi reported the round B1 ANSI editor status line
   corruption (round 66) *and*, separately, that invalid main-menu
   keystrokes stopped ringing the bell and just printed the pressed
   key, over a real telnet session (PuTTY -> SSH -> NetBSD's native
   `telnet` client -> this node, exactly the kind of real-client
   verification round 64 flagged as not yet done here). Extensive
   live testing (a real `TelnetServer` instance driven by a script
   speaking actual Telnet IAC negotiation, not just raw bytes)
   confirmed the server always puts the correct bytes on the wire --
   byte-identical between the commit immediately before and the
   commit introducing round B1's TUI work, ruling that commit out
   directly rather than by inspection alone -- and `printf '\a'`
   confirmed Thiesi's terminal itself rings a bell fine. The actual
   gap: `netbbs.net.telnet.TelnetServer` never set `TCP_NODELAY` on
   accepted sockets. This server took over character-mode input
   entirely (this module's own docstring, from the original Backspace/
   ^M fix) specifically so every keystroke's echo -- and a bare bell
   -- goes out as its own small, immediate write; Nagle's algorithm
   (on by default) is a well-documented source of exactly that traffic
   shape being held back on a real network path instead of sent
   promptly, unlike over a bare loopback `asyncio` test client where
   the effect is negligible -- which is exactly why the live tests
   above showed correct bytes while Thiesi's real client still didn't
   render them. Fixed by setting `TCP_NODELAY` on every accepted
   connection's socket in `TelnetServer._handle_connection`, the
   standard fix for any interactive character-mode server.
3. **Left as-is, deliberately**: SSH (`netbbs.net.ssh`, via `asyncssh`)
   and the web transport (`netbbs.net.web`, WebSocket framing) were
   not touched -- neither was implicated by Thiesi's report, and both
   already carry per-message framing overhead (SSH channel packets,
   WebSocket frames) that isn't the same bare-single-byte-write shape
   this fix targets. Worth the same fix if a real report ever
   implicates them, not preemptively.
4. **Testing**: `tests/test_telnet.py` gained
   `test_accepted_connections_have_tcp_nodelay_set`, spinning up a
   real `TelnetServer`, connecting a real client, and asserting the
   accepted socket's `TCP_NODELAY` option is `1`. Full suite re-run:
   **1261 passed, 3 skipped**.

## Sign-off notes, round 68 (invalid single-keystroke menu input: no more piled-up echoed characters — revises round 52)

1. **What changed**: with round 67's TCP_NODELAY fix confirmed working
   (bell now fires reliably), Thiesi's remaining, separate complaint
   turned out to be real too: an invalid single-keystroke menu choice
   left the pressed character sitting on screen, and repeated invalid
   presses piled up ("Choice: hhhhhhxxxxxxxh"). Round 52 explicitly
   accepted the character showing up once (echo is `read_key`'s job,
   not `login_flow`'s, and reprinting the prompt added nothing) but
   never actually revisited whether the echoed character *staying on
   screen* was itself the right call -- in practice, over a real
   client, an invalid keystroke now visibly and permanently marks up
   the prompt line instead of being cleanly rejected. Fixed by having
   the bell also erase the character(s) it's rejecting:
   `netbbs.rendering.ansi.reject_keystroke(count=1)` returns
   `("\b \b" * count) + "\a"` -- backspace, overwrite with a space,
   backspace again (repeated for multi-character reads), then the
   bell -- and every bare `session.write("\a")` bell-only call site
   across `netbbs.net.login_flow`, `netbbs.net.admin_flow`, and
   `netbbs.net.picker` (23 sites, 3 files) now calls this instead.
2. **Why erase-after-echo instead of suppressing the echo**: the
   character is already on screen inside `read_key()` itself by the
   time any of these dispatch loops see it and can judge validity
   (round 52's own reasoning for why echo is transport-level, not
   login_flow's) -- there is no hook to withhold it in the first
   place without a much larger change to `read_key`'s architecture
   (echo timing shared by ~50 call sites across the whole menu
   system, most of which are for *valid* keys where immediate echo is
   correct and wanted). Erasing after the fact gets the same visible
   outcome (nothing suspicious lingers from a rejected keystroke)
   without touching that architecture or how any valid keystroke
   behaves.
3. **Erase count varies by call site, verified individually, not
   assumed uniform**: every ordinary single-key menu dispatch (all of
   `admin_flow.py`, `login_flow.py`'s `_main_menu`/`_show_board`/
   `_edit_profile`, and `picker.py`'s page-boundary/unrecognized-key
   branches) rejects exactly one echoed character. `picker.py`'s
   two-digit item-selection path is the one exception: by the time
   either of its two invalid branches (non-digit second character;
   both digits valid but the number is out of range) is reached, two
   characters are already echoed, so those two call sites pass
   `reject_keystroke(2)`. `file_flow.py`'s single remaining bare-bell
   call site was deliberately left untouched -- it's reached via
   `read_line()`, not `read_key()` (that screen accepts multi-character
   `/download <filename>` commands, documented in `_show_area`'s own
   docstring as the reason it can't use single-keystroke dispatch),
   so by the time it's reached, Enter has already terminated the line
   and the terminal's cursor has already moved to a new one -- there's
   nothing left inline to erase.
4. **Testing**: every test asserting an exact bell-only byte sequence
   from round 52 was updated to the new erase-and-bell sequence rather
   than left passing on a stale assumption --
   `tests/test_menu_invalid_key.py` (both tests),
   `tests/test_admin_flow.py::test_invalid_key_writes_only_a_bell` and
   `::test_node_option_hidden_without_node_controls`, and
   `tests/test_picker.py::test_repeated_invalid_keys_produce_nothing_but_an_echo_and_a_bell`
   (updated to expect `b"z\b \b\a"`/`b"y\b \b\a"` over the real wire,
   echo included, rather than the non-echoing `FakeSession` the
   `login_flow`/`admin_flow` tests use). Tests that only checked bell
   *presence* as a substring needed no change. Full suite re-run:
   **1261 passed, 3 skipped**.

## Sign-off notes, round 69 (nano keybindings; post editing; prose editor round B2 — implemented)

Three related pieces of work, confirmed with Thiesi across several
rounds of discussion before any of it was written, then implemented
together: (1) nano-style keybindings, applied retroactively to the
round B1 ANSI editor and shared with the new prose editor; (2) the
post-editing data model this needed answered first, since it collided
with content-addressing; (3) the fullscreen prose editor itself
(design doc round 63/64's "Round B2"), plus the per-user opt-in
preference and its wiring into composing a post and editing a bio.

**1. Nano keybindings (round B1 ANSI editor, retroactive):** Ctrl+X
quit (was Esc), Ctrl+O save (was Ctrl+S — sidesteps that key's legacy
terminal-XOFF baggage as a side effect, not the primary motivation),
Ctrl+T glyph picker (was Ctrl+G, which collides with nano's own Help
binding — freed up, not wired to anything yet). Ctrl+P/Ctrl+B
(foreground/background color pickers) kept as-is: nano has no color
concept to defer to, so no collision existed to resolve. A bare Esc
press is now a genuine no-op (nano treats it as a Meta-combo prefix,
not "exit"), locked in by a new regression test
(`test_bare_escape_no_longer_quits`) rather than left unverified.

**2. Post editing — the data model question, settled before any editor
code, per this project's design-before-code convention:**
1. **The core tension**: `posts.post_id` is a content hash computed
   from the body itself (`netbbs.boards.content_id.compute_content_id`,
   design doc §7). An in-place `UPDATE` on edit would leave a row's own
   `post_id` silently mismatched against its current content, and
   would orphan any existing reply's `parent_post_id` (a specific,
   fixed reference). Resolved the way §13 already described for
   *moderator* edits on Linked boards ("a new, signed event that
   references and amends" the original) — generalized here to local
   self-edits too, confirmed with Thiesi as the "do it properly, not
   the simpler in-place shortcut" option over two others (in-place
   mutation; defer editing entirely).
2. **Schema**: `posts.root_post_id` (a post's own `post_id` if never
   edited; the *original* post's `post_id` for every edit of it, not
   the immediately-preceding revision) and `posts.edit_of_post_id`
   (the specific immediate predecessor each edit amends — kept for a
   future edit-history view, not surfaced anywhere yet). Plain `ADD
   COLUMN`, not the drop/rebuild pattern rounds 37/40/41/56-57/60 used
   for other tables — `posts` is a live parent of several tables' own
   foreign keys, and round 60 already found the hard way that SQLite's
   `DROP TABLE` (that pattern's first step) applies its own cascade/
   SET-NULL side effects to *any* row still referencing the dropped
   table, independent of that column's own declared `ON DELETE`
   behavior; avoided entirely by never dropping `posts` here.
3. **`netbbs.boards.posts.edit_post(db, post, board, *, subject, body,
   edited_by)`**: allowed for the post's own original author with no
   grant needed (this project's first "you may act on it because you
   own it" concept for posts — everything else here is purely grant-
   based), or anyone holding `BoardPermission.EDIT`, matching the
   existing moderator model. Always inserts a fresh row; re-resolves
   the actual current approved version itself via `post.root_post_id`
   rather than trusting the passed-in `post.post_id` for the new row's
   `edit_of_post_id` — necessary because a caller's `Post` may already
   be root-identified (see point 4) rather than the immediate
   predecessor, if the post's been edited more than once.
4. **`list_posts_page`/`list_pinned_posts` resolve each result to its
   root's identity, not the row shown.** `post_id`/`created_at`
   (and every other identity/curation field — `pinned`,
   `exempt_from_expiry`, `author_*`) always come from the *root* row;
   only `subject`/`body`/`status` are substituted from whichever row
   sharing that `root_post_id` is the newest currently `'approved'`
   one, via a new `is_edited` flag on `Post` telling callers which
   happened. This is what makes editing invisible to pagination:
   cursors and feed position never move on edit, confirmed by a
   dedicated test that edits the *older* of two posts and asserts it
   doesn't jump ahead of the newer one. A root only needs *some* row in
   its chain currently approved to stay listed — not the root row
   itself, which can independently age out via the existing expiry
   sweep (round 35) while a fresher edit keeps the logical post alive,
   the same way editing an old post would refresh it in any ordinary
   forum. On a moderated board, an edit re-enters moderation exactly
   like a new post (starts `'pending'`); the feed keeps showing the
   last-*approved* content until the edit itself is approved — an edit
   must not be a way to bypass moderation.
5. **A real, pre-existing bug found during testing, unrelated to this
   round's own changes — flagged, not fixed here:** deleting a post
   that still has a live reply referencing it via `parent_post_id`
   already raises `sqlite3.IntegrityError: FOREIGN KEY constraint
   failed` on current `main`, independent of anything built in this
   round (confirmed by reproducing it against `parent_post_id` alone,
   with none of this round's new columns involved). `_sweep_expired_
   posts`'s hard-delete step has apparently never been exercised
   against a post with a surviving reply. Not fixed here — the right
   answer (does the reply lose its parent reference? get excluded from
   the sweep? something else?) is its own design decision, out of
   scope for a round about editing. This round's own tests route around
   it (age a post just past `'expired'`, never all the way to the
   delete threshold) rather than silently relying on undefined
   behavior.
6. **Testing**: new `tests/test_post_editing.py` (12 cases — chain
   integrity across repeated edits, both authorization paths, the
   moderated-board re-entry behavior, the expiry interaction, pinned-
   post resolution). Caught and fixed two of its own early mistakes,
   not shipped: a wrong test expectation for `logical_position`'s
   row-index clamping (unrelated module, see point 8), and several
   tests initially flaky from two posts landing on the exact same
   microsecond `created_at` — the identical "two nearby timestamps
   need to provably differ" hazard rounds 20/21 already document,
   fixed the same way (explicit backdating), not by changing production
   code, since arbitrary-but-deterministic tie-breaking under a genuine
   same-microsecond collision is already an accepted class of edge case
   elsewhere in this codebase.

**3. The prose editor itself (design doc round 63/64's "Round B2"):**
1. **A genuinely different editing core from the round B1 ANSI
   editor, not a reskin** — confirmed by investigation before writing
   any code: the ANSI editor is a fixed-grid paint tool (arrows move a
   clamped cursor, typing overwrites a cell, no insertion, no wrap);
   prose needs real insert-mode text, word-wrap, and scrolling for
   content taller than the screen. Only the *shell* is shared (control
   loop shape, nano keybindings, quit-confirm, autosave, the screen-
   buffer/diff redraw discipline) — confirmed during B2's own planning
   research, not assumed from B1's "deliberately generic/reusable"
   framing alone.
2. **New `netbbs.rendering.prose_buffer`**: pure, I/O-free, mirroring
   `screen_buffer.py`'s own "no session/terminal dependency" shape.
   `ProseBuffer` holds logical lines (exactly what the SysOp typed
   between real Enter presses — never auto-rewrapped) plus a cursor;
   every edit operation (`insert_char`/`insert_newline`/`backspace`/
   `delete`/`move_left`/`move_right`/`move_home`/`move_end`) works
   purely on that logical model, entirely unaware of word-wrap.
   `wrap_lines` is the only place wrapping enters the picture, using
   the same stdlib `textwrap` primitives `netbbs.rendering.reflow`
   already relies on for consistency between what's shown while
   editing and what a posted body looks like once reflowed for
   display — but deliberately *not* `reflow()` itself, which collapses
   and rewraps a whole finished multi-paragraph text, wrong for
   something being actively edited a character at a time.
3. **A real bug caught by an exhaustive round-trip test, not by
   inspection**: `visual_position`/`logical_position` (converting a
   logical cursor position to its on-screen row/column and back) failed
   to round-trip whenever the cursor landed exactly on a consumed
   wrap-point space — `textwrap` discards the separating whitespace
   between two wrapped segments, so a cursor sitting exactly there
   (a real, reachable position — typing to the end of a wrapped row
   lands here) matched neither segment under a naive half-open range
   check, falling through to a nonsense fallback. Caught by a test
   that fuzzes every cursor position across several wrapped, blank, and
   short lines and asserts the round trip holds for all of them — a
   narrower test targeting only "typical" positions would have missed
   it, exactly the same "test at the actual boundary, not just the
   common case" lesson this project's history keeps re-learning. Fixed
   by making the matching range inclusive of a row's own end position.
4. **`netbbs.net.prose_editor.edit_prose`**: same control-loop/
   autosave/quit-confirm shape as `netbbs.net.ansi_editor.edit_ansi_art`
   (see point 1), painting each redraw's visible window of wrapped rows
   into a `ScreenBuffer` purely for the existing diffed-render
   machinery — text editing itself never touches `ScreenBuffer`
   directly. Up/Down move by *visual* (soft-wrapped) row, not logical
   line — pressing Down from the middle of a long wrapped paragraph
   moves one screen line, not past the whole paragraph in one keypress,
   confirmed by a dedicated test. **Viewport size is the session's own
   negotiated terminal size** (`terminal_width`/`terminal_height`, with
   a 40x10 floor matching design doc §4's general rendering floor), not
   a fixed canvas — a deliberate difference from the ANSI editor, where
   80x24 is the *content's* own dimensions; prose has no fixed size of
   its own, so this follows the session-adaptive sizing
   `netbbs.net.picker` already established (round 16) rather than
   inventing a new fixed default.
5. **Explicit V1 scope boundary, not silently dropped**: no cut/copy/
   paste, no search/replace, no syntax highlighting or spell-check —
   nano has all of these, but nothing this round's planning discussion
   settled required them, and building them now would be guessing at
   scope nothing asked for, the same restraint round 64 already applied
   to the ANSI editor's own "undo/redo, block copy/fill/select" list.
   Worth its own follow-up if actually wanted.
6. **New `netbbs.net.editor_preference`**: a per-user "compose with the
   fullscreen editor" opt-in, defaulting off, the exact same thin-
   typed-wrapper-over-`netbbs.user_preferences` shape
   `netbbs.chat.timestamps` already established for `/timestamps`.
   Surfaced as a new `[F]ullscreen editor` toggle on the Profile screen
   (confirmed with Thiesi over a chat-style slash command — this
   preference has nothing to do with chat, so it belongs where bio/
   visibility settings already live, not reachable only from inside a
   channel).
7. **Wired into composing a new board post and editing the bio** — the
   two candidates identified during B2's own planning survey (posts:
   the biggest real gap, previously single-line only despite multi-
   paragraph display; bio: previously a crude 6-line repeated-
   `read_line` loop). A new shared `_compose_body` helper in
   `netbbs.net.login_flow` is the one place that branches on the
   preference; `set_bio`'s own existing `MAX_BIO_LINES` validation is
   still the sole place that cap is enforced, not duplicated in the
   editor. **Not wired in this round, deliberately flagged rather than
   guessed at**: an `[E]dit` entry point on an *existing* post from the
   board-reading screen — `_render_board_page` has no mechanism today
   for selecting one specific post out of a displayed page at all
   (posts aren't numbered/selectable, just listed), and inventing a
   selection scheme without the same care design doc round 16 gave the
   picker's own "how do you pick one item from a list" problem would
   be exactly the kind of unilateral mid-implementation design call
   this project's process exists to avoid. The `edit_post` backend
   (point 2 above) is fully built and tested; only its UI entry point
   remains.
8. **A test-authoring mistake caught by its own failure, not shipped
   unnoticed**: an early integration test scripted a leading "b" (back)
   keystroke before composing a post against a freshly-created, empty
   board — `_show_board` skips its own `read_key()` reading loop
   entirely when a board has no posts yet (straight to the compose
   prompt), so that "b" was silently consumed as the post *subject*
   instead, and the next scripted token fed into the fullscreen
   editor's first keystroke read as one long multi-character "typed"
   insertion. Caught immediately by the resulting `AssertionError`
   once the input queue ran dry, not a silent false-pass — fixed in
   the test's own scripted input, not the code, which was correct
   throughout.
9. **Testing**: `tests/test_prose_buffer.py` (31 cases — wrap
   correctness including the round-trip fuzz test above, every
   `ProseBuffer` edit operation including line-merge boundaries),
   `tests/test_prose_editor.py` (16 cases — typing, word-wrap-aware
   navigation, save/quit/cancel, draft recovery, autosave), `tests/
   test_editor_preference.py` (4 cases), `tests/
   test_login_flow_fullscreen_editor.py` (10 cases — the Profile
   toggle, both post-composition paths with an exact saved-content
   assertion, bio editing and pre-fill, and the fullscreen-cancel-
   means-no-post path). Every pre-existing directory/board UI test
   re-run unchanged and still passing, confirming the opt-in-default-
   off preference leaves every existing account's behavior untouched.
   Full suite re-run: **1331 passed, 3 skipped**.

**Flagged, not blocking further work:** the pre-existing `parent_post_id`
delete/FK bug (point 2.5) and the edit-existing-post UI entry point
(point 3.7) are both real, known gaps going into whatever's next —
neither silently papered over nor guessed at unilaterally.

## Sign-off notes, round 70 (the two round 69 gaps closed out — implemented)

1. **The pre-existing FK delete bug (round 69 point 2.5), fixed at the
   root round 60 already established for this exact class of
   problem.** `_sweep_expired_posts`'s hard-delete step now excludes
   any post still referenced by another live row — as a reply's
   `parent_post_id`, an edit chain's `root_post_id`, or a later edit's
   `edit_of_post_id` — via a `NOT EXISTS` clause, rather than changing
   `parent_post_id`'s `ON DELETE` behavior (which would need the drop/
   rebuild migration pattern, and round 60 already found that
   rebuilding `posts` specifically — a live parent of several other
   tables' own foreign keys — risks SQLite's `DROP TABLE` cascading
   into *all* of those relationships at once, not just the one column
   being fixed). A referenced post simply stays `'expired'`
   indefinitely instead of being purged — already a valid, harmless
   state (delisted from browsing, still individually reachable via
   `get_post`), not a new one this introduces. Two regression tests in
   `tests/test_post_lifecycle.py` (the reply case and the edit-chain
   case), plus a standalone reproduction confirming the exact
   pre-existing failure against unmodified `parent_post_id` alone,
   with none of round 69's new columns involved, before touching
   anything.
2. **The edit-existing-post UI entry point (round 69 point 3.7).**
   `_render_post_page` now numbers each post on its page (`[1]`..`[N]`,
   page-relative only — not a stable identity across page changes) and
   marks an edited one `(edited)`. A new `[E]dit` option on the board-
   reading screen — shown only when at least one post on the current
   page is actually editable by the viewer (`_can_edit_post`, the exact
   same author-or-`BoardPermission.EDIT` rule `edit_post` itself
   enforces, checked *before* prompting for any new content so a
   rejection happens immediately, not after composing a whole
   revision) — prompts for a page-relative post number, then reuses
   the existing subject-with-current-default pattern
   (`admin_flow.py`'s `_edit_board_screen`: `"Subject [{current}]:
   "`) and `_compose_body` (round 69) for the body, pre-filled with
   the post's current content either way. Deliberately not the real
   picker (`netbbs.net.picker.pick_item`) — a board page is at most 5
   posts, too small to justify it, matching how the ANSI editor's own
   glyph/color pickers are the only place in this codebase that
   *does* reach for the real picker over inline numbering.
3. **A real UX regression caught and fixed before it shipped, not
   discovered after**: the first version simply re-fetched the
   *newest* page after every edit, which meant editing a post while
   browsing older history silently bounced the SysOp back to page one
   — an unrelated side effect nothing asked for. Fixed by having
   `_show_board` track which cursor produced the page currently on
   screen (`page_anchor`, updated at every navigation step, reused
   by `_edit_existing_post`'s own post-edit refresh) — editing a post
   never moves it in the feed to begin with (round 69 point 4), so the
   fix only needed to stop `_show_board` from discarding its own
   navigation state, not anything in the pagination model itself.
   Confirmed by a dedicated test: back one page, edit the post shown
   there, assert the older page's own content is still what's on
   screen afterward.
4. **The plain (non-fullscreen-editor) body-entry path gained real
   edit support it never needed before**: composing a new post never
   had anything to pre-fill, but editing does, and a whole body is too
   long to inline into a `"[current]: "`-style prompt the way the
   subject field can. `_compose_body`'s plain-line fallback now shows
   the current body as read-only context immediately above the prompt
   when editing, and — matching round 69's own `initial_text`
   pre-fill contract for the fullscreen path — treats a bare Enter as
   "keep it unchanged," not "replace it with an empty body."
5. **Testing**: `tests/test_post_lifecycle.py` gained the two FK-fix
   regression tests above (point 1). `tests/
   test_login_flow_fullscreen_editor.py` gained 6 new cases: the
   `[E]dit` option correctly hidden when nothing on the page is
   editable, editing via both the plain and fullscreen paths (the
   fullscreen case confirming the editor really was pre-filled with
   the existing body, not started blank), a cancelled fullscreen edit
   leaving the post untouched, an invalid post-number rejection, and
   the page-position-preservation regression from point 3. Full suite
   re-run: **1339 passed, 3 skipped**.

**Nothing further flagged as outstanding from this pair of rounds** —
both round 69 gaps are now closed. Round B2's own remaining unplanned
scope (cut/copy/paste, search/replace, wiring the fullscreen editor
into anything beyond posts/bio) remains exactly as round 69 already
described: real, deliberate, and not guessed at ahead of an actual
need.

## Sign-off notes, round 72 (a second round-2 code-review pass — 8 issues: #28, #29, #31, #32, #34, #36, #42, #43 — fixed)

A second automated review pass over the post-Phase-2 security-hardening
work (the same reviewer that filed the original round of issues closed
out earlier). It reopened four already-"fixed" issues with remaining
gaps, and filed two new ones, all closed in commit `b6ee18c`:

1. **#28 (reopened): invitation acceptance still had two real gaps.**
   `accept_invitation()` now wraps its SELECT/INSERT/UPDATE in an
   explicit `SAVEPOINT`, rolling back on any failure instead of leaving
   a partially-applied state vulnerable to a later unrelated commit;
   returns `bool` instead of silently no-op'ing, and `_handle_join`
   treats a failed acceptance (`False`, or the narrower race case,
   `MembershipError`) as authorization failure instead of switching
   channels off an earlier, now-stale `has_pending_invitation()` check.
2. **#29 (reopened): cross-process disable/delete revalidation only
   ran at the main-menu boundary.** `account_still_active()` (moved out
   of `login_flow.py`, a private `_main_menu`-only helper, into
   `netbbs.auth.users` — `chat_flow.py` can't import from
   `login_flow.py`, the dependency already runs the other way) is now
   also checked in `chat_flow`'s send loop, before every message/
   command, not just at the main menu.
3. **#31 (reopened): `ChatHub`'s queue-overflow policy could swallow a
   kick/ban/revocation notice itself.** `_deliver`/`send_to` gained a
   `priority` flag: on overflow, a priority event now occupies the
   freed queue slot itself (evicting ordinary traffic), instead of a
   `QueueOverflowNotice` taking that slot the way ordinary chat traffic
   still does. `_kick_live_sessions` (shared by `/kick`, `/ban`, and
   `/revokeaccess`) now passes `priority=True`.
4. **#32 (reopened): the new-post UI didn't catch its own domain
   rejection.** `_compose_new_post()` now catches `PostError` around
   `create_post()` (an oversized subject can clear the line editor's
   4096-char cap but still exceed the 300-byte domain limit), mirroring
   `_edit_existing_post()`'s existing handling instead of crashing the
   session.
5. **#34 (reopened): the bulk-transfer idle timeout had three
   remaining gaps.** `_read_subpacket()` only ever timed the *first*
   byte of each loop iteration — the byte following a lone `ZDLE`, and
   both CRC bytes after the terminator, could still stall forever. A
   new `_read_bulk_raw_byte()` helper, and an injectable `read_raw`
   parameter on `_read_zdle_byte()` (used only for the bulk phase — the
   header-phase default stays untimed, correctly, since it's already
   bounded as a whole by `_wait_for_header`'s own `_HANDSHAKE_TIMEOUT`),
   close all three.
6. **#36 (reopened): activity/volume ranking only checked *stored*
   status, not *effective* expiry.** Expiry sweeping is lazy — a post/
   file already past its board/area's `max_*_age_days` but not yet
   swept by something actually browsing that resource still counted
   toward both rankings. `list_boards()`/`list_file_areas()`'s
   aggregate queries now evaluate effective expiry inline
   (`exempt_from_expiry = 1 OR max_age_days IS NULL OR
   julianday(created_at) >= julianday(now) - max_age_days`), with no
   mutating sweep added to either listing function.
7. **#42 (new): offline channel invitees had no notification
   mechanism at all.** The mailbox `_deliver_private_message` uses is
   session-addressed and ephemeral (round 46/Track 5e) — an invitee
   with no active session at `/invite` time was silently never
   notified, though the durable `channel_invitations` row was always
   created regardless. `netbbs.chat.membership.
   list_pending_invitations_for_user()` is the durable, account-wide
   fix; `login_flow` announces a brief count once per login and offers
   an `[I]nvitations` main-menu screen (shown only while something's
   actually pending) with full detail. `/invite`'s live push is now
   gated on `presence.is_online` first (matching `/msg`'s existing
   check), so "(sent to X)" is no longer shown when nothing was
   actually delivered live.
8. **#43 (new): editor disconnect cleanup could leak the autosave
   task.** `edit_ansi_art()`/`edit_prose()`'s `finally` block wrote the
   screen-clear before cancelling the autosave task — a
   `SessionClosedError` from a genuinely dead transport (the write
   itself failing) skipped cancellation entirely. Cancellation is now
   unconditional; the screen clear is best-effort, wrapped in its own
   `try/except SessionClosedError`.
9. **Testing**: regression tests for all eight, several exercising
   real SQLite locking/rollback behavior (`accept_invitation`'s
   savepoint, via a proxied connection object rather than mocking
   around it) or genuine `asyncio.all_tasks()` leak detection (#43).
   Full suite re-run: **1527 passed, 3 skipped**.

## Sign-off notes, round 73 (the reviewer's round 72 follow-up — 3 remaining gaps in #28/#29/#34 — fixed)

The same reviewer verified round 72 and found three further gaps —
two narrow, one a real architectural fork each for #29 and #34 that
Thiesi picked a direction for before implementation (see the round 73
design-doc sign-off note for that decision and its reasoning).

1. **#28, part 1: `accept_invitation()`'s own fix had a bug.**
   `RELEASE SAVEPOINT` on an outermost savepoint already commits it —
   the trailing, unconditional `conn.commit()` right after it was
   either redundant in that case, or, if `accept_invitation()` is ever
   called inside a caller's own already-open transaction, actively
   wrong: it would commit that *entire enclosing transaction* early,
   directly contradicting the function's own "safe to nest" claim.
   Fixed by simply removing the stray `commit()` — releasing an
   outermost savepoint already persists this function's own work;
   releasing a nested one correctly leaves the enclosing transaction's
   boundary alone. Regression test: an unrelated write left
   deliberately uncommitted before calling `accept_invitation()`
   (simulating a caller's own wider transaction), confirmed to roll
   back together with it afterward — would have stayed committed
   against the pre-fix code.
2. **#28, part 2: the real access-control gap — channel entry never
   went through `/join` at all if picked from the browse list.**
   `browse_channels()` → `_pick_channel()` hands any *visible* channel
   (a non-hidden `members_only` one, or a hidden one the user merely
   holds a pending invitation for — both deliberately still listed, per
   `_visible_channels_for`'s own round 33 point 9 reasoning) straight
   to `_chat_loop()`, which only ever checked bans —
   membership/invitation enforcement lived *exclusively* inside
   `_handle_join`, `/join`'s own command handler. Picking such a
   channel directly from the browse list, never typing `/join`, used
   to grant entry with no check at all, and never consumed the
   invitation either — leaving it perpetually pending while the
   invitee kept re-entering through the picker. Fixed with one shared
   `_authorize_channel_entry()` (open channel/existing member → allow;
   valid pending invitation → atomically accept via the now-fixed
   `accept_invitation`; otherwise refuse), called both by `_handle_join`
   and by `browse_channels` before *every* `_chat_loop` entry — one
   check at the top of `browse_channels`' own loop covers the initial
   pick, a `/leave`-driven repick, and a `/join`-driven switch alike.
   Confirmed via a genuine end-to-end picker-driven test harness (a
   `FakeSession` combining `read_key` and `read_line` from one queue,
   since `pick_item` needs the former and `_chat_loop` the latter) —
   3 of the 5 new tests fail against the pre-fix code when checked by
   temporarily reverting just this change, confirming they're real,
   not vacuous.
3. **#29: cross-process revalidation still missed board/file-area/
   profile/admin loops, and never caught a genuinely idle session.**
   The two in-loop checks (main menu, chat's send loop) only ever fire
   on that loop's *next* keystroke — a SysOp stuck inside `admin_menu`
   after being disabled/deleted through a separate `python -m
   netbbs.admin` invocation could keep issuing privileged commands
   indefinitely, and nothing ever caught a session sitting fully idle.
   `_watch_for_account_revocation()` is a new background task, one per
   authenticated session (started in `run_authenticated_session`
   alongside `presence.enter`, cancelled in its own `finally`), that
   re-checks the account every `_REVOCATION_CHECK_INTERVAL_SECONDS`
   (5s) and disconnects the session the moment it comes back inactive
   — covering every loop at once, present or future, without a copy of
   the check bolted onto each. Kept the two existing in-loop checks
   too, as genuine defense-in-depth (zero-latency for an actively
   typing session) rather than replacing them.

   The tricky part was avoiding a mutual-wait deadlock: the watcher
   calling the existing `disconnect_one()` (which cancels the target
   task *and awaits its full unwind*) from inside its own task would
   deadlock the moment the target session's own cleanup tries to cancel
   and await the watcher task in turn — a hazard `disconnect_one`'s own
   docstring already flags for the *self*-cancellation case, but this
   is a *new*, mutual variant between two different tasks. Fixed with a
   new `ActiveSessionRegistry.cancel_one()` — schedules the
   cancellation without awaiting the unwind, so the watcher task
   finishes (and can safely itself be cancelled+awaited by the target's
   own cleanup) almost immediately after firing it. Regression tests
   drive the real `run_authenticated_session` with
   `_REVOCATION_CHECK_INTERVAL_SECONDS` patched down for speed: a
   genuinely idle main-menu session, and sessions stuck inside board
   composing, a file area, the profile screen, and the admin menu —
   all 5 confirmed to fail (timeout) against a neutered watcher stub,
   proving they exercise the real mechanism.
4. **#34: the idle-timeout fix was confirmed correct; the remaining
   gap was node-wide memory amplification.** `receive_file()` still
   accumulated the whole upload in a `bytearray` and returned a second
   `bytes` copy — the per-transfer `max_bytes` ceiling bounded one
   upload, but said nothing about several concurrent ones. Thiesi chose
   the root-cause fix (streaming to a temp file with incremental
   SHA-256) over a smaller concurrency-semaphore bolt-on. `receive_file`
   now writes each subpacket straight to a caller-supplied `dest_path`
   and hashes incrementally, returning `sha256`/`size_bytes` instead of
   `data: bytes` — never holding the complete transfer in memory at
   once. New `netbbs.files.storage` primitives:
   `new_incoming_temp_path()` (a fresh path under a `.incoming`
   staging directory *inside* `storage_root`, not the platform temp
   directory — guarantees the final placement's `os.replace()` is
   always same-filesystem, a real concern on this project's NetBSD
   deployment target, where `/tmp` is commonly its own separate mount)
   and `move_temp_file_into_storage()` (an atomic rename into the
   content-addressed layout, never re-reading the content back into
   memory). `netbbs.files.entries` gained `upload_file_from_temp()`
   alongside the existing bytes-based `upload_file()` (kept for callers
   that already have the whole thing in hand — dev scripts, most
   tests), sharing a new `_finalize_upload()` helper for the identical
   database-row half of both paths. `dest_path`/`temp_path` are
   cleaned up on every failure path in both `receive_file` and
   `upload_file_from_temp` (including `asyncio.CancelledError` — a
   session cancelled mid-upload, e.g. by the new round 73 revocation
   watcher, must not leak a partial temp file either), never silently
   orphaned.
5. **Testing**: `test_zmodem.py`'s whole suite updated for the new
   `receive_file(..., dest_path=...)` signature and
   `ReceivedFile.sha256`/`.size_bytes` fields (`_round_trip` now reads
   the destination file back for its own assertions). New coverage in
   `test_file_storage.py` (the two new storage primitives, including
   that identical content converges on the same final path regardless
   of which upload path wrote it) and `test_file_areas.py`
   (`upload_file_from_temp`, including temp-file cleanup on a
   permission-check failure). A new
   `test_file_flow_upload_integration.py` drives a *real* end-to-end
   upload — real `_show_area`/`_handle_upload` menu flow, a real
   `zmodem.send_file` client task on the other end of an in-memory
   duplex byte pipe, confirming the whole chain (menu → live Zmodem
   handshake → temp file → content-addressed storage → a queryable
   `FileEntry`) works together, plus that a rejected oversized upload
   leaves no temp file and no entry behind. Full suite re-run: **1551
   passed, 3 skipped**.

## Sign-off notes, round 74 (round 73 verification — two narrow gaps in #29/#34 — fixed)

The reviewer verified round 73's two architectural fixes (the
revocation watcher, the streaming upload rewrite) and confirmed both
directions were sound — this round closes two specific gaps found
during that verification, not further architecture questions.

1. **#29 (reopened a third time): the watcher's own disconnect notice
   could stall the disconnect itself.** `_watch_for_account_revocation`
   wrote its "Disconnecting" notice, *then* called `cancel_one` —
   `session.write_line` is an unbounded transport operation, and a peer
   that's stopped reading (real TCP backpressure on a still-open
   connection, distinct from a closed one, which already raised
   `SessionClosedError`) could stall that write indefinitely, delaying
   the actual security-critical cancellation right along with it.
   Fixed by wrapping the write in `asyncio.wait_for` (a new
   `_REVOCATION_NOTICE_TIMEOUT_SECONDS = 1.0`) and moving
   `cancel_one` into a `finally` — cancellation now happens
   unconditionally, regardless of whether the notice write finishes,
   times out, or fails. Regression test: a session whose `write_line`
   never returns for that specific notice text, confirmed to still get
   cancelled within a bounded time — verified non-vacuous by reverting
   to the old write-then-cancel ordering and confirming the test times
   out against it.
2. **#34 (reopened a third time): no crash/restart recovery for
   staging files.** `receive_file`'s `except BaseException` cleanup
   only runs for an ordinary Python exception or task cancellation — a
   `kill -9`, crash, or power loss mid-upload skips it entirely,
   leaving a UUID-named partial file under `.incoming` with no code
   path left to ever remove it (the existing blob GC deliberately only
   recognizes 64-character sha256 filenames, so it already, correctly,
   never touches `.incoming`). Fixed with a new
   `netbbs.files.storage.purge_incoming_staging()`, called once in
   `netbbs.__main__.run()` right after opening the database and before
   `_start_servers` — safe specifically because nothing can have a
   legitimate upload in flight yet at that point, so every regular
   file already present is guaranteed stale. Conservative about
   anything that isn't a plain regular file (a symlink or directory is
   skipped, not followed or recursively deleted) per the reviewer's own
   suggestion, since nothing legitimate should ever put one there.
3. **Testing**: `test_account_revocation_watcher.py` gained the
   blocking-notice regression test above. `test_file_storage.py`
   gained unit coverage for `purge_incoming_staging` (removes stale
   files, leaves real content-addressed blobs untouched, harmless when
   `.incoming` doesn't exist, skips a symlink rather than following it
   — this last one honestly `pytest.skip()`s on this sandboxed dev
   environment, which lacks the privilege to create a symlink at all,
   rather than silently passing without exercising it). A new test in
   `test_main_lifecycle.py` confirms the purge is actually wired into
   `run()` itself, not just correct in isolation — a stray file
   written before `run()` starts is gone by the time it's actually
   accepting connections. Full suite re-run: **1557 passed, 4
   skipped**.

Both issues closed pending the reviewer's next verification pass; round
73's two directions (background watcher, streaming upload rewrite)
were not in question this round, only these two narrower gaps in their
implementation.

## Sign-off notes, round 75 (chat status line — implemented)

Following v2.0.0's release, Thiesi asked for two final-polish items
(online contextual help, menu prettification) to be recorded as
deliberately last-priority — done, see this round's own design-doc
sign-off note — and for the chat status line to be built next, checked
for blockers first. None found; built using the scroll-region (DECSTBM)
approach Thiesi chose over a simpler no-new-primitives alternative
(see the design-doc note for the fork itself).

1. **Two new ANSI primitives**, `netbbs.rendering.ansi`:
   `set_scroll_region(top, bottom)`/`reset_scroll_region()` (DECSTBM,
   `CSI {top};{bottom} r` / `CSI r`) and `save_cursor()`/
   `restore_cursor()` (the classic VT100 `ESC 7`/`ESC 8`, not the
   ANSI.SYS `CSI s`/`CSI u` variant, for the widest real-terminal
   support). Neither existed anywhere in the codebase before this —
   confirmed by a repo-wide grep before starting.
2. **`netbbs.net.chat_flow._chat_loop` now reserves the terminal's
   last row** for a pinned status line, set up once per channel entry:
   `clear_screen()` followed by `set_scroll_region(1, height - 1)`.
   The `clear_screen()` is not cosmetic — DECSTBM moves the real
   terminal cursor to its home position as an unavoidable side effect
   of the escape sequence itself, so without it the jump would
   overwrite whatever screen preceded chat rather than land on a
   blank canvas. A genuine, visible behavior change from before (chat
   used to just continue printing inline under the previous screen).
   Skipped entirely below `_STATUS_LINE_MIN_HEIGHT` (2) — a client can
   report an arbitrarily small terminal height (`clamp_terminal_size`'s
   own floor is 1, not a sane minimum), and this degrades cleanly to
   the exact old unconfined-scrolling behavior rather than trying to
   render a status line with nothing left to reserve it from.
3. **`_render_chat_status_line`** (pure function) formats channel
   name, live participant count (`ChatHub.participant_count`), this
   user's own away/mute indicators (`[away]`, `[muted]`, or `[muted
   until HH:MM]`), and a clock — deliberately a bare `%H:%M`
   (`format_for_display`'s `override_format`), not the node's full
   configured display format (which includes the date) and would
   waste width on a bar that's redrawn continuously and only ever
   shows the current moment. Still honors the node's configured
   timezone, which `override_format` alone doesn't affect.
   `_repaint_status_line` wraps it with `save_cursor`/
   `set_scroll_region` (re-issued every call, not just at entry —
   see point 5) /`move_cursor`/`clear_line`/`restore_cursor`, so an
   in-progress input line is never disturbed.
4. **Five repaint call sites**, not fifty: one in `receive_loop`
   (after every `_TimestampedNotice` — covers joins/leaves/topic
   changes/moderation notices with one call, rather than enumerating
   which specific notice types are "count-relevant"), three in
   `send_loop` (after any dispatched slash command, when a muted
   message is rejected — the user may be learning they're muted for
   the first time right there, and after an ordinary message is sent),
   plus the initial draw right after the join broadcast. Centralized
   to these two functions' own bodies rather than scattered across the
   ~50 individual command handlers that write to the session directly.
5. **Resize handling with no dedicated resize-event hook**:
   `_repaint_status_line` re-reads `session.terminal_height` and
   re-issues `set_scroll_region` on *every* call, not just once at
   entry — confirmed via research that Telnet NAWS/SSH PTY-resize/web
   `resize` all update `terminal_height` live already, just passively,
   with nothing in the codebase reacting to the change as an event.
   Re-sending an unchanged region is harmless, so piggybacking on the
   repaint calls that already happen regularly gets basic resize
   adaptation for free without inventing a new notification mechanism.
6. **Cleanup**: `reset_scroll_region()` runs in `_chat_loop`'s
   existing `finally` block, wrapped in its own `try/except
   SessionClosedError` — best-effort, since the common reason this
   block is even running is that the session is already gone, and a
   failure here must not replace/mask whatever exception is already
   propagating out of the `try` above. Must happen before the session
   moves on to any other screen (the main menu, the channel picker) —
   left active, every subsequent screen would keep scrolling inside
   this same shrunk region.
7. **A real, foreseeable collision with an existing security test,
   found and fixed correctly rather than papered over**:
   `test_terminal_sanitization.py`'s hostile-payload test asserted a
   blanket "`\x1b[2J` never appears anywhere in the transcript" to
   confirm an attacker's embedded clear-screen sequence didn't survive
   sanitization — which broke the moment chat legitimately started
   emitting a real `clear_screen()` of its own on entry, containing
   the identical two bytes for a completely unrelated reason. Fixed by
   anchoring the check to the hostile payload's own contiguous
   fragment (`"PWNED\x07\x1b[2Jmore text"`) instead of a bare
   substring search — correctly distinguishes "the attacker's sequence
   survived" from "this byte sequence also legitimately occurs
   elsewhere in the same output," the identical class of collision the
   test's own docstring already flagged for `colored()`'s SGR codes,
   just not yet for a control sequence needing intact-survival
   checking. The security property itself is unweakened; every other
   assertion in that helper is untouched.
8. **Testing**: `test_ansi.py` gained 7 new cases for the two ANSI
   primitives (including rejecting an inverted/invalid scroll region
   and accepting a single-row region). The new `test_chat_status_line.py`
   covers the rendered content directly (channel/count/away/mute/clock,
   confirming the clock is genuinely time-only with no 4-digit year
   anywhere) and the surrounding mechanics through the real
   `_chat_loop`: scroll-region set on entry, screen cleared on entry,
   region reset on exit, gracefully skipped on a too-short terminal,
   the reset write's own failure doesn't crash cleanup, a repaint after
   `/away`, a repaint when a muted message is rejected, and — the one
   requiring two concurrent `_chat_loop` tasks — one participant's
   status line correctly reflecting a second participant's live
   arrival. Full suite re-run: **1579 passed, 4 skipped**.

Real third-party-client verification (a genuine Telnet client, SSH
client, and the web xterm.js terminal actually rendering the scroll
region correctly) remains unverified from this sandboxed dev
environment — flagged explicitly, same standing caveat this project's
other terminal-rendering work already carries, not a new one specific
to this feature. The self-service registration workflow is next, per
Thiesi's own explicit ordering (see this round's design-doc note).


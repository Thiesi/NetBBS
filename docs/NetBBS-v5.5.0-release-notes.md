# NetBBS v5.5.0

A second bundled door game, multi-key SSH support, a real production shutdown
hang fixed, section-grouped and paginated admin/profile screens, a genuine
SSH pre-auth banner security fix, and three closed design decisions —
alongside a broad dogfood-driven polish sweep. Includes one small, additive
database migration.

## War Dialer: a second bundled door game (issue #200)

Rival 80s/90s BBS-scene hacker/phreaker crews fight over ten shared phone
exchanges — Trade Warez, Crew Recruit, Job, Raid, and Root Exchange, five
turn-costing actions plus a free exchange/leaderboard Board view. Unlike
Voidrunner's single-player save, War Dialer is a genuinely shared,
persistent world: every caller who plays it acts against the same live
SQLite-backed state, with real concurrent-write safety (two players raiding
the same target at once can't lose an update) and a Rank built from
monotonic lifetime counters rather than current holdings, specifically so a
bust or a lost exchange can never knock a player's rank backward. 40 tests,
including a real-threads concurrency regression. A one-page "How to play"
screen — shown automatically once for a new player, reachable any time via
`[?]`, and fitting one page at the standard 80x24 terminal size — rounds it
out this cycle. (Text still wraps correctly, never overflowing a line, at
narrower supported widths down to 40 columns; it just runs past one screen
there, an accepted floor-width limitation rather than pagination for a
screen this door deliberately keeps to plain single-keystroke reads.) A
standalone Escape press at any "Press any key" pause is now handled
correctly too — distinguished from the leading byte of an arrow key's
escape sequence without ever blocking on a byte that isn't coming.

## Multi-key SSH support (issue #222)

A dogfood report: adding an SSH key from a second device (a phone, which
has to generate its own key since Android won't let an existing private key
be copied onto it) used to silently revoke whichever key was already
registered from the first device — the account model only ever held one. A
new `user_ssh_keys` table now holds every key an account registers;
authentication checks all of them, not just one. A shared list/add/remove
screen (self-service Profile and SysOp user-detail both use it) replaces
the old single-key add-or-replace prompt, marks which key is primary, and
warns specifically when removing it while others remain.

A real security gap surfaced during this work and was fixed alongside it:
local-account blocking used to key off fingerprint whenever the target had
a keypair — once an account can hold more than one key, that would have let
a blocked user's *other* key bypass the block entirely. Now always keyed by
account ID.

## A real ~9-minute production shutdown hang, fixed

Reported live, with real operational logs, from the project's own
persistent test node: Ctrl+C could take about nine minutes to actually
exit. Root cause was two compounding gaps in shutdown teardown — one
background task's cancellation wait reused the wrong config value (a
60-second "how long does a human get to notice a shutdown warning" number,
not "how long does a background task get to notice cancellation"), and
three other background tasks had no bound on their cancellation wait at
all, one of which reaches a blocking network call via `asyncio.to_thread`
that doesn't actually stop just because the awaiting coroutine is
cancelled. All four now share one dedicated, short bound. Shutdown log
timestamps also now show second precision instead of millisecond.

## Section-grouped, paginated admin and profile screens

A dogfood report: the main menu's grouped, multi-column layout and the
Profile screen's own flat 14-field list read as wildly different levels of
polish, for no principled reason. The shared draft-editor component behind
Profile, Board, File area, and Channel create/edit screens gained a real,
opt-in section-grouping option — fields now group under bold uppercase
headings (Identity/Access/Organization/... ) in both the value list and the
hotkey menu row, rolled out to all four of those screens.

Extending this to Profile specifically exposed a deeper problem no amount
of spacing could fix: Profile's 14 fields across 4 sections, plus its own
bio-preview/transport-diagnostic preamble, simply don't fit a real 80×24
terminal — the field list and menu row alone are already several rows over
budget before a single byte of preamble. The fix is real pagination: a
sectioned screen that still doesn't fit now pages by section (`Page Up`/
`Page Down`, wrapping at either end), while every field's own hotkey keeps
working regardless of which page is showing, and `[S]ave`/`[B]ack` stay
reachable from every page. Board/File area/Channel already fit unpaginated
and are unaffected. Profile's own bio preview is now capped to a short,
bounded preview (full bio stays one keystroke away via `[E]dit bio`), and
both the preview and its own truncation marker are counted correctly
against the screen's height budget at every terminal width, so a long bio
can no longer blow that budget regardless of pagination.

## SSH pre-auth banner: a real, not just cosmetic, fix (issue #203)

A dogfood report: the pre-auth SSH banner scrolled by too fast to read, and
what was visible was raw ANSI escape codes instead of rendered art. Traced
to the actual mechanism rather than assumed: `SSH_MSG_USERAUTH_BANNER` is
shown during authentication itself, before any pty/terminal channel
exists, and real clients (PuTTY confirmed) commonly route it through a
display path that never runs an ANSI parser over it at all — no color
depth fixes that, since it isn't a color-depth problem. The pre-auth
banner now sends plain text with every ANSI/VT100 escape sequence
stripped, verified against a proper ECMA-48-shaped grammar (private-mode
CSI, OSC, charset-select, and multi-byte bare-ESC sequences, not just
simple SGR color) after an initial narrower version let some of those
through. Telnet/web's pre-login banner and SSH's own post-auth welcome
screen are unaffected and keep full color.

## Timezone picker

Settings → Timestamp display's Timezone field was a bare free-text prompt
requiring an exact IANA identifier typed correctly. Now opens the same
searchable picker every other lookup in the app uses, over
`zoneinfo.available_timezones()`.

## SysOp console: status visibility rounded out (issue #206)

- A condensed one-line status ("Backup: ... Update: ...") now shows on
  every remaining SysOp screen that didn't already have one — roughly 30
  screens across the admin console.
- Settings gets its own current-values panel (node name, update-check
  mode/last result, timestamp format example, trust-policy exception
  count) — the one top-level screen still missing one.
- The Backup status screen now pauses for a keypress before returning,
  matching every sibling info screen's own convention — previously it
  returned straight into an immediate redraw, so a real answer could look
  like the hotkey had done nothing.

## Design-only decisions, no visible change

Three issues got a full design pass this cycle:

- **Issue #168 — real-time relay for Link direct chat**: the central
  security-model fork decided (raw-socket/TCP-level proxy below the Noise
  XX layer, not double-hop relay-as-participant) — zero changes needed to
  the already-shipped session/handshake code. A later pass this same
  cycle settled the remaining acceptance criteria too: bounded resource
  limits for live relay (a concurrent-bridged-pair cap, a byte-rate bound
  since raw-proxy never parses frames, a protocol-agnostic idle-bridge
  timeout, and a bounded/timed-out pending-rendezvous table), a v1
  fallback experience for two mutually-unreachable nodes (extends the
  already-shipped local `/msg` online-required convention, pointing at
  the already-shipped Link mail path as the fallback), and the
  rendezvous frame shape itself (new frame types on the existing
  real-time frame family, no new protocol version). Issue closed;
  implementation is separate, future, not yet its own tracked issue.
- **Issue #201 — managed netbbs.org subdomain + dynamic DNS**: opt-in
  mechanism, credential model, name-governance/abuse policy, and
  migration/reclaim all locked in. Provider choice and exact grace
  periods left as implementation-time detail.
- **Issue #219 — Reliable Link as default onboarding infrastructure**:
  positioned as the flagship entry of a future "reliable nodes" list for
  frictionless new-node onboarding, above both #168 and #201 without
  duplicating either.

## ReLink dogfood fixes (issues #204, #209, #210)

- A lone uncategorized door game didn't surface the main menu's
  `[U]ncategorized` entry (direct-jump navigation always worked; the
  entry's own visibility check just didn't look for doors).
- The fullscreen ANSI-art editor had no online help; wired up `Ctrl+G`,
  the key already reserved for it since v1.
- The same editor's ANSI parser had no branch for 24-bit truecolor SGR
  sequences — silently corrupting a truecolor banner's color state
  instead of rendering it, now fixed to round-trip both depths.

## Link-promotion wizards converted to the shared field-editor pattern

`[L]ink this board/file area/channel` each used to walk a fixed linear
prompt chain — a mistyped later field discarded every earlier answer and
aborted the whole screen. Converted to the same independently-addressable
field-editor pattern already used for boards/channels/file areas/
Communities/doors themselves. Shutdown/Drain scheduling got the same
conversion, plus a text-wrapping fix for explanatory text that used to
rely on the terminal's own soft-wrap and could overflow on narrow
terminals.

## Banner/masthead polish and codebase-wide fixes

A cleanup sweep following the branding work introduced in earlier
releases:

- Gallery and From-disk wired into the 6 banner/masthead screens that had
  shipped without them, plus a shared Ctrl-H "banner help" screen
  explaining exactly where a SysOp's own `.ans` file needs to live.
- Welcome-banner vertical bars now match the horizontal rainbow's own
  endpoint colors instead of sweeping independently; long banner/masthead
  menu subtitles now wrap instead of overflowing; status lines shortened
  to filename-only with a path-corruption fix.
- **Enter now dismisses every "Press any key to continue..." prompt**
  (~60 call sites) — the existing hotkey-menu reader's CR/LF-is-noise
  behavior is correct for menus but silently ate Enter at every one of
  these pauses; a new sibling primitive fixes it without touching
  hotkey-menu behavior anywhere.
- Board/chat/file picker nav instructions (including the Ctrl-H hint)
  used to be silently truncated once a sort label was active, under 40
  columns on an ordinary 80-column terminal — not a rare edge case. Now
  wraps onto its own line instead of losing content.
- Settings menu: an undocumented, redundant hotkey removed; Node Name
  gets the freed-up letter instead of a buried mid-word one it was on
  specifically to avoid the collision.
- Audit log: timestamps now show at display precision instead of raw
  microseconds, with distinct colors per field instead of one flat
  string.
- Title/breadcrumb harmonization pass across multiple screens, and a fix
  for the boxed help overlay not closing its own right-hand border.

## Upgrade notes

- **Includes a database migration**: a new `user_ssh_keys` table
  (additive-only — the existing `users` table's own columns are
  untouched; an existing account's single key becomes its primary key
  automatically). No manual action needed — it's the only schema change
  in this release, and it runs automatically on first startup after
  upgrading, the same as every prior migration.
- Every other change is either purely additive, opt-in, or a targeted
  correctness/visual fix with no behavior change for callers who don't
  touch the affected surface.
- The three SysOp-console status-panel additions (condensed status line
  on ~30 more screens, Settings' own status panel) are a real, visible UI
  change for every existing node, not opt-in.

## Validation

- The complete pytest suite passes (4588 passed, 5 skipped) at this
  release's exact commit.
- `python -m netbbs --version` reports v5.5.0 and schema version 58.
- The shutdown hang, the SSH pre-auth banner fix (and its own two Codex-
  review follow-up rounds), the section-grouping/pagination work (and its
  own Codex-review follow-up: a bio-preview-marker width bound and a
  cursor-nav page-priming gap), and War Dialer's help-screen arrow-key
  input handling (four Codex-review rounds, ending in a real cross-
  platform fix rather than a stub) each have a regression test confirmed
  to fail against the pre-fix code, verified with a direct repro before
  the fix in every case, not just reasoned about.

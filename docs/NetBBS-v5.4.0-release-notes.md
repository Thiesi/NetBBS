# NetBBS v5.4.0

A big one: NetBBS gets its first bundled door games, a full SysOp telemetry
dashboard overhaul, and another round of node-branding features, on top of
a broad visual-polish sweep across boards, mail, chat, and pickers. No
schema migration, no protocol change.

## Doors: a real sandbox, and two bundled games (issue #172)

The first real slice of native door-game support (design doc §Phase 7):
same-OS-user subprocess isolation with `resource.setrlimit` CPU/memory/
process-count ceilings, an async wall-time watchdog, unconditional reap on
every exit path, and a drop-file-shaped v1 API (static session metadata
before spawn, raw stdio relay during play, exit code as the only signal).
`netbbs.doors.registry` is the SysOp-facing catalogue — level-gated,
optionally Community-scoped, mirroring `netbbs.files.areas`'s own shape.

On top of that foundation:

- **SysOp registration UI and caller-facing launching**, plus a filesystem
  picker for a SysOp's own door scripts (the same `doors/` convention
  issue #170 gave custom banner art), and a doors registration gallery.
- **Retro Trivia**, a small real playable demo door proving the pipeline
  end to end.
- **Voidrunner**, an Elite/Trade-Wars-style persistent space-trading and
  exploration door that grew well past "demo" over several follow-up
  rounds: commodity trading with price drift and scheduled galaxy-wide
  economy events, delivery/bounty/escort missions, pirate combat with
  fight/evade/bribe, a futures exchange, paid NPC crew, two-faction
  reputation with contraband/customs, player notoriety and Concord Patrol
  interceptions, squadron fights at the highest danger tiers, named
  sectors and a full galaxy chart, ship upgrades and hull refits,
  credit-based ranks with retirement/New Game+, and a cross-save Hall of
  Fame.
- **Both bundled games ship as real installed package data**
  (`netbbs.doors.bundled`), not loose example files — confirmed by
  building a wheel and inspecting its contents.
- **Two dedicated hardening passes on Voidrunner specifically**: a
  gameplay/exploit audit (a derelict-salvage crash in fully safe systems,
  a zero-risk combat-bribe-refusal exploit, a contraband dead-end outside
  Haven, and several mission/UX gaps), and a from-scratch visual overhaul
  of its tactical HUD followed by a systemic fix for box-alignment
  overflow that turned out to affect nearly every screen in the game —
  the box-overflow class of bug this release's SysOp-console work below
  also spent real effort chasing down.

## SysOp console: telemetry gauges and consistent dashboard framing (issue #187)

The Users, Operations, and Content sub-consoles now share the same boxed
`double_frame` dashboard styling the landing page already used, with real
`telemetry_gauge` progress bars (`[██████░░░░] 6/10`, Unicode with an ASCII
fallback) for meaningful ratios like account activity and moderation
backlog.

This shipped alongside — and then went through several rounds of
Codex-assisted review to actually harden against — the same class of
real-terminal-width and stale-data bugs the Voidrunner work above hit:

- **A P1 fix**: Operations no longer reloads its full telemetry snapshot
  on every redraw, which used to reintroduce a cancellable database wait
  right after scheduling an immediate node shutdown or drain from
  `[N]ode` controls. It now reuses a durable snapshot and only reloads
  after an action that can actually change the numbers (Outbox,
  Diagnostics, Follow Log, Link status, backup, drafts, audit log —
  everything except Node itself).
- **Every compact-panel telemetry row now actually fits its frame**,
  down to the 40-column terminal floor these sub-consoles branch on —
  including edge cases only reachable at real scale (a 100+-item
  moderation backlog, four-digit account counts, a full ISO backup
  timestamp with no other fields to pad against) that a small dev
  database never exercised.
- **A crash fix**: a client reporting a pathologically narrow terminal
  width (1–3 columns, which this codebase's own `clamp_terminal_size`
  permits) no longer crashes the console outright.
- **Two fake-capacity gauges were dropped**: active-session and
  moderation-queue counts have no real configured capacity to measure
  against, so a `max(10, count)` placeholder denominator just made the
  bar permanently "100% full" past 10 — misleading rather than useful.
  Both surfaces now show the plain count instead.
- **A mislabeled standalone-mode Link badge**: the `python -m
  netbbs.admin` standalone CLI has no way to observe whether the live
  node process actually has Link configured, so it no longer claims
  "DISABLED" (which asserts a real, observed state it can't know) —
  it now says "UNAVAILABLE".
- Several smaller correctness fixes: an ASCII-mode Unicode-glyph leak, a
  usable-SysOp count that used to include disabled accounts, a
  "Descriptions hidden" notice that could silently disappear, and a
  moderation-queue empty-state gauge that used to render red next to its
  own "All clear" label.

## More node branding (issues #175, #176, #177, #178)

- **Per-character gradient node-name coloring**, threaded through every
  real render call site (~55, across admin/login/chat/file/mail flows,
  pickers, the resource editor, and composition review) that shows the
  node's name in a screen's breadcrumb corner.
- **Logoff and new-account (before/after) banners**, and the section
  masthead pattern (compact 6-line headers) extended to the board list,
  file-area, and chat-channel-picker screens.
- **96 new bundled ANSI/Unicode sample presets** across those 6 new
  customization surfaces, spanning all 16 core visual themes.
- **Settings reorganized** into one `Banners & Mastheads` submenu instead
  of several flat top-level entries — navigation only, no leaf screen's
  own behavior changed.

## file_flow: columnar directory listings (issue #184)

File listings render as an aligned table (`#`, filename, size, date,
uploader) instead of free-form text, with numbered download shortcuts and
verified-name display (`^Alice (=Alice Smith=)`) that no longer truncates.

## Chat polish (issue #183)

A styled, accent-colored input prompt (`❯ ` / `> ` ASCII fallback) and a
visual divider rule above the pinned chat status bar, in both channel and
direct chat.

## Linked-channel presence: the remaining gap closed (issue #195)

A node live-subscribed to a remote channel now shows that channel's actual
remote roster in `/who` and `/names` — the presence frames were already
being received and validated when node-wide presence shipped in v5.3.0,
but the roster itself was discarded rather than tracked. Kept in sync via
presence deltas too, not just the initial snapshot, and cleared on session
close the same way node-wide presence already was.

## Visual polish sweep: pickers, boards, mail, help (issues #180, #181, #182, #186)

- Item pickers and the resource editor: bolder, higher-contrast active-row
  styling for arrow-key cursor navigation.
- Board reading: subtle divider rules between posts, and quoted lines
  (`> ...`) rendered in a distinct muted color.
- Private mail and draft review: bounded divider rules separating
  headers, content, and action menus.
- The in-context help overlay's own visual presentation.

## Scoping only, no visible change

Two issues got a full design pass and were locked into a concrete,
narrow shape, but implementation was explicitly **not** authorized to
begin in either case — nothing here is user-visible yet:

- **Issue #165 — an MRC gateway**: a bounded, per-channel-opt-in,
  always-untrusted bridge to the classic MRC network, grounded in
  ENiGMA½'s own reference client since no formal protocol spec exists.
- **Issue #194 — trusted scrollback-on-join** for live Link channel
  chat: closing the up-to-five-minute backfill gap a fresh live
  subscription has today, by having the channel's origin node send its
  own already-trust-filtered scrollback as a sibling frame alongside the
  existing presence snapshot.

## Upgrade notes

- No database migration in this release — upgrade is install-and-restart.
- The SysOp console's Users/Operations/Content sub-consoles now render
  visibly differently (boxed dashboard panels with gauges, replacing bare
  action menus) — a real, deliberate UI change for every existing node,
  not opt-in like the branding features above.
- Every other change is either purely additive, opt-in (node-name
  gradient, new banners/mastheads, doors), or a targeted visual/
  correctness fix with no behavior change for callers who don't touch the
  affected surface.

## Validation

- The complete pytest suite passes (4406 passed, 5 skipped) at this
  release's exact commit.
- `python -m netbbs --version` reports v5.4.0 and the current (unchanged)
  schema version.
- Every correctness fix in this release — across Voidrunner's two
  hardening passes and the SysOp console's Codex-review fix cycle — has a
  regression test confirmed to fail against the pre-fix code.

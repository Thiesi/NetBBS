# NetBBS v7.2.0

Both bundled games get the presentation their overhauls never delivered.
Voidrunner and War Dialer were each rebuilt into deep, well-modelled games and
each shipped as a wall of grey sentences — and in both cases for the same
structural reason, found independently: the function every screen's rows passed
through on the way to the terminal stripped ANSI by construction, so no screen
could be coloured even when it tried. The frame restored in v7.1.0 was the only
styled thing on a page, because it is drawn afterwards. Both wrappers are fixed,
and both games' screens were rebuilt on top of them.

Minor rather than patch because Voidrunner gains a display preset that is saved
in the career, and doors gain a profile field. No database migration, no world
format change, no Link protocol change: the host database stays at schema 64,
Voidrunner careers stay at save schema 2, and War Dialer worlds stay at world
schema 10 — a schema bump written during this work was taken back out rather
than paid for (see *For the project*).

## What callers see

### Voidrunner looks like a game again

Captures below are real, taken from the same career on both sides — v7.1.1 for
*before*, this release for *after* — both at 80x24 in the monochrome preset so
the shapes show without colour.

Before, the station deck was a title, a `Day | Economy | Danger` line, one row of
telemetry and five rows of `[K] Label | [K] Label`:

```
╭── Command Deck: 1,200cr 1/1 ────────────────────────────────────────────────╮
│  Station Services: Freeport Anchorage                                       │
│  Day 0 | Industrial | Danger 1                                              │
│  Shuttle: Hull ■■■■■■■■ 60/60 | Fuel ■■■■■■■■ 24/24 | Cargo ░░░░░░ 0/24     │
│  [M] Commodity Market  |  [Y] Engineering Yard  |  [B] Mission Board        │
│  [C] Navigation Chart  |  [S] Pilot Status  |  [H] Hall of Fame             │
│  [G] Pilot Guide  |  [N] Archive Contacts  |  [T] Trading Ledger            │
│  [V] Viewport  |  [O] Display Options  |  [Q] Disembark & Save              │
│  [P] Concord Contacts  |  [W] Blackwake Contacts                            │
╰─────────────────────────────────────────────────────────────────────────────╯
```

After, the same career, the same keys, the same facts:

```
╭─ ◤ VOIDRUNNER · Command Deck: 1,200cr ───────────────────────────────── 1/1 ╮
│  FREEPORT ANCHORAGE  Industrial · Coreward Verge  CALM 1                    │
│  Day 0  Rank Rookie Hauler  ◈ 1,200cr                                       │
├─ SHIP ──────────────────────────────────────────────────────────────────────┤
│  HULL  ████████████████████  60/60  Intact           /\                     │
│  FUEL  ████████████████████  24/24  Shuttle     <===[__]=== >               │
│  HOLD  ░░░░░░░░░░░░░░░░░░░░   0/24  0 lots          /__\                    │
├─ STATION SERVICES ──────────────────────────────────────────────────────────┤
│  [M] Market 7 goods       [Y] Yard repair/refit    [B] Board 4 offers       │
│  [C] Chart 2 links        [S] Status               [H] Hall of Fame         │
│  [G] Guide                [N] Archive              [T] Ledger               │
│  [V] Viewport             [O] Display              [Q] Disembark            │
│  [P] Concord              [W] Blackwake                                     │
╰─────────────────────────────────────────────────────────────────────────────╯
```

Gauges on one scale with the ship's silhouette beside them, a service grid that
says how many goods and how many offers are behind each key, and — when there is
anything to act on — an alerts band where each row carries its own severity and
the key that answers it: a critical hull in red with `[Y] repair`, contraband
aboard in amber with `[D] jettison`, a tracked contract with a countdown gauge
and `[C] chart`.

**The market is a table.** Six prose sentences became seven rows and seven
columns. Nothing was removed — every figure the prose carried is in a column —
and the spread the prose never showed is in the last one.

```
│  [A] Food: buy 19cr; sell 17cr. Stock 48; demand 96; hold 0.                │
│  [C] Textiles: buy 18cr; sell 17cr. Stock 48; demand 48; hold 0.            │
│  [D] Machinery: buy 39cr; sell 36cr. Stock 96; demand 48; hold 0.           │
```

```
│  COMMODITY           BUY  SELL  STOCK  DEMAND  HELD  SPREAD                 │
│  [A] Food             19    17     48      96     0  ▁▄███ dear             │
│  [C] Textiles         18    17     48      48     0  ▁████ dear             │
│  [D] Machinery        39    36     96      48     0  ▁▄▄██ cheap            │
```

The `SPREAD` sparkline is real data, not decoration: it is that commodity's
price at each of the five economy types, cheapest first, straight out of the
price model, with a word for where the quote in front of you sits in it. The
galaxy keeps no price archive, so a sparkline of remembered history would have
been a picture pretending to be data.

**Contracts are cards, not one-liners** — the payout in gold at the right edge,
the deadline as a countdown gauge, the danger and the distance beside it, and
the kind of work as a badge:

```
│  [1] OFFER DELIVERY: Ravensbourne (+1,618cr)                                │
│  [2] OFFER DELIVERY: Obsidian Gate (+801cr)                                 │
```

```
│  [1] ⟦DELIVERY⟧ Ravensbourne  OFFER                                +1,618cr │
│      danger ? · 5 jumps · day 23 ██████ · HAUL                              │
│  [2] ⟦DELIVERY⟧ Obsidian Gate  OFFER                                 +801cr │
│      danger ? · 5 jumps · day 22 ██████ · HAUL                              │
```

**Everything else.** The chart is a departures table with danger as pips; the
yard shows each upgrade as `0 → 1` with its effect and its price, red when you
cannot afford it; the record opens with a rank-progress bar and faction
standings as bars; the Hall of Fame is a ranked table with medals and your own
row picked out; a fight puts both hulls on the same scale one above the other,
so which bar is longer *is* who is winning; and customs, the crew roster, the
star map, the contract terms and the career finale all got the same treatment.

**Motion, and how to turn it off.** Screens reveal themselves as you arrive at
them, and a fight's hull bars drain when a shot lands. Any keypress ends an
effect immediately, nothing waits on one before saving, and a screen redrawn
unchanged never replays it. Display Options has a new preset for callers who
want none of it: **Full palette, no motion**. Monochrome and Plain have no
motion either. The presets are renumbered as a result — 1 Full palette, 2 Full
palette no motion, 3 16-color, 4 Monochrome, 5 Plain — and each one now previews
itself on that screen, drawn with what your own terminal can actually do.

**At forty columns** there is still one layout. Every screen, key and gauge is
the same at 40 columns as at 80, sized for the room there is. A table that will
not fit gives up its least useful columns first — and only ones whose figure is
a keypress away, like the market's stock and demand, which are on the
commodity's own trade screen — and if what is left still does not fit, the
record stacks onto as many lines as it needs rather than overflowing. Nothing is
truncated.

### War Dialer is a phosphor terminal, not a paragraph

Same method, same career: one world, both builds, 80x24, monochrome.

```
╔═ SWITCHBOARD 1/2 ═════════════════════════════════════════════════════════╗
║  Operator: Thiesi [::]                                                    ║
║  Cash: $250  Heat: 8                                                      ║
║  Crew: 2 available; 1 assigned                                            ║
║  Turns left: 14/15                                                        ║
║  Turn refill in 1d 0h 0m                                                  ║
║  New operation: 3 turns and $50 to the next execution; 14 turns           ║
║  available. [O] Ops previews each step.                                   ║
║  Rank: 50 - Newbie                                                        ║
║  Next: Wannabe in 50 Rank                                                 ║
║  Holdings: 1/10 exchanges - $2/hour                                       ║
║  Owned: 212-555 Uptown Exchange                                           ║
║  New events: 0 - [H] History                                              ║
║  Raid shield: newcomer, 2d 0h 0m remaining                                ║
║  Exchange territory is always contestable.                                ║
║  Season 2 ends in 28d 0h 0m                                               ║
║  [O] Operations/recon: none active                                        ║
║  [I] Scene: crew insignia, NPC dossiers and public bulletins.             ║
║  [S] Skills/support: untrained; empty slot                                ║
╚═══════════════════════════════════════════════════════════════════════════╝
```

```
┏━ ▚ SWITCHBOARD ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ page 1/2 ━┓
┃  Thiesi ⟦[::]⟧   ⟦NEWBIE⟧   season 2 · 28d 0h 0m left                     ┃
┃  rank 50 ██████████░░░░░░░░░░   next Wannabe                              ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃  CASH $250   HEAT ██░░░░░░░░░░░░░░░░░░ 8                                  ┃
┃  CREW ●●○ 2 free   1 posted                                               ┃
┃  TURNS ▮▮▮▮▮▮▮▮▮▮▮▮▮▮▯ 14/15   refill 1d 0h 0m                            ┃
┃  HOLD ●○○○○○○○○○ 1/10   income $2/hr                                      ┃
┃  SHIELD newcomer 2d 0h 0m   NEW 0                                         ┃
┃  OPS none   KIT untrained / empty                                         ┃
┣━ THE SCENE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃  ◆ 1══◇ 2══◈ 3══◇ 4══◉ 5   ◆ yours 1   ◈ rival 1   ◉ NPC 3   ◇ free 5     ┃
┃  ║                     ║                                                  ┃
┃  ◇10══◇ 9══◇ 8══◉ 7══◉ 6                                                  ┃
┣━ FEED ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃  ● 12:19 Rooted 212-555 Uptown Exchange; +$2/hour and +50 Rank.           ┃
┃  ● 12:19 Kilobaud raided you and got away with $340! All-attacker raid    ┃
┃          shield: 24 hours.                                                ┃
┃  ● 12:19 Crackdown closed season 1. Final Rank 0; place 3/3; no medal.    ┃
┃          Fresh season 2: competitive resources reset. Back on the         ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

In colour, the feed row is magenta where a rival moved against you and phosphor
where you moved, money is amber, the gauge tracks are dim, and the hotkeys are
amber. The same exchange is the same colour on the ring, in the table and in the
feed, because one function decides who owns it.

The ring is not decoration either: it is the ten exchanges with their real
links, node 1 above node 10 and node 5 above node 6, which is exactly how the
world joins them. Pressing a digit opens that exchange's own card — owner, role
badge, defence dots, **your** odds as a bar, the capture price with its
neighbour discount, its links and its owner service. The number means the same
thing on every page, because it is the exchange's own number and not a position
in a moving list.

**Season standings became a ladder and a table**, instead of nine sentences:

```
┏━ ▚ SEASON STANDINGS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ crews 1-3 of 3 ━┓
┃  position 2/3   rank 50   ⟦NEWBIE⟧                                        ┃
┣━ LADDER ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃  ◇ Legend 2,800+                                                          ┃
┃  ◇ Elite 1,400+                                                           ┃
┃  ◇ Hacker 700+                                                            ┃
┃  ◇ Script Kiddie 300+                                                     ┃
┃  ◇ Wannabe 100+                                                           ┃
┃  ◆ Newbie 0+ you are here                                                 ┃
┣━ STANDINGS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃  #      CREW          TIER           RANK                                 ┃
┃  •   1  Kilobaud      Script Kiddie   310                                 ┃
┃  •   2  Thiesi (you)  Newbie           50                                 ┃
┃  •   3  Nightline     Newbie           30                                 ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

**Everywhere else**, the same shape: the masthead has a scanline and a season
chip; an operation preview is a `COST`/`ODDS`/`HEAT` card with a bust badge
above the terms; a result runs a carrier sweep with the payout ticking up and
lands on a signed `NET` card; rivals are a table with a per-row raid verdict;
crew and kit are a slot card above a board of `[SPECIALTY]`/`[SUPPORT]`
entries; an operation is `case ▸ prepare ▸ execute` with the stage in hand lit;
retained seasons are a `⟦GOLD⟧ handle  rank n` podium; and the help and
first-visit screens are eight sectioned cards with the keys in amber instead of
a page of prose.

**Motion** runs strictly after the commit — a result is in the database before
the first frame — and any key skips it. A key that skips is handed back to the
next reader rather than swallowed, so one press both skips the reveal and
answers the screen it was revealing. Fast mode and the monochrome and plain
presets never play it.

### Chat survives a resize

A caller resizing their terminal while the pinned chat input was up could crash
the render path: the chat loop could read the new height between the shrink and
the redraw meant to accompany it, compute a scroll region with a bottom row of
zero, and raise out of the renderer (#501). The height a repaint decided on is
now the height it uses, rather than whatever the transport has since reported.

## What SysOps gain

### A door gets long enough to save before it is killed

The grace between SIGTERM and SIGKILL for a door process was a fixed half
second. That is plenty for a process that exits on the signal and far too short
for one that flushes anything first — a DOS game writing its scores out through
the emulator being the case that matters — and it is reached on every caller
disconnect and every timeout, not only at node shutdown.

The grace is now a profile field defaulting to **5 seconds**, and the wait ends
the moment the process does, so a door that exits promptly never waits and only
the doors the grace exists for pay for it. The new default reaches doors that
are already registered: a stored profile predating the field deserialises with
it, so upgrading fixes an existing DOS door without a SysOp opening and
re-saving its Compatibility screen.

Deliberately not claimed: that this prevents data loss. It buys time for a door
that handles SIGTERM; one that ignores it is killed at the deadline either way.

## For the project

Both games now have a written presentation contract in the design document — a
palette of nine named roles with a deliberate 256-colour index each, a glyph
vocabulary with an ASCII substitute for every glyph, the component library every
screen is built from, what a table does when it will not fit, and a motion
policy that replaces the old "there are no animation delays" rule. The door
guide and the worklog follow, and AGENTS.md records what the tests can and
cannot prove.

More to the point, there are now tests that can fail because a screen is grey.
The old suites could assert that a screen *fits* — row counts, border widths,
"every term survives the page break" — all of which stayed true of a wall of
unstyled text, which is how two visual designs were lost with every slice
passing review. The new ones assert that colour reaches every body row at every
supported width, that the hotkey, label, value and frame roles are four
different colours and all four appear, that a table's columns line up measured
on the rendered rows, that the ASCII preset lets *none* of the Unicode
vocabulary through, that monochrome emits no SGR at all, and that the screen
left behind is identical whether motion played, was skipped, or was never
enabled.

One schema bump was written and then taken back out. Re-toning historical War
Dialer garrison receipts would have cost a world-schema version — which makes
older binaries refuse a world outright — two migration call sites, a
data-rewriting UPDATE and a MANUAL upgrade step, for worlds that would need
multiple crews, aged receipts and a caller who had renamed to a former rival's
handle. No such world exists, so the migration is gone and `WORLD_SCHEMA_VERSION`
is 10 again.

`scripts/door_gallery.py` renders both games: 405 Voidrunner panels over 27
screens and 1,029 War Dialer panels over 52 screens, at 80x24, 64x20 and 40x12
in every preset. Screens that no walk could reach before now have fixtures that
get there by playing the game rather than by writing a save — Voidrunner's
combat screen takes a bounty on the next system along and leaves the career
mid-fight; War Dialer's world captures an exchange during onboarding so the
garrison screens are not four panels of "No exchanges held".

Also in this release: the previous-callers test that failed on the clock rather
than on a regression (#507 — it matched the `05:39` in a rendered timestamp
while looking for a fifth caller row), and a door-process test budget set by
what the test proves rather than by five seconds.

Issues: #493 and #494 are the two presentation rebuilds; #501 and #507 landed
alongside them; the door terminate grace is the first half of #474, whose
ACPI-powerdown half stays with the VM adapter it was written for.

## Upgrade and rollback

**MANUAL — outside NetBBS:** the ordinary procedure — take a verified backup,
stop the service, install the v7.2.0 wheel into the existing virtual environment
with the same extras, restart. Nothing else: no migration runs, no setting is
required, and existing Voidrunner careers and War Dialer worlds are read exactly
as v7.1.1 left them.

**One rollback caveat, and it is narrow.** Voidrunner's new *Full palette, no
motion* preset is saved in the career like any other display preference. A build
older than this one does not know that value, so if a caller selects it and the
node is later rolled back to an earlier wheel, that career opens on Voidrunner's
recovery screen ("The saved display style is invalid") rather than at the
station. The caller can restore their previous checkpoint from there, and no
other preset is affected — but if a rollback is likely, it is worth knowing
before telling callers about the new preset. War Dialer has no equivalent: its
display keys are unchanged, so a rolled-back node reads its worlds and display
preferences as before.

The v7.0.0 upgrade notes still apply to a node coming from v6.0.0 or earlier, in
particular the two door data breaks described there.

## Verification boundaries

Full test suite on Windows: **8,640 passed, 36 skipped** in 19:18. Both games are
exercised at 40x12, 64x20 and 80x24 across every display preset, with the
colour, role, alignment, preset and motion assertions described above, and both
galleries were rendered and read back panel by panel.

What that does not establish: how either game looks on a specific caller's
client. Every capture above is the door's own output, driven as a subprocess and
stripped of colour so the shapes show in a text file; the gallery paints the
same screens through a terminal emulator written for this project. Both are
faithful models and neither is a real client — the presentation regressions this
release fixes were found by playing the games, not by the suite. Three Voidrunner gallery panels — Hall of
Fame Trading, Hall of Fame Completed careers, and Career Dossiers — show their
empty state, because a career that has not traded profitably or retired has
nothing to rank; that is the screen, not a gap in the capture. The longer door
terminate grace is asserted as a property (a door that exits promptly does not
wait) rather than measured against a real DOS game under DOSBox.

# NetBBS v7.1.0

File transfer for the callers who could never use it, and the two bundled games
get their screens back. v7.0.0 shipped both games flattened: the work that made
every screen fit a negotiated terminal had spent the frames, the gauges and the
spacing along with the fixed layout it replaced, and nothing had asked for that.

Minor rather than patch because file transfer gains a second path and a new
setting. No database migration, no save or world format change, no Link protocol
change: the host database stays at schema 64, and careers and War Dialer worlds
are read exactly as v7.0.0 left them.

## What callers see

### File transfer works without Zmodem

Zmodem lives inside the terminal byte stream, so the caller's *emulator* has to
implement it. SyncTERM, NetRunner, Qodem, Tera Term, ZOC, MobaXterm and minicom
do; PuTTY, Windows Terminal, a plain OpenSSH client and this project's own
browser terminal do not. For those callers `[U]pload` could only ever start a
handshake nobody answered.

They now get a link instead (#475). The file screen offers what the client can
actually do: Zmodem where the transport carries it, `[W] Web transfer` with
`[U] Upload link` / `[D] Download link` where it does not, and in the browser
terminal the page does the transfer itself -- drag or pick a file, and the
listing repaints with it there.

A link is a grant, not a capability: it records who asked for what, and every
gate the terminal path enforces -- read/write level, age and name requirements,
Community inheritance, moderation state, maximum upload size -- is enforced
again when it is redeemed, against live rows. It is good once, for ten minutes,
for that one caller and that one file. Uploads arrive through the same intake as
Zmodem, so FILE_ID.DIZ reading and Linked-area descriptor queueing apply
identically.

**MANUAL — inside NetBBS:** a node bound to `0.0.0.0` knows it is listening, not
how callers reach it. Set `[web] public_url` to the address a browser should
use; until it is set (and cannot be derived) the screen says so and names the
setting rather than printing a URL that fails.

### Voidrunner's tactical HUD is back

Every paged screen — the Command Deck, the market, the pilot record, the
engineering yard, the crew roster, the contract board and details, the chart,
the ledger, the route planner, the Pilot Guide, the Hall of Fame, the fight
screens — draws inside its box again, with its title in the top border and the
action bar below it. The cockpit's hull, fuel and hold gauges and the fight
screen's opponent and hull gauges are back with it.

```
╭── Command Deck: 1,200cr 1/1 ────────────────────────────────────────────────╮
│  Station Services: Freeport Anchorage                                       │
│  Day 0 | Industrial | Danger 1                                              │
│  Shuttle: Hull ■■■■■■■■ 60/60 | Fuel ■■■■■■■■ 24/24 | Cargo ░░░░░░ 0/24     │
│  [M] Commodity Market  |  [Y] Engineering Yard  |  [B] Mission Board        │
╰─────────────────────────────────────────────────────────────────────────────╯
[X] Expand [Q] Exit:
```

The frame is measured, not merely drawn: its row and its columns are part of
each screen's page budget, so a framed page still fits the terminal it was
negotiated for. Below 40 columns or 12 rows the flat layout stays, which is the
same rule the title splash already followed. A title is never cut to fit its
border; where it will not fit, the page draws a plain border and puts the whole
header in its first row, and the one screen that overflows a 40-column border
names itself more briefly instead.

The registration greeting is one colour again, with the callsign picked out in
gold — it used to change colour mid-sentence, and on some clients the second half
was indistinguishable from the box border.

### War Dialer's screens are framed, and its hotkeys read one way

The switchboard, the help and first-visit text, the event log and the record
picker draw inside the door's frame again, indented, instead of running together
as an unbroken wall of rows.

Hotkeys are written `[K] Label` everywhere the door prints — `[T] Trade`,
`[E] Map`, `[G] Garrison` — instead of the two styles it mixed, sometimes in one
bar. The switchboard's action bar is packed to the width it has: full labels,
shorter labels on a narrow terminal, and at the 20-column floor the keys alone
with `[?] Help` naming them. No action key is ever dropped.

The first screen a new caller sees no longer prints the Back bar and the
switchboard's title on the same row.

## Upgrade and rollback

**MANUAL — outside NetBBS:** the ordinary procedure — take a verified backup,
stop the service, install the v7.1.0 wheel into the existing virtual environment
with the same extras, restart. Nothing else changes: no migration runs, and a
rollback to v7.0.0 needs only the previous wheel, with no restore. A node that
wants the new link-based transfer also sets `[web] public_url`; one that does
not set it keeps working exactly as before, Zmodem included.

The v7.0.0 upgrade notes still apply to a node coming from v6.0.0 or earlier —
in particular the two door data breaks described there.

## Verification boundaries

Full test suite on Windows: **8,494 passed, 34 skipped**. The game screens are
exercised at 20x10, 40x12, 64x20 and 80x24, in full-palette, monochrome and
plain-ASCII presets, with assertions that every page fits the negotiated
terminal and that every box row matches its border. Most of the transfer-grant
tests do nothing but change the world underneath a live grant and assert that
redeeming it then fails.

What that does not establish: how the frames look on a specific caller's client,
which is what the playtest that reported them found in the first place; the
20-column floor in particular is measured, not played. Nor has the browser
transfer path been exercised against a node on a public address with a real
`[web] public_url`, only against the test application.

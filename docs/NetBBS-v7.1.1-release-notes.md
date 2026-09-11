# NetBBS v7.1.1

One layout per bundled game, and a floor of 40x12 under both of them. The second,
stripped presentation Voidrunner and War Dialer carried for terminals down to
twenty columns is gone -- and designing for that caller is what flattened them in
the first place: art, gauges and rules were dropped so the content would fit a
screen nobody dials in from, and the stripped result was then what an 80-column
caller saw as well.

Patch rather than minor because nothing gains a feature and nothing changes shape:
the host database stays at schema 64, careers and War Dialer worlds are read
exactly as v7.1.0 left them, and no Link protocol or setting changes. What changes
is that a launch below 40x12 is refused by name instead of served badly.

## What callers see

### A terminal below 40x12 is told so, and loses nothing

Both games now check the size the node reports before they open anything. Below
40 columns or 12 rows the door says what it needs, what the terminal reported,
and that nothing was touched -- no career opened, no world changed -- so a caller
who resizes and dials again is exactly where they were.

The message is written to fit the terminal it is refusing, which is a smaller job
than it sounds: it is chosen for both the width and the height, wrapped by the
door rather than left to the client's soft wrap, and it never ends with a newline
that would scroll it off a one-row screen. It also leaves free the three rows
NetBBS itself writes after any door exits, so the host's "Left Voidrunner." does
not push the reason away. From four rows up, the size the door needs and the size
the terminal reported both survive.

### Everyone else gets the layout the games were designed with

With no second tier to fall back to, every screen in both games is the full one.
Voidrunner's combat bar names every verb at every supported size, where below 40
columns it used to fall back to a bare letter list; War Dialer's switchboard keeps
labelled keys instead of dropping to keys alone; the spatial star map, the framed
panels and the gauges are simply what is there, with no narrow path to maintain
beside them.

This is a floor, not a target. 40x12 is what the games are guaranteed to fit, not
what they are designed for -- and a narrow case may never set the ceiling again.

## What SysOps see

Nothing to do. No migration, no configuration, no new setting. A caller on a
terminal smaller than 40x12 now gets a clear refusal from the door instead of an
unreadable screen, and the door exits normally, so the node does not report it as
a crash.

## For the project

`scripts/door_gallery.py` is new, and AGENTS.md now asks for its output on any
pull request that changes a bundled door's screens. It drives the real door in a
subprocess -- one keystroke at a time, because both doors discard bytes that
arrive together as an unframed paste -- and paints what the door wrote with the
same ANSI emulator the netbbs.org screenshots use, on a canvas the size of the
terminal being simulated. Voidrunner renders 192 panels and War Dialer 180: every
screen, at 80x24, 64x20 and 40x12, in every display preset, from one cached career
per door so that panels differ only by size and preset.

It exists because the suite cannot fail for the reason that mattered. A test can
assert that a screen fits its terminal; it can never assert that the screen looks
like anything, and both games lost their entire visual design across an overhaul
in which every slice passed review and every test stayed green.

The test matrix lost its 20x10 column with the tier: 179 cases across 77
parametrisation sites. The tests that were *about* a narrow terminal kept their
point and moved to the floor -- a wrapped picker entry, an offer too long for one
page, an oversized label read through before it can be chosen -- by making the
content long enough to do that at 40 columns, which is also the realistic case.

The survey behind the decision found the host has no sub-40 path at all: the prose
editor already clamps to 40, and the menu layout's only thresholds are 72 and 120,
for wide screens. Nothing outside the two doors changes.

Closes #495.

# Issue #1560 — quarter blocks per caller — decided and built

A decision recorded in [§16](../16-decisions.md) of the [NetBBS design](../../NetBBS-design-doc.md).

Lunar Lander draws its scenery and wordmark with the quarter blocks,
U+2596–U+259F. On a UTF-8 terminal whose font lacks them, every one shows as
a box. PuTTY's default Windows fonts, Courier New and Consolas, are like that.
PuTTY does not draw block characters itself, and it calls itself `xterm`.
#1557 had already fixed CP437 terminals, so this is the UTF-8 half. The
resulting rule is in §3.2 (Quarter blocks). It is a maintainer decision of
2026-10-25, and a freeze exception recorded on #612.

**Decision 1: a per-caller preference, Auto/On/Off (locked in).** It sits in
the Profile, next to Character set. On and Off always win. A caller's font is
theirs, so the preference is the caller's, not each game's.

**Decision 2: Auto uses an ENQ answerback probe (locked in).** At connect
NetBBS sends ENQ. PuTTY answers `PuTTY` unless its user changed that, and that
answer turns quarter blocks off under Auto. Only a known answerback is taken
out of the input. Over Telnet the probe rides the terminal-type exchange and
adds no wait. Over SSH a cursor position request follows ENQ, and its reply
ends the wait after one round trip, bounded at 0.5 s.

**Decision 3: NetBBS substitutes, and a door is told (locked in).** With
quarter blocks off, `Session.write` and a UTF-8 door's stream replace each one
with the half or full block CP437 gets, so no door shows boxes. `door_info.json`
gains `quadrants`, within `door_api` 5, so a door can draw a finer picture
with half blocks itself.

Rejected alternatives:
- **A per-pilot setting inside each game.** Every game would ask the same
  question about the same font, and a caller would answer it once per game.
- **The preference without a probe.** A PuTTY caller would see boxes until
  they found the setting, and most PuTTY callers have the default font.
- **Guessing from the terminal type.** PuTTY calls itself `xterm`, and PuTTY
  with a good font, such as Cascadia Code, shows quarter blocks. A guess would
  downgrade those callers as well as every real xterm.

Limits: a PuTTY user who changed the answerback, or another client with a
font that lacks quarter blocks, is not detected and sets Off by hand. A
terminal that answers ENQ with something other than a known answerback has
that answer delivered as input. No common client does so by default.

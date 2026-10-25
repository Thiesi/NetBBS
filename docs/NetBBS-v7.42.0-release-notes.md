# NetBBS v7.42.0

Door graphics now work properly on CP437 terminals such as SyncTERM:
- **No more `?` scenery:** quadrant block characters, which Lunar Lander draws
  its landscape with, now reach a CP437 or ASCII terminal as the nearest block
  that terminal has, instead of question marks.
- **Doors learn the caller's character set:** `door_info.json` tells a door
  whether the caller's terminal shows UTF-8, CP437 or ASCII, so the door can
  draw with characters the terminal really has.

This release keeps the feature freeze declared in v7.40.0. The new
`door_info.json` field is a maintainer-approved exception (#612): one optional
field within the existing door API.

**This release does not migrate:** the node database stays at schema 163.
Rolling back to v7.41.0 is the old wheel and a restart.

**Versions:** the Link real-time protocol stays at 4, the door API at 5 and the
bot API at 1. The door API gains one optional field without a version change.

## Door graphics on CP437 terminals (PR #1558, issue #1557)

Lunar Lander draws its scenery with Unicode quadrant blocks (`▖▗▘▝▚▞▙▛▜▟`)
for any caller with Unicode glyphs on. That includes CP437 callers, because
the glyph preference only means "not ASCII". CP437 has no quadrants, so
NetBBS's conversion turned every one into `?`, and the landscape on SyncTERM
was a field of question marks.

**Quadrants get real substitutes.** Each quadrant now becomes the CP437 block
nearest in coverage, at the same width, so the shape stays coarse but
recognisable for every door, not only Lunar Lander:

- `▘▝` become `▀`, and `▖▗` become `▄`.
- `▛▜▙▟` become `█`.
- The two diagonals, `▚▞`, become the half shade `▒`.

An ASCII terminal gets `^`, `_`, `#` and `:` in the same places.

**`door_info.json` now carries `charset`.** It holds `utf-8`, `cp437` or
`ascii`: what the caller's terminal can show, which NetBBS converts the door's
output to. `unicode_style` cannot say this, because it is true on a CP437
terminal too. A door that checks `charset` can draw with characters the
terminal really has, rather than leave NetBBS to approximate them afterwards.
The field is optional within door API 5: older nodes leave it out, and a door
must keep its own fallback. The Developer Handbook describes it.

The bundled Lunar Lander v0.1.0 draws with quadrants too, so its CP437
callers see the block substitutes from this release on. Lunar Lander's next
release reads `charset` and draws half blocks for CP437 callers. Until then, a
SysOp running Lunar Lander v0.2.0 as its own door, as the door guide's
"Running a newer game release" describes, can set `DOOR_LL_PIXELS=half` in the
door's profile environment for the finer picture.

## Documentation

- **Running a newer game release (PR #1554):** the door guide explains how to
  run a bundled game's newer release as an ordinary native door before NetBBS
  bundles it. Check the game's release notes first: a release that changes its
  saved data can make NetBBS backups refuse that data until a NetBBS release
  understands it. The SysOp Handbook points to the new section.

## Upgrade and rollback

- **Upgrade:** take a backup, install the wheel and restart. There is no
  migration.
- **Rollback to v7.41.0:** install the v7.41.0 wheel and restart. The database
  is unchanged. Doors no longer receive `charset`, and quadrant blocks reach
  CP437 and ASCII terminals as `?` again.

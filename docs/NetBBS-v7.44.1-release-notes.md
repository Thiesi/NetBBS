# NetBBS v7.44.1

Lunar Lander v0.7.2: playtest fixes to how the game shows things. Nothing else
in NetBBS changes.

**No migration:** the node database stays at schema 164, and Lunar Lander's
records stay at schema 3, as in v7.44.0.

**Versions:** the Link real-time protocol stays at 4, the door API at 5 and the
bot API at 1.

## Lunar Lander v0.7.2 (PR #1589)

The bundled Lunar Lander moves from v0.7.1 to v0.7.2.

- **Launch scene:** runs 4.5 seconds instead of 3. Space still skips it.
- **Medal, promotion and crash scenes:** each now holds its last frame until a
  key, so a caller can take in the result. Keys in a scene's first 1.2
  seconds are ignored, so a held key doesn't skip it by accident. Q still
  leaves.
- **Debrief:** a flight that earns several achievements shows them in turn,
  every 4 seconds, each with what it was for.
- **Pilot Record:** at 80 columns, the Achievements tab alternates each earned
  achievement's date with its description.

The game adds no files, environment switches, `door_info.json` fields or event
types.

**For scripts that drive the door:** anything that flies through a crash, a
medal or a promotion must now press a key there, after the 1.2-second grace.
The crash scene's prompt reads `[SPACE] Continue`.

## Upgrade and rollback

- **Upgrade:** install the wheel and restart. Nothing migrates.
- **Rollback to v7.44.0:** reinstall its wheel. The records are schema 3 under
  both releases.
- **Rollback to v7.43.0 or earlier:** the MANUAL step in the v7.44.0 notes still
  applies. Once Lunar Lander v0.7 has launched, the records are schema 3,
  which v7.43.0 refuses.

## Verification boundaries

- **Full check on the release tree:** 13,643 passed, 139 skipped and none
  failed, plus the timing-sensitive test, on Windows. The POSIX-only tests are
  among the skipped.
- **The new scene holds and the rotating debrief** come from playtest feedback
  on v0.7.1. Since then they've been checked by the game's own tests, NetBBS's
  door tests and the screen gallery, not by another human flight.

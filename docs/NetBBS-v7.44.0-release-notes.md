# NetBBS v7.44.0

All four bundled games get a new look, and doors open at the caller's window
size again:
- **New presentations:** War Dialer's "Carrier Detect", Voidrunner's "through
  the canopy" and Retro Trivia's "Studio Card".
- **Lunar Lander v0.7.1:** ghosts and replays, rivals, the Grand Tour, live
  descents, weekend specials and a hidden fourth craft.
- **Resizing after logon works:** a caller who resized their window shortly
  after logging on stayed at the logon size, and so did their doors.

This release keeps the feature freeze declared in v7.40.0. It adds no new
NetBBS feature; the game releases come from their own repositories.

**No node database migration:** the schema stays at 164. Lunar Lander's own
records change format (see below), which matters for a rollback.

**Versions:** the Link real-time protocol stays at 4, the door API at 5 and the
bot API at 1.

## Doors open at the caller's window size (PR #1576, issue #1575)

A window change that arrived while NetBBS was waiting on a short timed read was
lost. PuTTY over SSH sends its window change two to three seconds into a
session, and with password logins there is no prompt in between, so it landed
in exactly such a wait. The caller stayed at the logon size, and every door
they opened was told that size: on ReLink, a window maximized to 122x36
launched Retro Trivia at 80x24.

- **SSH:** every read now takes the new size, and a timed read keeps waiting
  until its deadline instead of ending at a resize.
- **Telnet:** a window-size report that landed in the same kind of wait reached
  the input as a stray byte. The wait now reads Telnet commands the way every
  other read does.

## The bundled games

| Game | v7.43.0 bundled | v7.44.0 bundles |
| --- | --- | --- |
| Lunar Lander | v0.5.1 | v0.7.1 |
| War Dialer | v1.3.1 | v1.5.0 |
| Voidrunner | v1.4.0 | v1.5.0 |
| Retro Trivia | v1.2.0 | v1.3.0 |

Each game ships from its own repository and is vendored by
`scripts/vendor_door.py`, checked against its release's SHA256SUMS. Every
release now also ships a `netbbs.json` profile, for SysOps who install a game
by hand. NetBBS registers its bundled copies itself and does not read it.

### Lunar Lander v0.7.1 (PRs #1573, #1583, #1585)

- **v0.5.2, playtest fixes:** the DAILY tab is DAILIES, its briefing counts
  down to the node's midnight, and the PROGRAM tab lists only keys that work.
- **v0.6.0:** CAPCOM callouts, a descent trace, weekend hops, ghosts and
  replays, rivals, the Grand Tour, instrument landings, postcards, the Surveyor
  and Altair craft, live descents and a HISTORY chapter.
- **v0.7.0:** weekend specials (on Saturdays and Sundays the Maria daily is a
  sample return and the Highlands daily a rover rendezvous), a hidden fourth
  craft, the Hopper, on cold gas with a heat cut-out, and art for each craft.
- **v0.7.1:** fixes found in NetBBS's review. A sample return no longer books
  a softest landing from a mid-air instant or earns crash achievements, and a
  crash on the way back up is judged by its own impact.

**Records change format.** The records go to schema 3, which adds `ghosts`
(the run behind each pilot's best landing) and `tours` (Grand Tours). The game
upgrades a schema 1 or 2 file in place the first time a caller launches it, by
only adding tables, so every pilot's records carry over. The upgrade is one
way: Lunar Lander v0.5.1 refuses a schema 3 file.

**Live descents:** the game keeps a `live/` folder beside `pilots.db`, one small
file per running door, so each pilot sees who else on the node is flying. The
files are refreshed every few seconds and removed when the door exits.
Backups leave them out. `DOOR_LL_LIVE=0` in the door's profile environment
turns this off.

**A date switch for testing:** `DOOR_LL_TODAY=YYYY-MM-DD` pins the game's date,
so the dailies and weekend specials can be previewed on any day. Don't set it
on a live node: dailies flown while it is set are filed under the pinned date.

**Backups know schema 3 (PR #1578, issue #1577).** A node backup still checks
the records before taking them, and now accepts schemas 1, 2 and 3, each with
its exact set of tables.

### War Dialer v1.5.0 (PR #1579)

- **"Carrier Detect":** pixel-art mastheads in the splash's phosphor style.
  Truecolor and 256-colour callers get dark glass backgrounds; 16-colour, CP437,
  ASCII, monochrome and Fast mode keep the previous look.
- **Wide screens:** from 100 columns up, screens are centred and up to 104
  columns wide. Below that, layouts and page breaks are unchanged.
- **Splash:** about 3.6 seconds instead of 2.5. Any key still skips it.
- **Quarter blocks:** the game reads `quadrants` from `door_info.json` and draws
  half blocks when it is false. `DOOR_PIXELS=half` does the same for every
  caller.
- **Fixes:** the tier-up wordmark has its drop shadow, a page break no longer
  splits a wrapped receipt, and the rank row says "new tier" on the rank that
  opens one.

The world schema stays at 11. Rooting now records the rooting crew's own
receipt, a new row in the existing events table.

### Voidrunner v1.5.0 (PR #1582)

- **"Through the canopy":** every paged screen draws the console on tinted
  glass in front of a full-screen picture, on live terminals in the auto and
  fast presets. It honours `quadrants`, like War Dialer.
- **Bandwidth:** a canopy page is about 15 KB at 80x24 and 30 KB at 120x36,
  against about 2 KB for a plain page. A page turn under the same sky is about
  5 KB. `DOOR_CANOPY=0` in the door's profile environment turns the canopy off
  for every caller.
- **Dates:** career dates follow the node's `timezone` and the date part of
  `datetime_format`.
- **Combat:** its options fit on the first page at 80x24.

Saved careers are unchanged.

### Retro Trivia v1.3.0 (PR #1581)

- **"Studio Card":** one rounded card per screen, podium answer keys, a row of
  marquee bulbs recording the round so far, and lit CORRECT / NOT QUITE
  verdicts. The verdict flickers on briefly for live callers; `DOOR_SPLASH=0`
  turns that off together with the splash and finale.
- **Fix:** the screen is cleared when the round starts, so the first question
  no longer appears under the round-length picker.

## Smaller changes

- **Profile ▸ Quarter blocks** on a CP437 or ASCII terminal reads "Auto (off:
  CP437 has none)" or "ASCII has none", instead of "Auto (now on)" with a
  sample that terminal cannot show (PR #1571).

## Upgrade and rollback

- **Upgrade:** take a backup, install the wheel and restart. The node database
  does not migrate. Lunar Lander upgrades its records the first time a caller
  launches it.
- **MANUAL: Lunar Lander records after a rollback.** Rolling back to v7.43.0
  needs only the old wheel, but once a caller has launched Lunar Lander v0.7.1
  the records are schema 3. v7.43.0's Lunar Lander refuses them ("Records
  schema is unsupported"), and its backups refuse to take them. To fly Lunar
  Lander again after rolling back, restore the backup you took before
  upgrading, using v7.43.0. The restore replaces the whole node, so everything
  since the backup is lost, Lunar Lander's ghosts and Grand Tours included.
  Without it, the rest of the node runs on v7.43.0, but Lunar Lander stays
  unavailable and every node backup stops with an error naming `pilots.db`
  until that file is moved aside.

## Verification boundaries

- **Full check on the release tree:** 13,643 passed, 139 skipped and none
  failed, plus the timing-sensitive test, on Windows. The POSIX-only tests are
  among the skipped.
- **Canopy bandwidth** is the game's own measurement; on a slow link a page
  turn under a new sky takes noticeably longer than a plain page.
- **The resize fix** is tested against simulated SSH and Telnet clients over
  real sockets, not yet against a real PuTTY.

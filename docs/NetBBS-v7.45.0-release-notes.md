# NetBBS v7.45.0

Three bundled games move on. Voidrunner turns its dense screens into data
sheets and gets a wider console on big terminals. Retro Trivia shows one card
per screen. Lunar Lander's flight card gets shorter. Nothing else in NetBBS
changes.

**No migration:** the node database stays at schema 164. Voidrunner careers,
Lunar Lander's records (schema 3) and War Dialer worlds are unchanged.

**Versions:** the Link real-time protocol stays at 4, the door API at 5 and the
bot API at 1.

## Voidrunner v1.7.1 (PRs #1593, #1595, #1597)

The bundled Voidrunner moves from v1.5.0 to v1.7.1. Saved careers carry over.

- **Return buys the obvious amount (v1.6.0):**
  - At the refuel prompt, Return fills the tanks as far as they hold and the pilot can pay.
  - At repair, Return accepts the offer.
  - In the market, Return buys what live contracts still need.
  - Escape always cancels.
- **The market connects the dots (v1.6.0):**
  - A NEED column and a "Contracts need:" line, while contracts need cargo.
  - A PRICE MEMORY table of better quotes remembered from other stations.
- **No more "Press any key" after an action (v1.6.0):** the result rides in
  the next page's bottom border, marked ● done, ▲ refused or ◆ said.
- **Data sheets (v1.7.0):** the contract page, contract route, encounters,
  survey, crew task and roster, Archive and Status are now labelled rows and
  tables, with the figures a pilot decides on picked out in colour. At 40
  columns the sheets stack their columns instead of dropping any.
- **Combat (v1.7.0):** the hull and enemy bars drain in place. Incoming
  damage, the odds of each choice, and bribe and surrender costs stand out.
- **The ship's computer works out more (v1.7.0):**
  - **Deadlines:** contracts about to expire, or that can't make it, get a Command Deck row.
  - **Route fuel:** the fuel a tracked route needs.
  - **Crew tasks:** what a task still needs.
  - **Cargo:** what a delivery target is short of.
  - **Promised cargo:** a sale that would sell promised cargo asks first.
- **Wide console (v1.7.0, v1.7.1):**
  - **Width:** from 130 columns the console grows to up to 112 columns and keeps a 30-column window for the scenery.
  - **RECENT panel:** that window shows the last few results.
  - **Below 130 columns:** nothing changes.
- **Fixes:**
  - A 40x12 page that held only headings no longer loops forever (v1.7.0).
  - Wide terminals no longer flash blank for about 0.6 seconds before the scenery draws (v1.7.1).

`netbbs.json` is unchanged since v1.5.0.

## Retro Trivia v1.4.0 (PR #1592)

- **One card per screen:** in colour, each question opens on a cleared screen
  instead of scrolling under the earlier cards. The verdict on the previous
  answer sits at the top, above the new card.
- **Plain ASCII:** callers without colour still scroll, as before.
- **Unchanged:** keys, flow and round lengths. The game keeps no saved data.

## Lunar Lander v0.7.3 (PR #1594)

- **Flight director:** the full card drops its "In flight" and "Touchdown"
  lines, which the instruments below already show. It is 18 rows instead of
  20, so the full card appears from 31 rows instead of 33.
- **Pilot Record:** shares that card, so it shows 13 lines at a time instead
  of 15.
- **Unchanged:** records stay at schema 3. As in v0.7.2, scripts that fly
  through a crash, medal or promotion must press a key there.

## For SysOps and scripts

- **The screen gallery** (`scripts/door_gallery.py`) also pictures Voidrunner
  at 132x50. It now reads a screen's text through its terminal emulator, which
  copes with rows the door rewrites in place.
- **Scripts that read Voidrunner's screen as text:** the combat bars and the
  title bar are now redrawn in place with cursor moves. A script that splits
  the output at line breaks will see those frames run together.

## Upgrade and rollback

- **Upgrade:** install the wheel and restart. Nothing migrates.
- **Rollback to v7.44.1 or v7.44.0:** reinstall that wheel. Careers, records
  and worlds are read the same by both.
- **Rollback to v7.43.0 or earlier:** the MANUAL step in the v7.44.0 notes still
  applies. Once Lunar Lander v0.7 has launched, the records are schema 3,
  which v7.43.0 refuses.

## Verification boundaries

- **Full check on the release tree:** 13,642 passed, 139 skipped and one
  failed, plus the timing-sensitive test, on Windows. The POSIX-only tests are
  among the skipped. The failure,
  `test_node_log.py::test_follow_shows_whole_entries_with_their_traceback`,
  came during a run slowed to 13 minutes by other work on the machine. The
  node log tests passed on all three reruns.
- **Screens:** every walked Voidrunner screen was rendered at 80x24, 64x20,
  40x12 and 132x50 with the screen gallery, which refuses a panel that doesn't
  show its screen. Pictures were attached to each game's PR. The 132x50
  panels were spot-checked by eye, not read one by one.
- **Play:** the Voidrunner v1.6.0 changes come from the maintainer's playtest
  of v1.5.0, and one v1.7.0 change was play-tested upstream. Otherwise the
  new versions are checked by their own test suites, NetBBS's door tests and
  the gallery. None has had a human play session on a NetBBS node yet.

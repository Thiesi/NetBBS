# NetBBS v7.41.0

Lunar Lander joins the bundled door games, and two fixes make door launches
cleaner:
- **Lunar Lander:** a precision flying game, the fourth bundled door, with its
  pilot records kept and backed up with the node.
- **No stray Enter:** an Enter typed after a two-digit choice no longer reaches
  the door, or the next screen, as its first key.
- **Date format for doors:** doors can show dates the way the rest of the node
  does.

This release keeps the feature freeze declared in v7.40.0. Lunar Lander is a
maintainer-approved exception (#612): a bundled door on the existing door API,
with no new protocol or settings surface.

**This release does not migrate:** the node database stays at schema 163.
Rolling back to v7.40.0 is the old wheel and a restart (see below for Lunar
Lander's records).

**Versions:** the Link real-time protocol stays at 4, the door API at 5 and the
bot API at 1. The door API gains one optional field without a version change.

## Lunar Lander (PR #1544, issue #1542)

Guide a fragile lander between lunar ridges, manage your descent and fuel, and
settle onto a lit pad. Lunar Lander v0.1.0 comes from its own repository,
[Thiesi/LunarLander](https://github.com/Thiesi/LunarLander), like the other
bundled games.

- **Missions:** a training basin and fifteen ranked missions in three chapters
  (Maria, Highlands, Frontier), plus a daily challenge that changes at the
  node's local midnight.
- **Medals and ranks:** gold, silver and bronze medals for every safe landing,
  and a career from Cadet to Lunar Ace.
- **Records:** each pilot's best landing per mission, on per-mission
  leaderboards, for up to eight callers at once.
- **Game events:** with the door's game events switched on, promotions and
  first golds are announced, and `!top` shows the career standings.
- **Display:** 80x24 or larger, with more scenery at 132x50; truecolor, 256-
  and 16-colour terminals; Unicode or ASCII glyphs.

**Setting it up:** **Content ▸ Doors ▸ <kbd>G</kbd> Gallery** lists it
with the other bundled games. Registered from there, it gets a 900-second CPU
allowance, which its real-time flight needs for a long session, instead of
the usual 300.

**Records live with the node.** A bundled Lunar Lander keeps its records in
`<db>.doors/lunar_lander/pilots.db`, next to Voidrunner's saves and War
Dialer's worlds, never inside the installed package, which an upgrade
replaces. A profile can point `DOOR_LL_DATA_DIR` elsewhere; records there are
not backed up.

**Backups include them.** `python -m netbbs.backup` takes a checked snapshot
of the records, and a restore puts them back. Damaged or foreign records stop
the backup with a message naming the file.

**Guests fly practice only:** a guest's landings never reach the node's
leaderboards.

**It needs an 80x24 terminal.** The other bundled games work down to 40x12,
but a flight simulator needs the room, so Lunar Lander is the stated exception
(design §15). Below 80x24 it pauses the flight and asks for a bigger window.

**Already running a candidate by hand?** Point the bundled door at your
existing records by moving `pilots.db` to `<db>.doors/lunar_lander/`, then
remove the hand-made registration.

## No stray Enter after a choice (PR #1548, issue #1546)

Lists that take a two-digit choice act on the second digit. Most callers add
an Enter out of habit, and that Enter used to reach whatever came next: a
door's first prompt, which then took its default, or the next screen.

- One line ending typed straight after a two-digit choice now counts as part
  of the choice. For a door, the window starts when the door begins reading,
  so a slow launch doesn't let the Enter through.
- Anything else typed after the choice still arrives: `08`, Enter, `2` gives
  the door the `2`.
- Door input now goes through the same input layer as the rest of the BBS on
  Telnet, SSH and the web, so a key typed ahead is no longer lost to the door
  and replayed at the BBS prompt after it.
- One digit and Enter, the #840 shortcut, works as before.

## The node's date format for doors (PR #1547, issue #1545)

`door_info.json` now carries `datetime_format`, the node's date and time
display format set under **Settings ▸ Timestamp format**. It is a `strftime`
pattern that NetBBS has already validated, so a door can show dates the way
the rest of the node does. The field is optional within door API 5: older
nodes leave it out, and a door must keep its own fallback. The Developer
Handbook describes it.

## Upgrade and rollback

- **Upgrade:** take a backup, install the wheel and restart. There is no
  migration. To offer Lunar Lander, register it from the gallery.
- **Rollback to v7.40.0:** install the v7.40.0 wheel and restart. The database
  is unchanged. v7.40.0 has no bundled Lunar Lander, so a door registered from
  the gallery won't start until you remove it; its records stay in
  `<db>.doors/lunar_lander/`. v7.40.0 does not know a backup's Lunar Lander
  component, so restore with v7.41.0 to get those records back.
